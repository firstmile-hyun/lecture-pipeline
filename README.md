# 강의 영상 리디자인 파이프라인

영상 1편 + 강의자료 PDF를 넣으면 프리미어에서 바로 열리는 조립 완료 타임라인(`sequence.xml`)을 만든다.
스펙: [LECTURE_PIPELINE_SPEC.md](LECTURE_PIPELINE_SPEC.md)

## 설치

```bash
cd lecture-pipeline
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## 데스크톱 앱 (권장)

터미널 없이 쓰려면 **`LecturePipeline.app` 더블클릭** (또는 `앱실행.command` — 터미널 창이 함께 열림).

1. 좌측 "새 에피소드"에서 영상·PDF를 파일 선택 창으로 고르고 이름 입력 → 만들기
   (원본은 복사되지 않고 심링크로 연결 — 용량 걱정 없음)
2. **▶ 전체 실행** → 진행 로그가 실시간 표시. 처음엔 "30초 샘플 누끼"로 엣지 먼저 확인 권장
3. 완료 후 매칭 표에서 노란색(review) 행만 확인, "미리보기"로 레이아웃 확인
4. **XML 내보내기…** 로 원하는 위치에 저장 → 프리미어 File > Import

앱은 pipeline.py를 그대로 실행하는 래퍼라서 CLI와 산출물·설정이 완전히 동일하다.
주의: .app은 이 폴더(lecture-pipeline) 안에 있어야 실행된다 (venv를 상대 경로로 찾음).

## 사용법 (CLI)

```bash
# 입력 배치
episodes/ep01/source.mp4     # 원본 강의 영상
episodes/ep01/slides.pdf     # 강의자료 (PPT는 PDF로 변환)

# 전 단계 실행 (이미 있는 산출물은 스킵)
.venv/bin/python pipeline.py ep01

# 특정 단계만 / 강제 재실행
.venv/bin/python pipeline.py ep01 --step match
.venv/bin/python pipeline.py ep01 --step detect --force

# 매팅은 30초 샘플로 먼저 엣지 검수 (speaker_alpha_sample.mov 생성)
.venv/bin/python pipeline.py ep01 --step matte --sample-sec 30

# 13편 일괄
.venv/bin/python pipeline.py all
```

결과: `episodes/ep01/output/sequence.xml` → 프리미어 File > Import → 검수 → 출력.

## 단계 (Phase)

| step   | 입력 | 출력 | 내용 |
|--------|------|------|------|
| detect | source.mp4 | cuts.csv, scene_frames/ | 슬라이드 전환 감지 (PySceneDetect, 강연자 영역 제외 crop) |
| slides | slides.pdf | slides_png/ | PDF → PNG (최소 1600px 폭) |
| match  | 위 둘 | match.csv | phash 매칭 + 단조 증가 DP, 역행은 backward, 저신뢰는 review 플래그 |
| matte  | source.mp4 | speaker_alpha.mov | RVM(mps)으로 강연자 누끼 → ProRes 4444 알파 |
| xml    | 전부 | sequence.xml | 프리미어 임포트용 xmeml 시퀀스 조립 |

- 스킵 판정은 mtime 기반 신선도 검사다: 업스트림 산출물이 갱신되면 다운스트림은 다음 실행에서
  자동 재생성된다 (예: `--step matte`를 나중에 돌리면 다음 `pipeline.py epNN`에서 xml이 V3 포함으로 갱신).
- `match.csv`에서 `flag=review` 행만 사람이 확인하면 된다. 같은 페이지 연속 매칭은 XML 단계에서 자동 병합된다.
- `speaker_alpha.mov`가 없으면 XML은 V3 없이 생성된다 (경고 출력) — V1+V2 먼저 검증하는 개발 흐름 지원.
- 인트로/아웃트로: `assets/intro.png`(편별 버전은 `assets/intro_ep01.png`가 우선), `outro.png`가 있으면 본편 앞뒤에 배치.
- 챕터 배지(Phase 6): `episodes/epNN/chapters.csv` (`start_tc,badge_png`)가 있으면 V4에 배치. 없으면 스킵.
  `start_tc`는 넌드롭(`HH:MM:SS:FF`)·드롭프레임(`HH:MM:SS;FF`, 프리미어 29.97 기본 표시)·초 표기 모두 허용.
- 원본은 CFR을 전제한다. VFR(화면 녹화 등) 의심 시 경고가 출력되며, 그 경우
  `ffmpeg -i in.mp4 -vsync cfr -r 30 ...`으로 변환 후 투입할 것.

## 설정

- 레이아웃 좌표·감지 파라미터는 전부 [config.yaml](config.yaml). 배경 카드 디자인 확정 시 `layout.slide_frame` 좌표만 갱신하면 13편 전체 반영.
- 편별 오버라이드: `episodes/epNN/config.yaml`을 만들면 공통 설정 위에 deep-merge (예: 강연자가 왼쪽인 편은 `detect: {speaker_side: left}`).
- `cuts.csv`의 `start_tc`는 표시용 넌드롭 타임코드다. 모든 계산은 `start_frame`(정수 프레임) 기준이라 29.97 드롭프레임 이슈가 없다.

## 프리미어 임포트 팁

- **Preferences > Media > Default Media Scaling을 None으로** 두고 임포트할 것.
  이 전역 설정이 켜져 있으면 XML의 명시적 Scale 값과 충돌할 수 있다 (XML로는 제어 불가).
- 첫 임포트 시 슬라이드 위치가 프레임과 어긋나면 `layout.slide_frame` 좌표를 확인하고
  `--step xml --force`로 재생성하면 된다 (13편 전체 반영은 config.yaml만 수정).
- 클립 배치 좌표는 실제 프리미어 export에서 검증된 규약(캔버스 크기 정규화 center)을 쓴다.

## 테스트

실데이터 없이 전체 흐름 검증:

```bash
.venv/bin/python tools/make_placeholder_assets.py   # bg_card/intro/outro 플레이스홀더
.venv/bin/python tools/make_sample_episode.py       # episodes/sample/ 합성 에피소드
.venv/bin/python pipeline.py sample
```

합성 에피소드는 29.97fps(NTSC 타임베이스 경로), 역행 구간(3→2페이지), 미사용 페이지를 포함해
detect/match의 경계 상황을 함께 검증한다.
