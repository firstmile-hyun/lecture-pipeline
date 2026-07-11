"""전환 마커 전용 — 임의 영상 → PPT 전환 프레임 감지 → 프리미어 마커 XML.

즉시 컷 전환 기준. 전환이 일어나는 '새 슬라이드 첫 프레임'에 마커를 찍는다.
VFR(화면 녹화본 등)이면 CFR로 정규화해 프레임 인덱싱의 정확도를 보장한다 —
프리미어도 이 CFR본을 쓰면 프레임 오차 0.

출력(원본 옆 또는 지정 폴더):
  <name>_markers.xml   원본 클립 1개(V1) + 시퀀스 마커(전환마다)
  <name>_markers.csv   검수용 (전환번호, 프레임, 타임코드, 초)
  <name>_cfr.mp4       VFR였을 때만 — 프리미어에서 이 파일을 쓸 것
"""

import csv
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from scenedetect import ContentDetector, SceneManager, open_video

from .util import ffprobe, fps_to_timebase, frame_to_tc, log, video_info
from .xmlgen import _pathurl, _rate, _sub  # xmeml 헬퍼 재사용


# ---------------------------------------------------------------- CFR 정규화


def _is_vfr(src: Path) -> bool:
    """r_frame_rate ≠ avg_frame_rate면 VFR로 간주 (싸고 표준적인 휴리스틱)."""
    try:
        data = ffprobe(src)
        v = next(s for s in data["streams"] if s["codec_type"] == "video")
        r, a = v.get("r_frame_rate"), v.get("avg_frame_rate")
        if not r or not a or a in ("0/0", "0", None):
            return False
        return Fraction(r) != Fraction(a)
    except Exception:
        return False


def ensure_cfr(src: Path, out_dir: Path, force: bool = False) -> tuple[Path, bool]:
    """VFR이면 CFR로 재인코딩한 사본을 만들어 그 경로를 반환. CFR이면 원본 그대로.

    반환: (기준 영상 경로, 변환했는지 여부)
    """
    if not _is_vfr(src):
        return src, False
    info = video_info(src)
    dst = out_dir / f"{src.stem}_cfr.mp4"
    if dst.exists() and not force:
        log(f"markers: CFR 사본 이미 있음 → 재사용 ({dst.name})")
        return dst, True
    log(f"markers: VFR 감지 → CFR({float(info.fps):.3f}fps)로 정규화 중… (프레임 정확도 확보)")
    tmp = dst.with_suffix(".mp4.part")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(src),
            "-fps_mode", "cfr", "-r", f"{info.fps.numerator}/{info.fps.denominator}",
            "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            str(tmp),
        ],
        check=True,
    )
    tmp.replace(dst)
    log(f"markers: CFR 정규화 완료 → {dst.name} (프리미어에선 이 파일을 사용하세요)")
    return dst, True


# ---------------------------------------------------------------- 전환 감지


def detect_cut_frames(src: Path, info, *, threshold: float, min_scene_sec: float,
                      analyze_frac: float, speaker_side: str,
                      progress: bool = True) -> list[int]:
    """전환(새 씬 시작) 프레임 목록. 즉시 컷 기준으로 새 슬라이드 첫 프레임을 준다.

    analyze_frac < 1.0 이면 강연자 반대쪽만 분석(인물 움직임 오탐 방지).
    화면 녹화(인물 없음)는 analyze_frac=1.0 → 전체 프레임 분석.
    """
    fps = float(info.fps)
    video = open_video(str(src))
    sm = SceneManager()
    sm.add_detector(
        ContentDetector(threshold=threshold, min_scene_len=round(fps * min_scene_sec))
    )
    if analyze_frac < 0.999:
        aw = int(info.width * analyze_frac)
        y1 = info.height - 2  # scenedetect 0.7 경계 비교 버그의 허위 경고 회피
        if speaker_side == "right":
            sm.crop = (0, 0, aw - 1, y1)
        else:
            sm.crop = (info.width - aw, 0, info.width - 2, y1)

    sm.detect_scenes(video, show_progress=progress)
    scenes = sm.get_scene_list(start_in_scene=True)
    # 각 씬 시작 프레임이 전환점. 첫 씬은 0에서 시작(전환 아님) → 제외.
    def _fn(tc):  # scenedetect 버전 간 호환 (frame_num 프로퍼티 우선)
        return getattr(tc, "frame_num", None) if getattr(tc, "frame_num", None) is not None else tc.get_frames()
    return [_fn(s) for s, _ in scenes if _fn(s) > 0]


# ---------------------------------------------------------------- 마커 XML


def write_markers_xml(src: Path, cut_frames: list[int], info, dst: Path) -> None:
    """원본 클립 1개(V1) + 전환마다 시퀀스 마커를 담은 xmeml 생성.

    모든 시간은 소스 fps timebase 기준 정수 프레임. 마커 <in>=전환 프레임, <out>=-1(무길이).
    """
    timebase, ntsc = fps_to_timebase(info.fps)
    W, H, total = info.width, info.height, info.nb_frames

    xmeml = ET.Element("xmeml", version="4")
    seq = _sub(xmeml, "sequence", id="sequence-1")
    _sub(seq, "name", src.stem)
    _sub(seq, "duration", total)
    _rate(seq, timebase, ntsc)
    media = _sub(seq, "media")

    video = _sub(media, "video")
    fmt = _sub(video, "format")
    sc = _sub(fmt, "samplecharacteristics")
    _rate(sc, timebase, ntsc)
    _sub(sc, "width", W)
    _sub(sc, "height", H)
    _sub(sc, "anamorphic", "FALSE")
    _sub(sc, "pixelaspectratio", "square")
    _sub(sc, "fielddominance", "none")

    # V1: 원본 클립 (풀 길이)
    track = _sub(video, "track")
    ci = _sub(track, "clipitem", id="clipitem-1")
    _sub(ci, "name", src.name)
    _sub(ci, "enabled", "TRUE")
    _sub(ci, "duration", total)
    _rate(ci, timebase, ntsc)
    _sub(ci, "start", 0)
    _sub(ci, "end", total)
    _sub(ci, "in", 0)
    _sub(ci, "out", total)
    f = _sub(ci, "file", id="file-1")
    _sub(f, "name", src.name)
    _sub(f, "pathurl", _pathurl(src))
    _rate(f, timebase, ntsc)
    _sub(f, "duration", total)
    fmedia = _sub(f, "media")
    fv = _sub(fmedia, "video")
    fsc = _sub(fv, "samplecharacteristics")
    _rate(fsc, timebase, ntsc)
    _sub(fsc, "width", W)
    _sub(fsc, "height", H)
    if info.audio_channels > 0:
        fa = _sub(fmedia, "audio")
        fasc = _sub(fa, "samplecharacteristics")
        _sub(fasc, "depth", 16)
        _sub(fasc, "samplerate", 48000)
        _sub(fa, "channelcount", info.audio_channels)

    # 시퀀스 마커 (전환마다). <out>-1 = 무길이 단일 마커.
    for i, fr in enumerate(cut_frames, start=1):
        mk = _sub(seq, "marker")
        _sub(mk, "name", f"전환 {i:02d}")
        _sub(mk, "comment", frame_to_tc(fr, info.fps))
        _sub(mk, "in", fr)
        _sub(mk, "out", -1)

    ET.indent(xmeml, space=" ")
    tmp = dst.with_suffix(".xml.part")
    tmp.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
        + ET.tostring(xmeml, encoding="unicode") + "\n",
        encoding="utf-8",
    )
    tmp.replace(dst)


def write_markers_csv(cut_frames: list[int], info, dst: Path) -> None:
    fps = float(info.fps)
    tmp = dst.with_suffix(".csv.part")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["marker", "frame", "timecode", "sec"])
        for i, fr in enumerate(cut_frames, start=1):
            w.writerow([i, fr, frame_to_tc(fr, info.fps), f"{fr / fps:.3f}"])
    tmp.replace(dst)


# ---------------------------------------------------------------- 오케스트레이션


def run(video: str, *, out_dir: str | None = None, full_frame: bool = False,
        speaker_side: str = "right", threshold: float = 4.0, min_scene_sec: float = 3.0,
        cfr: bool = True, progress: bool = True) -> dict:
    """영상 하나 → 마커 XML/CSV 생성. 반환: {xml, csv, cuts, cfr_path}."""
    src = Path(video)
    if not src.is_file():
        raise FileNotFoundError(f"영상을 찾을 수 없어요: {video}")
    outd = Path(out_dir) if out_dir else src.parent
    outd.mkdir(parents=True, exist_ok=True)

    ref = src
    cfr_path = None
    if cfr:
        ref, converted = ensure_cfr(src, outd)
        if converted:
            cfr_path = ref

    info = video_info(ref)
    log(f"markers: {ref.name} ({info.width}x{info.height} @ {float(info.fps):.3f}fps) 전환 감지…")
    cuts = detect_cut_frames(
        ref, info,
        threshold=threshold, min_scene_sec=min_scene_sec,
        analyze_frac=1.0 if full_frame else 0.65,
        speaker_side=speaker_side, progress=progress,
    )

    xml = outd / f"{src.stem}_markers.xml"
    csv_path = outd / f"{src.stem}_markers.csv"
    write_markers_xml(ref, cuts, info, xml)
    write_markers_csv(cuts, info, csv_path)
    log(f"markers: 전환 {len(cuts)}개 → {xml.name}")
    return {"xml": str(xml), "csv": str(csv_path), "cuts": cuts,
            "cfr_path": str(cfr_path) if cfr_path else None}


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="전환 마커 XML 생성 (여러 영상 배치 지원)")
    p.add_argument("videos", nargs="+")
    p.add_argument("--out-dir", default=None, help="산출물 폴더 (기본: 각 원본 옆)")
    p.add_argument("--full-frame", action="store_true", help="전체 프레임 분석(화면 녹화·인물 없음)")
    p.add_argument("--side", default="right", choices=["right", "left"], help="강연자 쪽(인물 영상)")
    p.add_argument("--threshold", type=float, default=4.0)
    p.add_argument("--no-cfr", action="store_true", help="VFR여도 CFR 정규화 생략")
    args = p.parse_args()

    failed: list[str] = []
    for i, v in enumerate(args.videos, start=1):
        if len(args.videos) > 1:
            log(f"markers: ===== [{i}/{len(args.videos)}] {Path(v).name} =====")
        try:
            run(v, out_dir=args.out_dir, full_frame=args.full_frame,
                speaker_side=args.side, threshold=args.threshold, cfr=not args.no_cfr)
        except Exception as e:  # 한 편 실패가 나머지를 막지 않게
            log(f"markers: {Path(v).name} 실패: {e}")
            failed.append(Path(v).name)
    if len(args.videos) > 1:
        log(f"markers: 배치 완료 — 성공 {len(args.videos) - len(failed)} / 실패 {len(failed)}")
        for name in failed:
            log(f"markers:   실패: {name}")
    if failed:
        sys.exit(1)
