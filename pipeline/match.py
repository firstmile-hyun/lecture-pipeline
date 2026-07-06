"""Phase 3 — 대표 프레임 ↔ 슬라이드 매칭 (perceptual hash).

비교 조건을 맞추기 위해 양쪽 모두 강연자 반대편 analyze_width 비율로 크롭 후
phash로 거리 행렬을 만들고, 단조 증가(순차 진행) 제약의 DP 정렬을 우선 적용한다.
전역 최적 매칭이 단조 매칭보다 backward_margin 이상 가까우면 역행으로 인정하고 flag=backward.
출력: match.csv (scene, start_tc, matched_page, confidence, flag)
"""

import csv
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image

from .config import output_dir
from .util import log, outputs_fresh


def _crop_analysis_region(img: Image.Image, analyze_width: float, speaker_side: str) -> Image.Image:
    w, h = img.size
    aw = int(w * analyze_width)
    if speaker_side == "right":
        return img.crop((0, 0, aw, h))
    return img.crop((w - aw, 0, w, h))


def _pad_to_aspect(img: Image.Image, aspect: float) -> Image.Image:
    """슬라이드 종횡비가 영상과 다르면(예: 4:3 덱) 전체화면 표시 형태(검은 여백)를
    재현해 영상 프레임과 같은 기하로 비교되게 한다."""
    w, h = img.size
    if abs(w / h - aspect) / aspect < 0.02:
        return img
    if w / h < aspect:  # 좌우 필러박스
        nw = round(h * aspect)
        canvas = Image.new("RGB", (nw, h), (0, 0, 0))
        canvas.paste(img, ((nw - w) // 2, 0))
    else:  # 상하 레터박스
        nh = round(w / aspect)
        canvas = Image.new("RGB", (w, nh), (0, 0, 0))
        canvas.paste(img, (0, (nh - h) // 2))
    return canvas


def _hashes(paths: list[Path], analyze_width: float, speaker_side: str, hash_size: int,
            pad_aspect: float | None = None):
    out = []
    for p in paths:
        with Image.open(p) as img:
            rgb = img.convert("RGB")
            if pad_aspect:
                rgb = _pad_to_aspect(rgb, pad_aspect)
            cropped = _crop_analysis_region(rgb, analyze_width, speaker_side)
            out.append(imagehash.phash(cropped, hash_size=hash_size))
    return out


def run(episode: str, cfg: dict, force: bool = False) -> None:
    out = output_dir(episode)
    match_csv = out / "match.csv"
    cuts_csv = out / "cuts.csv"
    frames_dir = out / "scene_frames"
    slides_dir = out / "slides_png"

    for req, step in ((cuts_csv, "detect"), (slides_dir, "slides")):
        if not req.exists():
            raise FileNotFoundError(f"{req} 없음 — 먼저 --step {step} 실행 필요")

    page_paths = sorted(slides_dir.glob("page_*.png"))
    if not page_paths:
        raise FileNotFoundError(f"{slides_dir} 에 page_*.png 없음")

    if not force and outputs_fresh([match_csv], [cuts_csv, *page_paths]):
        log(f"[{episode}] match: 산출물 최신 → 스킵 (--force로 재실행)")
        return

    mcfg = cfg["match"]
    dcfg = cfg["detect"]

    with open(cuts_csv, encoding="utf-8") as f:
        cuts = list(csv.DictReader(f))
    frame_paths = [frames_dir / f"scene_{int(c['scene']):03d}.png" for c in cuts]
    # detect가 대표 프레임 추출에 실패한 씬은 매칭 불가 → review로 넘긴다
    missing = {i for i, p in enumerate(frame_paths) if not p.exists()}
    if missing:
        log(f"[{episode}] match: 대표 프레임 없는 씬 {len(missing)}개 → flag=review 처리")

    log(f"[{episode}] match: 씬 {len(frame_paths)}개 ↔ 페이지 {len(page_paths)}개 phash 매칭…")

    # 영상 프레임은 강연자 영역을 잘라내고, 슬라이드도 동일 비율로 잘라 조건을 맞춘다
    avail = [i for i in range(len(frame_paths)) if i not in missing]
    if not avail:
        raise RuntimeError(f"[{episode}] match: 대표 프레임이 하나도 없음 — --step detect --force 필요")
    scene_h = _hashes([frame_paths[i] for i in avail],
                      dcfg["analyze_width"], dcfg["speaker_side"], mcfg["hash_size"])
    with Image.open(frame_paths[avail[0]]) as first:  # 슬라이드는 영상 종횡비로 패딩 후 비교
        video_aspect = first.width / first.height
    page_h = _hashes(page_paths, dcfg["analyze_width"], dcfg["speaker_side"],
                     mcfg["hash_size"], pad_aspect=video_aspect)

    n_bits = mcfg["hash_size"] ** 2
    S, P = len(scene_h), len(page_h)
    D = np.array([[(sh - ph) / n_bits for ph in page_h] for sh in scene_h])  # 정규화 거리

    # 단조 증가 DP 정렬: dp[i][j] = D[i][j] + min_{k<=j} dp[i-1][k]
    mono = np.zeros(S, dtype=int)
    if S > 0:
        dp = np.zeros_like(D)
        back = np.zeros((S, P), dtype=int)
        dp[0] = D[0]
        for i in range(1, S):
            prefix_min = np.minimum.accumulate(dp[i - 1])
            prefix_arg = np.zeros(P, dtype=int)
            best = 0
            for j in range(1, P):
                if dp[i - 1][j] < dp[i - 1][best]:
                    best = j
                prefix_arg[j] = best
            dp[i] = D[i] + prefix_min
            back[i] = prefix_arg
        # 역추적
        mono[-1] = int(np.argmin(dp[-1]))
        for i in range(S - 1, 0, -1):
            mono[i - 1] = back[i][mono[i]]

    rows = []
    prev_page = 0
    k = 0  # avail(대표 프레임이 있는 씬) 인덱스
    for i, cut in enumerate(cuts):
        if i in missing:
            # 대표 프레임이 없으면 직전 페이지 유지 + 사람이 확인
            chosen, conf, flags = prev_page, 0.0, ["review"]
        else:
            m = int(mono[k])
            g = int(np.argmin(D[k]))
            chosen = m
            # 전역 최적이 단조 매칭보다 충분히 가까우면 역행 인정
            if g != m and D[k][m] - D[k][g] > mcfg["backward_margin"]:
                chosen = g
            flags = []
            if chosen < prev_page:  # 최종 선택이 이전 씬보다 앞 페이지면 역행
                flags.append("backward")
            conf = 1.0 - float(D[k][chosen])
            if conf < mcfg["review_confidence"]:
                flags.append("review")
            k += 1
        rows.append({
            "scene": cut["scene"],
            "start_tc": cut["start_tc"],
            "matched_page": chosen + 1,
            "confidence": f"{conf:.3f}",
            "flag": "+".join(flags),
        })
        prev_page = chosen

    tmp_csv = match_csv.with_suffix(".csv.tmp")  # 중단 시 잘린 파일 방지
    with open(tmp_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["scene", "start_tc", "matched_page", "confidence", "flag"])
        w.writeheader()
        w.writerows(rows)
    tmp_csv.replace(match_csv)

    n_review = sum(1 for r in rows if "review" in r["flag"])
    n_back = sum(1 for r in rows if "backward" in r["flag"])
    log(f"[{episode}] match: 완료 → {match_csv} (review {n_review}건, backward {n_back}건)")
