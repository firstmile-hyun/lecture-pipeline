"""Phase 4 — 강연자 누끼 (RobustVideoMatting, device=mps).

원본에서 강연자 쪽 영역만 크롭해 매팅한 뒤 (속도/품질 유리),
RVM 출력(fgr+pha)을 ffmpeg ProRes 4444 (yuva444p10le)로 인코딩한다.
출력: speaker_alpha.mov (크롭 영역 크기, 알파 포함)
--sample-sec N 이면 앞 N초만 speaker_alpha_sample.mov 로 출력 (엣지 검수용).
"""

import os
import subprocess

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # mps 미지원 연산은 CPU로

import cv2
import numpy as np
import torch
from tqdm import tqdm

from .config import episode_dir, output_dir
from .util import log, outputs_fresh, video_info


def _device() -> torch.device:
    if torch.backends.mps.is_available():
        return torch.device("mps")
    log("matte: mps 사용 불가 → CPU로 진행 (느림)")
    return torch.device("cpu")


def run(episode: str, cfg: dict, force: bool = False, sample_sec: float | None = None) -> None:
    src = episode_dir(episode) / "source.mp4"
    if not src.exists():
        raise FileNotFoundError(f"{src} 없음")

    out = output_dir(episode)
    dst = out / ("speaker_alpha_sample.mov" if sample_sec else "speaker_alpha.mov")
    if not force and outputs_fresh([dst], [src]):
        log(f"[{episode}] matte: {dst.name} 최신 → 스킵 (--force로 재실행)")
        return
    # 임시 파일에 쓰고 성공 시에만 최종 이름으로 교체 — 중단된 실행이 남긴
    # 부분 파일이 완성본으로 오인되는 것을 방지
    tmp = dst.with_name(dst.name + ".part.mov")

    mcfg = cfg["matte"]
    dcfg = cfg["detect"]
    info = video_info(src)

    # 강연자 쪽 크롭 영역 (짝수 폭으로 맞춤 — 인코더 호환)
    cw = int(info.width * mcfg["crop_frac"]) // 2 * 2
    if dcfg["speaker_side"] == "right":
        x0 = info.width - cw
    else:
        x0 = 0

    device = _device()
    log(f"[{episode}] matte: RVM({mcfg['model']}) 로드 중… (최초 실행 시 모델 다운로드)")
    model = torch.hub.load("PeterL1n/RobustVideoMatting", mcfg["model"], trust_repo=True)
    model = model.to(device).eval()

    total = info.nb_frames
    if sample_sec:
        total = min(total, round(float(info.fps) * sample_sec))

    ff = subprocess.Popen(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "rgba",
            "-s", f"{cw}x{info.height}",
            "-r", f"{info.fps.numerator}/{info.fps.denominator}",
            "-i", "-",
            "-c:v", "prores_ks", "-profile:v", "4444",
            "-pix_fmt", "yuva444p10le", "-vendor", "apl0",
            str(tmp),
        ],
        stdin=subprocess.PIPE,
    )

    cap = cv2.VideoCapture(str(src))
    rec = [None] * 4
    dsr = mcfg["downsample_ratio"]
    written = 0
    try:
        with torch.inference_mode():
            for _ in tqdm(range(total), desc=f"matte {episode}", unit="f"):
                ok, frame = cap.read()
                if not ok:
                    break
                crop = frame[:, x0 : x0 + cw]  # BGR
                rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                srct = (
                    torch.from_numpy(rgb).to(device)
                    .permute(2, 0, 1).unsqueeze(0).float() / 255.0
                )
                fgr, pha, *rec = model(srct, *rec, downsample_ratio=dsr)
                rgba = torch.cat([fgr, pha], dim=1)[0]          # (4, H, W) 0..1
                frame_out = (
                    (rgba.permute(1, 2, 0) * 255.0)
                    .clamp(0, 255).byte().cpu().numpy()
                )
                ff.stdin.write(np.ascontiguousarray(frame_out).tobytes())
                written += 1
    finally:
        cap.release()
        ff.stdin.close()
        ff.wait()

    if ff.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("matte: ffmpeg ProRes 인코딩 실패")
    # ffprobe nb_frames가 실디코딩 수보다 1~2프레임 많은 컨테이너가 있어 소폭 허용
    if written < total - max(2, total // 1000):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"matte: 원본에서 {total}프레임 중 {written}프레임만 디코딩됨 — "
            f"원본 손상 또는 VFR 여부 확인 필요"
        )
    tmp.replace(dst)
    log(f"[{episode}] matte: 완료 → {dst} ({cw}x{info.height}, {written}프레임, 알파 포함)")
