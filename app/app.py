#!/usr/bin/env python3
"""강의 영상 리디자인 파이프라인 — 데스크톱 앱 (pywebview).

파이프라인 자체는 CLI(pipeline.py)를 서브프로세스로 실행하고 로그를 실시간
스트리밍한다. 프론트는 app/ui.html 단일 파일. 마지막 내보내기 위치 등은
app/settings.json에 저장.
"""

import base64
import csv
import json
import re
import shutil
import signal
import subprocess
import sys
import threading
import os
from pathlib import Path

import webview

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
SETTINGS = Path(__file__).parent / "settings.json"

# Finder로 실행하면 PATH에 /opt/homebrew/bin이 없어 ffmpeg/ffprobe를 못 찾는다
if "/opt/homebrew/bin" not in os.environ.get("PATH", ""):
    os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

STEP_OUTPUTS = {
    "detect": "cuts.csv",
    "slides": "slides_png",
    "match": "match.csv",
    "matte": "speaker_alpha.mov",
    "xml": "sequence.xml",
}

MATTE_WS = "_matte"  # 누끼 전용 작업 폴더 (에피소드 목록에서 숨김)


def _load_settings() -> dict:
    try:
        d = json.loads(SETTINGS.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_settings(d: dict) -> None:
    SETTINGS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


class Api:
    def __init__(self):
        self.window: webview.Window | None = None
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 내부 유틸

    def _push(self, kind: str, data) -> None:
        payload = json.dumps({"kind": kind, "data": data}, ensure_ascii=False)
        try:
            self.window.evaluate_js(f"window.pipelineEvent({payload})")
        except Exception:
            pass  # 창이 닫힌 뒤 도착한 이벤트

    # ------------------------------------------------------------ 파일 선택

    def pick_video(self):
        r = self.window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("동영상 (*.mp4;*.mov;*.m4v)", "모든 파일 (*.*)"),
        )
        return r[0] if r else None

    def pick_pdf(self):
        r = self.window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("PDF (*.pdf)", "모든 파일 (*.*)"),
        )
        return r[0] if r else None

    def pick_videos(self):
        """여러 영상 선택 (전환 마커 배치용)."""
        r = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("동영상 (*.mp4;*.mov;*.m4v)", "모든 파일 (*.*)"),
        )
        return list(r) if r else []

    # ------------------------------------------------------------ 에피소드

    def list_episodes(self):
        eps = []
        ep_root = ROOT / "episodes"
        if not ep_root.is_dir():
            return eps
        for d in sorted(ep_root.iterdir()):
            # _로 시작하는 폴더는 예약 작업 공간(누끼 전용 등) — 목록에서 숨김
            if not d.is_dir() or d.name.startswith("_") or not (d / "source.mp4").exists():
                continue
            out = d / "output"
            status = {step: (out / f).exists() for step, f in STEP_OUTPUTS.items()}
            review = backward = 0
            if status["match"]:
                with open(out / "match.csv", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        flag = row.get("flag") or ""  # 잘린/구버전 CSV 방어
                        review += "review" in flag
                        backward += "backward" in flag
            eps.append({
                "name": d.name,
                "status": status,
                "review": review,
                "backward": backward,
                "has_pdf": (d / "slides.pdf").exists(),
                "has_chapters": (d / "chapters.csv").exists(),
            })
        return eps

    def create_episode(self, name: str, video: str, pdf: str):
        name = name.strip()
        if not re.fullmatch(r"[\w가-힣][\w가-힣.-]*", name):
            return {"error": "에피소드 이름은 한글/영문/숫자/-_. 만 사용할 수 있어요"}
        if name.casefold() == "sample" or name.startswith("_"):
            return {"error": "sample과 _로 시작하는 이름은 예약되어 있어요"}
        # macOS 기본 볼륨은 대소문자 무구분 — 다른 표기의 기존 에피소드에 겹쳐 쓰지 않게
        ep_root = ROOT / "episodes"
        if ep_root.is_dir():
            for d in ep_root.iterdir():
                if d.is_dir() and d.name.casefold() == name.casefold() and d.name != name:
                    return {"error": f"대소문자만 다른 에피소드가 이미 있어요: {d.name}"}
        vp, pp = Path(video), Path(pdf)
        if not vp.is_file():
            return {"error": f"영상 파일을 찾을 수 없어요: {video}"}
        if not pp.is_file():
            return {"error": f"PDF 파일을 찾을 수 없어요: {pdf}"}
        if vp.resolve() == pp.resolve():
            return {"error": "영상과 PDF에 같은 파일이 선택됐어요"}
        if vp.suffix.lower() not in (".mp4", ".mov", ".m4v"):
            return {"error": f"영상 파일이 아니에요: {vp.name}"}
        if pp.suffix.lower() != ".pdf":
            return {"error": f"PDF 파일이 아니에요: {pp.name}"}

        ep = ep_root / name
        pairs = ((ep / "source.mp4", vp), (ep / "slides.pdf", pp))
        # 검증을 전부 끝낸 뒤에만 변이 — 중간 에러로 반쪽 상태가 남지 않게
        for dst, _ in pairs:
            if dst.exists() and not dst.is_symlink():
                return {"error": f"{dst.name}이 이미 실제 파일로 존재해요 — Finder에서 직접 정리 후 다시 시도"}
        ep.mkdir(parents=True, exist_ok=True)
        # 원본은 복사하지 않고 심링크 — 수 GB 영상 복사 방지
        changed = False
        for dst, src in pairs:
            target = src.resolve()
            if dst.is_symlink():
                if dst.resolve() != target:
                    changed = True
                dst.unlink()
            dst.symlink_to(target)
        # 다른 원본으로 교체됐으면 이전 산출물은 무효 — mtime 신선도 검사가
        # 새 원본이 더 오래된 파일일 때 스킵으로 오판하지 않도록 정리한다
        out = ep / "output"
        if changed and out.is_dir():
            shutil.rmtree(out)
            return {"ok": True, "name": name, "note": "원본이 바뀌어 이전 산출물을 정리했어요"}
        return {"ok": True, "name": name}

    # ------------------------------------------------------------ 실행

    def run_pipeline(self, episode: str, step: str | None, force: bool, sample_sec: float | None):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return {"error": "이미 실행 중이에요"}
            cmd = [str(PY), "-u", str(ROOT / "pipeline.py"), episode]
            if step:
                cmd += ["--step", step]
            if force:
                cmd += ["--force"]
            if sample_sec:
                cmd += ["--sample-sec", str(sample_sec)]
            # start_new_session: 취소 시 ffmpeg 등 손자 프로세스까지 그룹으로 종료
            proc = subprocess.Popen(
                cmd, cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=True,
            )
            self.proc = proc
            threading.Thread(target=self._stream, args=(proc,), daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------ 전환 마커
    #
    # PPT 전환(즉시 컷) 프레임을 감지해 프리미어 마커 XML을 만든다.
    # 산출물은 각 원본 옆: <name>_markers.xml / <name>_markers.csv / (VFR이면) <name>_cfr.mp4

    def run_markers(self, videos: list, full_frame: bool, side: str, threshold):
        if not videos:
            return {"error": "영상을 먼저 선택해 주세요"}
        for v in videos:
            p = Path(v)
            if not p.is_file():
                return {"error": f"영상 파일을 찾을 수 없어요: {v}"}
            if p.suffix.lower() not in (".mp4", ".mov", ".m4v"):
                return {"error": f"영상 파일이 아니에요: {p.name}"}
        if side not in ("right", "left"):
            return {"error": "강연자 위치 값이 올바르지 않아요"}
        try:
            th = float(threshold)
        except (TypeError, ValueError):
            return {"error": "감지 민감도가 숫자가 아니에요"}
        if not 0.5 <= th <= 100:
            return {"error": "감지 민감도는 0.5~100 사이여야 해요"}

        with self._lock:
            if self.proc and self.proc.poll() is None:
                return {"error": "이미 실행 중이에요"}
            cmd = [str(PY), "-u", "-m", "pipeline.markers", *videos, "--threshold", str(th)]
            if full_frame:
                cmd += ["--full-frame"]
            else:
                cmd += ["--side", side]
            proc = subprocess.Popen(
                cmd, cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=True,
            )
            self.proc = proc
            threading.Thread(target=self._stream, args=(proc,), daemon=True).start()
        return {"ok": True}

    def markers_results(self, videos: list):
        """실행 후 결과 요약 — 영상별 전환 수·산출물 경로."""
        out = []
        for v in videos:
            src = Path(v)
            csv_p = src.parent / f"{src.stem}_markers.csv"
            xml_p = src.parent / f"{src.stem}_markers.xml"
            cfr_p = src.parent / f"{src.stem}_cfr.mp4"
            count = None
            if csv_p.exists():
                with open(csv_p, encoding="utf-8") as f:
                    count = max(0, sum(1 for _ in f) - 1)  # 헤더 제외
            out.append({
                "video": src.name,
                "count": count,
                "xml": str(xml_p) if xml_p.exists() else None,
                "cfr": str(cfr_p) if cfr_p.exists() else None,
            })
        return out

    def marker_rows(self, video: str):
        """검수용 — 해당 영상의 마커 CSV 행."""
        src = Path(video)
        csv_p = src.parent / f"{src.stem}_markers.csv"
        if not csv_p.exists():
            return []
        with open(csv_p, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def reveal_path(self, path: str):
        p = Path(path)
        if not p.exists():
            return {"error": "파일이 없어요"}
        subprocess.run(["open", "-R", str(p)])
        return {"ok": True}

    # ------------------------------------------------------------ 누끼 전용
    #
    # 통합 파이프라인과 무관하게 영상 하나만 매팅한다. 임의의 영상을 예약 작업
    # 폴더(episodes/_matte)에 스테이징하고 matte 단계만 돌린다 — matte는 detect/
    # match 산출물에 의존하지 않으므로 기존 파이프라인을 그대로 재사용할 수 있다.

    def run_matte(self, video: str, side: str, model: str, crop_frac,
                  sample_sec: float | None, force: bool):
        vp = Path(video)
        if not vp.is_file():
            return {"error": f"영상 파일을 찾을 수 없어요: {video}"}
        if vp.suffix.lower() not in (".mp4", ".mov", ".m4v"):
            return {"error": f"영상 파일이 아니에요: {vp.name}"}
        if side not in ("right", "left", "center"):
            return {"error": "강연자 위치 값이 올바르지 않아요"}
        if model not in ("matanyone2", "mobilenetv3", "resnet50"):
            return {"error": "모델 값이 올바르지 않아요"}
        try:
            cf = float(crop_frac)
        except (TypeError, ValueError):
            return {"error": "크롭 비율이 숫자가 아니에요"}
        if not 0.1 <= cf <= 1.0:
            return {"error": "크롭 비율은 0.1~1.0 사이여야 해요"}

        with self._lock:
            if self.proc and self.proc.poll() is None:
                return {"error": "이미 실행 중이에요"}

        ep = ROOT / "episodes" / MATTE_WS
        ep.mkdir(parents=True, exist_ok=True)
        src = ep / "source.mp4"
        if src.exists() and not src.is_symlink():
            return {"error": "episodes/_matte/source.mp4가 실제 파일로 존재해요 — Finder에서 정리 후 다시 시도"}

        # 원본은 심링크(수 GB 복사 방지). 소스나 옵션이 바뀌면 이전 산출물을
        # 무효화한다 — mtime 프레시 검사가 스킵으로 오판하지 않도록.
        changed = False
        target = vp.resolve()
        if src.is_symlink():
            if src.resolve() != target:
                changed = True
            src.unlink()
        src.symlink_to(target)

        cfg_text = (
            "# 누끼 전용 UI가 자동 생성 — 실행마다 덮어씀\n"
            "detect:\n"
            f"  speaker_side: {side}\n"
            "matte:\n"
            f"  model: {model}\n"
            f"  crop_frac: {cf}\n"
        )
        cfg_path = ep / "config.yaml"
        if not cfg_path.exists() or cfg_path.read_text(encoding="utf-8") != cfg_text:
            cfg_path.write_text(cfg_text, encoding="utf-8")
            changed = True

        out = ep / "output"
        if changed and out.is_dir():
            shutil.rmtree(out)

        return self.run_pipeline(MATTE_WS, "matte", force, sample_sec)

    def export_matte(self, sample: bool = False):
        name = "speaker_alpha_sample.mov" if sample else "speaker_alpha.mov"
        src = ROOT / "episodes" / MATTE_WS / "output" / name
        if not src.exists():
            return {"error": f"{name}이 아직 없어요 — 먼저 실행하세요"}
        s = _load_settings()
        r = self.window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=s.get("export_dir", str(Path.home() / "Desktop")),
            save_filename=name,
        )
        if not r:
            return {"cancelled": True}
        dest = Path(r if isinstance(r, str) else r[0])
        shutil.copy2(src, dest)
        s["export_dir"] = str(dest.parent)
        _save_settings(s)
        return {"ok": True, "path": str(dest)}

    def _stream(self, proc: subprocess.Popen) -> None:
        """서브프로세스 출력을 \\n과 \\r(tqdm 진행바) 둘 다 기준으로 잘라 UI로 전달."""
        buf = b""
        while True:
            chunk = proc.stdout.read1(256)  # read()는 256바이트 찰 때까지 블록 — 즉시 반환형 사용
            if not chunk:
                break
            buf += chunk
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                line, sep = buf[: m.start()], buf[m.start() : m.start() + 1]
                buf = buf[m.end() :]
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self._push("progress" if sep == b"\r" else "log", text)
        if buf.strip():
            self._push("log", buf.decode("utf-8", errors="replace").strip())
        code = proc.wait()
        self._push("done", {"code": code})

    def cancel(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                try:  # 파이프라인 + ffmpeg 등 프로세스 그룹 전체 종료
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                return {"ok": True}
        return {"error": "실행 중인 작업이 없어요"}

    # ------------------------------------------------------------ 결과

    def match_rows(self, episode: str):
        p = ROOT / "episodes" / episode / "output" / "match.csv"
        if not p.exists():
            return []
        with open(p, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def preview(self, episode: str, sec: float):
        r = subprocess.run(
            [str(PY), str(ROOT / "tools" / "preview_frame.py"), episode, str(sec)],
            cwd=ROOT, capture_output=True, text=True,
        )
        png = ROOT / "episodes" / episode / "output" / f"preview_{sec:g}s.png"
        if r.returncode != 0 or not png.exists():
            return {"error": (r.stderr or r.stdout).strip()[-400:] or "미리보기 파일이 생성되지 않았어요"}
        return {"image": base64.b64encode(png.read_bytes()).decode()}

    def export_xml(self, episode: str):
        src = ROOT / "episodes" / episode / "output" / "sequence.xml"
        if not src.exists():
            return {"error": "sequence.xml이 아직 없어요 — 먼저 실행하세요"}
        s = _load_settings()
        r = self.window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=s.get("export_dir", str(Path.home() / "Desktop")),
            save_filename=f"{episode}_sequence.xml",
        )
        if not r:
            return {"cancelled": True}
        dest = Path(r if isinstance(r, str) else r[0])
        shutil.copy2(src, dest)
        s["export_dir"] = str(dest.parent)
        _save_settings(s)
        return {"ok": True, "path": str(dest)}

    def reveal(self, episode: str, what: str = "output"):
        ep = ROOT / "episodes" / episode
        target = ep / "output" if what == "output" else ep / "output" / what
        if not target.exists():
            target = ep
        if target.is_file():
            subprocess.run(["open", "-R", str(target)])  # Finder에서 해당 파일 선택
        else:
            subprocess.run(["open", str(target)])
        return {"ok": True}

    def open_file(self, episode: str, filename: str):
        p = ROOT / "episodes" / episode / "output" / filename
        if not p.exists():
            return {"error": f"{filename} 없음"}
        subprocess.run(["open", str(p)])
        return {"ok": True}


def main() -> None:
    api = Api()
    window = webview.create_window(
        "강의 파이프라인",
        url=str(Path(__file__).parent / "ui.html"),
        js_api=api,
        width=1180, height=820, min_size=(980, 680),
    )
    api.window = window

    def on_closed():
        # 창을 닫으면 실행 중인 파이프라인도 함께 종료 (고아 프로세스 방지).
        # pywebview 이벤트 핸들러는 반환값이 있으면 안 됨 (내부에서 set에 add 시도)
        api.cancel()

    window.events.closed += on_closed

    def smoke():
        import time
        time.sleep(float(os.environ["LP_SMOKE"]))
        window.destroy()

    if os.environ.get("LP_SMOKE"):  # 자동 종료 스모크 테스트 (값 = 유지 시간 초)
        threading.Thread(target=smoke, daemon=True).start()

    def selftest():
        import time
        time.sleep(5)  # pywebviewready + refreshEpisodes 대기
        checks = {
            "에피소드 목록": "document.querySelectorAll('#epList .ep').length",
            "선택된 에피소드": "document.querySelector('#epTitle')?.textContent",
            "단계 칩": "document.querySelectorAll('#stepChips .chip.done').length",
            "매칭 행": "document.querySelectorAll('#matchBody tr').length",
            "review/backward 행": "document.querySelectorAll('#matchBody tr.review, #matchBody tr.backward').length",
            "실행 버튼 활성": "!document.querySelector('#runAll').disabled",
        }
        for label, expr in checks.items():
            print(f"[selftest] {label}: {window.evaluate_js(expr)}", flush=True)
        if os.environ.get("LP_SELFTEST") == "run":
            # 실제 실행 경로: XML 재생성 버튼 클릭 → 로그 스트림 → 완료 이벤트
            window.evaluate_js("document.querySelector('#force').checked = true")
            window.evaluate_js("document.querySelector('#runXml').click()")
            for _ in range(120):
                time.sleep(0.5)
                log = window.evaluate_js("document.querySelector('#log').textContent") or ""
                if "작업 완료" in log or "종료 코드" in log:
                    break
            enabled = window.evaluate_js("!document.querySelector('#runAll').disabled")
            print(f"[selftest] 실행 로그 tail: …{log[-160:]}", flush=True)
            print(f"[selftest] 실행 후 버튼 활성: {enabled}", flush=True)
        window.destroy()

    if os.environ.get("LP_SELFTEST"):
        threading.Thread(target=selftest, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
