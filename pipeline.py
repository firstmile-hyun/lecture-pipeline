#!/usr/bin/env python3
"""강의 영상 리디자인 파이프라인 CLI.

사용법:
  python pipeline.py ep01                      # 전 단계 실행 (있는 산출물은 스킵)
  python pipeline.py ep01 --step match         # 특정 단계만
  python pipeline.py ep01 --step matte --sample-sec 30   # 매팅 30초 샘플
  python pipeline.py ep01 --force              # 재실행
  python pipeline.py all                       # 13편 일괄
"""

import argparse
import sys
from pathlib import Path

from pipeline import detect, match, matte, slides, xmlgen
from pipeline.config import ROOT, load_config

STEPS = ["detect", "slides", "match", "matte", "xml"]


def run_episode(episode: str, steps: list[str], force: bool, sample_sec: float | None) -> None:
    cfg = load_config(episode)
    for step in steps:
        if step == "detect":
            detect.run(episode, cfg, force=force)
        elif step == "slides":
            slides.run(episode, cfg, force=force)
        elif step == "match":
            match.run(episode, cfg, force=force)
        elif step == "matte":
            matte.run(episode, cfg, force=force, sample_sec=sample_sec)
        elif step == "xml":
            xmlgen.run(episode, cfg, force=force)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("episode", help="에피소드 이름 (episodes/ 하위 폴더명) 또는 'all'")
    p.add_argument("--step", choices=STEPS, help="특정 단계만 실행 (기본: 전 단계)")
    p.add_argument("--force", action="store_true", help="산출물이 있어도 재실행")
    p.add_argument("--sample-sec", type=float, default=None,
                   help="matte 단계를 앞 N초만 샘플 처리 (speaker_alpha_sample.mov)")
    args = p.parse_args()

    steps = [args.step] if args.step else STEPS

    if args.episode == "all":
        episodes = sorted(
            d.name for d in (ROOT / "episodes").iterdir()
            if d.is_dir() and (d / "source.mp4").exists()
            and d.name != "sample" and not d.name.startswith("_")  # 테스트 픽스처 제외
        )
        if not episodes:
            sys.exit("episodes/ 아래에 source.mp4가 있는 에피소드가 없습니다")
        failed: list[tuple[str, Exception]] = []
        for ep in episodes:
            print(f"\n===== {ep} =====")
            try:
                run_episode(ep, steps, args.force, args.sample_sec)
            except Exception as e:  # 한 편의 실패가 나머지 편을 막지 않게
                print(f"[pipeline] {ep} 실패: {e}", file=sys.stderr)
                failed.append((ep, e))
        print(f"\n===== 일괄 완료: 성공 {len(episodes) - len(failed)} / 실패 {len(failed)} =====")
        for ep, e in failed:
            print(f"  실패 {ep}: {e}", file=sys.stderr)
        if failed:
            sys.exit(1)
    else:
        if not (ROOT / "episodes" / args.episode).is_dir():
            sys.exit(f"episodes/{args.episode} 폴더가 없습니다")
        run_episode(args.episode, steps, args.force, args.sample_sec)


if __name__ == "__main__":
    main()
