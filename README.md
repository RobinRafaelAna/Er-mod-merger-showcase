# ER AutoModder — code sample

Two parts of **ER AutoModder**, a desktop tool I build for Elden Ring modding:

1. **The mod merger**: combines several mods into one package the game (via the ME3 mod
   loader) can run. Two mods that change the same game file normally overwrite each
   other; this engine opens the file formats and merges individual records instead.
2. **The Finnish localisation pipeline**: translates game and mod text into Finnish with
   a local LLM, and generates Finnish speech in each character's own voice with
   Chatterbox voice cloning.

Full project: [RobinRafaelAna/eldenring-automodder](https://github.com/RobinRafaelAna/eldenring-automodder)

## Part 1: mod merger

### What it does

`MergeEngine` takes an ordered list of mod folders (highest priority first) and:

1. runs a merger per file format:
   - **Params** (`regulation.bin`): row-level three-way merge against vanilla, so each
     mod's own changes survive; version/schema mismatches are reported.
   - **Text** (`.msgbnd.dcx`): entry-level merge; an empty row in the winning mod no longer
     hides real text from a lower one.
   - **Animations** (`.anibnd.dcx`): every archive entry merged by name.
   - **Sound banks** (`.bnk`): sounds and HIRC objects merged three-way against vanilla.
   - **Event scripts, maps and generic containers** (map writing is the least tested path).
2. copies every other file from the highest-priority mod that has it, recording each
   overwrite as a conflict;
3. **verifies the output**: every file each mod provides must exist with the right size,
   and known header faults that break other tools are corrected.

Every decision becomes a `Conflict` record (type, location, winner, severity) that the
GUI shows to the user.

### Things worth looking at

| Where | What it shows |
|---|---|
| `core/merge_engine.py` | Orchestration, priority rules, post-merge verification |
| `core/mergers/param_merger.py` | Three-way row merge against vanilla, restoring rows a mod built on an older patch lacks |
| `core/mergers/bnk_merger.py` | Merging a binary audio format (Wwise banks) by parsing its sections |
| `soulstruct_patches.py` | Fixes to the third-party file-format library, each with the bug, how it was found and how it was verified |
| `tests/` | Runs without any game files |

Most of the hard work was debugging, not writing new code. A few cases, all documented
in the code:

- **A merged package crashed at a black screen.** I bisected by restoring files one at a
  time and round-tripping single files with no edits. Cause: the library wrote BND4
  archives without 16-byte data alignment, and the game's animation loader relies on it.
- **`regulation.bin` doubled in size** (194 → 388 params) after a plain load and save:
  the library wrote each param to its default path instead of the path it came from.
- **Merged files crashed the Item and Enemy Randomizer** (a third-party tool) although the
  game ran them. I tested the output with SoulsFormats, the parsing library the randomizer
  uses: 91 of
  194 params were unreadable because UTF-16 row names were cut to one character on read
  and written one byte short. After the fix: 194/194, row data identical to the source.
- **Rows silently lost:** vanilla data has 26 rows with duplicate IDs (20 with different
  data); the library kept only the first. They are now preserved in file order.

## Part 2: Finnish localisation (LLM translation + Chatterbox voices)

### Translation

`core/localisation/llm_translate_engine.py` runs a local model (Gemma 3 12B, quantised,
through llama.cpp's `llama-server`) and talks to it over its OpenAI-compatible HTTP API.
The server is started once and kept warm: about 0.4 s per line on an RTX 4070, no cloud
service, no per-request cost.

- **Prompt design came from testing.** Putting the whole glossary in the system prompt
  made the model keep writing glossary entries until it hit the token limit. What works:
  a short rule-only system prompt, one example pair, and per line only the glossary
  terms that actually occur in it.
- **Output is checked, not trusted.** A translation is rejected if it loses a game tag or
  placeholder (`<?itemName?>`, `<font>`) or collapses into repetition; that line then
  goes to the offline fallback translator.
- Item and place names use a separate prompt: prose keeps proper names, but descriptive
  names should be translated ("Serpent-Hunter" → "Käärmejahti").

`core/localisation/machine_translate.py` is the fallback (argostranslate, offline) and
the glossary machinery shared by both:

- Game tags and glossary terms are masked as tokens before translation and restored
  after. When the model writes a Finnish case ending onto a token, it is attached
  grammatically: consonant gradation, vowel harmony and stem changes
  ("Rajahauta" + "ssa" → "Rajahaudassa", "Virtaus" → "Virtauksen").
- Glossary matching is case-insensitive and keeps the capitalisation of the match
  ("TORRENT" → "VIRTAUS"); English possessives are derived automatically.

### Speech in the character's own voice

`core/localisation/chatterbox_engine.py` drives Chatterbox Multilingual (Resemble AI,
MIT), which speaks Finnish directly in a target voice from about ten seconds of
reference audio: here, the character's own original line. It beat the earlier two-step
pipeline (Microsoft neural TTS + voice conversion) in listening tests.

- Chatterbox needs its own dependency stack (PyTorch, transformers), so it runs in an
  isolated virtual environment. The app talks to it through a manifest file and a
  line-based stdout protocol (`core/localisation/rvc_scripts/er_chatterbox.py`), and the
  model loads once per batch instead of once per line.
- **Crash recovery:** a CUDA error kills every later GPU call in the same process. Early
  on, one bad line failed the other 6,398 in its batch. Now the engine restarts the
  driver on the remaining lines, retries the crashing line once (generation is random,
  so a retry often succeeds), and then skips it.
- A reported success only counts if the audio file really exists.

Setup guides for both external toolchains are in `tools/`.

## Running the tests

```
pip install -r requirements.txt
python -m pytest
```

(If another installed package breaks pytest's plugin loading, set
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.)

The tests need no game files, models or GPU: they build small mods and params in
memory, point the LLM engine at a fake HTTP server, and replace the Chatterbox driver
with a script that speaks the same protocol. Merging real mods needs the game's own
files, and real translation and speech need the models; those were checked against my
installed game, and by reading and listening.

## How it was built

I built this project with an AI coding assistant (Claude Code). I decided what to build,
directed the work, did the investigation and testing (including in game), and made the
calls on what to change. The commit history of the full project shows how the work went.

## License

GPL-3.0-or-later. `soulstruct_patches.py` re-implements parts of
[soulstruct](https://github.com/Grimrukh/soulstruct), which is GPL-3.0-or-later.
