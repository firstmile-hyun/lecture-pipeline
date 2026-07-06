"""Phase 1 — 슬라이드 전환 감지 (PySceneDetect ContentDetector).

강연자 영역을 제외하기 위해 analyze_width 비율만큼만 crop해서 감지한다.
출력: cuts.csv (scene, start_tc, start_sec, start_frame), scene_frames/scene_NNN.png
"""

import csv
from pathlib import Path

import cv2
from scenedetect import ContentDetector, SceneManager, open_video

from .config import episode_dir, output_dir
from .util import frame_to_tc, log, outputs_fresh, video_info


def run(episode: str, cfg: dict, force: bool = False) -> None:
    src = episode_dir(episode) / "source.mp4"
    if not src.exists():
        raise FileNotFoundError(f"{src} 없음 — 원본 영상을 넣어주세요")

    out = output_dir(episode)
    cuts_csv = out / "cuts.csv"
    frames_dir = out / "scene_frames"

    if not force and frames_dir.is_dir() and outputs_fresh([cuts_csv], [src]):
        log(f"[{episode}] detect: 산출물 최신 → 스킵 (--force로 재실행)")
        return

    dcfg = cfg["detect"]
    info = video_info(src)
    fps = float(info.fps)

    video = open_video(str(src))
    sm = SceneManager()
    sm.add_detector(
        ContentDetector(
            threshold=dcfg["threshold"],
            min_scene_len=round(fps * dcfg["min_scene_sec"]),
        )
    )

    # 강연자 반대쪽만 분석 (crop = (x0, y0, x1, y1) — 양끝 포함 좌표).
    # y1을 height-2로 두는 것은 scenedetect 0.7의 경계 비교 버그가 내는 허위 경고 회피용.
    aw = int(info.width * dcfg["analyze_width"])
    y1 = info.height - 2
    if dcfg["speaker_side"] == "right":
        sm.crop = (0, 0, aw - 1, y1)
    else:
        sm.crop = (info.width - aw, 0, info.width - 2, y1)

    log(f"[{episode}] detect: {src.name} ({info.width}x{info.height} @ {fps:.3f}fps) 분석 중…")
    sm.detect_scenes(video, show_progress=True)
    scenes = sm.get_scene_list(start_in_scene=True)
    if not scenes:  # 전환이 하나도 없으면 전체를 1개 씬으로
        rows = [(0, info.nb_frames)]
    else:
        rows = [(s.get_frames(), e.get_frames()) for s, e in scenes]

    frames_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(src))
    offset = round(fps * dcfg.get("rep_frame_offset_sec", 1.0))

    # 중단 시 잘린 파일이 완성본으로 오인되지 않게 임시 파일에 쓰고 마지막에 교체
    tmp_csv = cuts_csv.with_suffix(".csv.tmp")
    with open(tmp_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["scene", "start_tc", "start_sec", "start_frame"])
        for i, (start_f, end_f) in enumerate(rows, start=1):
            w.writerow([
                i,
                frame_to_tc(start_f, info.fps),
                f"{start_f / fps:.3f}",
                start_f,
            ])
            # 대표 프레임: 전환 후 offset 지점 (씬 길이를 넘지 않게 클램프)
            rep = min(start_f + offset, max(start_f, end_f - 1))
            cap.set(cv2.CAP_PROP_POS_FRAMES, rep)
            ok, frame = cap.read()
            if not ok:  # 영상 끝 근처 시크 실패 시 씬 시작 프레임으로 재시도
                cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
                ok, frame = cap.read()
            if ok:
                cv2.imwrite(str(frames_dir / f"scene_{i:03d}.png"), frame)
            else:
                log(f"[{episode}] detect: scene {i} 대표 프레임 추출 실패 (frame {rep})")
    cap.release()
    tmp_csv.replace(cuts_csv)

    log(f"[{episode}] detect: 씬 {len(rows)}개 → {cuts_csv}")
