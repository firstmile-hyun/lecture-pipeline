"""Phase 2 — 강의자료 PDF → PNG 추출 (PyMuPDF).

슬라이드 프레임 영역보다 큰 해상도로 렌더링한다 (최소 min_width px 폭).
출력: slides_png/page_NNN.png
"""

import fitz

from .config import episode_dir, output_dir
from .util import log, outputs_fresh


def run(episode: str, cfg: dict, force: bool = False) -> None:
    pdf = episode_dir(episode) / "slides.pdf"
    if not pdf.exists():
        raise FileNotFoundError(f"{pdf} 없음 — 강의자료 PDF를 넣어주세요 (PPT는 PDF로 변환)")

    out = output_dir(episode) / "slides_png"
    doc = fitz.open(pdf)
    n = len(doc)

    existing = sorted(out.glob("page_*.png")) if out.is_dir() else []
    if not force and len(existing) == n and outputs_fresh(existing, [pdf]):
        log(f"[{episode}] slides: 산출물 최신 ({n}페이지) → 스킵 (--force로 재실행)")
        doc.close()
        return
    out.mkdir(parents=True, exist_ok=True)
    for old in existing:  # 이전 덱의 잔여 페이지가 매칭을 오염시키지 않도록 정리
        old.unlink()

    target_w = max(
        cfg["slides"]["min_width"],
        cfg["layout"]["slide_frame"]["width"],
    )

    for i, page in enumerate(doc, start=1):
        zoom = target_w / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(out / f"page_{i:03d}.png")
    doc.close()

    log(f"[{episode}] slides: {n}페이지 → {out} (폭 {target_w}px)")
