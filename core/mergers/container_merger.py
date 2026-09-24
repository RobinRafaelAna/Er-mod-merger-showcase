"""
Container merger — handles the plain BND4 archives no other merger owns.

  chr/c5220.chrbnd.dcx      character models and their Havok collision
  chr/c5220_h.texbnd.dcx    that character's textures
  parts/*.partsbnd.dcx      armour pieces
  asset/**/*.geombnd.dcx    asset geometry

These were copied whole by the generic pass, so two mods touching the same character meant
one of them lost everything — even when they changed different files inside the archive.
Measured on this user's mods (2026-09-23): 4 shared archives, e.g. Elden Vins' c5220.chrbnd
(10.3 MB) against the Patches mod's (6.5 MB), and c5220_h.texbnd in both.

An archive is a list of named entries, so it merges like everything else here:

  - entry only in a lower-priority mod        → taken
  - entry both mods changed vs VANILLA        → highest priority wins, conflict recorded
  - entry the winner left at vanilla          → the lower mod's version is taken
  - no vanilla copy to compare against        → highest priority wins, conflict recorded

`.anibnd.dcx` is deliberately NOT handled here: TaeMerger merges its animation events as
well as its entries, and it runs first.
"""
from __future__ import annotations
from pathlib import Path

from .base_merger import BaseMerger
from core.conflict import Conflict, ConflictType, Severity
from core.mod import Mod

PATTERNS = ("*.chrbnd.dcx", "*.texbnd.dcx", "*.partsbnd.dcx",
            "*.geombnd.dcx", "*.geomhkxbnd.dcx", "*.objbnd.dcx")


class ContainerMerger(BaseMerger):

    def _shared(self, mods: list[Mod]) -> dict[str, list[Mod]]:
        out: dict[str, list[Mod]] = {}
        for pattern in PATTERNS:
            for rel, owners in self._group_files(mods, pattern).items():
                if len(owners) > 1:
                    out[rel] = owners
        return out

    def can_handle(self, mods: list[Mod]) -> bool:
        return bool(self._shared(mods))

    def merge(
        self,
        mods: list[Mod],
        output_path: Path,
        vanilla_path: Path | None = None,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for rel, owners in self._shared(mods).items():
            try:
                conflicts.extend(self._merge_archive(rel, owners, output_path, vanilla_path))
            except Exception as exc:
                self.log(f"Container: merge failed for {rel} ({exc}), copying {owners[0].name}")
                self._copy_winner(rel, owners, output_path)
                conflicts.append(Conflict(
                    type=ConflictType.FILE_REPLACE, file=rel, location="entire archive",
                    resolved_by=owners[0].name,
                    values={m.name: "<archive>" for m in owners}, severity=Severity.ERROR))
        return conflicts

    # ------------------------------------------------------------------ #

    def _merge_archive(
        self,
        rel: str,
        owners: list[Mod],
        output_path: Path,
        vanilla_path: Path | None,
    ) -> list[Conflict]:
        from soulstruct.containers import Binder

        conflicts: list[Conflict] = []
        winner = owners[0]
        result = Binder.from_path((winner.path / rel).as_posix())
        entries = {e.name: e for e in result.entries}

        van: dict[str, bytes] = {}
        if vanilla_path and (vanilla_path / rel).exists():
            try:
                van = {e.name: bytes(e.data) for e in Binder.from_path((vanilla_path / rel).as_posix()).entries}
            except Exception as exc:
                self.log(f"Container: could not read vanilla {rel} ({exc}), priority decides")

        taken = kept = 0
        for mod in owners[1:]:
            try:
                other = Binder.from_path((mod.path / rel).as_posix())
            except Exception as exc:
                self.log(f"Container: could not read {mod.name}/{rel} ({exc}), skipped")
                continue
            for entry in other.entries:
                mine = entries.get(entry.name)
                if mine is None:
                    result.entries.append(entry)
                    entries[entry.name] = entry
                    taken += 1
                    continue
                if bytes(mine.data) == bytes(entry.data):
                    continue
                # Both have it and they differ. If the winner is still at vanilla here, it
                # did not touch this file and the other mod's version is the only edit.
                if van and entry.name in van and bytes(mine.data) == van[entry.name]:
                    mine.data = entry.data
                    taken += 1
                    continue
                kept += 1
                conflicts.append(Conflict(
                    type=ConflictType.FILE_REPLACE, file=rel,
                    location=f"{entry.name}", resolved_by=winner.name,
                    values={winner.name: "<entry>", mod.name: "<entry>"},
                    severity=Severity.WARNING))

        dst = output_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        result.write(dst.as_posix())
        self.log(f"Container: {rel} merged {len(owners)} mods — {taken} entry(s) taken from "
                 f"lower priority, {kept} kept from {winner.name}, {len(result.entries)} total")
        return conflicts
