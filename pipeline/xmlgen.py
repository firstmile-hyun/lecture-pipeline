"""Phase 5(+6) — 프리미어 임포트용 xmeml(FCP7 XML) 시퀀스 생성.

트랙 구성 (아래가 하위 레이어):
  V1: intro.png → bg_card.png(본편 전체) → outro.png
  V2: match.csv 기반 슬라이드 PNG (같은 페이지 연속 구간은 병합)
  V3: speaker_alpha.mov (알파, layout.speaker 배치)
  V4: chapters.csv 있으면 챕터 배지
  A1/A2: source.mp4 오디오 (스테레오 페어, <link>로 묶음)

모든 시간 값은 소스 fps timebase 기준 정수 프레임. 29.97 등 NTSC 계열은
<timebase>30</timebase><ntsc>TRUE</ntsc>로 표기 (드롭프레임 계산 없음 — 프레임 번호가 진실).
"""

import csv
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from PIL import Image

from .config import assets_dir, episode_dir, output_dir
from .util import fps_to_timebase, log, outputs_fresh, tc_to_frame, video_info


# ---------------------------------------------------------------- 기본 요소


def _sub(parent: ET.Element, tag: str, text=None, **attrib) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = str(text)
    return el


def _rate(parent: ET.Element, timebase: int, ntsc: bool) -> None:
    r = _sub(parent, "rate")
    _sub(r, "timebase", timebase)
    _sub(r, "ntsc", "TRUE" if ntsc else "FALSE")


def _video_sc(parent: ET.Element, width: int, height: int, timebase: int, ntsc: bool) -> None:
    sc = _sub(parent, "samplecharacteristics")
    _rate(sc, timebase, ntsc)
    _sub(sc, "width", width)
    _sub(sc, "height", height)
    _sub(sc, "anamorphic", "FALSE")
    _sub(sc, "pixelaspectratio", "square")
    _sub(sc, "fielddominance", "none")


def _pathurl(path: Path) -> str:
    return "file://localhost" + urllib.parse.quote(str(path.resolve()))


# ---------------------------------------------------------------- 파일 레지스트리
# 같은 미디어는 첫 등장에서만 완전 정의하고 이후 <file id="..."/> 참조로 재사용해야
# 프리미어가 하나의 프로젝트 아이템으로 dedupe한다.


@dataclass
class _FileRegistry:
    timebase: int
    ntsc: bool
    _ids: dict = field(default_factory=dict)
    _defined: set = field(default_factory=set)

    def attach(self, clipitem: ET.Element, path: Path, *,
               kind: str, width: int = 0, height: int = 0,
               duration: int = 0, audio_channels: int = 0) -> None:
        """clipitem 아래에 file 정의(최초) 또는 참조(이후)를 붙인다.

        kind: 'still' | 'video'
        """
        key = str(path.resolve())
        if key not in self._ids:
            self._ids[key] = f"file-{len(self._ids) + 1}"
        fid = self._ids[key]

        if key in self._defined:
            _sub(clipitem, "file", id=fid)
            return
        self._defined.add(key)

        f = _sub(clipitem, "file", id=fid)
        _sub(f, "name", path.name)
        _sub(f, "pathurl", _pathurl(path))
        # 스틸도 rate를 명시 (Apple 스펙상 file의 rate는 required). duration은
        # 실제 미디어에서 온 값만 기입하고 스틸에는 생략 (OTIO 어댑터 방침과 동일).
        _rate(f, self.timebase, self.ntsc)
        if kind == "video":
            _sub(f, "duration", duration)
        media = _sub(f, "media")
        video = _sub(media, "video")
        sc = _sub(video, "samplecharacteristics")
        if kind == "video":
            _rate(sc, self.timebase, self.ntsc)
        _sub(sc, "width", width)
        _sub(sc, "height", height)
        if audio_channels > 0:
            audio = _sub(media, "audio")
            asc = _sub(audio, "samplecharacteristics")
            _sub(asc, "depth", 16)
            _sub(asc, "samplerate", 48000)
            _sub(audio, "channelcount", audio_channels)


# ---------------------------------------------------------------- 모션 필터


def _center_value(dx_px: float, dy_px: float, canvas_w: int, canvas_h: int) -> tuple[float, float]:
    """캔버스 중심 기준 픽셀 오프셋 → Basic Motion center (horiz, vert).

    프리미어의 xmeml Basic Motion center는 캔버스 전체 크기로 정규화된
    중심 기준 오프셋이다: horiz = dx / width, vert = dy / height
    (중앙 = 0, 캔버스 안 범위 = ±0.5). 실제 프리미어 export 및 프리미어
    임포트 대상 생성기들의 캘리브레이션으로 교차 검증된 규약.
    """
    return dx_px / canvas_w, dy_px / canvas_h


def _basic_motion(clipitem: ET.Element, scale_pct: float,
                  dx_px: float, dy_px: float, canvas_w: int, canvas_h: int) -> None:
    """스케일(%)과 캔버스 중심 기준 픽셀 오프셋으로 Basic Motion 필터를 붙인다."""
    if abs(scale_pct - 100.0) < 1e-9 and abs(dx_px) < 1e-9 and abs(dy_px) < 1e-9:
        return  # 기본 배치면 필터 불필요
    flt = _sub(clipitem, "filter")
    eff = _sub(flt, "effect")
    _sub(eff, "name", "Basic Motion")
    _sub(eff, "effectid", "basic")
    _sub(eff, "effectcategory", "motion")
    _sub(eff, "effecttype", "motion")
    _sub(eff, "mediatype", "video")

    p_scale = _sub(eff, "parameter", authoringApp="PremierePro")
    _sub(p_scale, "parameterid", "scale")
    _sub(p_scale, "name", "Scale")
    _sub(p_scale, "valuemin", 0)
    _sub(p_scale, "valuemax", 1000)
    _sub(p_scale, "value", f"{scale_pct:.4f}")

    horiz, vert = _center_value(dx_px, dy_px, canvas_w, canvas_h)
    p_center = _sub(eff, "parameter", authoringApp="PremierePro")
    _sub(p_center, "parameterid", "center")
    _sub(p_center, "name", "Center")
    val = _sub(p_center, "value")
    _sub(val, "horiz", f"{horiz:.6f}")
    _sub(val, "vert", f"{vert:.6f}")


def _anchor_center(anchor: str, ax: float, ay: float, w: float, h: float) -> tuple[float, float]:
    """앵커 기준점 + 렌더 크기 → 클립 중심 좌표 (캔버스 절대 px)."""
    if anchor == "bottom-right":
        return ax - w / 2, ay - h / 2
    if anchor == "bottom-left":
        return ax + w / 2, ay - h / 2
    if anchor == "top-right":
        return ax - w / 2, ay + h / 2
    if anchor == "top-left":
        return ax + w / 2, ay + h / 2
    return ax, ay  # center


def _png_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size


# ---------------------------------------------------------------- 클립 빌더


class _SeqBuilder:
    def __init__(self, episode: str, cfg: dict, fps: Fraction):
        self.cfg = cfg
        self.canvas_w = cfg["canvas"]["width"]
        self.canvas_h = cfg["canvas"]["height"]
        self.timebase, self.ntsc = fps_to_timebase(fps)
        self.files = _FileRegistry(self.timebase, self.ntsc)
        self.clip_seq = 0
        self.episode = episode

    def next_clip_id(self) -> str:
        self.clip_seq += 1
        return f"clipitem-{self.clip_seq}"

    def still_clip(self, track: ET.Element, path: Path, start: int, end: int,
                   *, scale_pct: float = 100.0, dx: float = 0.0, dy: float = 0.0) -> None:
        """스틸 PNG를 [start, end) 프레임 구간에 배치."""
        w, h = _png_size(path)
        dur = end - start
        ci = _sub(track, "clipitem", id=self.next_clip_id())
        _sub(ci, "name", path.name)
        _sub(ci, "enabled", "TRUE")
        _sub(ci, "duration", dur)
        _rate(ci, self.timebase, self.ntsc)
        _sub(ci, "start", start)
        _sub(ci, "end", end)
        _sub(ci, "in", 0)
        _sub(ci, "out", dur)
        _sub(ci, "alphatype", "straight")
        self.files.attach(ci, path, kind="still", width=w, height=h)
        _basic_motion(ci, scale_pct, dx, dy, self.canvas_w, self.canvas_h)

    def video_clip(self, track: ET.Element, path: Path, start: int, end: int,
                   *, src_in: int, width: int, height: int, src_frames: int,
                   scale_pct: float = 100.0, dx: float = 0.0, dy: float = 0.0,
                   alpha: bool = False, audio_channels: int = 0) -> None:
        dur = end - start
        ci = _sub(track, "clipitem", id=self.next_clip_id())
        _sub(ci, "name", path.name)
        _sub(ci, "enabled", "TRUE")
        _sub(ci, "duration", src_frames)
        _rate(ci, self.timebase, self.ntsc)
        _sub(ci, "start", start)
        _sub(ci, "end", end)
        _sub(ci, "in", src_in)
        _sub(ci, "out", src_in + dur)
        if alpha:
            _sub(ci, "alphatype", "straight")
        self.files.attach(ci, path, kind="video", width=width, height=height,
                          duration=src_frames, audio_channels=audio_channels)
        _basic_motion(ci, scale_pct, dx, dy, self.canvas_w, self.canvas_h)

    def audio_pair(self, audio_el: ET.Element, path: Path, start: int, end: int,
                   *, width: int, height: int, src_frames: int, channels: int) -> None:
        """source.mp4의 오디오를 A1/A2 스테레오 페어(모노면 A1 단독)로 배치."""
        n = min(channels, 2)
        clip_ids = [self.next_clip_id() for _ in range(n)]
        for ch in range(n):
            track = _sub(audio_el, "track")
            ci = _sub(track, "clipitem", id=clip_ids[ch],
                      premiereChannelType="stereo" if n == 2 else "mono")
            _sub(ci, "name", path.name)
            _sub(ci, "enabled", "TRUE")
            _sub(ci, "duration", src_frames)
            _rate(ci, self.timebase, self.ntsc)
            _sub(ci, "start", start)
            _sub(ci, "end", end)
            _sub(ci, "in", 0)
            _sub(ci, "out", end - start)
            self.files.attach(ci, path, kind="video", width=width, height=height,
                              duration=src_frames, audio_channels=channels)
            st = _sub(ci, "sourcetrack")
            _sub(st, "mediatype", "audio")
            _sub(st, "trackindex", ch + 1)
            if n == 2:  # 스테레오 페어 링크 (groupindex 공유)
                for li, lid in enumerate(clip_ids):
                    link = _sub(ci, "link")
                    _sub(link, "linkclipref", lid)
                    _sub(link, "mediatype", "audio")
                    _sub(link, "trackindex", li + 1)
                    _sub(link, "clipindex", 1)
                    _sub(link, "groupindex", 1)
            _sub(track, "enabled", "TRUE")
            _sub(track, "locked", "FALSE")
            _sub(track, "outputchannelindex", ch + 1)


# ---------------------------------------------------------------- 데이터 로드


def _load_segments(match_csv: Path, nb_frames: int, fps: Fraction) -> list[dict]:
    """match.csv → 같은 페이지 연속 구간 병합된 [{start, end, page}] (프레임)."""
    with open(match_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    segs: list[dict] = []
    for r in rows:
        start = tc_to_frame(r["start_tc"], fps)
        page = int(r["matched_page"])
        if segs and segs[-1]["page"] == page:
            continue  # 같은 페이지 연속 매칭 → 병합
        if segs:
            segs[-1]["end"] = start
        segs.append({"start": start, "page": page, "end": nb_frames})
    return segs


def _find_asset(name: str, episode: str) -> Path | None:
    """편별 자산(intro_ep01.png)이 있으면 우선, 없으면 공통(intro.png)."""
    stem, ext = name.rsplit(".", 1)
    for cand in (assets_dir() / f"{stem}_{episode}.{ext}", assets_dir() / name):
        if cand.exists():
            return cand
    return None


# ---------------------------------------------------------------- 메인


def run(episode: str, cfg: dict, force: bool = False) -> None:
    out = output_dir(episode)
    dst = out / "sequence.xml"

    src = episode_dir(episode) / "source.mp4"
    match_csv = out / "match.csv"
    slides_dir = out / "slides_png"
    for req, step in ((src, "(source.mp4 없음)"), (match_csv, "match"), (slides_dir, "slides")):
        if not req.exists():
            raise FileNotFoundError(f"{req} 없음 — 먼저 --step {step} 실행 필요")

    # 입력물(매칭 결과·강연자 알파·챕터)이 갱신됐으면 재생성 —
    # matte를 나중에 돌린 경우에도 V3가 자동으로 포함되게 한다
    ins = [match_csv, out / "speaker_alpha.mov", episode_dir(episode) / "chapters.csv"]
    if not force and outputs_fresh([dst], ins):
        log(f"[{episode}] xml: {dst.name} 최신 → 스킵 (--force로 재실행)")
        return

    info = video_info(src)
    timebase, ntsc = fps_to_timebase(info.fps)
    layout = cfg["layout"]
    W, H = cfg["canvas"]["width"], cfg["canvas"]["height"]

    body = info.nb_frames
    segs = _load_segments(match_csv, body, info.fps)

    intro = _find_asset("intro.png", episode)
    outro = _find_asset("outro.png", episode)
    bg_card = assets_dir() / "bg_card.png"
    speaker_mov = out / "speaker_alpha.mov"

    intro_f = round(layout["intro_sec"] * float(info.fps)) if intro else 0
    outro_f = round(layout["outro_sec"] * float(info.fps)) if outro else 0
    total = intro_f + body + outro_f

    b = _SeqBuilder(episode, cfg, info.fps)

    xmeml = ET.Element("xmeml", version="4")
    seq = _sub(xmeml, "sequence", id="sequence-1", explodedTracks="true")
    _sub(seq, "name", episode)
    _sub(seq, "duration", total)
    _rate(seq, timebase, ntsc)
    media = _sub(seq, "media")
    video = _sub(media, "video")
    fmt = _sub(video, "format")
    _video_sc(fmt, W, H, timebase, ntsc)

    # ---- V1: intro / bg_card / outro
    v1 = _sub(video, "track")
    if intro:
        b.still_clip(v1, intro, 0, intro_f)
    if bg_card.exists():
        b.still_clip(v1, bg_card, intro_f, intro_f + body)
    else:
        log(f"[{episode}] xml: assets/bg_card.png 없음 — V1 배경 생략")
    if outro:
        b.still_clip(v1, outro, intro_f + body, total)

    # ---- V2: 슬라이드 (프레임 영역에 fit-inside)
    v2 = _sub(video, "track")
    sf = layout["slide_frame"]
    frame_cx = sf["x"] + sf["width"] / 2 - W / 2
    frame_cy = sf["y"] + sf["height"] / 2 - H / 2
    for seg in segs:
        page_png = slides_dir / f"page_{seg['page']:03d}.png"
        if not page_png.exists():
            log(f"[{episode}] xml: {page_png.name} 없음 — 구간 스킵 (scene {seg})")
            continue
        iw, ih = _png_size(page_png)
        s = min(sf["width"] / iw, sf["height"] / ih)
        b.still_clip(v2, page_png, intro_f + seg["start"], intro_f + seg["end"],
                     scale_pct=s * 100, dx=frame_cx, dy=frame_cy)

    # ---- V3: 강연자 알파
    sinfo = None
    if speaker_mov.exists():
        try:
            sinfo = video_info(speaker_mov)
        except Exception:
            log(f"[{episode}] xml: {speaker_mov.name} 읽기 실패 (미완성/손상?) — V3 생략, "
                f"--step matte 재실행 후 xml 재생성 필요")
    if sinfo:
        sp = layout["speaker"]
        v3_body = body
        if sinfo.nb_frames < body:  # 알파 영상이 본편보다 짧으면(샘플/중단 흔적) 클램프
            log(f"[{episode}] xml: 경고 — speaker_alpha.mov가 본편보다 짧음 "
                f"({sinfo.nb_frames}f < {body}f). V3를 그 길이까지만 배치. "
                f"전체 매팅은 --step matte --force")
            v3_body = sinfo.nb_frames
        rw, rh = sinfo.width * sp["scale"], sinfo.height * sp["scale"]
        cx, cy = _anchor_center(sp["anchor"], sp["x"], sp["y"], rw, rh)
        v3 = _sub(video, "track")
        b.video_clip(v3, speaker_mov, intro_f, intro_f + v3_body,
                     src_in=0, width=sinfo.width, height=sinfo.height,
                     src_frames=sinfo.nb_frames,
                     scale_pct=sp["scale"] * 100, dx=cx - W / 2, dy=cy - H / 2,
                     alpha=True)
    elif not speaker_mov.exists():
        log(f"[{episode}] xml: speaker_alpha.mov 없음 — V3 생략 (--step matte 실행 후 재생성)")

    # ---- V4: 챕터 배지 (옵션)
    chapters_csv = episode_dir(episode) / "chapters.csv"
    if chapters_csv.exists():
        with open(chapters_csv, encoding="utf-8") as f:
            chapters = list(csv.DictReader(f))
        if chapters:
            bcfg = layout["badge"]
            v4 = _sub(video, "track")
            starts = [tc_to_frame(c["start_tc"], info.fps) for c in chapters]
            for i, ch in enumerate(chapters):
                badge = (episode_dir(episode) / ch["badge_png"].strip())
                if not badge.exists():
                    badge = assets_dir() / ch["badge_png"].strip()
                if not badge.exists():
                    log(f"[{episode}] xml: 배지 {ch['badge_png']} 없음 — 스킵")
                    continue
                bw, bh = _png_size(badge)
                rw, rh = bw * bcfg["scale"], bh * bcfg["scale"]
                cx, cy = _anchor_center(bcfg["anchor"], bcfg["x"], bcfg["y"], rw, rh)
                end = starts[i + 1] if i + 1 < len(starts) else body
                b.still_clip(v4, badge, intro_f + starts[i], intro_f + end,
                             scale_pct=bcfg["scale"] * 100,
                             dx=cx - W / 2, dy=cy - H / 2)

    # ---- 오디오: source.mp4 (본편 구간)
    audio = _sub(media, "audio")
    _sub(audio, "numOutputChannels", 2)
    afmt = _sub(audio, "format")
    asc = _sub(afmt, "samplecharacteristics")
    _sub(asc, "depth", 16)
    _sub(asc, "samplerate", 48000)
    if info.audio_channels > 0:
        b.audio_pair(audio, src, intro_f, intro_f + body,
                     width=info.width, height=info.height,
                     src_frames=info.nb_frames, channels=info.audio_channels)
    else:
        log(f"[{episode}] xml: source.mp4에 오디오 없음 — A1 생략")

    ET.indent(xmeml, space=" ")
    body_str = ET.tostring(xmeml, encoding="unicode")
    tmp = dst.with_suffix(".xml.tmp")  # 중단 시 잘린 XML 방지
    tmp.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body_str + "\n",
        encoding="utf-8",
    )
    tmp.replace(dst)
    log(f"[{episode}] xml: 완료 → {dst} "
        f"(시퀀스 {total}f = 인트로 {intro_f} + 본편 {body} + 아웃트로 {outro_f}, "
        f"슬라이드 구간 {len(segs)}개)")
