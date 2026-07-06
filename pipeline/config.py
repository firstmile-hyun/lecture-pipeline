"""공통 설정 로드 + 편별 오버라이드 deep-merge."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(episode: str | None = None) -> dict:
    """루트 config.yaml을 읽고, episodes/<ep>/config.yaml이 있으면 덮어쓴다."""
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if episode:
        ep_cfg_path = ROOT / "episodes" / episode / "config.yaml"
        if ep_cfg_path.exists():
            with open(ep_cfg_path, encoding="utf-8") as f:
                ep_cfg = yaml.safe_load(f) or {}
            cfg = _deep_merge(cfg, ep_cfg)
    return cfg


def episode_dir(episode: str) -> Path:
    return ROOT / "episodes" / episode


def output_dir(episode: str) -> Path:
    d = episode_dir(episode) / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def assets_dir() -> Path:
    return ROOT / "assets"
