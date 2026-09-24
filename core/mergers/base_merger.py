"""
Abstract base class for all mergers.

Every merger (params, TAE, EMEVD, MSB, MSG) follows the same contract:
  1. can_handle(mods)  → does this merger have anything to do?
  2. merge(mods, output_path, vanilla_path) → do the merge, return conflicts

The merge() method must:
  - Be safe to call even when soulstruct can't parse a file (fallback to copy)
  - Write output files to output_path, preserving the game's folder structure
  - Return every Conflict it detected (even resolved ones)
  - Append human-readable lines to self.log_lines for the GUI log panel
"""
from __future__ import annotations
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from core.mod import is_scaffolding      # needed at run time, not just for typing

if TYPE_CHECKING:
    from core.mod import Mod
    from core.conflict import Conflict


class BaseMerger(ABC):

    def __init__(self):
        self.log_lines: list[str] = []

    def log(self, msg: str) -> None:
        self.log_lines.append(msg)

    @abstractmethod
    def can_handle(self, mods: list[Mod]) -> bool:
        ...

    @abstractmethod
    def merge(
        self,
        mods: list[Mod],
        output_path: Path,
        vanilla_path: Path | None = None,
    ) -> list[Conflict]:
        ...

    # ------------------------------------------------------------------ #
    # Shared utility: group files by relative path across all mods
    # ------------------------------------------------------------------ #

    def _group_files(
        self,
        mods: list[Mod],
        glob_pattern: str,
    ) -> dict[str, list[Mod]]:
        """
        Scan all enabled mods for files matching glob_pattern.
        Returns {relative_path: [mods_that_have_it]}, ordered by mod priority.

        This is the standard first step in every merger's merge() method —
        it tells us which files need merging (appear in 2+ mods) and which
        can just be copied (appear in only 1 mod).

        Example:
            _group_files(mods, "*.emevd.dcx") might return:
            {
                "event/m60_00_00_00.emevd.dcx": [ModA, ModB],  ← needs merging
                "event/m60_01_00_00.emevd.dcx": [ModA],        ← just copy
            }
        """
        file_map: dict[str, list[Mod]] = {}
        for mod in mods:
            if not mod.enabled:
                continue
            for p in mod.path.rglob(glob_pattern):
                rel = p.relative_to(mod.path).as_posix()
                if is_scaffolding(rel):
                    continue        # _tyo backups etc. are not mod content
                file_map.setdefault(rel, []).append(mod)
        return file_map

    # ------------------------------------------------------------------ #
    # Shared utility: copy a file from the highest-priority mod
    # ------------------------------------------------------------------ #

    def _copy_winner(
        self,
        relative: str,
        mods_with_file: list[Mod],
        output_path: Path,
    ) -> None:
        """
        Fallback used by all mergers when smart-merging isn't possible.
        Copies the file from the highest-priority mod (index 0) to output.
        Logs a warning and returns without raising if the source is missing.
        """
        if not mods_with_file:
            self.log(f"  copy skipped — no mods provided for {relative}")
            return

        winner = mods_with_file[0]  # index 0 = highest priority
        src = winner.path / relative

        if not src.exists():
            self.log(f"  copy failed — {winner.name}/{relative} not found on disk")
            return

        dst = output_path / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        self.log(f"  copy ({winner.name}) → {relative}")
