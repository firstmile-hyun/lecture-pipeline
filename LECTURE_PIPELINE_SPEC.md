# 강의 영상 리디자인 자동화 파이프라인 — 개발 스펙

## 배경과 목표

강의 영상 13편을 청년마을 브랜드 톤으로 리디자인한다. 원본 영상은 "강의 슬라이드 전체화면 + 누끼 딴 강연자가 오른쪽에 합성"된 형태다. 최종 결과물은 새 레이아웃이다: 청년마을 배경 카드(고정 PNG) 위에, 라운드 프레임 안에 원본 슬라이드가 들어가고, 강연자는 우하단에 배치된다.

이 파이프라인의 목표: 영상 1편 + 강의자료 1개를 넣으면, **프리미어 프로에서 열었을 때 이미 조립이 끝난 타임라인(XML 시퀀스)** 이 나오는 것. 사람은 검수와 미세조정만 한다.

```
python pipeline.py ep01
→ 프리미어에서 sequence.xml 임포트 → 검수 → 출력
```

## 환경

- macOS (Apple Silicon M2 Pro) — PyTorch는 mps 디바이스 사용 가능
- Python 3.11+, ffmpeg 설치됨
- 편집 소프트웨어: Adobe Premiere Pro (XML 임포트 대상)
- 시드 코드: `slide_marker.py` (슬라이드 전환 감지, PySceneDetect 기반) — 이미 작성되어 있으며 Phase 1에 통합한다

## 폴더 구조

```
lecture-pipeline/
├── config.yaml              # 공통 설정 (아래 참조)
├── assets/                  # 디자인 자산 (전 편 공통, 사람이 제작)
│   ├── bg_card.png          # 1920x1080 배경 카드 (프레임 안은 투명/비움)
│   ├── intro.png            # 인트로 타이틀 카드 (편별 버전은 intro_ep01.png)
│   └── outro.png
├── episodes/
│   ├── ep01/
│   │   ├── source.mp4       # 원본 강의 영상 (입력)
│   │   ├── slides.pdf       # 강의자료 원본 (입력, PPT면 PDF로 변환해서 넣음)
│   │   └── output/          # 파이프라인 산출물 (자동 생성)
│   │       ├── cuts.csv
│   │       ├── scene_frames/
│   │       ├── slides_png/
│   │       ├── match.csv
│   │       ├── speaker_alpha.mov
│   │       └── sequence.xml
│   └── ep02/ ...
└── pipeline/                # 소스 코드
```

## config.yaml 설계

레이아웃 좌표를 코드에 하드코딩하지 말고 전부 여기서 관리한다. 배경 카드 디자인이 확정되면 좌표만 갱신하면 13편 전체에 반영되도록.

```yaml
canvas: { width: 1920, height: 1080 }

detect:                      # Phase 1 파라미터
  threshold: 20
  min_scene_sec: 3
  analyze_width: 0.65        # 왼쪽부터 이 비율만 분석 (강연자 제외)
  speaker_side: right

layout:
  slide_frame:               # 슬라이드가 들어갈 프레임 내부 영역 (px)
    x: 106, y: 162, width: 1170, height: 713
  speaker:                   # 강연자 알파 영상 배치
    anchor: bottom-right
    x: 1560, y: 1080         # 앵커 기준점
    scale: 0.62              # 원본 대비
  intro_sec: 5
  outro_sec: 5
```

## 파이프라인 단계 (Phase)

각 Phase는 독립 실행 가능해야 한다: `python pipeline.py ep01 --step match` 처럼. 이미 산출물이 있으면 스킵하고, `--force` 로 재실행. 13편 일괄은 `python pipeline.py all`.

### Phase 1 — 슬라이드 전환 감지
- 시드 코드 `slide_marker.py` 의 로직을 모듈로 통합
- 입력: source.mp4 / 출력: `cuts.csv` (scene, start_tc, start_sec, start_frame), `scene_frames/scene_001.png` (각 씬 대표 프레임, 전환 후 1초 지점)
- 수용 기준: ep01에서 감지된 씬 수가 실제 슬라이드 전환 수와 ±2 이내

### Phase 2 — 강의자료 PNG 추출
- 입력: slides.pdf / 출력: `slides_png/page_001.png` ...
- pdftoppm 또는 pymupdf 사용, 슬라이드 프레임 영역보다 큰 해상도로 (최소 1600px 폭)
- 수용 기준: 페이지 수 일치, 이미지 깨짐 없음

### Phase 3 — 대표 프레임 ↔ 슬라이드 매칭
- scene_frames와 slides_png를 perceptual hash(imagehash 라이브러리, phash)로 매칭
- **중요**: 영상 쪽 프레임은 비교 전에 강연자 영역을 잘라낸다 (config의 analyze_width 재사용). 슬라이드 쪽도 동일 비율로 잘라 비교해 조건을 맞춘다
- 강의는 대체로 순차 진행이므로 단조 증가 제약을 우선 적용하되, 강사가 이전 슬라이드로 돌아가는 역행도 허용. 역행이 감지되면 플래그
- 출력: `match.csv` (scene, start_tc, matched_page, confidence, flag)
- confidence 하위 항목은 flag=review 로 표시해 사람이 확인할 목록을 최소화
- 수용 기준: ep01 기준 자동 매칭 정확도 90% 이상, 나머지는 review 플래그로 잡힘

### Phase 4 — 강연자 누끼 (알파 추출)
- RobustVideoMatting (PeterL1n/RobustVideoMatting) 사용, device=mps
- 입력: source.mp4 / 출력: `speaker_alpha.mov`
- **알파 채널 필수**: RVM 출력(fgr+pha)을 ffmpeg로 ProRes 4444 (`-c:v prores_ks -profile:v 4444 -pix_fmt yuva444p10le`) 인코딩
- 처리 전에 원본에서 강연자 쪽 영역만 크롭해서 매팅하면 속도와 품질 모두 유리 (강연자가 화면 오른쪽 ~40%에 고정)
- 수용 기준: 30초 샘플에서 엣지가 육안 검수 통과 수준, 알파가 프리미어에서 정상 인식

### Phase 5 — 프리미어 XML 시퀀스 생성
- xmeml (FCP7 XML) 포맷으로 `sequence.xml` 생성. 프리미어 File > Import로 열림
- 시퀀스 스펙: 1920x1080, fps는 source.mp4에서 읽어서 동일하게
- 트랙 구성:
  - V1: assets/bg_card.png — 본편 전체 길이로 1클립
  - V2: match.csv 순서대로 slides_png 이미지들을 각 씬 구간에 배치. layout.slide_frame에 맞는 scale/position 값을 모션 파라미터로 기입
  - V3: speaker_alpha.mov — layout.speaker의 scale/position 적용, 본편 전체
  - A1: source.mp4의 오디오
  - 인트로/아웃트로: assets에 파일이 있으면 본편 앞뒤에 배치하고 본편 전체를 그만큼 뒤로 시프트
- pathurl은 file:// 절대경로
- fps가 29.97 등 드롭프레임 계열이면 타임코드 변환에 주의 (프레임 번호 기준으로 계산하면 안전)
- 수용 기준: 프리미어에서 임포트 시 오류 없이 열리고, 모든 클립이 올바른 시간/크기/위치로 배치되어 재생됨

### Phase 6 (옵션) — 챕터 배지
- 편별로 `chapters.csv` (start_tc, badge_png)가 있으면 V4 트랙에 배지 PNG 배치. 없으면 스킵

## 개발 진행 방식 (중요)

한 번에 전부 만들지 말 것. Phase 순서대로 구현하고, 각 Phase가 끝날 때마다 ep01 실데이터로 검증을 요청한다. 특히 Phase 5(XML)는 최소 구성(V1+V2만)으로 먼저 프리미어 임포트를 검증한 뒤 트랙을 추가한다. Phase 4(RVM)는 모델 다운로드와 mps 호환 이슈가 있을 수 있으니 30초 샘플로 먼저 확인한다.

## 알려진 리스크

- 원본 영상 속 강연자와 배경(슬라이드)의 색이 비슷한 구간은 매팅 엣지가 거칠 수 있음 → 최종 레이아웃에서 강연자가 단색 존 위에 놓이므로 어느 정도 허용, 심하면 Phase 4에서 후처리(엣지 수축/블러) 옵션 추가
- 슬라이드 내 빌드 애니메이션(한 슬라이드에서 요소가 추가되는 경우)은 전환으로 오탐될 수 있음 → threshold 튜닝 + match.csv에서 같은 페이지 연속 매칭은 자동 병합
- 13편의 원본 레이아웃(강연자 위치·크기)이 편마다 다를 수 있음 → detect/speaker 설정을 편별 오버라이드 가능하게 (episodes/epNN/config.yaml이 있으면 공통 설정을 덮어씀)
