"""
EMEVD event script merger — handles .emevd.dcx files.

EMEVD files contain the event scripts that drive almost all dynamic behaviour:
  - Boss triggers and scripted sequences
  - Item pickup spawns
  - Fog wall / door logic
  - NPC dialogue triggers
  - Map hazards

Each .emevd.dcx file corresponds to one map area (e.g. m60_00_00_00 = Limgrave).
Inside are numbered Events; each Event is a sequence of instructions.

Merge algorithm:
  For each .emevd.dcx that appears in multiple mods:
    1. Load both EMEVD files with soulstruct
    2. Collect events by ID from each mod
    3. Events only in one mod → free merge (include them)
    4. Same event ID in both → compare instructions
       - Different instructions = conflict, highest priority wins
       - Identical instructions = no conflict, keep once
  Falls back to full-file copy if soulstruct can't parse.
"""
from __future__ import annotations
from pathlib import Path

from .base_merger import BaseMerger
from core.conflict import Conflict, ConflictType, Severity
from core.mod import Mod


class EmevdMerger(BaseMerger):

    def can_handle(self, mods: list[Mod]) -> bool:
        return bool(self._group_files(mods, "*.emevd.dcx"))

    def merge(
        self,
        mods: list[Mod],
        output_path: Path,
        vanilla_path: Path | None = None,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        file_map = self._group_files(mods, "*.emevd.dcx")

        for rel, owners in file_map.items():
            if len(owners) == 1:
                self._copy_winner(rel, owners, output_path)
                continue

            self.log(f"EMEVD: merging {rel} ({len(owners)} mods)")
            try:
                new_conflicts = self._merge_emevd(rel, owners, output_path)
                conflicts.extend(new_conflicts)
            except Exception as exc:
                self.log(f"EMEVD: smart merge failed for {rel} ({exc}), copying winner")
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

    def _merge_emevd(
        self,
        rel: str,
        mods: list[Mod],
        output_path: Path,
    ) -> list[Conflict]:
        from soulstruct.eldenring.events.emevd import EMEVD

        conflicts: list[Conflict] = []

        # Load each mod independently so one corrupt file doesn't abort the
        # whole merge — bad files are skipped with a log entry.
        loaded: list[tuple[str, EMEVD]] = []
        for mod in mods:
            try:
                emevd = EMEVD.from_path((mod.path / rel).as_posix())
                loaded.append((mod.name, emevd))
            except Exception as exc:
                self.log(f"EMEVD: could not load {mod.name}/{rel} — {exc}, skipping")

        if not loaded:
            raise RuntimeError("No EMEVD files could be loaded")

        if len(loaded) == 1:
            # Only one mod loaded successfully — nothing to merge, just copy it
            surviving_mod = next(m for m in mods if m.name == loaded[0][0])
            self._copy_winner(rel, [surviving_mod], output_path)
            return []

        winner_name, result_emevd = loaded[0]

        # Build lookup: event_id → Event for the winner
        result_events: dict[int, object] = {e.event_id: e for e in result_emevd.events}

        for mod_name, emevd in loaded[1:]:
            for event in emevd.events:
                eid = event.event_id
                if eid not in result_events:
                    # Unique to this mod — add it
                    result_emevd.events.append(event)
                    result_events[eid] = event
                    self.log(f"EMEVD: added event {eid} from {mod_name}")
                else:
                    # Same ID in both — check if they differ
                    winner_event = result_events[eid]
                    winner_bytes = _event_bytes(winner_event)
                    other_bytes  = _event_bytes(event)
                    if winner_bytes != other_bytes:
                        conflicts.append(Conflict(
                            type=ConflictType.EMEVD_EVENT,
                            file=rel,
                            location=f"event {eid}",
                            resolved_by=winner_name,
                            values={
                                winner_name: _describe_event(winner_event),
                                mod_name:    _describe_event(event),
                            },
                            severity=Severity.WARNING,
                        ))

        dst = output_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        result_emevd.write(dst.as_posix())
        return conflicts

    # ------------------------------------------------------------------ #


def _event_bytes(event) -> bytes:
    """
    Serialize an event to bytes for comparison.

    Tries multiple approaches in order of reliability.
    The old str(event).encode() fallback was removed because soulstruct's
    __str__ can include memory addresses, making two identical events compare
    as different and producing false conflict reports.
    """
    # Preferred: soulstruct's binary serializer
    try:
        return bytes(event)
    except Exception:
        pass
    # Fallback: pack the instruction list as a repr of (opcode, args) tuples,
    # which is deterministic and doesn't include object addresses.
    # Use explicit is-not-None check — `or` would treat an empty instruction
    # list [] as falsy and incorrectly skip to evs_str for 0-instruction events.
    try:
        instructions = getattr(event, "instructions", None)
        if instructions is None:
            instructions = getattr(event, "evs_str", None)
        if instructions is not None:
            return repr(instructions).encode()
    except Exception:
        pass
    # Last resort: compare event ID + instruction count only.
    # Uses id(event) as a tiebreaker so two events that both fail all
    # serialization never silently compare as equal — that would cause
    # real conflicts to be missed entirely.
    try:
        eid   = getattr(event, "event_id", 0)
        count = len(getattr(event, "instructions", []))
        return f"{eid}:{count}:{id(event)}".encode()
    except Exception:
        return str(id(event)).encode()


def _describe_event(event) -> str:
    """
    Return a short human-readable description of an event for the conflict viewer.
    Shows instruction count and restart type so the user can gauge how different
    two conflicting versions are at a glance.
    """
    try:
        instr_count  = len(getattr(event, "instructions", []))
        restart_type = getattr(event, "restart_type", None)
        restart_str  = f", restart={restart_type}" if restart_type is not None else ""
        return f"{instr_count} instructions{restart_str}"
    except Exception:
        return "<event>"
