#!/bin/zsh
# 더블클릭 실행용 폴백 (터미널 창이 함께 열립니다)
cd "$(dirname "$0")"
exec .venv/bin/python app/app.py
