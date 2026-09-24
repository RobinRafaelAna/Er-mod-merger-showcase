"""
Param merger — handles regulation.bin.

regulation.bin is a BND4 archive (handled by soulstruct) containing every
game parameter table: weapons, enemies, spells, items, etc.

Merge algorithm
---------------
WITHOUT vanilla path (simple mode):
  For every param table that appears in 2+ mods, compare every row/field.
  If all mods agree on a value → use it (no conflict).
  If mods disagree → highest-priority mod wins, log a conflict.

WITH vanilla path (smart mode):
  Compute a diff for each mod: {row_id: {field: new_value}} vs vanilla.
  Apply all diffs to vanilla, lowest priority first so higher priority
  overwrites on collision.  This way every mod's UNIQUE changes are kept
  even if it's not the top-priority mod — only true conflicts are lost.

Both modes write the result to output_path/regulation.bin.
If soulstruct can't parse any file we fall back to copying the winner.
"""
from __future__ import annotations
import copy
import shutil
from pathlib import Path

from .base_merger import BaseMerger
from core.conflict import Conflict, ConflictType, Severity
from core.mod import Mod
from core.version_detector import _read_version, compare_schemas


class ParamMerger(BaseMerger):

    def can_handle(self, mods: list[Mod]) -> bool:
        return any(m.regulation_bin is not None for m in mods if m.enabled)

    def merge(
        self,
        mods: list[Mod],
        output_path: Path,
        vanilla_path: Path | None = None,
    ) -> list[Conflict]:
        enabled = [m for m in mods if m.enabled and m.regulation_bin]
        if not enabled:
            return []

        out_file = output_path / "regulation.bin"
        out_file.parent.mkdir(parents=True, exist_ok=True)

        if len(enabled) == 1:
            shutil.copy2(enabled[0].regulation_bin, out_file)
            self.log(f"Params: only one mod has regulation.bin, copied {enabled[0].name}")
            return []

        try:
            return self._smart_merge(enabled, out_file, vanilla_path)
        except Exception as exc:
            self.log(f"Params: smart merge failed ({exc}), falling back to copy")
            shutil.copy2(enabled[0].regulation_bin, out_file)
            return [Conflict(
                type=ConflictType.FILE_REPLACE,
                file="regulation.bin",
                location="entire file",
                resolved_by=enabled[0].name,
                values={m.name: "<file>" for m in enabled},
                severity=Severity.ERROR,
            )]

    # ------------------------------------------------------------------ #

    def _smart_merge(
        self,
        mods: list[Mod],
        out_file: Path,
        vanilla_path: Path | None,
    ) -> list[Conflict]:
        from soulstruct.eldenring.params.gameparambnd import GameParamBND

        conflicts: list[Conflict] = []

        # Load each mod's regulation.bin.
        # mods[0] is highest priority.
        loaded: list[tuple[str, GameParamBND]] = []
        for mod in mods:
            try:
                bnd = GameParamBND.from_encrypted_path(mod.regulation_bin.as_posix())
                loaded.append((mod.name, bnd))
                self.log(f"Params: loaded {mod.name}/regulation.bin")
            except Exception as exc:
                self.log(f"Params: could not load {mod.name}/regulation.bin — {exc}")

        if not loaded:
            raise RuntimeError("No regulation.bin could be loaded by soulstruct")

        # ── Version check ────────────────────────────────────────────── #
        # Detect each mod's regulation.bin version and warn if they differ.
        # Different versions = different schemas = merge may produce garbage.
        # We don't abort — the user may have already verified compatibility —
        # but we log clearly and record ERROR-level conflicts.
        versions: dict[str, str] = {
            name: _read_version(bnd) for name, bnd in loaded
        }
        unique_versions = set(versions.values())

        if len(unique_versions) > 1:
            summary = ", ".join(f"{n}={v}" for n, v in versions.items())
            self.log(f"Params: !! VERSION MISMATCH DETECTED — {summary}")
            self.log("Params: !! Mods were built on different game patches.")
            self.log("Params: !! Update all mods to the same patch in DSMapStudio/Smithbox first.")

            # Schema diff between every pair so the user sees exactly what differs
            for i, (name_a, bnd_a) in enumerate(loaded):
                for name_b, bnd_b in loaded[i + 1:]:
                    mismatches = compare_schemas(bnd_a, name_a, bnd_b, name_b)
                    if mismatches:
                        self.log(f"Params: schema diff {name_a} vs {name_b}:")
                        for line in mismatches:
                            self.log(line)
                        conflicts.append(Conflict(
                            type=ConflictType.FILE_REPLACE,
                            file="regulation.bin",
                            location="param schema mismatch",
                            resolved_by=loaded[0][0],
                            values=versions,
                            severity=Severity.ERROR,
                        ))

        # Optionally load vanilla for smart diffing
        vanilla_bnd: GameParamBND | None = None
        if vanilla_path:
            vanilla_reg = vanilla_path / "regulation.bin"
            if vanilla_reg.exists():
                try:
                    vanilla_bnd = GameParamBND.from_encrypted_path(vanilla_reg.as_posix())
                    self.log("Params: loaded vanilla regulation.bin for smart diff")
                except Exception as exc:
                    self.log(f"Params: could not load vanilla ({exc}), using simple mode")

        # Start with the highest-priority mod as the base result.
        # We will either apply lower-priority diffs on top (smart mode)
        # or just report conflicts (simple mode).
        winner_name, result_bnd = loaded[0]

        # Gather param names present in any mod
        all_param_names: set[str] = set()
        for _, bnd in loaded:
            all_param_names.update(bnd.params.keys())

        for param_name in sorted(all_param_names):
            # Collect {mod_name: {row_id: {field: value}}} for this param
            param_tables: dict[str, dict[int, dict[str, object]]] = {}
            for mod_name, bnd in loaded:
                tbl = _param_to_dict(bnd, param_name)
                if tbl is not None:
                    param_tables[mod_name] = tbl

            if len(param_tables) < 2:
                continue  # only one mod has this param, no conflict possible

            if vanilla_bnd is not None:
                # Smart mode: diff each mod vs vanilla, apply from low→high priority
                vanilla_tbl = _param_to_dict(vanilla_bnd, param_name) or {}
                new_conflicts = self._merge_with_vanilla(
                    param_name, param_tables, vanilla_tbl, result_bnd, mods,
                    {name: bnd.params.get(param_name) for name, bnd in loaded[1:]},
                    vanilla_bnd.params.get(param_name),
                )
            else:
                # Simple mode: report conflicts, highest priority already wins
                new_conflicts = self._detect_conflicts_simple(
                    param_name, param_tables, winner_name
                )

            conflicts.extend(new_conflicts)

        result_bnd.write_encrypted(out_file)
        self.log(f"Params: wrote merged regulation.bin ({len(conflicts)} conflicts)")
        return conflicts

    # ------------------------------------------------------------------ #
    # Simple mode (no vanilla)
    # ------------------------------------------------------------------ #

    def _detect_conflicts_simple(
        self,
        param_name: str,
        tables: dict[str, dict[int, dict[str, object]]],
        winner_name: str,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        # All row IDs across all mods
        all_rows: set[int] = set()
        for tbl in tables.values():
            all_rows.update(tbl.keys())

        for row_id in all_rows:
            # All fields for this row across all mods
            all_fields: set[str] = set()
            for tbl in tables.values():
                all_fields.update(tbl.get(row_id, {}).keys())

            for field in all_fields:
                vals = {
                    mod: (
                        str(tbl[row_id][field])
                        if field in tbl.get(row_id, {})
                        else "<missing>"
                    )
                    for mod, tbl in tables.items()
                    if row_id in tbl
                }
                if len(set(vals.values())) > 1:
                    conflicts.append(Conflict(
                        type=ConflictType.PARAM_FIELD,
                        file=f"regulation.bin / {param_name}",
                        location=f"row {row_id}, field '{field}'",
                        resolved_by=winner_name,
                        values=vals,
                        severity=Severity.WARNING,
                    ))
        return conflicts

    # ------------------------------------------------------------------ #
    # Smart mode (with vanilla baseline)
    # ------------------------------------------------------------------ #

    def _merge_with_vanilla(
        self,
        param_name: str,
        tables: dict[str, dict[int, dict[str, object]]],
        vanilla_tbl: dict[int, dict[str, object]],
        result_bnd,
        mods: list[Mod],
        row_sources: dict | None = None,
        vanilla_param=None,
    ) -> list[Conflict]:
        """
        For each mod, compute its diff vs vanilla.
        Apply diffs from lowest priority to highest (so highest wins on clash).
        Track and return every conflict (same field changed by 2+ mods).
        """
        conflicts: list[Conflict] = []
        row_sources = row_sources or {}
        added_rows: list[tuple[str, int, str]] = []

        # {row_id: {field: {mod_name: value}}}
        # Insertion order = priority order (lowest first, highest last).
        # Storing ALL mod values lets us emit one conflict per field showing
        # every involved mod, instead of recording a new partial conflict
        # each time a higher-priority mod joins the same field clash.
        applied: dict[int, dict[str, dict[str, object]]] = {}

        # Pass 1 — collect all diffs, lowest priority first
        for mod in reversed(mods):
            mod_tbl = tables.get(mod.name)
            if mod_tbl is None:
                continue
            diff = _compute_diff(mod_tbl, vanilla_tbl)
            for row_id, changed_fields in diff.items():
                for field_name, new_val in changed_fields.items():
                    applied.setdefault(row_id, {}).setdefault(field_name, {})[mod.name] = new_val

        # Pass 2 — record conflicts and write winners back into result_bnd
        result_param = result_bnd.params.get(param_name)
        row_lookup = result_param.rows if result_param else {}

        for row_id, field_dict in applied.items():
            row = row_lookup.get(row_id)

            if row is None:
                # Row exists in a lower-priority mod but not in the winner. It used to be
                # skipped "because we can't know the row type" - but the row OBJECT is
                # right there in that mod's own loaded regulation, already the correct
                # type, so it can simply be copied in. Skipping it silently dropped every
                # item, weapon and effect a lower-priority mod added: merging Elden Vins
                # under the Portal Radio mod produced a 2.0 MB regulation.bin where Vins'
                # alone is 2.8 MB (2026-09-23).
                donor = next((n for n in row_sources
                              if row_id in (row_sources[n].rows if row_sources[n] else {})),
                             None)
                if donor is None or result_param is None:
                    self.log(f"Params: {param_name} row {row_id} could not be copied "
                             f"(no source row, or the winner has no such param table)")
                    continue
                result_param.rows[row_id] = copy.deepcopy(row_sources[donor].rows[row_id])
                row_lookup = result_param.rows
                row = result_param.rows[row_id]
                added_rows.append((param_name, row_id, donor))

            for field_name, mod_values in field_dict.items():
                # Determine winner: highest-priority mod that changed this field
                winner_name = next(
                    m.name for m in mods if m.name in mod_values
                )
                winner_val = mod_values[winner_name]

                # Record a conflict if more than one mod changed this field
                # to different values — show ALL mods' values in one record
                if (len(mod_values) > 1 and
                        len(set(str(v) for v in mod_values.values())) > 1):
                    conflicts.append(Conflict(
                        type=ConflictType.PARAM_FIELD,
                        file=f"regulation.bin / {param_name}",
                        location=f"row {row_id}, field '{field_name}'",
                        resolved_by=winner_name,
                        values={k: str(v) for k, v in mod_values.items()},
                        severity=Severity.WARNING,
                    ))

                try:
                    setattr(row, field_name, winner_val)
                except Exception:
                    pass

        # A mod built on an older patch is missing the rows the update added, and the
        # merge starts from that mod's file - so ActionButtonParam 9560 ("Summon Knight
        # Leontiel", new in 1.17.1) vanished from the result even though the game needs
        # it. Put back any row the CURRENT game has that the winner lacks and no mod
        # touched; it is the game's own data, not an edit.
        restored = 0
        result_param = result_bnd.params.get(param_name)
        if result_param is not None and vanilla_param is not None:
            for rid in vanilla_tbl:
                if rid not in result_param.rows and rid in vanilla_param.rows:
                    result_param.rows[rid] = copy.deepcopy(vanilla_param.rows[rid])
                    restored += 1
        if restored:
            self.log(f"Params: {param_name} — {restored} row(s) restored from vanilla "
                     f"(the winning mod predates this game patch)")
        if added_rows:
            self.log(f"Params: {param_name} — {len(added_rows)} row(s) added from "
                     f"lower-priority mods ({added_rows[0][2]} …)")
        return conflicts


# ------------------------------------------------------------------ #
# Module-level helpers
# ------------------------------------------------------------------ #

def _param_to_dict(bnd, param_name: str) -> dict[int, dict[str, object]] | None:
    """
    Convert a soulstruct Param table to a plain Python dict:
      {row_id: {field_name: value, ...}, ...}

    Returns None if the param doesn't exist in this BND.
    """
    param = bnd.params.get(param_name)
    if param is None:
        return None

    result: dict[int, dict[str, object]] = {}
    for row_id, row in param.rows.items():
        # Rows are slotted dataclasses (no __dict__) — iterate binary fields
        # via ParamRow.__iter__, which is the supported generic accessor.
        try:
            fields = {
                k: v for k, v in row
                if not k.startswith("_") and k not in ("ID", "name")
            }
        except Exception:
            fields = {}
        result[row_id] = fields
    return result


def _compute_diff(
    mod_tbl: dict[int, dict[str, object]],
    vanilla_tbl: dict[int, dict[str, object]],
) -> dict[int, dict[str, object]]:
    """
    Return only the rows/fields that differ from vanilla.
    This is the "what did this mod change?" answer.
    """
    diff: dict[int, dict[str, object]] = {}
    new_row_ids = set(mod_tbl) - set(vanilla_tbl)

    for row_id, fields in mod_tbl.items():
        if row_id in new_row_ids:
            # New row — include all fields unconditionally.
            # Skips the field-by-field compare which would miss None-valued
            # fields (str(None) == str(None) would exclude them).
            diff[row_id] = fields
        else:
            vanilla_row = vanilla_tbl[row_id]
            changed = {
                f: v for f, v in fields.items()
                if str(v) != str(vanilla_row.get(f))
            }
            if changed:
                diff[row_id] = changed
    return diff
