"""Phase 4 — 강연자 누끼 (RobustVideoMatting, device=mps).

원본에서 강연자 쪽 영역만 크롭해 매팅한 뒤 (속도/품질 유리),
RVM 출력(fgr+pha)을 ffmpeg ProRes 4444 (yuva444p10le)로 인코딩한다.
출력: speaker_alpha.mov (크롭 영역 크기, 알파 포함)
--sample-sec N 이면 앞 N초만 speaker_alpha_sample.mov 로 출력 (엣지 검수용).
"""

import os
import subprocess
import sys

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # mps 미지원 연산은 CPU로

import cv2
import numpy as np
import torch
from PIL import Image
from tqdm import tqdm

from .config import ROOT, episode_dir, output_dir
from .util import log, outputs_fresh, video_info


def _device() -> torch.device:
    if torch.backends.mps.is_available():
        return torch.device("mps")
    log("matte: mps 사용 불가 → CPU로 진행 (느림)")
    return torch.device("cpu")


def run(episode: str, cfg: dict, force: bool = False, sample_sec: float | None = None) -> None:
    """Phase 4 디스패처: config matte.model에 따라 MatAnyone2 또는 RVM 경로."""
    if cfg["matte"].get("model") == "matanyone2":
        _run_matanyone2(episode, cfg, force=force, sample_sec=sample_sec)
    else:
        _run_rvm(episode, cfg, force=force, sample_sec=sample_sec)


# ---------------------------------------------------------------- MatAnyone 2 경로


def _crop_geom(width: int, crop_frac: float, side: str) -> tuple[int, int]:
    """강연자 쪽 크롭 폭(짝수)과 x 오프셋. 워커와 반드시 동일한 수식."""
    cw = int(width * crop_frac) // 2 * 2
    x0 = width - cw if side == "right" else 0
    return cw, x0


def _gen_firstframe_mask(src, cw: int, x0: int, out_png) -> None:
    """RVM(mobilenetv3)으로 첫 프레임 강연자 마스크 1장 생성 → MatAnyone2 입력.

    SAM2 포인트 프롬프트 대신, 이미 검증된 RVM 알파를 이진화해 쓴다(단일 프레임이라 빠름).
    """
    device = _device()
    log(f"[matte] 첫 프레임 마스크 생성(RVM)…")
    model = torch.hub.load("PeterL1n/RobustVideoMatting", "mobilenetv3", trust_repo=True)
    model = model.to(device).eval()
    cap = cv2.VideoCapture(str(src))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("[matte] 첫 프레임 마스크: 소스 읽기 실패")
    crop = frame[:, x0:x0 + cw]
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    t = torch.from_numpy(rgb).to(device).permute(2, 0, 1).unsqueeze(0).float() / 255.0
    with torch.inference_mode():
        _, pha, *_ = model(t, downsample_ratio=0.25)
    m = (pha[0, 0].cpu().numpy() > 0.5).astype(np.uint8) * 255
    Image.fromarray(m, mode="L").save(out_png)


def _run_matanyone2(episode: str, cfg: dict, force: bool, sample_sec: float | None) -> None:
    src = episode_dir(episode) / "source.mp4"
    if not src.exists():
        raise FileNotFoundError(f"{src} 없음")

    out = output_dir(episode)
    dst = out / ("speaker_alpha_sample.mov" if sample_sec else "speaker_alpha.mov")
    if not force and outputs_fresh([dst], [src]):
        log(f"[{episode}] matte: {dst.name} 최신 → 스킵 (--force로 재실행)")
        return

    mcfg = cfg["matte"]
    dcfg = cfg["detect"]
    info = video_info(src)
    cw, x0 = _crop_geom(info.width, mcfg["crop_frac"], dcfg["speaker_side"])

    mask_png = out / "_firstframe_mask.png"
    _gen_firstframe_mask(src, cw, x0, mask_png)

    worker = ROOT / "pipeline" / "matte_mat2.py"
    py = ROOT / mcfg.get("mat2_python", ".venv-matanyone2/bin/python")
    if not py.exists():
        raise FileNotFoundError(
            f"{py} 없음 — MatAnyone2 환경 미구축. README의 설치 절차 참고")
    cmd = [
        str(py), str(worker),
        "--source", str(src), "--mask", str(mask_png), "--out", str(dst),
        "--crop-frac", str(mcfg["crop_frac"]), "--side", dcfg["speaker_side"],
        "--warmup", str(mcfg.get("warmup", 10)),
        "--erode", str(mcfg.get("mask_erode", 10)),
        "--dilate", str(mcfg.get("mask_dilate", 10)),
    ]
    if sample_sec:
        cmd += ["--sample-sec", str(sample_sec)]

    log(f"[{episode}] matte: MatAnyone2 서브프로세스 실행 (mps, 느림 — 진행바 참고)")
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    r = subprocess.run(cmd, cwd=ROOT, env=env)  # stdout/stderr 상속 → 상위 로그로 스트리밍
    if r.returncode != 0:
        raise RuntimeError(f"[{episode}] matte: MatAnyone2 워커 실패 (code {r.returncode})")


# ---------------------------------------------------------------- RVM 경로 (레거시/폴백)


def _run_rvm(episode: str, cfg: dict, force: bool = False, sample_sec: float | None = None) -> None:
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
