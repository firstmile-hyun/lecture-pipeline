#!/usr/bin/env python3
"""파이프라인 검증용 합성 에피소드 생성 (실데이터 없이 전 단계 테스트).

원본 포맷을 흉내 낸다: 슬라이드 전체화면 + 오른쪽에 '강연자' 실루엣 합성.
- slides.pdf: 6페이지 (서로 다른 색/번호/도형)
- source.mp4: 1920x1080 @ 29.97fps (NTSC 경로 검증), 페이지 순서 1→2→3→2→4→6
  (3→2 역행 구간 포함 — match의 backward 플래그 검증용, 5페이지는 미사용 페이지)

사용: .venv/bin/python tools/make_sample_episode.py [이름]   # 기본: sample
"""

import subprocess
import sys
from pathlib import Path

import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

FPS = "30000/1001"
SEG_SEC = 5.0
PAGE_ORDER = [1, 2, 3, 2, 4, 6]  # 1-based
COLORS = [
    (0.93, 0.35, 0.14), (0.13, 0.55, 0.80), (0.18, 0.65, 0.35),
    (0.55, 0.27, 0.68), (0.85, 0.65, 0.13), (0.75, 0.22, 0.45),
]


def make_pdf(path: Path, n_pages: int = 6) -> None:
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page(width=960, height=540)
        r, g, b = COLORS[i % len(COLORS)]
        page.draw_rect(fitz.Rect(0, 0, 960, 540), color=None, fill=(0.97, 0.97, 0.95))
        page.draw_rect(fitz.Rect(0, 0, 960, 90), color=None, fill=(r, g, b))
        page.insert_text((40, 60), f"Sample Slide {i + 1}", fontsize=36, color=(1, 1, 1))
        page.insert_text((60, 300), f"PAGE {i + 1}", fontsize=120, color=(r, g, b))
        # 페이지마다 다른 위치의 도형 (phash 구분력 확보)
        cx, cy = 700, 380 - i * 40
        page.draw_circle((cx, cy), 60 + i * 8, color=None, fill=(r * 0.6, g * 0.6, b * 0.6))
        for k in range(6):
            y = 130 + k * 28
            page.draw_rect(fitz.Rect(60, y, 60 + (i + 1) * 90 + k * 25, y + 12),
                           color=None, fill=(0.75, 0.78, 0.80))
    doc.save(path)
    doc.close()
    print(f"slides.pdf 생성 ({n_pages}페이지) → {path}")


def make_video(pdf: Path, dst: Path) -> None:
    W, H = 1920, 1080
    fps = 30000 / 1001
    seg_frames = round(SEG_SEC * fps)

    doc = fitz.open(pdf)
    slide_imgs = []
    for page in doc:
        zoom = W / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).resize((W, H))
        slide_imgs.append(img)
    doc.close()

    total = seg_frames * len(PAGE_ORDER)
    ff = subprocess.Popen(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", FPS, "-i", "-",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={total / fps:.3f}",
            "-ac", "2", "-shortest",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(dst),
        ],
        stdin=subprocess.PIPE,
    )

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
    except OSError:
        font = ImageFont.load_default()

    for f in range(total):
        page = PAGE_ORDER[f // seg_frames] - 1
        frame = slide_imgs[page].copy()
        d = ImageDraw.Draw(frame)
        # 오른쪽 '강연자' 실루엣 (살짝 흔들리는 머리+몸통)
        bob = int(8 * np.sin(f / 12))
        cx = 1600 + int(6 * np.sin(f / 30))
        d.ellipse((cx - 90, 480 + bob, cx + 90, 660 + bob), fill=(40, 34, 30))
        d.rounded_rectangle((cx - 170, 640 + bob, cx + 170, 1080), radius=60, fill=(52, 46, 42))
        d.text((cx, 850), "speaker", font=font, fill=(230, 225, 220), anchor="mm")
        ff.stdin.write(frame.tobytes())

    ff.stdin.close()
    ff.wait()
    if ff.returncode != 0:
        sys.exit("ffmpeg 인코딩 실패")
    print(f"source.mp4 생성 ({total}프레임, {total / fps:.1f}s @29.97fps) → {dst}")


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "sample"
    ep_dir = ROOT / "episodes" / name
    ep_dir.mkdir(parents=True, exist_ok=True)
    make_pdf(ep_dir / "slides.pdf")
    make_video(ep_dir / "slides.pdf", ep_dir / "source.mp4")


if __name__ == "__main__":
    main()
