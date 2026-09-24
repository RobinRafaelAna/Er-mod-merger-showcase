"""
version_detector.py — figures out which game patch a regulation.bin was built for.

Why this matters:
  FromSoftware patches the game and changes regulation.bin each time — new rows,
  new fields, sometimes renumbered rows.  If Mod A was built on patch 1.09 and
  Mod B was built on patch 1.12, merging them field-by-field can produce garbage.
  We need to detect this early and warn the user before any merge runs.

How version detection works (in order of reliability):
  1. Direct version attribute on the soulstruct GameParamBND object
  2. Schema fingerprint — hash of (param names + field counts per param).
     Two regulation.bins with identical schemas are almost certainly the same version.
     Two with different schemas are definitely different versions.

The fingerprint approach means we don't need a lookup table of known patch versions.
If two mods produce different fingerprints, we know they differ and can report it.
"""
from __future__ import annotations
import hashlib
from pathlib import Path


def detect_version(regulation_bin_path: str | Path) -> str:
    """
    Load a regulation.bin and return a version string.
    Returns "unknown" if soulstruct can't load it.
    Returns a "schema-XXXX" fingerprint if no explicit version is found.
    """
    try:
        from soulstruct.eldenring.params.gameparambnd import GameParamBND
        bnd = GameParamBND.from_path(Path(regulation_bin_path).as_posix())
        return _read_version(bnd)
    except Exception:
        return "unknown"


def _read_version(bnd) -> str:
    """
    Try every known soulstruct attribute that might hold a version, then
    fall back to building a schema fingerprint.
    """
    # Attempt 1 — direct attributes soulstruct might expose
    for attr in ("game_version", "version", "regulation_version"):
        try:
            v = getattr(bnd, attr, None)
            if v is not None:
                cleaned = str(v).strip("\x00 \t")
                if cleaned and cleaned != "0":
                    return cleaned
        except Exception:
            pass

    # Attempt 2 — some soulstruct builds nest a BND4 inside
    for inner_attr in ("bnd", "_bnd"):
        try:
            inner = getattr(bnd, inner_attr, None)
            if inner is not None:
                v = getattr(inner, "version", None)
                if v:
                    cleaned = str(v).strip("\x00 \t")
                    if cleaned and cleaned != "0":
                        return cleaned
        except Exception:
            pass

    # Attempt 3 — schema fingerprint (most reliable fallback)
    return _schema_fingerprint(bnd)


def _schema_fingerprint(bnd) -> str:
    """
    Build a short hash from (param names + field count per param).
    Same game version → same fingerprint.
    Different game version → almost certainly different fingerprint.
    """
    try:
        parts: list[str] = []
        for param_name in sorted(bnd.params.keys()):
            param = bnd.params[param_name]
            if param.rows:
                field_count = len([
                    k for k in vars(param.rows[0]).keys()
                    if not k.startswith("_") and k not in ("ID", "name")
                ])
                parts.append(f"{param_name}:{field_count}")
        if parts:
            # Use a deterministic hash — Python's built-in hash() is
            # randomized per-process so it changes every run, making
            # fingerprints useless for comparing across sessions.
            digest = hashlib.md5("\n".join(parts).encode()).hexdigest()
            return f"schema-{digest[:4].upper()}"
    except Exception:
        pass
    return "unknown"


# ------------------------------------------------------------------ #

def compare_schemas(
    bnd_a,
    name_a: str,
    bnd_b,
    name_b: str,
) -> list[str]:
    """
    Compare the param schemas of two loaded GameParamBND objects.
    Returns a list of human-readable mismatch descriptions (empty = identical schemas).

    Each entry in the returned list is one problem:
      - A param table that exists in one mod but not the other
      - A param table where the two mods have different field counts
    """
    mismatches: list[str] = []

    try:
        names_a = set(bnd_a.params.keys())
        names_b = set(bnd_b.params.keys())

        only_a = names_a - names_b
        only_b = names_b - names_a
        shared = names_a & names_b

        if only_a:
            mismatches.append(
                f"  Param tables only in {name_a}: {', '.join(sorted(only_a))}"
            )
        if only_b:
            mismatches.append(
                f"  Param tables only in {name_b}: {', '.join(sorted(only_b))}"
            )

        for param_name in sorted(shared):
            fields_a = _field_names(bnd_a.params[param_name])
            fields_b = _field_names(bnd_b.params[param_name])
            if fields_a != fields_b:
                only_in_a = fields_a - fields_b
                only_in_b = fields_b - fields_a
                details = []
                if only_in_a:
                    details.append(f"+{len(only_in_a)} fields in {name_a}")
                if only_in_b:
                    details.append(f"+{len(only_in_b)} fields in {name_b}")
                mismatches.append(
                    f"  {param_name}: schema differs ({', '.join(details)})"
                )
    except Exception as exc:
        mismatches.append(f"  Schema comparison failed: {exc}")

    return mismatches


def _field_names(param) -> set[str]:
    try:
        if param.rows:
            return {
                k for k in vars(param.rows[0]).keys()
                if not k.startswith("_") and k not in ("ID", "name")
            }
    except Exception:
        pass
    return set()
