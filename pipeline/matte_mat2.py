"""Phase 4 워커 — MatAnyone 2 강연자 누끼.

matte.py(본체, .venv 3.11)가 subprocess로 별도 환경(.venv-matanyone2, 3.10)의
이 스크립트를 호출한다. 소스에서 강연자 쪽만 크롭 → MatAnyone2 매팅 →
ProRes 4444 알파(.mov). 프레임은 cv2 스트리밍(메모리 절약 + torchvision.io.read_video 우회).

첫 프레임 마스크(--mask)는 matte.py가 RVM으로 만들어 넘긴다(크롭 해상도와 동일).
"""

import argparse
import subprocess
import sys
import time

import cv2
import numpy as np
import torch
from PIL import Image
from tqdm import tqdm

from matanyone2.utils.download_util import load_file_from_url
from matanyone2.utils.inference_utils import gen_dilate, gen_erosion
from matanyone2.inference.inference_core import InferenceCore
from matanyone2.utils.get_default_model import get_matanyone2_model
from matanyone2.utils.device import get_default_device

CKPT_URL = "https://github.com/pq-yang/MatAnyone2/releases/download/v1.0.0/matanyone2.pth"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--mask", required=True, help="첫 프레임 마스크 PNG (크롭 해상도)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--crop-frac", type=float, default=0.45)
    ap.add_argument("--side", default="right", choices=["right", "left", "center"])
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--erode", type=int, default=10)
    ap.add_argument("--dilate", type=int, default=10)
    ap.add_argument("--sample-sec", type=float, default=None)
    ap.add_argument("--start-frame", type=int, default=None, help="처리 시작 프레임 (청크)")
    ap.add_argument("--end-frame", type=int, default=None, help="처리 끝 프레임(미포함, 청크)")
    ap.add_argument("--lead-frames", type=int, default=0,
                    help="시작 전 실프레임 리드인(출력 제외) — 청크 경계 알파 연속성용")
    a = ap.parse_args()

    device = get_default_device()
    print(f"[matte] MatAnyone2 로드 중… device={device} (최초 실행 시 모델 다운로드)", flush=True)
    ckpt = load_file_from_url(CKPT_URL, "pretrained_models")
    model = get_matanyone2_model(ckpt, device)
    processor = InferenceCore(model, cfg=model.cfg)

    cap = cv2.VideoCapture(a.source)
    W0 = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_src = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # 처리 구간 [start, end) — 청크 처리용. 미지정 시 전체(+sample-sec 제한).
    start = a.start_frame or 0
    end = a.end_frame if a.end_frame is not None else total_src
    end = min(end, total_src)
    if a.sample_sec:
        end = min(end, start + round(fps * a.sample_sec))
    n = end - start
    if n <= 0:
        sys.exit(f"[matte] 처리 구간이 비어 있음 (start={start}, end={end})")
    # 리드인: 청크 시작 전 실제 프레임을 추적만 하고 출력하지 않는다 —
    # 첫 프레임 반복 예열만으로는 이전 청크가 이어온 메모리 상태와 다르게 수렴해
    # 경계에서 알파가 튀는 것을 완화 (마스크도 리드인 시작 프레임 기준).
    lead = min(max(0, a.lead_frames), start)
    seek_to = start - lead
    if seek_to > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, seek_to)

    # matte.py의 RVM 마스크 생성과 동일한 크롭 (짝수 폭 · 짝수 오프셋)
    cw = int(W0 * a.crop_frac) // 2 * 2
    if a.side == "right":
        x0 = W0 - cw
    elif a.side == "center":
        x0 = (W0 - cw) // 2 // 2 * 2
    else:
        x0 = 0
    print(f"[matte] 크롭 {cw}x{H} (x0={x0}), 프레임 {start}–{end} ({n}f), fps={fps:.3f}", flush=True)

    mask = np.array(Image.open(a.mask).convert("L"))
    if a.dilate > 0:
        mask = gen_dilate(mask, a.dilate, a.dilate)
    if a.erode > 0:
        mask = gen_erosion(mask, a.erode, a.erode)
    mask = torch.from_numpy(mask).float().to(device)

    # 임시 파일에 쓰고 성공 시에만 교체 (중단본이 완성본으로 오인되지 않게)
    tmp = a.out + ".part.mov"
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
         "-s", f"{cw}x{H}", "-r", f"{fps}", "-i", "-",
         "-c:v", "prores_ks", "-profile:v", "4444",
         "-pix_fmt", "yuva444p10le", "-vendor", "apl0", tmp],
        stdin=subprocess.PIPE,
    )

    ok, f0 = cap.read()
    if not ok:
        sys.exit("[matte] 소스 첫 프레임 읽기 실패")
    f0 = f0[:, x0:x0 + cw]

    # 반복 순서: [0..warmup) 첫 프레임 반복 예열 → [warmup..warmup+lead) 리드인 실프레임
    # (추적만) → [warmup+lead..total) 본 구간 출력. lead=0이면 기존과 동일.
    out_from = a.warmup + lead
    total = a.warmup + lead + n
    written = 0
    t0 = time.time()
    with torch.inference_mode():
        for ti in tqdm(range(total), desc="matte(MatAnyone2)", unit="f"):
            if ti <= a.warmup:            # 0..warmup: 리드인 시작 프레임 반복
                bgr = f0
            else:
                ok, fr = cap.read()
                if not ok:
                    break
                bgr = fr[:, x0:x0 + cw]
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            img = (torch.from_numpy(rgb).permute(2, 0, 1).float() / 255.).to(device)

            if ti == 0:
                processor.step(img, mask, objects=[1])        # 마스크 인코딩
                prob = processor.step(img, first_frame_pred=True)
            elif ti <= a.warmup:
                prob = processor.step(img, first_frame_pred=True)
            else:
                prob = processor.step(img)

            pha = processor.output_prob_to_mask(prob).cpu().numpy()   # HxW 0..1
            if ti >= out_from:
                al = np.clip(pha * 255, 0, 255).astype(np.uint8)[..., None]
                rgba = np.concatenate([rgb, al], axis=2)
                ff.stdin.write(np.ascontiguousarray(rgba).tobytes())
                written += 1

    cap.release()
    ff.stdin.close()
    ff.wait()

    import os
    if ff.returncode != 0:
        os.path.exists(tmp) and os.remove(tmp)
        sys.exit("[matte] ffmpeg ProRes 인코딩 실패")
    if written < n - max(2, n // 1000):
        os.path.exists(tmp) and os.remove(tmp)
        sys.exit(f"[matte] {n}프레임 중 {written}프레임만 처리됨 — 원본 손상/VFR 확인 필요")
    os.replace(tmp, a.out)
    el = time.time() - t0
    print(f"[matte] 완료 → {a.out} ({cw}x{H}, {written}프레임, 알파 포함) — "
          f"{el:.0f}초, {total / el:.2f} f/s", flush=True)


if __name__ == "__main__":
    main()
