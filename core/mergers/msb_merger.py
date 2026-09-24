"""
MSB map merger — handles .msb.dcx files.

MSB (Map Studio Binary) files define what's in each map area:
  - Parts: map pieces, character spawns, objects, connect regions
  - Regions: named areas (used for triggers, teleports, boss arenas)
  - Events: item lots, NPC talk, patrol paths, etc.

Each entity has a name and optionally an entity_id (used to reference it
from EMEVD scripts).

Merge algorithm:
  For each .msb.dcx that appears in multiple mods:
    1. Load both MSB files with soulstruct
    2. Compare parts/regions/events by name
    3. Entities only in one mod → free merge
    4. Same name in both → compare fields
       - Different = conflict, highest priority wins
       - Identical = keep once, no conflict
  Falls back to full-file copy if soulstruct can't parse.
"""
from __future__ import annotations
from pathlib import Path

from .base_merger import BaseMerger
from core.conflict import Conflict, ConflictType, Severity
from core.mod import Mod


class MsbMerger(BaseMerger):

    def can_handle(self, mods: list[Mod]) -> bool:
        return bool(self._group_files(mods, "*.msb.dcx"))

    def merge(
        self,
        mods: list[Mod],
        output_path: Path,
        vanilla_path: Path | None = None,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        file_map = self._group_files(mods, "*.msb.dcx")

        for rel, owners in file_map.items():
            if len(owners) == 1:
                self._copy_winner(rel, owners, output_path)
                continue

            self.log(f"MSB: merging {rel} ({len(owners)} mods)")
            try:
                new_conflicts = self._merge_msb(rel, owners, output_path)
                conflicts.extend(new_conflicts)
            except Exception as exc:
                self.log(f"MSB: smart merge failed for {rel} ({exc}), copying winner")
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

    def _merge_msb(
        self,
        rel: str,
        mods: list[Mod],
        output_path: Path,
    ) -> list[Conflict]:
        import soulstruct.eldenring.maps.msb as msb_module
        from soulstruct.eldenring.maps.msb import MSB

        conflicts: list[Conflict] = []

        loaded: list[tuple[str, MSB]] = []
        version_by_mod: dict[str, int] = {}
        for mod in mods:
            try:
                msb = MSB.from_path((mod.path / rel).as_posix())
                loaded.append((mod.name, msb))
                # Captured by main.py's patched `_unpack_supertype_list` —
                # see the comment there for why this can't just live on the
                # MSB instance (it uses __slots__).
                version_by_mod[mod.name] = msb_module.LAST_READ_VERSION
            except Exception as exc:
                self.log(f"MSB: could not load {mod.name}/{rel} — {exc}, skipping")

        if not loaded:
            raise RuntimeError("No MSB files could be loaded")

        if len(loaded) == 1:
            surviving_mod = next(m for m in mods if m.name == loaded[0][0])
            self._copy_winner(rel, [surviving_mod], output_path)
            return []

        winner_name, result_msb = loaded[0]

        # "parts"/"regions"/"events" aren't real MSB attributes — only the
        # specific subtype lists (`characters`, `other_regions`, ...) and the
        # `get_parts()`/`get_regions()`/`get_events()` getter methods exist.
        # The old `getattr(result_msb, category_name, None)` always silently
        # returned `None` here, so this whole loop body was a no-op on every
        # call — confirmed directly: a real two-mod conflict test (same
        # entity, different `translate` in each mod) reported 0 conflicts
        # and silently dropped the lower-priority mod's unique entities.
        category_getters = {
            "PARTS_PARAM_ST": "get_parts",
            "POINT_PARAM_ST": "get_regions",
            "EVENT_PARAM_ST": "get_events",
        }

        for supertype_name, getter_name in category_getters.items():
            result_entities = getattr(result_msb, getter_name)()
            # Regions/events commonly reuse the same `name` on purpose (e.g.
            # several identically-named multiplayer spawn points in one map
            # — confirmed directly: a real two-mod test with exactly one
            # genuine field change produced 23 EXTRA false-positive
            # conflicts, all on duplicate-named regions). A plain
            # `{name: entity}` dict silently collapses every duplicate down
            # to the last one, so every other same-named entity then gets
            # compared against the wrong sibling. Group by name and match
            # same-named entries positionally instead (1st occurrence vs
            # 1st occurrence, 2nd vs 2nd, ...) - not a perfect identity
            # model, but correct for the common case of N untouched
            # duplicates, and far better than comparing against a random
            # sibling.
            result_by_name: dict[str, list[object]] = {}
            for e in result_entities:
                result_by_name.setdefault(e.name, []).append(e)
            consumed_index: dict[str, int] = {}

            for mod_name, msb in loaded[1:]:
                other_entities = getattr(msb, getter_name)()

                for entity in other_entities:
                    candidates = result_by_name.get(entity.name)
                    idx = consumed_index.get(entity.name, 0)
                    if not candidates or idx >= len(candidates):
                        # Entity is unique to this lower-priority mod (or this
                        # mod has MORE same-named duplicates than the winner
                        # does). Append it to its REAL subtype list on
                        # `result_msb` (e.g. `result_msb.characters`) -
                        # `get_parts()` etc. just return a freshly-built flat
                        # IDList each call, not a live reference, so mutating
                        # it would do nothing.
                        try:
                            _migrate_model_reference(entity, result_msb)
                            subtype_info = result_msb.MSB_ENTRY_SUBTYPES[supertype_name][entity.SUBTYPE_ENUM]
                            target = getattr(result_msb, subtype_info.subtype_list_name)
                            target.append(entity)
                            result_by_name.setdefault(entity.name, []).append(entity)
                            self.log(
                                f"MSB: added {subtype_info.subtype_list_name} "
                                f"'{entity.name}' from {mod_name}"
                            )
                        except Exception as exc:
                            self.log(
                                f"MSB: cannot add '{entity.name}' from {mod_name} — {exc}"
                            )
                    else:
                        winner_entity = candidates[idx]
                        consumed_index[entity.name] = idx + 1
                        if _entity_differs(winner_entity, entity):
                            conflicts.append(Conflict(
                                type=ConflictType.MSB_ENTITY,
                                file=rel,
                                location=f"{supertype_name} '{entity.name}'",
                                resolved_by=winner_name,
                                values={
                                    winner_name: _describe_entity(winner_entity),
                                    mod_name:    _describe_entity(entity),
                                },
                                severity=Severity.WARNING,
                            ))

        dst = output_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        msb_module.WRITE_VERSION_OVERRIDE = version_by_mod.get(winner_name)
        try:
            result_msb.write(dst.as_posix())
        finally:
            msb_module.WRITE_VERSION_OVERRIDE = None
        return conflicts

    # ------------------------------------------------------------------ #


# ------------------------------------------------------------------ #

def _migrate_model_reference(entity, result_msb) -> None:
    """
    When free-merging a `Part` entity from a lower-priority mod's MSB tree,
    its `.model` attribute still points at a model object that belongs to
    THAT mod's own independently-loaded object graph, not `result_msb`'s.
    `try_index()` resolves references by identity (`is`), not equality, so
    writing the merged file would fail with "Could not find referenced
    entry ... in MSB list while packing" — confirmed directly with a real
    free-merge test (a part referencing a model not yet in the winner's
    model list crashed the write). Fix: re-point `.model` at the equivalent
    (same-name) model already in `result_msb`, or migrate the model object
    itself first if the winner doesn't have one by that name at all.
    """
    model = getattr(entity, "model", None)
    if model is None:
        return
    existing = next((m for m in result_msb.get_models() if m.name == model.name), None)
    if existing is not None:
        entity.model = existing
        return
    subtype_info = result_msb.MSB_ENTRY_SUBTYPES["MODEL_PARAM_ST"][model.SUBTYPE_ENUM]
    getattr(result_msb, subtype_info.subtype_list_name).append(model)


def _public_fields(entity) -> dict:
    """
    Return only the public (non-underscore) fields of an MSB entity.

    NOTE: every concrete MSB entry class is declared `@dataclass(slots=True)`,
    so `vars(entity)` always raises (no `__dict__`) and this always returns
    `{}` — `_entity_differs()` below falls back to `str(a) != str(b)` for
    the actual comparison, which works because these classes have a custom
    `__repr__` (declared with `repr=False` precisely so dataclass doesn't
    generate a content-blind default) that includes real field values.
    Kept as a best-effort first attempt in case a future soulstruct version
    drops slots.
    """
    try:
        return {k: v for k, v in vars(entity).items() if not k.startswith("_")}
    except Exception:
        return {}


def _entity_differs(a, b) -> bool:
    fa, fb = _public_fields(a), _public_fields(b)
    if fa and fb:
        return fa != fb
    # fallback if vars() didn't work
    return str(a) != str(b)


def _describe_entity(entity) -> str:
    """
    Short human-readable summary of an MSB entity for the conflict viewer.
    Shows model name and translate (position) so the user can see at a glance
    what's different between two conflicting versions.
    """
    try:
        parts = []
        for attr in ("model_name", "model", "character_id"):
            val = getattr(entity, attr, None)
            if val is not None:
                parts.append(str(val))
                break
        translate = getattr(entity, "translate", None)
        if translate is not None:
            parts.append(f"pos={tuple(round(v, 1) for v in translate)}")
        return ", ".join(parts) if parts else "<entity>"
    except Exception:
        return "<entity>"
