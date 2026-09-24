"""
chatterbox_engine.py — Chatterbox Multilingual one-stage Finnish cloning TTS.

Chatterbox (Resemble AI, MIT) generates Finnish speech directly in a target
voice from ~10s of reference audio — no edge-tts base voice, no separate
conversion stage, no per-character training. Ear-tested 2026-07-06 against
the shipped edge-tts+RVC pipeline on Varré and Godrick: clearly better, so
it is the default voice engine whenever its toolchain is present.

Its dependency chain (torch 2.6, transformers 5.x, its own model stack)
lives in an isolated Python 3.11 venv — same convention as rvc_engine.py:
an externally installed toolchain (see tools/CHATTERBOX_SETUP.md),
discovered by path search, invoked via subprocess. The driver script
(rvc_scripts/er_chatterbox.py) processes a whole manifest per invocation so
the ~15s model load is paid once per batch, not once per line.
"""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_SCRIPTS_DIR = Path(__file__).parent / "rvc_scripts"

SETUP_DOC = "tools/CHATTERBOX_SETUP.md"


def _search_dirs() -> list[Path]:
    return [
        Path(sys.executable).parent / "tools",
        Path.home() / "tools",
        Path("C:/tools"),
        Path.cwd(),
        Path.cwd() / "tools",
    ]


def _find_chatterbox_venv(explicit: str | Path | None = None) -> Path | None:
    """Return the venv root containing Scripts/python.exe + chatterbox, or None."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for base in _search_dirs():
        candidates.append(base / "chatterbox_venv")
    for p in candidates:
        python = p / "Scripts" / "python.exe"
        if python.exists() and (p / "Lib" / "site-packages" / "chatterbox").is_dir():
            return p
    return None


# One job for generate_batch: (text, reference/prompt wav, output wav).
ChatterboxJob = tuple[str, Path, Path]


class ChatterboxEngine:

    def __init__(self, venv_path: str | Path | None = None):
        self._venv = _find_chatterbox_venv(venv_path)
        self.last_error = ""
        self.restarts = 0  # driver relaunches during the last generate_batch

    @property
    def available(self) -> bool:
        return self._venv is not None

    @property
    def _python(self) -> Path:
        return self._venv / "Scripts" / "python.exe"

    def generate_batch(
        self,
        jobs: list[ChatterboxJob],
        language: str = "fi",
        exaggeration: float = 0.5,
        on_progress: "Callable[[int, int], None] | None" = None,
        should_stop: "Callable[[], bool] | None" = None,
        max_restarts: int = 40,
    ) -> dict[Path, bool]:
        """
        Generate speech for every (text, prompt_wav, output_wav) job.
        Returns {output_wav: success}; per-line failures don't abort the
        batch. A CUDA device-side assert poisons the whole driver process
        (every later kernel call fails), so when the driver dies the batch
        marks the crashing job failed and RESTARTS the driver on the
        remaining jobs — one bad line costs one model reload, not the rest
        of the run (observed for real: 1 poisoned line failed 6398). On a
        cancelled run the missing entries are simply absent/False.
        """
        self.last_error = ""
        self.restarts = 0
        if not jobs:
            return {}
        if not self.available:
            self.last_error = f"Chatterbox toolchain not found (see {SETUP_DOC})."
            return {out: False for _, _, out in jobs}

        results: dict[Path, bool] = {out: False for _, _, out in jobs}
        total = len(jobs)
        remaining = list(jobs)
        done_count = 0
        first_fail = ""
        cancelled = False
        # T3 sampling is stochastic — a line that crashed the driver often
        # succeeds on a second try in a fresh process. One retry per line.
        crash_counts: dict[Path, int] = {}

        while remaining and not cancelled:
            resolved_this_run = 0
            with tempfile.TemporaryDirectory(prefix="er_chatterbox_") as tmp_dir:
                manifest_path = Path(tmp_dir) / "manifest.json"
                manifest_path.write_text(json.dumps({
                    "language": language,
                    "jobs": [{"text": t, "prompt": str(p), "out": str(o),
                              "exaggeration": exaggeration}
                             for t, p, o in remaining],
                }, ensure_ascii=False), encoding="utf-8")
                cmd = [str(self._python), str(_SCRIPTS_DIR / "er_chatterbox.py"),
                       str(manifest_path)]
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=_NO_WINDOW,
                )
                tail: list[str] = []
                resolved_outs: set[Path] = set()  # RESULT arrived (ok OR fail)
                fatal = False
                saw_model = False
                try:
                    for line in proc.stdout:  # type: ignore[union-attr]
                        line = line.rstrip()
                        tail.append(line)
                        del tail[:-20]
                        if line.startswith("MODEL_LOADED|"):
                            saw_model = True
                        elif line.startswith("RESULT|"):
                            parts = line.split("|", 3)
                            out_path = Path(parts[2])
                            ok = parts[1] == "ok"
                            err = parts[3] if len(parts) > 3 else ""
                            if (not ok and ("CUDA" in err or "device-side" in err)
                                    and crash_counts.get(out_path, 0) < 1):
                                # first CUDA crash on this line: leave it
                                # unsettled so the next driver run retries it
                                crash_counts[out_path] = 1
                                continue
                            results[out_path] = ok
                            resolved_outs.add(out_path)
                            resolved_this_run += 1
                            done_count += 1
                            if not ok and not first_fail and err:
                                first_fail = err
                            if on_progress:
                                on_progress(done_count, total)
                        elif line.startswith("FATAL|"):
                            fatal = True
                        if should_stop and should_stop():
                            proc.kill()
                            self.last_error = "cancelled"
                            cancelled = True
                            break
                    proc.wait()
                finally:
                    if proc.poll() is None:
                        proc.kill()

                if cancelled:
                    break
                crashed = fatal or proc.returncode != 0
                if not crashed and resolved_this_run < len(remaining):
                    crashed = True  # exited "cleanly" but didn't finish
                if not crashed:
                    break

                # Driver died. Everything that got a settled RESULT is done;
                # the first unresolved job is the crash victim (unless the
                # driver already RESULT|fail'ed it pre-FATAL, in which case
                # it's either settled or queued for its one retry).
                remaining = [j for j in remaining if j[2] not in resolved_outs]
                if not saw_model:
                    # The model never even loaded — systemic (broken venv,
                    # OOM, missing model files): restarting won't help.
                    self.last_error = "\n".join(tail) or "er_chatterbox.py crashed at startup"
                    break
                if remaining and not fatal:
                    # Hard crash without a RESULT — the first remaining job
                    # is the victim: give it one retry, then mark it failed.
                    victim_out = remaining[0][2]
                    if crash_counts.get(victim_out, 0) < 1:
                        crash_counts[victim_out] = 1
                    else:
                        remaining.pop(0)
                        results[victim_out] = False
                        done_count += 1
                        if not first_fail:
                            first_fail = "driver crashed on this line (twice)"
                        if on_progress:
                            on_progress(done_count, total)
                self.restarts += 1
                if self.restarts > max_restarts:
                    self.last_error = (f"driver crashed {self.restarts} times — "
                                       f"giving up; last output: " + "\n".join(tail[-5:]))
                    break

        if first_fail and not self.last_error:
            self.last_error = first_fail

        # Trust the filesystem over the protocol: a reported "ok" with no
        # file on disk is a failure.
        for out, ok in results.items():
            if ok and not out.exists():
                results[out] = False
        return results
