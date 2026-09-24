"""
Represents a single Elden Ring mod folder.

A mod is just a directory that mirrors the game's file structure.
ME3 (and other mod loaders) load these folders and patch files into the game at runtime.
Our tool reads them and merges them into one combined output folder.

Priority: lower number = higher priority (0 wins over 1, etc.)
The user controls this order via the GUI drag-drop list.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


def is_scaffolding(rel: str) -> bool:
    """Build leftovers that live inside a mod folder but are not part of the mod.

    Every build keeps its rollback backups in `_tyo/` - whole .bnk, .msgbnd and .wem
    copies. The game never looks at them (ME3 asks for known game paths only), but the
    mergers walk the whole folder, so they were being copied into the merged package and
    even picked up by the `*.bnk` / `*.msgbnd.dcx` scans. Anything under a top-level
    folder starting with "_" is scaffolding, as is Smithbox's project directory.
    """
    first = rel.split("/", 1)[0]
    return first.startswith("_") or first in (".smithbox", ".git")


@dataclass
class Mod:
    name: str
    path: Path
    priority: int = 0      # position in load order (0 = highest priority)
    enabled: bool = True   # can be toggled without removing from list
    regulation_version: str | None = None  # detected game patch version, e.g. "schema-3F2A"
    notes: str = ""                         # user-editable notes shown as tooltip

    # ------------------------------------------------------------------ #
    # File discovery helpers
    # ------------------------------------------------------------------ #

    def has_file(self, relative: str) -> bool:
        """Check whether this mod contains a given path (e.g. 'regulation.bin')."""
        return (self.path / relative).exists()

    def get_file(self, relative: str) -> Path | None:
        """Return the absolute Path to a file inside this mod, or None."""
        p = self.path / relative
        return p if p.exists() else None

    def all_relative_files(self) -> list[str]:
        """
        Walk the mod folder and return all file paths relative to the mod root.
        Used to find which files this mod provides (and therefore which other mods
        it might conflict with).
        """
        return [
            rel for rel in (p.relative_to(self.path).as_posix()
                            for p in self.path.rglob("*") if p.is_file())
            if not is_scaffolding(rel)
        ]

    # ------------------------------------------------------------------ #
    # Convenience properties for the most common mod files
    # ------------------------------------------------------------------ #

    @property
    def regulation_bin(self) -> Path | None:
        """regulation.bin contains all game params (stats, weapons, spells, etc.)"""
        return self.get_file("regulation.bin")

    @property
    def event_dir(self) -> Path | None:
        """event/ folder holds .emevd.dcx event scripts (boss triggers, spawns)."""
        p = self.path / "event"
        return p if p.is_dir() else None

    @property
    def msg_dir(self) -> Path | None:
        """msg/ folder holds .msgbnd.dcx message/text files."""
        p = self.path / "msg"
        return p if p.is_dir() else None

    @property
    def map_dir(self) -> Path | None:
        """map/ folder holds .msb.dcx map layout files."""
        p = self.path / "map"
        return p if p.is_dir() else None

    @property
    def action_dir(self) -> Path | None:
        """action/ folder holds .anibnd.dcx animation TAE files."""
        p = self.path / "action"
        return p if p.is_dir() else None

    def __repr__(self) -> str:
        status = "ON" if self.enabled else "OFF"
        return f"Mod[{self.priority}] {self.name!r} ({status})"
