"""
Message / text merger — handles .msgbnd.dcx files.

MSG files contain all the game's localized text:
  - Item names and descriptions
  - NPC dialogue subtitles
  - Tutorial messages
  - Menu strings

Structure:
  msg/engus/                    ← one folder per language
    item.msgbnd.dcx             ← BND archive
      ├─ WeaponName.fmg         ← one FMG (Fire Message Group) per category
      ├─ WeaponInfo.fmg
      ├─ NpcName.fmg
      └─ ...

Each FMG is a flat table: integer ID → string.

Merge algorithm:
  For each .msgbnd.dcx that appears in multiple mods:
    1. Load both with soulstruct
    2. For each FMG inside, compare entries by ID
    3. IDs only in one mod → free merge (add them)
    4. Same ID in both → compare text
       - Different text = conflict, highest priority wins
       - Identical text = keep once, no conflict
"""
from __future__ import annotations
from pathlib import Path

from .base_merger import BaseMerger
from core.conflict import Conflict, ConflictType, Severity
from core.mod import Mod


class MsgMerger(BaseMerger):

    def can_handle(self, mods: list[Mod]) -> bool:
        return bool(self._group_files(mods, "*.msgbnd.dcx"))

    def merge(
        self,
        mods: list[Mod],
        output_path: Path,
        vanilla_path: Path | None = None,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        file_map = self._group_files(mods, "*.msgbnd.dcx")

        for rel, owners in file_map.items():
            if len(owners) == 1:
                self._copy_winner(rel, owners, output_path)
                continue

            self.log(f"MSG: merging {rel} ({len(owners)} mods)")
            try:
                new_conflicts = self._merge_msgbnd(rel, owners, output_path)
                conflicts.extend(new_conflicts)
            except Exception as exc:
                self.log(f"MSG: smart merge failed for {rel} ({exc}), copying winner")
                self._copy_winner(rel, owners, output_path)
                conflicts.append(Conflict(
                    type=ConflictType.FILE_REPLACE,
                    file=rel,
                    location="entire file",
                    resolved_by=owners[0].name,
                    values={m.name: "<file>" for m in owners},
                    severity=Severity.ERROR,
                ))

        return conflicts

    # ------------------------------------------------------------------ #

    def _merge_msgbnd(
        self,
        rel: str,
        mods: list[Mod],
        output_path: Path,
    ) -> list[Conflict]:
        from soulstruct.containers import Binder as BND4
        from soulstruct.base.text.fmg import FMG

        conflicts: list[Conflict] = []

        # Load each mod independently — a corrupt BND skips that mod
        # rather than aborting the entire file's merge.
        loaded: list[tuple[str, BND4]] = []
        for mod in mods:
            try:
                bnd = BND4.from_path((mod.path / rel).as_posix())
                loaded.append((mod.name, bnd))
            except Exception as exc:
                self.log(f"MSG: could not load {mod.name}/{rel} — {exc}, skipping")

        if not loaded:
            raise RuntimeError("No MSGBND files could be loaded")

        if len(loaded) == 1:
            surviving_mod = next(m for m in mods if m.name == loaded[0][0])
            self._copy_winner(rel, [surviving_mod], output_path)
            return []

        winner_name, result_bnd = loaded[0]

        # Build {fmg_name: FMG} for the result
        result_fmgs: dict[str, FMG] = {}
        for entry in result_bnd.entries:
            if entry.name.lower().endswith(".fmg"):
                try:
                    result_fmgs[entry.name] = FMG.from_bytes(entry.data)
                except Exception:
                    pass

        # O(1) lookup for writing FMG bytes back into BND entries.
        # Built once here instead of scanning result_bnd.entries every time.
        result_entry_map = {e.name: e for e in result_bnd.entries}

        # Merge lower-priority mods into result
        for mod_name, bnd in loaded[1:]:
            for entry in bnd.entries:
                if not entry.name.lower().endswith(".fmg"):
                    continue
                try:
                    other_fmg = FMG.from_bytes(entry.data)
                except Exception:
                    continue

                if entry.name not in result_entry_map:
                    # FMG only in this mod — add the whole entry
                    result_bnd.entries.append(entry)
                    result_entry_map[entry.name] = entry
                    result_fmgs[entry.name] = other_fmg
                    continue
                if entry.name not in result_fmgs:
                    # The winner has this FMG but soulstruct could not read its copy.
                    # Testing `result_fmgs` (parsed files only) instead of the entry list
                    # would append a SECOND entry with the same name and id beside it -
                    # that is exactly what broke the merged c0000.anibnd.dcx, where the
                    # same mistake in tae_merger produced 84 duplicate entries and the
                    # game crashed (2026-09-23). Keep the winner's entry.
                    continue

                result_fmg = result_fmgs[entry.name]
                # Use the live dict directly — keeps result_entries in sync
                # as we add new entries within this loop.
                result_entries = result_fmg.entries

                for eid, text in other_fmg.entries.items():
                    if eid not in result_entries:
                        # Unique string in lower-priority mod → add it
                        result_entries[eid] = text
                        self.log(f"MSG: added entry {eid} from {mod_name} ({entry.name})")
                    elif not (result_entries[eid] or "").strip():
                        # The winner only has vanilla's EMPTY placeholder here. Mods put new
                        # items into those unused IDs, so an empty row is "not set", not a
                        # choice to override - letting it win blanked Elden Vins' own item
                        # names under a translation mod (38 rows, 2026-09-22 test).
                        if (text or "").strip():
                            result_entries[eid] = text
                            self.log(f"MSG: filled empty entry {eid} from {mod_name} "
                                     f"({entry.name})")
                    elif result_entries[eid] != text:
                        # Same ID, different text → conflict
                        conflicts.append(Conflict(
                            type=ConflictType.MSG_ENTRY,
                            file=rel,
                            location=f"{entry.name} entry {eid}",
                            resolved_by=winner_name,
                            values={
                                winner_name: result_entries[eid][:80],
                                mod_name:    text[:80],
                            },
                            severity=Severity.WARNING,
                        ))

                # Write updated FMG bytes back — O(1) via the lookup dict
                bnd_entry = result_entry_map.get(entry.name)
                if bnd_entry is not None:
                    bnd_entry.data = bytes(result_fmg)

        dst = output_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        result_bnd.write(dst.as_posix())
        return conflicts
