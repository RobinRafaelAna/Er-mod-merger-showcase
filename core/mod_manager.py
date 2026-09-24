"""
ModManager: owns the ordered list of mods and project-level paths.

Key idea: mods[0] is HIGHEST priority, mods[-1] is LOWEST.
The GUI shows them top-to-bottom in this order (like most mod managers).
Changing priority = moving items up/down in this list.

Saves/loads to a JSON config file so the user's session persists.
"""
from __future__ import annotations
import json
from pathlib import Path
from .mod import Mod
from .version_detector import detect_version


class ModManager:
    def __init__(self):
        self.mods: list[Mod] = []           # ordered: index 0 = highest priority
        self.vanilla_path: Path | None = None   # optional: game's data folder
        self.output_path: Path | None = None    # where merged files go
        self.me3_profile_path: Path | None = None  # optional: ME3 .me3 profile

    # ------------------------------------------------------------------ #
    # Adding / removing mods
    # ------------------------------------------------------------------ #

    def add_mod(self, path: str | Path) -> Mod:
        """Add a mod folder. New mods start with the LOWEST priority."""
        path = Path(path).resolve()
        if not path.is_dir():
            raise ValueError(f"Not a directory: {path}")
        if any(m.path == path for m in self.mods):
            raise ValueError(f"Mod already added: {path}")
        mod = Mod(name=path.name, path=path, priority=len(self.mods))

        # Try to detect the game version this mod was built for.
        # detect_version() is safe to call — returns "unknown" on any failure.
        if mod.regulation_bin is not None:
            mod.regulation_version = detect_version(mod.regulation_bin)

        self.mods.append(mod)
        return mod

    def remove_mod(self, index: int) -> None:
        self.mods.pop(index)
        self._renumber()

    # ------------------------------------------------------------------ #
    # Reordering (priority)
    # ------------------------------------------------------------------ #

    def move_up(self, index: int) -> None:
        """Move a mod one step higher in priority (closer to index 0)."""
        if index > 0:
            self.mods.insert(index - 1, self.mods.pop(index))
            self._renumber()

    def move_down(self, index: int) -> None:
        """Move a mod one step lower in priority."""
        if index < len(self.mods) - 1:
            self.mods.insert(index + 1, self.mods.pop(index))
            self._renumber()

    def _renumber(self) -> None:
        """Keep mod.priority in sync with list position."""
        for i, mod in enumerate(self.mods):
            mod.priority = i

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def enabled_mods(self) -> list[Mod]:
        return [m for m in self.mods if m.enabled]

    def conflicts_exist(self) -> bool:
        """Quick check: do any two mods share a file path?"""
        return bool(self.file_conflicts())

    def file_conflicts(self) -> dict[str, list[str]]:
        """
        Returns {relative_path: [mod_names]} for every file that appears
        in more than one enabled mod.  Used by the UI to preview conflicts
        before running the merge.
        """
        seen: dict[str, list[str]] = {}
        for mod in self.enabled_mods():
            for rel in mod.all_relative_files():
                seen.setdefault(rel.lower(), []).append(mod.name)
        return {path: mods for path, mods in seen.items() if len(mods) > 1}

    # ------------------------------------------------------------------ #
    # Persistence (JSON config)
    # ------------------------------------------------------------------ #

    def save(self, config_path: str | Path) -> None:
        config_path = Path(config_path)
        data = {
            "vanilla_path":     self.vanilla_path.as_posix()     if self.vanilla_path     else None,
            "output_path":      self.output_path.as_posix()      if self.output_path      else None,
            "me3_profile_path": self.me3_profile_path.as_posix() if self.me3_profile_path else None,
            "mods": [
                {
                    "name":               mod.name,
                    "path":               mod.path.as_posix(),
                    "priority":           mod.priority,
                    "enabled":            mod.enabled,
                    "regulation_version": mod.regulation_version,
                    "notes":              mod.notes,
                }
                for mod in self.mods
            ],
        }
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load(self, config_path: str | Path) -> None:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.vanilla_path     = Path(data["vanilla_path"])     if data.get("vanilla_path")     else None
        self.output_path      = Path(data["output_path"])      if data.get("output_path")      else None
        self.me3_profile_path = Path(data["me3_profile_path"]) if data.get("me3_profile_path") else None
        self.mods = [
            Mod(
                name=m["name"],
                path=Path(m["path"]),
                priority=m["priority"],
                enabled=m.get("enabled", True),
                regulation_version=m.get("regulation_version"),
                notes=m.get("notes", ""),
            )
            for m in data.get("mods", [])
        ]
