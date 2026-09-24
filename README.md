# ER Mod Merger — code sample

The merge engine from **ER AutoModder**, a desktop tool I build for combining several
Elden Ring mods into one package that the game (via the ME3 mod loader) can run.
Two mods that both change the same game file normally overwrite each other; this engine
opens the file formats and merges them at the level of individual records instead.

Full project: [RobinRafaelAna/eldenring-automodder](https://github.com/RobinRafaelAna/eldenring-automodder)

## What it does

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

## Things worth looking at

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

## Running the tests

```
pip install -r requirements.txt
python -m pytest
```

(If another installed package breaks pytest's plugin loading, set
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.)

The tests build small mods and params in memory. Merging real mods needs the game's own
files, which cannot be redistributed, so those checks were done against my installed
game and in game.

## How it was built

I built this project with an AI coding assistant (Claude Code). I decided what to build,
directed the work, did the investigation and testing (including in game), and made the
calls on what to change. The commit history of the full project shows how the work went.

## License

GPL-3.0-or-later. `soulstruct_patches.py` re-implements parts of
[soulstruct](https://github.com/Grimrukh/soulstruct), which is GPL-3.0-or-later.
