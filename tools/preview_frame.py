#!/usr/bin/env python3
"""sequence.xml과 동일한 레이아웃 수식으로 특정 프레임을 합성해 PNG로 저장.

프리미어 임포트 전에 좌표/스케일 계산을 시각 검증하는 용도.
트랙 순서 동일: bg_card(V1) → 슬라이드(V2) → 강연자(V3).

사용: .venv/bin/python tools/preview_frame.py <episode> [초, 기본 15]
출력: episodes/<ep>/output/preview_<초>s.png
"""

import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.config import assets_dir, load_config, output_dir  # noqa: E402
from pipeline.xmlgen import _anchor_center, _load_segments  # noqa: E402
from pipeline.util import video_info  # noqa: E402


def paste_scaled(canvas: Image.Image, img: Image.Image, scale: float, cx: float, cy: float):
    w, h = round(img.width * scale), round(img.height * scale)
    img = img.resize((w, h), Image.LANCZOS)
    canvas.alpha_composite(img, (round(cx - w / 2), round(cy - h / 2)))


def main() -> None:
    episode = sys.argv[1] if len(sys.argv) > 1 else "sample"
    at_sec = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    cfg = load_config(episode)
    out = output_dir(episode)
    W, H = cfg["canvas"]["width"], cfg["canvas"]["height"]
    layout = cfg["layout"]

    src = ROOT / "episodes" / episode / "source.mp4"
    info = video_info(src)
    frame_no = round(at_sec * float(info.fps))

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 255))

    # V1: bg_card
    bg = assets_dir() / "bg_card.png"
    if bg.exists():
        canvas.alpha_composite(Image.open(bg).convert("RGBA"))

    # V2: 해당 시점 슬라이드
    segs = _load_segments(out / "match.csv", info.nb_frames, info.fps)
    page = next((s["page"] for s in segs if s["start"] <= frame_no < s["end"]), None)
    if page:
        sf = layout["slide_frame"]
        img = Image.open(out / "slides_png" / f"page_{page:03d}.png").convert("RGBA")
        s = min(sf["width"] / img.width, sf["height"] / img.height)
        paste_scaled(canvas, img, s, sf["x"] + sf["width"] / 2, sf["y"] + sf["height"] / 2)

    # V3: 강연자 알파 프레임
    mov = out / "speaker_alpha.mov"
    if mov.exists():
        tmp = out / "_speaker_frame.png"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(mov),
             "-vf", f"select=eq(n\\,{frame_no})", "-frames:v", "1", str(tmp)],
            check=True,
        )
        sp = layout["speaker"]
        img = Image.open(tmp).convert("RGBA")
        rw, rh = img.width * sp["scale"], img.height * sp["scale"]
        cx, cy = _anchor_center(sp["anchor"], sp["x"], sp["y"], rw, rh)
        paste_scaled(canvas, img, sp["scale"], cx, cy)
        tmp.unlink()

    dst = out / f"preview_{at_sec:g}s.png"
    canvas.convert("RGB").save(dst)
    print(f"미리보기 저장 → {dst}")


if __name__ == "__main__":
    main()
