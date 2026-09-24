"""
Sound bank merger — handles Wwise .bnk files (sd/).

Two mods that both touch one bank used to mean one of them simply lost: banks were
copied whole by the generic pass, so a music replacer and a voice mod could not coexist
in the same bank. Measured on `cs_smain.bnk` (2026-09-23): the MGRR music replacer
changes 22 embedded sounds and 252 structure objects, Elden Vins changes 180 structure
objects and removes 2 sounds — and 72 of MGRR's edits are its alone. Whichever mod won,
those edits were thrown away.

A .bnk is a flat list of sections:

    BKHD   header
    DIDX   table of (audio_source_id, offset, size), 12 bytes each
    DATA   the embedded audio those offsets point into
    HIRC   the objects that tie sounds to events: {type, size, id, body}
    STID   bank name table

Both interesting sections are keyed by ID, so they merge the same way the text does:
compare each mod against VANILLA, and apply what that mod actually changed.

  - sound changed by one mod          → taken
  - sound changed by two mods         → highest priority wins, conflict recorded
  - sound removed by a mod            → removed, unless a higher-priority mod changed it
  - HIRC (the bank's structure)       → taken WHOLE from one mod, never mixed: the winner,
                                        or the other mod if the winner left it at vanilla
  - BKHD/STID                         → from the highest-priority mod

Without vanilla to compare against there is no way to tell an edit from an untouched
copy, so in that case the bank is copied from the winner, exactly as before.
"""
from __future__ import annotations
import struct
from pathlib import Path

from .base_merger import BaseMerger
from core.conflict import Conflict, ConflictType, Severity
from core.mod import Mod

_ALIGN = 16


def _sections(data: bytes) -> list[tuple[bytes, int, int]]:
    out, off = [], 0
    while off + 8 <= len(data):
        tag = data[off:off + 4]
        size = struct.unpack_from("<I", data, off + 4)[0]
        out.append((tag, off + 8, size))
        off += 8 + size
    return out


def _sounds(data: bytes) -> dict[int, bytes]:
    """{audio_source_id: payload} from the DIDX/DATA pair, in DIDX order."""
    didx = datao = None
    for tag, start, size in _sections(data):
        if tag == b"DIDX":
            didx = (start, size)
        elif tag == b"DATA":
            datao = start
    if didx is None or datao is None:
        return {}
    out = {}
    for i in range(didx[1] // 12):
        sid, off, size = struct.unpack_from("<III", data, didx[0] + i * 12)
        out[sid] = data[datao + off:datao + off + size]
    return out


def _hirc(data: bytes) -> dict[int, tuple[int, bytes]]:
    """{object_id: (type, body)} where body starts with the id, as stored."""
    for tag, start, size in _sections(data):
        if tag != b"HIRC":
            continue
        count = struct.unpack_from("<I", data, start)[0]
        out, off, end = {}, start + 4, start + size
        for _ in range(count):
            if off + 5 > end:
                break
            otype = data[off]
            osize = struct.unpack_from("<I", data, off + 1)[0]
            if off + 5 + osize > end:
                break
            oid = struct.unpack_from("<I", data, off + 5)[0]
            out[oid] = (otype, data[off + 5:off + 5 + osize])
            off += 5 + osize
        return out
    return {}


def _build(base: bytes, sounds: dict[int, bytes], hirc: dict[int, tuple[int, bytes]]) -> bytes:
    """Rebuild a bank from `base`'s section layout with new DIDX/DATA and HIRC."""
    didx = bytearray()
    blob = bytearray()
    for sid, payload in sounds.items():
        didx += struct.pack("<III", sid, len(blob), len(payload))
        blob += payload
        blob += b"\x00" * ((-len(payload)) % _ALIGN)
    hirc_body = bytearray(struct.pack("<I", len(hirc)))
    for oid, (otype, body) in hirc.items():
        hirc_body += bytes([otype]) + struct.pack("<I", len(body)) + body

    out = bytearray()
    for tag, start, size in _sections(base):
        if tag == b"DIDX":
            payload = bytes(didx)
        elif tag == b"DATA":
            payload = bytes(blob)
        elif tag == b"HIRC":
            payload = bytes(hirc_body)
        else:
            payload = base[start:start + size]
        out += tag + struct.pack("<I", len(payload)) + payload
    return bytes(out)


class BnkMerger(BaseMerger):

    def can_handle(self, mods: list[Mod]) -> bool:
        return any(len(owners) > 1 for owners in self._group_files(mods, "*.bnk").values())

    def merge(
        self,
        mods: list[Mod],
        output_path: Path,
        vanilla_path: Path | None = None,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for rel, owners in self._group_files(mods, "*.bnk").items():
            if len(owners) == 1:
                continue        # the generic pass copies it; nothing to decide
            van = (vanilla_path / rel) if vanilla_path else None
            if van is None or not van.exists():
                self.log(f"BNK: {rel} — no vanilla copy, {owners[0].name} wins whole file")
                self._copy_winner(rel, owners, output_path)
                conflicts.append(Conflict(
                    type=ConflictType.FILE_REPLACE, file=rel, location="entire bank",
                    resolved_by=owners[0].name,
                    values={m.name: "<bank>" for m in owners}, severity=Severity.WARNING))
                continue
            try:
                conflicts.extend(self._merge_bank(rel, owners, van, output_path))
            except Exception as exc:
                self.log(f"BNK: merge failed for {rel} ({exc}), copying {owners[0].name}")
                self._copy_winner(rel, owners, output_path)
                conflicts.append(Conflict(
                    type=ConflictType.FILE_REPLACE, file=rel, location="entire bank",
                    resolved_by=owners[0].name,
                    values={m.name: "<bank>" for m in owners}, severity=Severity.ERROR))
        return conflicts

    # ------------------------------------------------------------------ #

    def _merge_bank(
        self,
        rel: str,
        owners: list[Mod],
        vanilla_file: Path,
        output_path: Path,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        van_data = vanilla_file.read_bytes()
        van_snd, van_hirc = _sounds(van_data), _hirc(van_data)

        winner = owners[0]
        base = (winner.path / rel).read_bytes()
        snd, hirc = dict(_sounds(base)), dict(_hirc(base))
        # What the winner itself changed — a lower mod may not overwrite these silently.
        won_snd = {k for k, v in snd.items() if van_snd.get(k) != v}
        won_snd |= {k for k in van_snd if k not in snd}
        won_hirc = {k for k, v in hirc.items() if van_hirc.get(k) != v}
        won_hirc |= {k for k in van_hirc if k not in hirc}

        taken = clashed = 0
        for mod in owners[1:]:
            other = (mod.path / rel).read_bytes()
            o_snd, o_hirc = _sounds(other), _hirc(other)

            for sid, payload in o_snd.items():
                if van_snd.get(sid) == payload:
                    continue                    # untouched by this mod
                if sid in won_snd:
                    clashed += 1
                    conflicts.append(Conflict(
                        type=ConflictType.FILE_REPLACE, file=rel,
                        location=f"sound {sid}", resolved_by=winner.name,
                        values={winner.name: "<audio>", mod.name: "<audio>"},
                        severity=Severity.WARNING))
                    continue
                snd[sid] = payload
                taken += 1
            for sid in van_snd:
                if sid not in o_snd and sid not in won_snd and sid in snd:
                    del snd[sid]                # this mod removed it; winner did not touch it
                    taken += 1

            # HIRC is NOT merged object by object. Its objects reference each other -
            # segments, durations, playlists - so taking 72 objects from one mod and
            # keeping 30 from the other leaves a structure neither mod ever tested. Doing
            # exactly that to cs_smain.bnk silenced the game's music completely
            # (2026-09-23). Instead the whole chunk comes from ONE mod: the winner,
            # unless the winner never touched it and this mod did.
            if any(van_hirc.get(oid) != obj for oid, obj in o_hirc.items()) or                     any(oid not in o_hirc for oid in van_hirc):
                if not won_hirc:
                    hirc = dict(o_hirc)
                    taken += 1
                    self.log(f"BNK: {rel} — HIRC taken whole from {mod.name} "
                             f"({winner.name} left it at vanilla)")
                else:
                    clashed += 1
                    conflicts.append(Conflict(
                        type=ConflictType.FILE_REPLACE, file=rel,
                        location="HIRC (bank structure)", resolved_by=winner.name,
                        values={winner.name: "<structure>", mod.name: "<structure>"},
                        severity=Severity.WARNING))

        merged = _build(base, snd, hirc)
        dst = output_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(merged)
        self.log(f"BNK: {rel} merged {len(owners)} mods — {taken} edit(s) taken from lower "
                 f"priority, {clashed} kept from {winner.name}, "
                 f"{len(snd)} sounds, {len(hirc)} objects")
        return conflicts
