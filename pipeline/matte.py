"""Phase 4 — 강연자 누끼 (RobustVideoMatting, device=mps).

원본에서 강연자 쪽 영역만 크롭해 매팅한 뒤 (속도/품질 유리),
RVM 출력(fgr+pha)을 ffmpeg ProRes 4444 (yuva444p10le)로 인코딩한다.
출력: speaker_alpha.mov (크롭 영역 크기, 알파 포함)
--sample-sec N 이면 앞 N초만 speaker_alpha_sample.mov 로 출력 (엣지 검수용).
"""

import os
import shutil
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
    """강연자 쪽 크롭 폭(짝수)과 x 오프셋. 워커와 반드시 동일한 수식.

    side: right | left | center (중앙 인물 — 신규 촬영본의 흰회색 배경용)
    """
    cw = int(width * crop_frac) // 2 * 2
    if side == "right":
        x0 = width - cw
    elif side == "center":
        x0 = (width - cw) // 2 // 2 * 2  # 짝수 오프셋
    else:
        x0 = 0
    return cw, x0


def _gen_firstframe_mask(src, cw: int, x0: int, out_png, frame_idx: int = 0,
                         _model_cache: dict = {}) -> None:
    """RVM(mobilenetv3)으로 지정 프레임의 강연자 마스크 1장 생성 → MatAnyone2 입력.

    SAM2 포인트 프롬프트 대신, 이미 검증된 RVM 알파를 이진화해 쓴다(단일 프레임이라 빠름).
    청크 처리에선 청크 시작 프레임마다 호출되므로 모델은 프로세스 내 캐시.
    """
    device = _device()
    log(f"[matte] 프레임 {frame_idx} 마스크 생성(RVM)…")
    if "model" not in _model_cache:
        m = torch.hub.load("PeterL1n/RobustVideoMatting", "mobilenetv3", trust_repo=True)
        _model_cache["model"] = m.to(device).eval()
    model = _model_cache["model"]
    cap = cv2.VideoCapture(str(src))
    if frame_idx > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"[matte] 마스크 생성: 프레임 {frame_idx} 읽기 실패")
    crop = frame[:, x0:x0 + cw]
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    t = torch.from_numpy(rgb).to(device).permute(2, 0, 1).unsqueeze(0).float() / 255.0
    with torch.inference_mode():
        _, pha, *_ = model(t, downsample_ratio=0.25)
    m = (pha[0, 0].cpu().numpy() > 0.5).astype(np.uint8) * 255
    Image.fromarray(m, mode="L").save(out_png)


def _mask_from_alpha(mov, local_idx: int, out_png) -> None:
    """완성된 알파 영상(mov)의 local_idx 프레임 알파를 이진화해 마스크 PNG로 저장.

    다음 청크의 초기 마스크로 쓴다 — RVM을 다시 돌리면 청크마다 분할이 미묘하게
    달라져 경계에서 알파가 튀지만, 이전 청크가 수렴한 알파를 그대로 이어받으면
    추적 대상이 동일하게 유지된다.
    """
    tmp = out_png.with_suffix(".gray.png")
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(mov),
         "-vf", f"select=eq(n\\,{local_idx}),alphaextract", "-frames:v", "1", str(tmp)])
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError(f"matte: 이전 청크 알파 추출 실패 (frame {local_idx})")
    gray = cv2.imread(str(tmp), cv2.IMREAD_GRAYSCALE)
    tmp.unlink(missing_ok=True)
    if gray is None:
        raise RuntimeError("matte: 이전 청크 알파 읽기 실패")
    binary = (gray > 127).astype(np.uint8) * 255
    Image.fromarray(binary, mode="L").save(out_png)


def _chunk_ok(path, expected_frames: int) -> bool:
    """청크 산출물이 존재하고 프레임 수가 정확히 맞는가 (이어하기 판정)."""
    if not path.exists():
        return False
    try:
        return video_info(path).nb_frames == expected_frames
    except Exception:
        return False


def _run_mat2_worker(py, src, mask_png, dst, mcfg, side: str,
                     start_f: int | None = None, end_f: int | None = None,
                     lead_f: int = 0) -> None:
    """MatAnyone2 워커 서브프로세스 1회 실행 ([start_f, end_f) 프레임 구간)."""
    cmd = [
        str(py), str(ROOT / "pipeline" / "matte_mat2.py"),
        "--source", str(src), "--mask", str(mask_png), "--out", str(dst),
        "--crop-frac", str(mcfg["crop_frac"]), "--side", side,
        "--warmup", str(mcfg.get("warmup", 10)),
        "--erode", str(mcfg.get("mask_erode", 10)),
        "--dilate", str(mcfg.get("mask_dilate", 10)),
    ]
    if start_f is not None:
        cmd += ["--start-frame", str(start_f)]
    if end_f is not None:
        cmd += ["--end-frame", str(end_f)]
    if lead_f > 0:
        cmd += ["--lead-frames", str(lead_f)]
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    r = subprocess.run(cmd, cwd=ROOT, env=env)  # stdout/stderr 상속 → 상위 로그로 스트리밍
    if r.returncode != 0:
        raise RuntimeError(f"matte: MatAnyone2 워커 실패 (code {r.returncode})")


def _run_matanyone2(episode: str, cfg: dict, force: bool, sample_sec: float | None) -> None:
    """청크 단위 처리 + 이어하기.

    15~30분 영상을 chunk_sec 구간으로 잘라 청크별 산출물(matte_chunks/)을 남긴다.
    중간에 죽어도 완료된 청크는 프레임 수 검증 후 재사용 → 시간·전력 낭비 최소화.
    전 청크 완료 시 ffmpeg concat(-c copy, 무손실)으로 최종 병합.
    """
    src = episode_dir(episode) / "source.mp4"
    if not src.exists():
        raise FileNotFoundError(f"{src} 없음")

    out = output_dir(episode)
    dst = out / ("speaker_alpha_sample.mov" if sample_sec else "speaker_alpha.mov")
    if not force and outputs_fresh([dst], [src]):
        log(f"[{episode}] matte: {dst.name} 최신 → 스킵 (--force로 재실행)")
        return

    mcfg = cfg["matte"]
    side = cfg["detect"]["speaker_side"]
    info = video_info(src)
    fps = float(info.fps)
    cw, x0 = _crop_geom(info.width, mcfg["crop_frac"], side)

    py = ROOT / mcfg.get("mat2_python", ".venv-matanyone2/bin/python")
    if not py.exists():
        raise FileNotFoundError(
            f"{py} 없음 — MatAnyone2 환경 미구축. README의 설치 절차 참고")

    total = info.nb_frames
    if sample_sec:
        total = min(total, round(fps * sample_sec))
    chunk_frames = max(1, round(fps * mcfg.get("chunk_sec", 300)))

    # ---- 단일 실행 경로 (짧은 영상/샘플): 청크 오버헤드 불필요
    if total <= chunk_frames:
        mask_png = out / "_firstframe_mask.png"
        _gen_firstframe_mask(src, cw, x0, mask_png)
        log(f"[{episode}] matte: MatAnyone2 단일 실행 ({total}f, mps — 진행바 참고)")
        _run_mat2_worker(py, src, mask_png, dst, mcfg, side, start_f=0, end_f=total)
        return

    # ---- 청크 경로
    ranges = [(s, min(s + chunk_frames, total)) for s in range(0, total, chunk_frames)]
    chunk_dir = out / "matte_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    log(f"[{episode}] matte: 청크 {len(ranges)}개 × ~{mcfg.get('chunk_sec', 300)}초 "
        f"(총 {total}f) — 완료 청크는 자동 스킵(이어하기)")

    chunks = []
    for i, (s, e) in enumerate(ranges, start=1):
        cpath = chunk_dir / f"chunk_{i:03d}.mov"
        chunks.append(cpath)
        if not force and _chunk_ok(cpath, e - s):
            log(f"[{episode}] matte: 청크 {i}/{len(ranges)} 완료본 재사용 ({cpath.name})")
            continue
        # 경계 연속성: 초기 마스크는 이전 청크가 수렴한 알파에서 이어받는다
        # (첫 청크만 RVM). 리드인은 마스크 기준 프레임부터 실프레임 추적(출력 제외).
        mask_png = chunk_dir / f"mask_{i:03d}.png"
        if i == 1:
            lead = 0
            _gen_firstframe_mask(src, cw, x0, mask_png, frame_idx=0)
        else:
            prev_s, prev_e = ranges[i - 2]
            # 마스크는 이전 청크 내부 프레임이어야 하므로 lead ≥ 1 보장
            lead = max(1, min(mcfg.get("chunk_lead_frames", 30), s, prev_e - prev_s))
            # 이전 청크의 로컬 인덱스 (전역 s-lead 프레임 = 이전 청크의 끝-lead)
            _mask_from_alpha(chunks[i - 2], (prev_e - prev_s) - lead, mask_png)
        log(f"[{episode}] matte: 청크 {i}/{len(ranges)} 처리 중 (프레임 {s}–{e}, 리드인 {lead}f)…")
        _run_mat2_worker(py, src, mask_png, cpath, mcfg, side, start_f=s, end_f=e, lead_f=lead)
        if not _chunk_ok(cpath, e - s):
            raise RuntimeError(f"[{episode}] matte: 청크 {i} 프레임 수 불일치 — 재실행 필요")

    # ---- 무손실 병합 (ProRes 스트림 카피)
    log(f"[{episode}] matte: 청크 {len(ranges)}개 병합 중…")
    concat_list = chunk_dir / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{c.resolve()}'\n" for c in chunks), encoding="utf-8")
    tmp = dst.with_name(dst.name + ".part.mov")
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(concat_list), "-c", "copy", str(tmp)])
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"[{episode}] matte: 청크 병합 실패")
    if not _chunk_ok(tmp, total):
        got = video_info(tmp).nb_frames if tmp.exists() else 0
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"[{episode}] matte: 병합본 프레임 수 불일치 ({got} ≠ {total})")
    tmp.replace(dst)
    shutil.rmtree(chunk_dir)  # 성공 시에만 정리 (실패 시 남겨 이어하기)
    log(f"[{episode}] matte: 완료 → {dst} ({cw}x{info.height}, {total}프레임, 알파 포함)")


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
