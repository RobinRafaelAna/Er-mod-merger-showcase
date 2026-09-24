"""
Represents a detected conflict between two or more mods.

A conflict means: multiple mods tried to change the same thing.
The merger resolves it automatically (highest priority wins), but we record
every conflict so the user can review what was overridden.

Examples of conflicts:
  - Two mods both edit NpcParam row 1000 field 'hp'
  - Two mods both replace the same .emevd.dcx event script
  - Two mods both add animation events to the same TAE animation ID
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class ConflictType(Enum):
    PARAM_FIELD  = "Param field"      # regulation.bin row/field
    TAE_ANIM     = "TAE animation"    # animation timing event
    EMEVD_EVENT  = "Event script"     # .emevd.dcx event ID
    MSB_ENTITY   = "Map entity"       # .msb.dcx part/region/event
    MSG_ENTRY    = "Message entry"    # .msgbnd.dcx string
    FILE_REPLACE = "File (full)"      # whole file replaced, no smart merge


class Severity(Enum):
    INFO    = "info"     # informational — no actual data loss
    WARNING = "warning"  # one mod's changes overrode another's
    ERROR   = "error"    # merge failed, fell back to copy


@dataclass
class Conflict:
    type: ConflictType

    # Which file the conflict is in (e.g. "regulation.bin/NpcParam")
    file: str

    # Human-readable location within the file (e.g. "row 1000, field 'hp'")
    location: str

    # Which mod's value was used (the winner)
    resolved_by: str

    # All mods involved and their values at this location
    # Keys = mod names, values = string representation of the value
    values: dict[str, str] = field(default_factory=dict)

    severity: Severity = Severity.WARNING

    def __str__(self) -> str:
        val_summary = " | ".join(f"{k}={v}" for k, v in self.values.items())
        return (
            f"[{self.severity.value.upper()}] {self.file} @ {self.location} "
            f"→ {self.resolved_by} wins ({val_summary})"
        )
