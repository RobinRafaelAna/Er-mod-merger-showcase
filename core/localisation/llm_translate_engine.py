"""
llm_translate_engine.py — "Smart MT": local LLM English→Finnish translation.

A small instruction-tuned LLM (Gemma 3 12B, Q4 GGUF) run through llama.cpp's
llama-server beats the argos NMT pipeline clearly on real Convergence lines
(tested 2026-07-07): it translates font-tag content argos leaves English,
keeps negations/structure right, applies the user glossary with proper
Finnish inflection, and — with the server kept warm — is not meaningfully
slower (~0.4s/line on an RTX 4070).

Same external-toolchain convention as rvc_engine/chatterbox_engine: the
llama.cpp CUDA build lives in tools/llama_cpp and models in tools/llm_models
(see tools/LLM_MT_SETUP.md), discovered by path search, driven over
llama-server's OpenAI-compatible HTTP API. argos remains the fallback when
the toolchain is missing or a single line fails.

Prompting lessons baked in (learned the hard way — both tested models
pattern-continued into runaway spam otherwise):
- keep the system prompt short; NEVER inline the whole glossary,
- per line, include only the glossary terms that actually occur in it,
- one few-shot pair teaches placeholder handling + terse output.
"""
from __future__ import annotations
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

SETUP_DOC = "tools/LLM_MT_SETUP.md"

# Preferred models first; any other *.gguf in tools/llm_models works too.
_PREFERRED_MODELS = ("gemma-3-12b-it-Q4_K_M.gguf",)

_SYSTEM_PROMPT = """You are a professional game localizer translating Elden Ring mod text from English to Finnish.

Rules:
- Output ONLY the Finnish translation, nothing else — no quotes, no notes, no extra lines.
- Preserve structure exactly: same number of lines and same placement of line breaks.
- Keep placeholders like <?itemName?> and tags like <font ...>...</font> EXACTLY unchanged; translate only human-readable text.
- Translate parenthetical content as well.
- Style: solemn, slightly archaic Finnish fitting a dark fantasy game.
- Proper names keep their spelling but may take natural Finnish case endings.
- If the message lists glossary terms, use EXACTLY those Finnish equivalents, inflected naturally."""

_FEWSHOT = [
    {"role": "user", "content":
        "Translate to Finnish:\nRead <?codenameIcon?><?codenamePCName?>'s message"},
    {"role": "assistant", "content":
        "Lue <?codenameIcon?><?codenamePCName?>:n viesti"},
]

# Item/place NAMES need the opposite instinct from prose: the prose prompt
# tells the model to keep proper names, which makes descriptive item names
# ("Fulgurbloom", "Serpent-Hunter") pass through untranslated. This prompt
# localizes the descriptive parts while still keeping true person names.
# Verified 2026-07-09: Fulgurbloom→Salamankukka, Serpent-Hunter→Käärmejahti,
# "Stormhawk Deenh +9"→"Myrskyhaukan Deenh +9", Melina→Melina (kept).
_NAME_SYSTEM_PROMPT = """You are localizing Elden Ring item and place NAMES from English to Finnish.

Rules:
- Output ONLY the Finnish name, nothing else — no quotes, no notes.
- Translate descriptive elements into natural Finnish ("Fulgurbloom" → "Salamankukka"; "Stormhawk" → "Myrskyhaukka"; "Serpent-Hunter" → "Käärmejahti").
- Keep a genuine person's name (Melina, Ranni, Deenh, Radagon) but you MAY attach it to a translated descriptor and inflect it naturally.
- Keep real-world loanwords that have no Finnish form (Kukri) as-is.
- Keep trailing upgrade markers like "+3" and any <?...?> placeholders / <font> tags EXACTLY unchanged.
- Style: solemn, dark-fantasy Finnish.
- If the message lists glossary terms, use EXACTLY those Finnish equivalents, inflected naturally."""

_NAME_FEWSHOT = [
    {"role": "user", "content": "Localize this name to Finnish:\nSerpent-Hunter Greatspear +2"},
    {"role": "assistant", "content": "Käärmenmetsästäjän suurikeihäs +2"},
]

_TAG_RE = re.compile(r"<\?[^?>]*\?>|<[^<>]+>")


def _search_dirs() -> list[Path]:
    return [
        Path(sys.executable).parent / "tools",
        Path.home() / "tools",
        Path("C:/tools"),
        Path.cwd(),
        Path.cwd() / "tools",
    ]


def _find_llama_server(explicit: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for base in _search_dirs():
        candidates.append(base / "llama_cpp" / "llama-server.exe")
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_model(explicit: str | Path | None = None) -> Path | None:
    if explicit and Path(explicit).exists():
        return Path(explicit)
    for base in _search_dirs():
        model_dir = base / "llm_models"
        if not model_dir.is_dir():
            continue
        for name in _PREFERRED_MODELS:
            if (model_dir / name).exists():
                return model_dir / name
        ggufs = sorted(model_dir.glob("*.gguf"))
        if ggufs:
            return ggufs[0]
    return None


_REPEAT_RE = re.compile(r"(?<!\w)(\w{2,})((?:\s+\1){3,})(?!\w)", re.IGNORECASE | re.UNICODE)


def has_runaway_repetition(source: str, translated: str) -> bool:
    """Detect LLM repetition collapse: the same word 4+ times in a row, or
    output ballooning far past the source length (pattern-continuation to
    max_tokens). Shipped example: a caption ending in 'suvun' repeated ~300
    times. Legit game text never does either."""
    if _REPEAT_RE.search(translated):
        return True
    return len(translated) > 4 * len(source) + 80


def tags_preserved(source: str, translated: str) -> bool:
    """Every <?placeholder?> / <tag> from the source must survive verbatim —
    a translation that mangled one would break the game text, so such lines
    are handed to the argos fallback instead."""
    for tag in _TAG_RE.findall(source):
        if tag not in translated:
            return False
    return True


class LLMTranslateEngine:

    def __init__(self, server_path: str | Path | None = None,
                 model_path: str | Path | None = None):
        self._server = _find_llama_server(server_path)
        self._model = _find_model(model_path)
        self._proc: subprocess.Popen | None = None
        self._port: int | None = None
        self.last_error = ""

    @property
    def available(self) -> bool:
        return self._server is not None and self._model is not None

    @property
    def model_name(self) -> str:
        return self._model.stem if self._model else ""

    # ------------------------------------------------------------------ #
    # Server lifecycle — start once, keep warm for the whole batch
    # ------------------------------------------------------------------ #

    def start(self, timeout: float = 240.0) -> bool:
        """Launch llama-server and wait until the model is loaded."""
        if self._proc is not None and self._proc.poll() is None:
            return True
        if not self.available:
            self.last_error = f"LLM toolchain not found (see {SETUP_DOC})."
            return False
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self._port = s.getsockname()[1]
        # llama-server's own output used to go to DEVNULL, which made a mid-run death
        # impossible to diagnose: the process just vanished and left nothing to read. A CUDA
        # out-of-memory, a driver reset or a bad request announces itself here and NOWHERE
        # else — not in the Windows event log, and not in the app. A 12-17 h run is too
        # expensive to lose blind, so keep the current run's log on disk.
        log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ER-AutoModder"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.server_log = log_dir / "llama_server.log"
        self._log_handle = open(self.server_log, "w", encoding="utf-8", errors="replace")
        self._proc = subprocess.Popen(
            [str(self._server), "-m", str(self._model), "-ngl", "99",
             "-c", "4096", "--port", str(self._port), "--host", "127.0.0.1",
             "--jinja"],
            stdout=self._log_handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._proc.poll() is not None:
                self.last_error = f"llama-server exited with code {self._proc.returncode}"
                self._proc = None
                return False
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self._port}/health", timeout=3) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(1.5)
        self.last_error = "llama-server did not become healthy in time"
        self.stop()
        return False

    def stop(self) -> None:
        if self._proc is not None:
            subprocess.run(["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=_NO_WINDOW)
            self._proc = None
        handle = getattr(self, "_log_handle", None)
        if handle is not None:
            handle.close()          # the log stays on disk for post-mortem reading
            self._log_handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------ #
    # Translation
    # ------------------------------------------------------------------ #

    def translate(self, text: str,
                  glossary: list[tuple[str, str]] | None = None,
                  mode: str = "prose") -> str | None:
        """Translate one line/entry. Returns None on any failure (server
        down, HTTP error, mangled tags) — caller falls back to argos.

        mode: "prose" (default) for descriptions/dialogue; "name" for item
        and place names, which need a prompt that localizes descriptive
        elements instead of preserving them (see _NAME_SYSTEM_PROMPT)."""
        if self._proc is None or self._proc.poll() is not None:
            if not self.start():
                return None
        terms = [(s, d) for s, d in (glossary or ())
                 if s.lower() in text.lower()]
        parts = []
        if terms:
            parts.append("Glossary (use exactly, inflect naturally):\n"
                         + "\n".join(f"{s} = {d}" for s, d in terms))
        if mode == "name":
            system, fewshot = _NAME_SYSTEM_PROMPT, _NAME_FEWSHOT
            parts.append(f"Localize this name to Finnish:\n{text}")
        else:
            system, fewshot = _SYSTEM_PROMPT, _FEWSHOT
            parts.append(f"Translate to Finnish:\n{text}")
        payload = json.dumps({
            "messages": [{"role": "system", "content": system},
                         *fewshot,
                         {"role": "user", "content": "\n\n".join(parts)}],
            "temperature": 0.2,
            "max_tokens": 900,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/v1/chat/completions",
            data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            out = data["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            self.last_error = str(exc)
            return None
        if not out or not tags_preserved(text, out):
            self.last_error = "LLM output empty or mangled a tag/placeholder"
            return None
        if has_runaway_repetition(text, out):
            self.last_error = "LLM output collapsed into runaway repetition"
            return None
        return out
