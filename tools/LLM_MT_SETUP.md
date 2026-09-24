# Smart MT (local LLM translation) one-time setup

The Text Editor's Translate buttons use a local LLM as the primary
translation engine when this toolchain is present — measured clearly better
Finnish than argostranslate on real mod text (glossary terms inflected
properly, `<font>`/`<?placeholder?>` tags preserved, negations and structure
right), at ~0.4 s/line on an RTX 4070. argostranslate remains the automatic
fallback (no toolchain, or a single line failing).

Same convention as the RVC/Chatterbox toolchains: external tools discovered
by path search (`tools/` next to the app, `~/tools`, `C:/tools`), driven via
subprocess — here llama.cpp's `llama-server.exe` with an OpenAI-compatible
HTTP API on localhost.

## Install

1. **llama.cpp CUDA build** → `tools/llama_cpp/`
   Download from https://github.com/ggml-org/llama.cpp/releases the two zips
   for your CUDA generation (NVIDIA 40-series → cuda-12.4):
   - `llama-<build>-bin-win-cuda-12.4-x64.zip`
   - `cudart-llama-bin-win-cuda-12.4-x64.zip`
   Extract BOTH into `tools/llama_cpp/` so that `llama-server.exe` and the
   `cudart64_*.dll`/`cublas*.dll` files sit in the same folder.

2. **Model** → `tools/llm_models/`
   Recommended (what the comparison test was run with):
   ```
   curl.exe -L -o tools\llm_models\gemma-3-12b-it-Q4_K_M.gguf https://huggingface.co/ggml-org/gemma-3-12b-it-GGUF/resolve/main/gemma-3-12b-it-Q4_K_M.gguf
   ```
   (7.3 GB. Any other instruction-tuned `.gguf` dropped into `tools/llm_models/`
   is picked up too; the Gemma file above is preferred when present.)

## Verify

```powershell
tools\llama_cpp\llama-server.exe -m tools\llm_models\gemma-3-12b-it-Q4_K_M.gguf -ngl 99 --port 8123 --jinja
```

Wait for "model loaded", then open http://127.0.0.1:8123/health — it should
return ok. Ctrl+C to stop; the app manages the server itself from here on.

## Notes

- Tested working combo (2026-07-07): llama.cpp b9893 win-cuda-12.4,
  gemma-3-12b-it-Q4_K_M, RTX 4070 12 GB (`-ngl 99` fits fully in VRAM).
- The server is started once per Translate batch and stopped after — the
  ~10 s model load is paid per batch, not per line.
- Prompting is handled by the app (`core/localisation/llm_translate_engine.py`);
  glossary terms are injected per line. Do not inline a large glossary into
  a system prompt if you modify this — small models pattern-continue big
  listings into runaway output (observed directly).
