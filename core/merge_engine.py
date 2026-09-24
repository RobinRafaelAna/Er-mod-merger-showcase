"""
MergeEngine: runs all mergers and handles generic file conflicts.

This is the single entry point the GUI calls.  It:
  1. Runs every registered smart merger (params, TAE, EMEVD, MSB, MSG)
  2. Handles any remaining files with a generic "copy winner" pass
  3. Emits progress via a callback so the GUI can update its progress bar
  4. Returns all Conflict objects collected across every merger

Usage:
    engine = MergeEngine()
    engine.on_progress = lambda pct, msg: print(f"{pct:.0f}% {msg}")
    conflicts = engine.run(manager)
"""
from __future__ import annotations
import shutil
from pathlib import Path
from typing import Callable

from .mod_manager import ModManager
from .conflict import Conflict, ConflictType, Severity
from .mergers.param_merger import ParamMerger
from .mergers.tae_merger import TaeMerger
from .mergers.emevd_merger import EmevdMerger
from .mergers.msb_merger import MsbMerger
from .mergers.msg_merger import MsgMerger
from .mergers.bnk_merger import BnkMerger
from .mergers.container_merger import ContainerMerger
from .bank_sync import sync as sync_banks


class MergeEngine:

    def __init__(self):
        # Progress callback: (percent: float, message: str) → None
        self.on_progress: Callable[[float, str], None] | None = None
        # Stop callback: returns True when the user has requested cancellation
        self.should_stop: Callable[[], bool] | None = None
        self.log_lines: list[str] = []

    # ------------------------------------------------------------------ #

    def run(self, manager: ModManager) -> list[Conflict]:
        """
        Run the full merge.  Returns every conflict found.
        Output is written to manager.output_path.
        """
        if not manager.output_path:
            raise ValueError("No output path set")

        # A mod that IS the output folder (or sits inside it) makes every merger copy
        # files onto themselves: shutil.copy2 raises "are the same file" and the merge
        # dies halfway. It also makes the result unexplainable - once a merged folder is
        # fed back in, nothing knows which mod a row came from, so priority is moot.
        out_res = manager.output_path.resolve()
        for mod in manager.mods:
            if not mod.enabled:
                continue
            mod_res = mod.path.resolve()
            if mod_res == out_res or out_res in mod_res.parents:
                raise ValueError(
                    f"Mod '{mod.name}' is the output folder (or inside it): {mod.path}\n\n"
                    "Choose an empty output folder, or remove that mod from the list. "
                    "Merge the original mods together instead of merging a previous result."
                )

        manager.output_path.mkdir(parents=True, exist_ok=True)

        mods       = manager.mods
        out_path   = manager.output_path
        vanilla    = manager.vanilla_path
        conflicts: list[Conflict] = []

        # Register all smart mergers
        mergers = [
            ("Params",      ParamMerger()),
            ("Animations",  TaeMerger()),
            ("Events",      EmevdMerger()),
            ("Maps",        MsbMerger()),
            ("Messages",    MsgMerger()),
            ("Sound banks", BnkMerger()),
            ("Containers",  ContainerMerger()),
        ]

        total_steps = len(mergers) + 1  # +1 for generic pass
        for step, (label, merger) in enumerate(mergers):
            if self.should_stop and self.should_stop():
                self._log("Merge cancelled by user.")
                return conflicts
            self._progress(step / total_steps, f"Merging {label}…")
            if merger.can_handle(mods):
                new_conflicts = merger.merge(mods, out_path, vanilla)
                conflicts.extend(new_conflicts)
                self.log_lines.extend(merger.log_lines)
            else:
                self._log(f"{label}: nothing to merge")

        # Generic pass: copy any remaining files that weren't handled by a
        # smart merger (textures, sounds, shaders, etc.)
        self._progress((len(mergers)) / total_steps, "Copying remaining files…")
        generic_conflicts = self._generic_copy(mods, out_path)
        conflicts.extend(generic_conflicts)

        # A dubbed line also lives inside every bank that references it, and a take
        # generated after those banks were written stays original in them. Nothing in the
        # merge can see that - each mod is already inconsistent with itself - so the
        # package is made consistent here, using only its own loose WEMs.
        if vanilla:
            banks, sounds = sync_banks(out_path, vanilla, self._log)
            if banks:
                self._log(f"Banks: {sounds} sound(s) in {banks} bank(s) synced to the "
                          f"package's loose WEMs")

        conflicts.extend(self._verify_output(mods, out_path))
        self._fix_krak_headers(out_path)

        self._progress(1.0, f"Done — {len(conflicts)} conflict(s)")
        return conflicts

    # ------------------------------------------------------------------ #

    def _verify_output(self, mods, out_path: Path) -> list[Conflict]:
        """Check that every file each mod provides actually reached the output.

        A mod that is missing one file in the game's eyes is not a smaller mod - it is a
        crash. Elden Vins' merged package lost `chr/c0000.anibnd.dcx` (the player's
        animations) and `menu/hi/01_common.tpf.dcx`, its two LARGEST files, and the game
        went to a black screen and died (2026-09-23). Nothing reported it, because
        copying is fire-and-forget. This reads the result back and names what is missing
        or truncated, comparing sizes for files that were copied verbatim.
        """
        rewritten = (".msgbnd.dcx", ".emevd.dcx", ".msb.dcx", ".tae", "regulation.bin")
        # Winner per path, exactly as _generic_copy picks it: sizes are only comparable
        # against the mod whose copy was meant to survive. A lower-priority mod's
        # different-sized file is the load order working, not a fault.
        winners: dict[str, tuple[str, object]] = {}
        shared: set[str] = set()
        for mod in mods:
            if not mod.enabled:
                continue
            for rel in mod.all_relative_files():
                key = rel.lower()
                if key in winners:
                    shared.add(key)     # 2+ mods own it, so a merger may have rewritten it
                winners.setdefault(key, (rel, mod))
        missing: list[tuple[str, str]] = []
        checked = 0
        for rel, mod in winners.values():
            dst = out_path / rel
            checked += 1
            if not dst.exists():
                missing.append((mod.name, rel))
                continue
            if rel.endswith(rewritten) or rel.lower() in shared:
                continue            # a merger rewrote it; its size legitimately differs
            try:
                if (mod.path / rel).stat().st_size != dst.stat().st_size:
                    missing.append((mod.name, rel + " (koko eroaa)"))
            except OSError:
                missing.append((mod.name, rel + " (ei luettavissa)"))
        if not missing:
            self._log(f"Verify: all {checked} files present in the output")
            return []
        self._log(f"Verify: {len(missing)} file(s) MISSING or truncated in the output — "
                  f"the merged mod is incomplete and the game may crash")
        for name, rel in missing[:20]:
            self._log(f"  missing: {rel}  (from {name})")
        return [Conflict(
            type=ConflictType.FILE_REPLACE,
            file=rel,
            location="missing from output",
            resolved_by="-",
            values={name: "<missing>"},
            severity=Severity.ERROR,
        ) for name, rel in missing]

    # ------------------------------------------------------------------ #

    def _fix_krak_headers(self, out_path: Path) -> None:
        """Set the DCX version of KRAK files in the output to 0x11000.

        Elden Vins ships `event/m60_52_38_00.emevd.dcx` (and two texbnds) with version
        0x1000 at offset 4. The game ignores the field, but SoulsFormats asserts 0x11000
        for KRAK, so the item/enemy randomizer died reading the merged package
        (2026-09-24). Every vanilla KRAK file has 0x11000; only that one byte changes,
        in the output copy - the source mods are never touched. DFLT files are left as
        they are: SoulsFormats does not check the field there.
        """
        fixed = []
        for dcx in out_path.rglob("*.dcx"):
            try:
                with open(dcx, "r+b") as f:
                    head = f.read(0x2C)
                    if (len(head) == 0x2C and head[:4] == b"DCX\0"
                            and head[4:8] == b"\x00\x00\x10\x00"
                            and head[0x28:0x2C] == b"KRAK"):
                        f.seek(5)
                        f.write(b"\x01")
                        fixed.append(dcx.relative_to(out_path).as_posix())
            except OSError as e:
                self._log(f"DCX header: could not check {dcx.name}: {e}")
        if fixed:
            self._log(f"DCX header: set version 0x11000 on {len(fixed)} KRAK file(s): "
                      + ", ".join(fixed))

    # ------------------------------------------------------------------ #

    def _generic_copy(self, mods, out_path: Path) -> list[Conflict]:
        """
        Copy files that no smart merger handled.
        If a file appears in multiple mods, the highest-priority version wins
        and we record a FILE_REPLACE conflict.
        Files already written by a smart merger are detected by dst.exists().
        """
        conflicts: list[Conflict] = []

        # Collect all files grouped by relative path (case-insensitive key
        # so Windows paths that differ only in case are treated as the same).
        file_map: dict[str, list] = {}
        for mod in mods:
            if not mod.enabled:
                continue
            for rel in mod.all_relative_files():
                file_map.setdefault(rel.lower(), []).append((rel, mod))

        for rel_lower, owners in file_map.items():
            if self.should_stop and self.should_stop():
                break
            # Use the original-case path from the first (highest-priority) owner
            rel_orig, winner_mod = owners[0]
            dst = out_path / rel_orig

            if dst.exists():
                # Already written by a smart merger — skip
                continue

            src = winner_mod.path / rel_orig
            if not src.exists():
                self._log(f"Generic: skipped {rel_orig} — source not found on disk")
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

            if len(owners) > 1:
                # Multiple mods provide this file — log it as a conflict
                self._log(f"Generic: {rel_orig} conflict ({len(owners)} mods) → {winner_mod.name} wins")
                conflicts.append(Conflict(
                    type=ConflictType.FILE_REPLACE,
                    file=rel_orig,
                    location="entire file",
                    resolved_by=winner_mod.name,
                    values={mod.name: "<file>" for _, mod in owners},
                    severity=Severity.WARNING,
                ))

        return conflicts

    # ------------------------------------------------------------------ #

    def _progress(self, pct: float, msg: str) -> None:
        self._log(msg)
        if self.on_progress:
            self.on_progress(pct * 100, msg)

    def _log(self, msg: str) -> None:
        self.log_lines.append(msg)
