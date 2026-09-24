# Chatterbox one-time setup

Chatterbox Multilingual (Resemble AI, MIT) is the app's preferred voice
engine: it speaks Finnish directly in a character's voice from a short
reference clip — no base TTS voice, no per-character training. When this
toolchain is present, full localisation runs use it for **every** line
(each line's own vanilla audio is the voice reference); edge-tts +
FreeVC/RVC remain as automatic fallbacks.

Like the RVC toolchain, it lives in its own isolated venv because its
dependency pins (torch 2.6, transformers 5.x) conflict with the main app
environment. The app discovers it by path search (`tools/chatterbox_venv`
next to the app, `~/tools`, `C:/tools`) and drives it via subprocess
(`core/localisation/rvc_scripts/er_chatterbox.py`).

## Install (Windows, NVIDIA GPU)

From the app/repo root:

```powershell
py -3.11 -m venv tools\chatterbox_venv
tools\chatterbox_venv\Scripts\python.exe -m pip install --upgrade pip
tools\chatterbox_venv\Scripts\python.exe -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
tools\chatterbox_venv\Scripts\python.exe -m pip install chatterbox-tts
```

Verify CUDA is really in use (a silently-installed CPU build makes
generation ~20x slower):

```powershell
tools\chatterbox_venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available())"
```

Must print `True`. If it prints `False`, re-run the torch install line
above (the `--index-url` part is what selects the CUDA build).

The multilingual model (~4 GB) downloads from Hugging Face automatically
on first use and is cached under `%USERPROFILE%\.cache\huggingface`.

## Notes

- Tested working install (2026-07-06): `chatterbox-tts==0.1.7`,
  `torch 2.6.0+cu124`, Python 3.11.9, RTX 4070.
- Generation speed is roughly 2–5 s per line on an RTX 4070; a full
  ~7000-line localisation run takes several hours. The model loads once
  per run, not per line.
- Every generated clip carries Resemble's inaudible PerTh watermark —
  harmless for mod use, by design of the model authors.
