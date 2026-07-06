#!/usr/bin/env python3
"""디자인 확정 전까지 쓸 플레이스홀더 자산 생성.

bg_card.png  — 1920x1080, 슬라이드 프레임 영역은 투명 창, 나머지는 카드 톤
intro.png / outro.png — 타이틀 카드
실제 디자인이 나오면 같은 파일명으로 교체하면 된다.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.config import load_config  # noqa: E402


def _font(size: int):
    for cand in (
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    cfg = load_config()
    W, H = cfg["canvas"]["width"], cfg["canvas"]["height"]
    sf = cfg["layout"]["slide_frame"]
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)

    # bg_card: 카드 배경 + 프레임 창(투명) + 라운드 보더
    bg = Image.new("RGBA", (W, H), (24, 38, 58, 255))
    d = ImageDraw.Draw(bg)
    d.rounded_rectangle((40, 40, W - 40, H - 40), radius=36, fill=(240, 244, 248, 255))
    pad = 14
    d.rounded_rectangle(
        (sf["x"] - pad, sf["y"] - pad, sf["x"] + sf["width"] + pad, sf["y"] + sf["height"] + pad),
        radius=24, fill=(24, 38, 58, 255),
    )
    # 프레임 안쪽은 완전히 뚫는다 (슬라이드가 아래 트랙…이 아니라 위 트랙이므로 시각 참고용)
    d.rounded_rectangle(
        (sf["x"], sf["y"], sf["x"] + sf["width"], sf["y"] + sf["height"]),
        radius=16, fill=(0, 0, 0, 0),
    )
    d.text((sf["x"], H - 88), "PLACEHOLDER bg_card — 디자인 확정 시 교체",
           font=_font(28), fill=(24, 38, 58, 255))
    bg.save(assets / "bg_card.png")

    for name, label in (("intro", "INTRO 타이틀 카드"), ("outro", "OUTRO 카드")):
        img = Image.new("RGBA", (W, H), (24, 38, 58, 255))
        d = ImageDraw.Draw(img)
        d.text((W // 2, H // 2), f"{label}\n(PLACEHOLDER)",
               font=_font(72), fill=(240, 244, 248, 255), anchor="mm", align="center")
        img.save(assets / f"{name}.png")

    print(f"플레이스홀더 자산 생성 완료 → {assets}")


if __name__ == "__main__":
    main()
