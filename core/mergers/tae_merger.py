"""
TAE animation merger — handles .anibnd.dcx files.

TAE (TimeAct Editor) files control animation events:
  - When a hitbox is active (iframes, attack frames)
  - When sound effects play
  - When particle effects spawn
  - When the game reads inputs (cancel windows)

File structure:
  mod/action/chr/c0000.anibnd.dcx   ← BND4 archive
    └─ c0000_a003.tae               ← one TAE per animation set
         └─ animation 3000          ← animation entry by ID
              └─ event group        ← e.g. "Hitbox" group
                   └─ event 0..N   ← individual timed events

Merge algorithm:
  For each .anibnd.dcx that appears in multiple mods:
    1. Open both archives, list their TAE entries
    2. For each TAE (by file name), compare animations by ID
    3. If same animation ID exists in both with different events → conflict
    4. Highest priority mod's version of the conflicting animation wins
    5. Animations only in a lower-priority mod are KEPT (no conflict = free merge)

Falls back to full-file copy if soulstruct can't parse the TAE format.
"""
from __future__ import annotations
import dataclasses
from pathlib import Path

from .base_merger import BaseMerger
from core.conflict import Conflict, ConflictType, Severity
from core.mod import Mod


class TaeMerger(BaseMerger):

    def can_handle(self, mods: list[Mod]) -> bool:
        return bool(self._group_files(mods, "*.anibnd.dcx"))

    def merge(
        self,
        mods: list[Mod],
        output_path: Path,
        vanilla_path: Path | None = None,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        file_map = self._group_files(mods, "*.anibnd.dcx")

        for rel, owners in file_map.items():
            if len(owners) == 1:
                # No conflict — just copy
                self._copy_winner(rel, owners, output_path)
                continue

            self.log(f"TAE: merging {rel} ({len(owners)} mods)")
            try:
                new_conflicts = self._merge_anibnd(
                    rel, owners, output_path
                )
                conflicts.extend(new_conflicts)
            except Exception as exc:
                self.log(f"TAE: smart merge failed for {rel} ({exc}), copying winner")
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

    def _merge_anibnd(
        self,
        rel: str,
        mods: list[Mod],
        output_path: Path,
    ) -> list[Conflict]:
        """
        Open each mod's .anibnd.dcx, compare TAE entries animation by animation.
        Write a merged archive to output_path/rel.
        """
        from soulstruct.containers import Binder as BND4
        from soulstruct.base.animations.tae import TAE

        conflicts: list[Conflict] = []

        # Load each mod independently — a corrupt BND skips that mod
        # rather than aborting the entire archive's merge.
        bnds: list[tuple[str, BND4]] = []
        for mod in mods:
            try:
                bnd = BND4.from_path((mod.path / rel).as_posix())
                bnds.append((mod.name, bnd))
            except Exception as exc:
                self.log(f"TAE: could not load {mod.name}/{rel} — {exc}, skipping")

        if not bnds:
            raise RuntimeError("No .anibnd.dcx files could be loaded")

        if len(bnds) == 1:
            surviving_mod = next(m for m in mods if m.name == bnds[0][0])
            self._copy_winner(rel, [surviving_mod], output_path)
            return []

        winner_name, result_bnd = bnds[0]

        # Parse every TAE entry in the winner's archive.
        # entry_map lets us write modified TAE bytes back in O(1).
        # modified_taes tracks which ones actually changed so we only
        # re-serialise what needs re-serialising.
        result_taes: dict[str, object] = {}
        entry_map:   dict[str, object] = {}
        winner_tae_count = 0
        for entry in result_bnd.entries:
            entry_map[entry.name] = entry       # every entry, not just .tae — see below
            if entry.name.lower().endswith(".tae"):
                winner_tae_count += 1
                try:
                    result_taes[entry.name] = TAE.from_bytes(entry.data)
                except Exception:
                    pass

        if winner_tae_count and not result_taes:
            # Every single .tae entry failed to parse — this is a structural
            # soulstruct/TAE-format incompatibility, not a content-specific
            # parse failure for one file. Raise so the caller's fallback to
            # copy-winner + a logged ERROR conflict actually triggers, instead
            # of silently "succeeding" with 0 conflicts while every entry
            # from every lower-priority mod gets quietly dropped below.
            raise RuntimeError(
                f"{winner_tae_count} .tae entries found in {winner_name}'s archive "
                f"but none could be parsed by soulstruct — TAE format support is "
                f"broken in this environment, not specific to this file's content."
            )

        modified_taes: set[str] = set()

        for mod_name, bnd in bnds[1:]:
            mod_tae_total = 0
            mod_tae_failed = 0
            for entry in bnd.entries:
                if not entry.name.lower().endswith(".tae"):
                    continue
                mod_tae_total += 1
                try:
                    other_tae = TAE.from_bytes(entry.data)
                except Exception:
                    mod_tae_failed += 1
                    # soulstruct cannot read every TAE variant (69 of Elden Vins' 153
                    # own .tae files in c0000.anibnd, 2026-09-23). A file the winner
                    # does not have at all is still better carried across unparsed than
                    # dropped - there is nothing to merge it with.
                    if entry.name not in entry_map:
                        result_bnd.entries.append(entry)
                        entry_map[entry.name] = entry
                        self.log(f"TAE: added {entry.name} from {mod_name} unparsed")
                    continue

                if entry.name not in entry_map:
                    # TAE file only in this lower-priority mod — add it wholesale
                    result_bnd.entries.append(entry)
                    entry_map[entry.name] = entry
                    result_taes[entry.name] = other_tae
                    self.log(f"TAE: added {entry.name} from {mod_name}")
                    continue
                if entry.name not in result_taes:
                    # The winner HAS this file but soulstruct could not parse its copy.
                    # Checking `result_taes` here (parsed files only) instead of the full
                    # entry list appended a second copy beside it: the merged
                    # c0000.anibnd.dcx ended up with 84 duplicated entries sharing 84
                    # duplicated entry ids (2026-09-23). Keep the winner's file.
                    continue

                result_tae    = result_taes[entry.name]
                result_anim_ids = {a.animation_id for a in result_tae.animations}
                other_anim_ids  = {a.animation_id for a in other_tae.animations}

                # Animations only in the lower-priority mod → free merge
                for anim in other_tae.animations:
                    if anim.animation_id not in result_anim_ids:
                        result_tae.animations.append(anim)
                        modified_taes.add(entry.name)
                        self.log(f"TAE: added anim {anim.animation_id} from {mod_name} ({entry.name})")

                # Animations in both → conflict if content differs.
                # Compared via `_tae_animations_differ()` (semantic fields
                # only, ignores `_pad`-prefixed filler bytes) rather than
                # plain `==` — two mods' authoring tools can legitimately
                # leave different garbage in unused padding bytes for an
                # animation neither of them actually changed, and a naive
                # full dataclass comparison would flag that as a false
                # conflict. Confirmed directly: re-serialising a real TAE
                # through this app's own (newly-implemented) writer zeroes
                # pad bytes the original vanilla file had as non-zero
                # garbage — semantically identical, but `==` would call it
                # a conflict on every single animation in the file.
                # Wrapped per-pair: a single unserializable animation won't
                # abort the merge for the entire archive.
                for anim_id in result_anim_ids & other_anim_ids:
                    result_anim = next(a for a in result_tae.animations if a.animation_id == anim_id)
                    other_anim  = next(a for a in other_tae.animations  if a.animation_id == anim_id)
                    try:
                        differ = _tae_animations_differ(result_anim, other_anim)
                    except Exception:
                        differ = True  # can't compare → conservatively flag as conflict
                    if differ:
                        conflicts.append(Conflict(
                            type=ConflictType.TAE_ANIM,
                            file=rel,
                            location=f"{entry.name} animation {anim_id}",
                            resolved_by=winner_name,
                            values={
                                winner_name: _describe_anim(result_anim),
                                mod_name:    _describe_anim(other_anim),
                            },
                            severity=Severity.WARNING,
                        ))

            # An .anibnd is not only TAE. The bulk of it is the .hkx animation data the
            # TAE events refer to, and until now only .tae entries were looked at: the
            # archive written out was the WINNER's, so every animation a lower-priority
            # mod added was dropped. Merging Elden Vins (20.8 MB, its movesets) under the
            # Honda Torrent mod (11.5 MB) produced a 12.9 MB c0000.anibnd.dcx, and the
            # game crashed at a black screen because params still pointed at the missing
            # animations (2026-09-23). Free-merge those entries; the winner keeps any
            # file both mods ship.
            added = kept = 0
            for entry in bnd.entries:
                if entry.name.lower().endswith(".tae"):
                    continue
                existing = entry_map.get(entry.name)
                if existing is None:
                    result_bnd.entries.append(entry)
                    entry_map[entry.name] = entry
                    added += 1
                elif bytes(existing.data) != bytes(entry.data):
                    kept += 1
                    conflicts.append(Conflict(
                        type=ConflictType.FILE_REPLACE,
                        file=rel,
                        location=f"{entry.name} (animation data)",
                        resolved_by=winner_name,
                        values={winner_name: "<entry>", mod_name: "<entry>"},
                        severity=Severity.WARNING,
                    ))
            if added or kept:
                self.log(f"TAE: {rel} — {added} file(s) taken from {mod_name}, "
                         f"{kept} kept from {winner_name}")

            if mod_tae_total and mod_tae_failed == mod_tae_total:
                # Every .tae entry from this specific mod failed to parse —
                # its animation changes for this archive were entirely
                # dropped. Not raised (the winner's own entries already
                # parsed fine, so the rest of the merge is still valid) but
                # logged loudly so this isn't a silent loss like before.
                self.log(f"TAE: WARNING — {mod_name}'s {mod_tae_total} .tae entries in {rel} "
                         f"all failed to parse; none of its animation changes were merged.")

        # Write modified TAE bytes back into their BND entries before saving.
        # Without this step the BND still holds the original bytes and all
        # animation additions above are silently discarded.
        for tae_name in modified_taes:
            try:
                entry_map[tae_name].data = bytes(result_taes[tae_name])
            except Exception as exc:
                self.log(f"TAE: could not serialise {tae_name} — {exc}")

        dst = output_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        result_bnd.write(dst.as_posix())
        return conflicts


def _tae_animations_differ(a, b) -> bool:
    """Semantic comparison of two `TAEAnimation` objects, ignoring `_pad`
    fields (cosmetic filler — different authoring tools can leave different
    garbage there for content neither mod actually changed)."""
    if a.animation_id != b.animation_id:
        return True
    if len(a.events) != len(b.events) or len(a.event_groups) != len(b.event_groups):
        return True
    if a.is_animation_file_reference != b.is_animation_file_reference:
        return True
    if a.animation_file_name != b.animation_file_name:
        return True
    for e1, e2 in zip(a.events, b.events):
        if e1.event_type != e2.event_type or e1.start_time != e2.start_time or e1.end_time != e2.end_time:
            return True
        d1, d2 = e1.event_data, e2.event_data
        if type(d1) is not type(d2):
            return True
        for f in dataclasses.fields(d1):
            if f.name.startswith("_"):
                continue
            if getattr(d1, f.name) != getattr(d2, f.name):
                return True
    for g1, g2 in zip(a.event_groups, b.event_groups):
        if g1.event_type != g2.event_type or g1.event_indices != g2.event_indices:
            return True
    return False


def _describe_anim(anim) -> str:
    """Short summary of a TAE animation for the conflict viewer."""
    try:
        event_count = sum(
            len(getattr(g, "events", []))
            for g in getattr(anim, "event_groups", [])
        )
        return f"id={anim.animation_id}, {event_count} events"
    except Exception:
        return f"id={getattr(anim, 'animation_id', '?')}"

