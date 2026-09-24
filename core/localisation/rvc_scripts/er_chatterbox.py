"""
er_chatterbox.py — batch Chatterbox Multilingual TTS driver.

Runs INSIDE the isolated tools/chatterbox_venv (see tools/CHATTERBOX_SETUP.md),
never imported by the main app — same convention as er_train.py/er_infer.py.
One invocation loads the multilingual model once and generates every job in
the manifest, so the ~15s model load is paid per batch, not per line.

Usage:  python er_chatterbox.py <manifest.json>

Manifest format (UTF-8 JSON):
    {
      "language": "fi",
      "jobs": [
        {"text": "...", "prompt": "<reference wav path>",
         "out": "<output wav path>", "exaggeration": 0.5},
        ...
      ]
    }

Streams progress on stdout, one line per event:
    MODEL_LOADED|<device>
    RESULT|ok|<out path>
    RESULT|fail|<out path>|<error>
    PROGRESS|<done>|<total>
    FATAL|<error>          — CUDA context is poisoned; exits with code 3.
                             The caller restarts the driver on the remaining
                             jobs (a device-side assert kills every later
                             kernel call in the same process, so continuing
                             here would just fail all of them).
"""
import json
import sys
from pathlib import Path

# A device-side assert can come from over-long text; cap defensively at a
# length Chatterbox handles comfortably (whole entries are one utterance).
_MAX_TEXT_CHARS = 400


def _prompt_usable(path: str) -> bool:
    """CPU-side reference sanity check — never touches the GPU."""
    try:
        import torchaudio
        info = torchaudio.info(path)
        return info.num_frames / max(info.sample_rate, 1) >= 0.3
    except Exception:
        return False


def main() -> int:
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    language = manifest.get("language", "fi")
    jobs = manifest["jobs"]

    import torch
    import torchaudio
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = ChatterboxMultilingualTTS.from_pretrained(device=device)

    # T3 sampling can emit a token id beyond an embedding table, and
    # chatterbox's own drop_invalid_tokens() doesn't catch every case —
    # reproduced with 'Ei...' + one particular reference clip: the invalid
    # token reached s3gen's flow.input_embedding (flow.py:166) and the
    # device-side assert poisoned the whole CUDA context. Clamp ids at
    # every embedding the sampled token stream flows through; a clamped
    # token costs a tiny audio artifact instead of killing the run.
    def _clamp_embedding(emb):
        orig_forward = emb.forward
        max_id = emb.num_embeddings - 1

        def _clamped(ids):
            return orig_forward(ids.clamp(0, max_id))

        emb.forward = _clamped

    _clamp_embedding(model.t3.speech_emb)
    _clamp_embedding(model.s3gen.flow.input_embedding)
    print(f"MODEL_LOADED|{device}", flush=True)

    # generate() re-prepares speaker conditionals whenever audio_prompt_path
    # is given — only pass it when the prompt actually changes so consecutive
    # same-prompt jobs (one character's lines) reuse the prepared conditionals.
    last_prompt: tuple[str, float] | None = None
    total = len(jobs)
    for i, job in enumerate(jobs):
        out = job["out"]
        try:
            text = job["text"].strip()
            if len(text) > _MAX_TEXT_CHARS:
                cut = text.rfind(" ", 0, _MAX_TEXT_CHARS)
                text = text[:cut if cut > 0 else _MAX_TEXT_CHARS]
            exaggeration = float(job.get("exaggeration", 0.5))
            prompt_key = (job["prompt"], exaggeration)
            if not text:
                print(f"RESULT|fail|{out}|empty text", flush=True)
                print(f"PROGRESS|{i + 1}|{total}", flush=True)
                continue
            if prompt_key != last_prompt and not _prompt_usable(job["prompt"]):
                print(f"RESULT|fail|{out}|unusable reference audio: {job['prompt']}",
                      flush=True)
                print(f"PROGRESS|{i + 1}|{total}", flush=True)
                continue
            wav = model.generate(
                text,
                language_id=language,
                audio_prompt_path=job["prompt"] if prompt_key != last_prompt else None,
                exaggeration=exaggeration,
            )
            last_prompt = prompt_key
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            torchaudio.save(out, wav.cpu(), model.sr)
            print(f"RESULT|ok|{out}", flush=True)
        except Exception as exc:
            # A failed prompt may have left half-prepared conditionals behind —
            # force a re-prepare on the next job rather than trusting them.
            last_prompt = None
            err = str(exc).replace("\n", " ").replace("|", "/")
            print(f"RESULT|fail|{out}|{err}", flush=True)
            if "CUDA" in err or "device-side" in err or "cuda" in type(exc).__name__.lower():
                print(f"PROGRESS|{i + 1}|{total}", flush=True)
                print(f"FATAL|{err}", flush=True)
                return 3
        print(f"PROGRESS|{i + 1}|{total}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
