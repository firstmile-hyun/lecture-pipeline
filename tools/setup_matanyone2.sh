#!/bin/zsh
# Phase 4(MatAnyone 2)용 별도 환경(.venv-matanyone2, Python 3.10) 구축.
# 본체(.venv 3.11)와 분리하고 matte.py가 subprocess로 호출한다.
#
# 주의:
#  - MatAnyone2 pyproject에 패키징 버그(중복 force-include)가 있어 그대로는 빌드 실패 →
#    클론 후 해당 블록을 제거하고 설치한다.
#  - pyproject가 torch를 CUDA(cu128) 인덱스로 고정하지만, torch를 Mac 기본 인덱스로
#    먼저 설치해두면 kornia 등이 이미 설치된 torch를 그대로 쓴다(mps 지원 휠).
#
# 사용: cd lecture-pipeline && zsh tools/setup_matanyone2.sh
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
BUILD="$ROOT/.matanyone2_build"

echo "== 1) MatAnyone2 클론 =="
rm -rf "$BUILD"
git clone --depth 1 https://github.com/pq-yang/MatAnyone2 "$BUILD"

echo "== 2) 패키징 버그 우회 (중복 force-include 제거) =="
python3 - "$BUILD/pyproject.toml" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = re.sub(r'\n\[tool\.hatch\.build\.targets\.wheel\.force-include\]\n"matanyone2/config" = "matanyone2/config"\n', "\n", s)
open(p, "w", encoding="utf-8").write(s)
print("  force-include 블록 제거 완료")
PY

echo "== 3) venv 생성 (Python 3.10) =="
uv venv --python 3.10 .venv-matanyone2

echo "== 4) torch/torchvision (Mac 기본 인덱스 = mps 지원) =="
uv pip install --python .venv-matanyone2/bin/python torch torchvision

echo "== 5) MatAnyone2 설치 =="
uv pip install --python .venv-matanyone2/bin/python "$BUILD"

echo "== 6) 정리 & 확인 =="
rm -rf "$BUILD"
.venv-matanyone2/bin/python -c "
import torch
from matanyone2.utils.device import get_default_device
assert torch.backends.mps.is_available(), 'mps 사용 불가'
print('OK — device:', get_default_device())
"
echo "완료. Phase 4가 MatAnyone2로 동작합니다 (config.yaml matte.model: matanyone2)."
