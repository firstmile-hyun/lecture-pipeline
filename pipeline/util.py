"""ffprobe / 타임코드 / 공용 헬퍼."""

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: Fraction          # 정확한 유리수 (예: 30000/1001)
    nb_frames: int
    duration_sec: float
    audio_channels: int    # 0이면 오디오 없음


def ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def video_info(path: Path) -> VideoInfo:
    data = ffprobe(path)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    a = [s for s in data["streams"] if s["codec_type"] == "audio"]

    rate = v.get("avg_frame_rate", "0/0")
    if rate in ("0/0", "0", None):
        rate = v.get("r_frame_rate", "30/1")
    fps = Fraction(rate)

    # 파이프라인 전체가 CFR을 전제한다. VFR(화면 녹화 등)이 의심되면 경고.
    r_rate = v.get("r_frame_rate")
    if r_rate and r_rate not in ("0/0",) and Fraction(r_rate) != fps:
        log(f"경고: {path.name} 가변 프레임레이트(VFR) 의심 (avg={fps}, r={r_rate}) — "
            f"타이밍이 어긋나면 ffmpeg로 CFR 변환 후 투입 권장")

    duration = float(v.get("duration") or data["format"].get("duration") or 0)
    nb = v.get("nb_frames")
    nb_frames = int(nb) if nb and nb != "N/A" else round(duration * fps)

    return VideoInfo(
        width=int(v["width"]),
        height=int(v["height"]),
        fps=fps,
        nb_frames=nb_frames,
        duration_sec=duration,
        audio_channels=int(a[0]["channels"]) if a else 0,
    )


def fps_to_timebase(fps: Fraction) -> tuple[int, bool]:
    """xmeml용 (timebase, ntsc). 29.97 → (30, True), 25 → (25, False)."""
    if fps.denominator == 1001:
        return round(fps.numerator / 1000), True
    return round(float(fps)), False


def frame_to_tc(frame: int, fps: Fraction) -> str:
    """넌드롭 HH:MM:SS:FF (timebase 기준). 표시용 — 계산의 기준은 항상 frame."""
    timebase, _ = fps_to_timebase(fps)
    ff = frame % timebase
    total_sec = frame // timebase
    hh, rem = divmod(total_sec, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def tc_to_frame(tc: str, fps: Fraction) -> int:
    """타임코드 문자열 → 프레임 번호.

    허용 형식: HH:MM:SS:FF / HH:MM:SS / MM:SS (넌드롭, timebase 기준 명목 시간),
    HH;MM;SS;FF (프리미어 29.97 기본 표시인 드롭프레임 — 실제 프레임 번호로 환산),
    초(float — 벽시계 기준, 플레이어에서 읽은 시간).
    """
    timebase, ntsc = fps_to_timebase(fps)
    tc = tc.strip()
    drop = ";" in tc
    sep_tc = tc.replace(";", ":")
    if ":" not in sep_tc:
        return round(float(tc) * float(fps))
    parts = [int(p) for p in sep_tc.split(":")]
    if len(parts) == 2:
        parts = [0, *parts]
    if len(parts) == 3:
        parts = [*parts, 0]
    hh, mm, ss, ff = parts
    frame = (hh * 3600 + mm * 60 + ss) * timebase + ff
    if drop and ntsc and timebase in (30, 60):
        # 드롭프레임: 매 분마다 앞 프레임 번호 2개(60fps는 4개)를 건너뛰고 표시,
        # 10분 단위 분은 예외 — 표시 TC를 실제 프레임 번호로 되돌린다.
        per_min = 2 * (timebase // 30)
        total_min = hh * 60 + mm
        frame -= per_min * (total_min - total_min // 10)
    return frame


def outputs_exist(*paths: Path) -> bool:
    return all(p.exists() for p in paths)


def outputs_fresh(outputs: list[Path], inputs: list[Path]) -> bool:
    """산출물이 모두 존재하고, 존재하는 입력물 전부보다 새것인가 (mtime 기준).

    업스트림 단계를 --force로 재실행하면 다운스트림은 이 검사에 걸려
    자동으로 재생성된다.
    """
    if not all(p.exists() for p in outputs):
        return False
    ins = [p.stat().st_mtime for p in inputs if p.exists()]
    if not ins:
        return True
    newest_in = max(ins)
    return all(p.stat().st_mtime >= newest_in for p in outputs)


def log(msg: str) -> None:
    print(f"[pipeline] {msg}", flush=True)
