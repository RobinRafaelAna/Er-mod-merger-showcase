"""
Runtime fixes for soulstruct 2.3.2 (Elden Ring file formats).

soulstruct reads and writes FromSoftware's binary formats, but several of its Elden
Ring writers produced files the game crashed on, or files other tools (SoulsFormats,
Smithbox, the Item and Enemy Randomizer) could not read. Each block below documents
the bug it fixes, how it was found and how the fix was verified, then patches the
affected soulstruct function at import time.

Import this module once, before using the mergers:

    import soulstruct_patches  # noqa: F401

Some fixes re-implement a soulstruct function with a small change; those parts are
derived from soulstruct (GPL-3.0-or-later), which is why this repository is GPL-3.0.
"""
import os
import sys
from pathlib import Path

# soulstruct 2.3.2 ships without several JSON data files it tries to load at
# import time — including eldenring/events/emevd/er-common.emedf.json, the
# table that supplies argument types for "common" EMEVD instructions (e.g.
# class 2000 "System", class 2009 "Script"). Without it, EMEVD.from_path()
# fails on real vanilla event files with "Cannot find argument types for
# instruction ...". The previous fix here no-op'd add_common_emedf_info()
# entirely, which avoided the import-time crash but meant ALL common
# instructions silently had no argument types — not actually fixed.
#
# Real fix: bundle a copy of this exact JSON (core/mergers/data/er-common.emedf.json,
# sourced from Smithbox's identically-formatted, actively-maintained bundled copy —
# confirmed byte-for-byte schema match against soulstruct's own
# add_common_emedf_info() parser: same "main_classes"/"instrs"/"args" shape,
# and the two specific instructions that failed to load this session,
# 2000[00] and 2009[03], are both present and correctly defined in it) and
# serve it whenever soulstruct's own (missing) copy is requested. The
# add_common_emedf_info() call itself is no longer stubbed out — it now runs
# for real, using this bundled file.
try:
    import pathlib as _pl
    import soulstruct.utilities.files as _ss_files
    _SS_PKG = _pl.Path(_ss_files.__file__).parent.parent
    _BUNDLED_EMEDF = {
        "er-common.emedf.json": _pl.Path(__file__).resolve().parent / "core" / "mergers" / "data" / "er-common.emedf.json",
    }
    _orig_rj = _ss_files.read_json
    def _safe_rj(path, *a, **kw):
        if path is None:
            return {}
        p = _pl.Path(path)
        if not p.exists() and _SS_PKG in p.parents:
            bundled = _BUNDLED_EMEDF.get(p.name)
            if bundled and bundled.exists():
                return _orig_rj(bundled, *a, **kw)
            return {}
        return _orig_rj(path, *a, **kw)
    _ss_files.read_json = _safe_rj

    # Other games' (DS1, etc.) common-emedf JSON files are still genuinely
    # missing and not bundled here (this app only touches Elden Ring) — skip
    # gracefully instead of crashing on common_emedf_raw["main_classes"] for
    # an empty dict.
    #
    # Also: soulstruct's own hardcoded ER instruction catalog has at least
    # one instruction (found this session: category 3, index 46) that isn't
    # present in the bundled JSON snapshot — a real content gap between two
    # community-maintained sources, not something fixable by picking a
    # "more correct" one. The original add_common_emedf_info() hard-raises
    # on the first such mismatch, aborting the entire pass (leaving every
    # later instruction in dict iteration order completely unprocessed, not
    # just the one that's actually missing). Reimplemented here to skip only
    # the specific instruction that can't be resolved and keep going —
    # everything the bundled JSON does cover (confirmed: includes the two
    # instructions that were actually blocking real EMEVD files this
    # session, 2000[00] and 2009[03]) still gets its argument types filled
    # in correctly.
    import soulstruct.base.events.emevd.emedf as _ss_emedf
    from soulstruct.base.events.emevd.emedf import ArgType as _ArgType
    def _tolerant_acei(emedf, common_emedf_path):
        common_emedf_raw = _ss_files.read_json(common_emedf_path)
        if not common_emedf_raw:
            return
        common_emedf = {}
        for cat_dict in common_emedf_raw["main_classes"]:
            category = cat_dict["index"]
            for instr_dict in cat_dict["instrs"]:
                common_emedf[category, instr_dict["index"]] = instr_dict
        for (category, index), info in emedf.items():
            instr = common_emedf.get((category, index))
            if instr is None or len(info["args"]) != len(instr["args"]):
                continue  # not in this snapshot, or arg count disagrees — leave as-is
            for i, arg_name in enumerate(info["args"]):
                if "internal_type" in instr["args"][i]:
                    continue
                try:
                    instr_type = _ArgType(int(instr["args"][i]["type"]))
                except ValueError:
                    continue
                info["args"][arg_name] = info["args"][arg_name].copy() | {"internal_type": instr_type}
    _ss_emedf.add_common_emedf_info = _tolerant_acei
except Exception:
    pass

# soulstruct FMG.to_writer passes `unknown1` to object_to_writer but none of
# the FMGHeader classes (V0/V1/V2) have that field — it was removed from the
# structs without updating the writer.  Override object_to_writer on each
# header class as a proper classmethod that strips the stale kwarg first.
try:
    from soulstruct.base.text.fmg import FMGHeaderV0, FMGHeaderV1, FMGHeaderV2
    from constrata import BinaryStruct as _BinaryStruct
    _parent_otw = _BinaryStruct.object_to_writer.__func__
    def _make_fmg_header_otw(parent):
        @classmethod
        def _otw(cls, obj, writer=None, byte_order=None, long_varints=None, **field_values):
            field_values.pop("unknown1", None)
            return parent(cls, obj, writer, byte_order, long_varints, **field_values)
        return _otw
    _patched_otw = _make_fmg_header_otw(_parent_otw)
    for _cls in (FMGHeaderV0, FMGHeaderV1, FMGHeaderV2):
        _cls.object_to_writer = _patched_otw
    del _cls, _patched_otw, _make_fmg_header_otw, _parent_otw
except Exception:
    pass

# soulstruct FMG.from_reader hard-raises on duplicate text entry IDs, but
# modded FMGs contain them routinely (e.g. Convergence's item_dlc02
# WeaponName.fmg repeats ID 7090000) and the game itself tolerates them.
# Replace with a lenient copy of the same parser that keeps the last
# occurrence of a duplicated ID and logs a warning instead of failing the
# whole file.
try:
    import logging as _logging
    from soulstruct.base.text.fmg import FMG as _FMG, FMGVersion as _FMGVersion
    from soulstruct.utilities.binary import ByteOrder as _FMGByteOrder

    @classmethod
    def _lenient_fmg_from_reader(cls, reader):
        version = _FMGVersion(reader["b", 2])
        reader.byte_order = _FMGByteOrder.big_endian_bool(version == _FMGVersion.V0)
        reader.long_varints = version >= 2
        header = cls.HEADER_VERSIONS[version].from_bytes(reader)
        ranges = []
        for _ in range(header.range_count):
            first_index, first_id, last_id = reader.unpack("3i")
            if version == 2:
                reader.assert_pad(4)
            ranges.append((first_index, first_id, last_id))
        string_offsets = reader.unpack(f"{header.string_count}v")
        entries = {}
        duplicates = 0
        for first_index, first_id, last_id in ranges:
            for string_id in range(first_id, last_id + 1):
                if string_id in entries:
                    duplicates += 1
                string_offset = string_offsets[first_index]
                if string_offset == 0:
                    entries[string_id] = ""
                else:
                    entries[string_id] = reader.unpack_string(
                        offset=string_offset, encoding=reader.get_utf_16_encoding(),
                    )
                first_index += 1
        if duplicates:
            _logging.getLogger("soulstruct_patches").warning(
                f"FMG contained {duplicates} duplicate text entry ID(s); kept the last occurrence of each."
            )
        return cls(entries=entries, version=version)

    _FMG.from_reader = _lenient_fmg_from_reader
    del _lenient_fmg_from_reader
except Exception:
    pass

# soulstruct writes every param into regulation.bin TWICE, and the game crashes on
# the result (black screen at boot, write to a null pointer inside eldenring.exe).
#
# GameParamBND.entry_autogen() writes each param back with set_default_entry() at its
# DEFAULT path, "N:\GR\data\Param\param\GameParam\X.param". Since the DLC, the real
# regulation keeps its params one level deeper, in "...\GameParam\merged\DLC02\", so
# no existing entry matches and a second copy is added beside the original. Its own
# clean-up step only removes entries whose NAME is gone, and the name is unchanged,
# so nothing is removed. Measured on Elden Vins' regulation: a plain load-and-save
# round trip, with no merging at all, turned 194 entries into 388 and grew the file
# from 2.8 MB to 4.8 MB. That file is what crashed the game (2026-09-23).
#
# Fix: write each param back into the entry it CAME from, matched by name, keeping
# the entry's real path. A param with no entry yet still falls back to the default.
try:
    from soulstruct.base.params.gameparambnd import GameParamBND as _BGPB

    def _entry_autogen(self):
        names = {f"{stem}.param": param for stem, param in self.params.items()}
        for entry_name in [e.name for e in self.entries]:
            if entry_name not in names:
                self.remove_entry_name(entry_name)
        existing = {e.name: e for e in self.entries}
        for name, param in names.items():
            entry = existing.get(name)
            if entry is None:
                entry = self.set_default_entry(
                    self.get_default_entry_path(name),
                    new_id=self.get_first_new_entry_id_in_range(0, 1000000),
                )
            entry.set_from_binary_file(param)

    _BGPB.entry_autogen = _entry_autogen
    del _entry_autogen
except Exception:
    pass

# soulstruct's BND4 writer never aligns entry data, and the game crashes on the result.
#
# `Binder._entries_into_writer_v4()` puts ten pad bytes between entry data blocks and
# nothing else, so each block starts wherever the previous one ended. Vanilla files align
# every block to 16 bytes: in Elden Vins' c0000.anibnd.dcx the first entry's data sits at
# 0x1c540, and a plain load-and-save through soulstruct moved it to 0x1c53a. Nothing else
# changed - same 800 entries, same ids, flags, paths and bytes - and the game died at a
# black screen (2026-09-23). Havok animation data has to be aligned to be read in place.
# The BND3 writer in the same file already does `pad_align(16)`; V4 was simply missed.
try:
    from soulstruct.containers.core import Binder as _Binder

    def _entries_into_writer_v4(self, header_writer, entry_writer, rebuild_hash_table=False):
        from soulstruct.containers.binder_hash import BinderHashTable
        sorted_entries = list(sorted(self.entries, key=lambda e: e.entry_id))
        sorted_entry_headers = [entry.get_header(self.flags) for entry in sorted_entries]
        for entry_header in sorted_entry_headers:
            entry_header.into_bnd4_writer(header_writer, self.flags, self.bit_big_endian)

        if self.flags.has_names:
            path_encoding = (entry_writer.get_utf_16_encoding() if self.v4_info.unicode
                             else self.ENTRY_PATH_ENCODING)
            for entry, entry_header in zip(sorted_entries, sorted_entry_headers):
                entry_header.pack_path(header_writer,
                                       entry.get_packed_path(encoding=path_encoding))

        if self.v4_info.hash_table_type == 4:
            header_writer.fill_with_position("_hash_table_offset", obj=self)
            if rebuild_hash_table:
                header_writer.append(BinderHashTable.build_hash_table(self.entries))
            else:
                header_writer.append(self.v4_info.most_recent_hash_table)
        else:
            header_writer.fill("_hash_table_offset", 0, obj=self)

        entry_writer.pad_align(16)
        header_writer.fill("_data_offset", entry_writer.position, obj=self)

        for entry, entry_header in zip(sorted_entries, sorted_entry_headers):
            entry_writer.pad_align(16)          # THE FIX: every data block 16-byte aligned
            entry_header.pack_data(header_writer, entry_writer, entry.data + b"\0" * 10)

    _Binder._entries_into_writer_v4 = _entries_into_writer_v4
    del _entries_into_writer_v4
except Exception:
    pass

# Python 3.13 removed audioop from stdlib (pydub uses it).
# audioop-lts provides it back; it's a no-op on earlier Python versions.
try:
    import pyaudioop  # noqa: F401 — imported for side-effect (registers the module)
except ImportError:
    pass

# Two soulstruct/constrata bugs were originally found and fixed while testing
# Python 3.13 compatibility and gated to "if sys.version_info >= (3, 13)" on
# the (incorrect) assumption they were 3.13-specific. Confirmed directly:
# `"abc".rstrip(b"\0")` raises the exact same TypeError on 3.11 — str.rstrip()
# has never accepted a bytes argument in Python 3, on any version. Both
# patches are applied unconditionally now; this is what actually let the
# TAE merger (core/mergers/tae_merger.py) load a real .anibnd.dcx for the
# first time on this project's recommended Python 3.11 runtime.

# constrata: BinaryString's field factory passes b"\0" as the strip arg when
# the field is a raw-bytes field (encoding=None) — but the asserted values
# can be str (e.g. TAE's magic field: binary_string(4, asserted="TAE ")).
# Pre-process them and call the original with rstrip_null=False.
#
# Second, related bug found the same way (testing the TAE merger end-to-end
# against a real .anibnd.dcx): a field declared with a str asserted value
# but no encoding= (TAE's magic field again) never gets decoded — it stays
# raw bytes after unpacking. constrata.metadata.finish_metadata() then
# defaults that field's unpack_func to its annotated type (str, since the
# field is declared `magic: str = ...`) whenever no unpack_func was given —
# so `str(raw_bytes_value)` runs, which doesn't decode bytes, it returns
# their repr text (e.g. b"TAE " becomes the 7-character string "b'TAE '").
# The real fix: when the caller passed a str asserted value but no
# encoding, that's a clear signal the field is meant to decode — default
# encoding to "ascii" so it actually does, which also makes the str-typed
# unpack_func fallback a harmless no-op.
try:
    import constrata.fields as _cf
    _orig_cf_BS = _cf.BinaryString
    def _patched_BinaryString(fmt_or_byte_size, asserted=None, unpack_func=None,
                               pack_func=None, encoding=None, rstrip_null=True):
        if asserted is not None:
            if not isinstance(asserted, (list, tuple)):
                asserted = (asserted,)
            if encoding is None and any(isinstance(s, str) for s in asserted):
                encoding = "ascii"
            if encoding is not None:
                if rstrip_null:
                    asserted = tuple(s.rstrip("\0") for s in asserted)
            else:
                if rstrip_null:
                    asserted = tuple(s.rstrip(b"\0") for s in asserted)
            return _orig_cf_BS(fmt_or_byte_size, asserted, unpack_func,
                               pack_func, encoding, rstrip_null=False)
        return _orig_cf_BS(fmt_or_byte_size, asserted, unpack_func,
                           pack_func, encoding, rstrip_null)
    _cf.BinaryString = _patched_BinaryString
except Exception:
    pass

# soulstruct: DataclassMeta is documented to apply kw_only=True but the
# implementation omits it, so any subclass (e.g. TAE) that defines a
# non-default field after a default one fails with Python's standard
# dataclass ordering error — not Python-version-specific either.
# Wrap the dataclass() call inside the metaclass to add kw_only=True.
try:
    import soulstruct.base.dataclass_meta as _dm
    _orig_dm_dc = _dm.dataclass
    _dm.dataclass = lambda cls, **kw: _orig_dm_dc(cls, kw_only=kw.pop("kw_only", True), **kw)
except Exception:
    pass

# soulstruct: TAEHeaderStruct's `_fixed_0` field (list[sbyte], i.e. signed
# bytes) asserts [0, 0, 0, 0xff] — but 0xff (255) is the unsigned reading of
# the same bit pattern a signed byte unpacks as -1. Wrong literal in
# soulstruct's own field declaration (found the same way as the two bugs
# above — testing the TAE merger against a real .anibnd.dcx). Patched
# generically (signed/unsigned byte-width tolerance) rather than fixing one
# field, since the same mistake could recur elsewhere in this struct.
try:
    import constrata.metadata as _cm
    _orig_bm_validate = _cm.BinaryMetadata.validate
    def _signed_unsigned_equivalent(a, b):
        if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool) and not isinstance(b, bool):
            for bits in (8, 16, 32, 64):
                if (a % (1 << bits)) == (b % (1 << bits)):
                    return True
        return False
    def _patched_bm_validate(self, value):
        if self.asserted and value not in self.asserted:
            for candidate in self.asserted:
                if isinstance(value, list) and isinstance(candidate, list) and len(value) == len(candidate):
                    if all(v == c or _signed_unsigned_equivalent(v, c) for v, c in zip(value, candidate)):
                        return
                elif _signed_unsigned_equivalent(value, candidate):
                    return
        return _orig_bm_validate(self, value)
    _cm.BinaryMetadata.validate = _patched_bm_validate
except Exception:
    pass

# constrata: `BinaryMetadata.get_null()` returns a bare scalar (0/False/
# b"\0"*size) for ANY field being reserved-for-later-fill - correct for
# plain fields, but `BinaryArrayMetadata` (fixed-length `list[T]` fields,
# e.g. MSB's `_child_regions_indices: list[int] = binary_array(16, ...)`)
# never overrides it, so a reserved array field gets a single int as its
# placeholder instead of a list of `length` nulls. Found while testing the
# MSB write path: `MSBSoundRegion.to_msb_writer()` crashed with "'int'
# object is not iterable" packing `_child_regions_indices` (left as `None`
# since nothing resolves `child_regions` -> indices before writing) -
# `struct_input.extend(packing_value)` expects an iterable for any
# `length > 0` field, but got the scalar `0` straight from `get_null()`.
try:
    import constrata.metadata as _cm_arr
    _orig_get_null = _cm_arr.BinaryMetadata.get_null
    def _patched_get_null(self, size):
        null = _orig_get_null(self, size)
        if self.length > 0 and not isinstance(null, (list, bytes)):
            return [null] * self.length
        return null
    _cm_arr.BinaryMetadata.get_null = _patched_get_null
except Exception:
    pass

# constrata: `BinaryStruct._initialize_metadata()` has a real bug in its
# bit-field-run-merging logic — found while investigating why regulation.bin
# (Params merger) couldn't load ANY real file, including completely
# unmodified vanilla copies. `ACTIONBUTTON_PARAM_ST` (and likely many other
# param types, plus any other `BinaryStruct` subclass with a bitfield run
# followed by more fields) failed with "unpack requires a buffer of 105
# bytes" against real 100-byte rows — NOT a missing field or a schema
# version mismatch (ruled both out directly: the field list sums to exactly
# 100 bytes by hand, and the row's own self-declared stride in the file is
# genuinely 100). Traced the actual bug by tracing every `append_fmt_only`
# call during class construction: after a run of bit fields finishes, the
# loop variable `run_bit_offset` is never reset to -1, so every SUBSEQUENT
# non-bit field also satisfies `if run_bit_offset >= 0:` and re-appends the
# same stale finished-run format string again — once per remaining field.
# For `ACTIONBUTTON_PARAM_ST` specifically this re-appended "1B" five extra
# times after its one real 8-bit run, exactly matching the 100-vs-105 byte
# gap. This is a generic constrata bug, not specific to this one param —
# reimplemented here verbatim except for the missing reset.
try:
    import constrata.binary_struct as _bs

    @classmethod
    def _patched_initialize_metadata(cls) -> None:
        if not hasattr(cls, "__dataclass_fields__"):
            raise TypeError(
                f"BinaryStruct subclass `{cls.__name__}` has not been processed as a dataclass. Was its metaclass "
                f"replaced?"
            )

        cls_name = cls.__name__
        binary_fields = cls.get_binary_fields()
        if not binary_fields:
            raise TypeError(f"`BinaryStruct` subclass `{cls_name}` has no binary fields.")

        all_metadata = []
        cls._STRUCT_METADATA = cls._StructMetadata()

        for binary_field, field_type in zip(binary_fields, cls.get_binary_field_types()):
            if isinstance(field_type, _bs.GenericAlias):
                if field_type.__origin__ is not list:
                    raise _bs.BinaryFieldTypeError(
                        binary_field, cls_name, "Binary fields types cannot be `tuple`. Use `list[type]`."
                    )
                field_type_name = "list"
            else:
                field_type_name = field_type.__name__

            metadata = binary_field.metadata.get("binary", None)

            if metadata is None:
                if field_type_name in cls.METADATA_FACTORIES:
                    try:
                        metadata = cls.METADATA_FACTORIES[field_type_name]()
                    except Exception as ex:
                        raise _bs.BinaryFieldTypeError(
                            binary_field, cls_name,
                            f"Failed to construct default metadata for field type `{field_type_name}`: {ex}",
                        )
                elif issubclass(field_type, _bs.BinaryStruct):
                    metadata = _cm.BinaryMetadata(
                        fmt=f"{field_type.get_size()}s",
                        unpack_func=field_type.from_bytes,
                        pack_func=lambda struct_value: struct_value.to_bytes(),
                    )
                else:
                    try:
                        fmt = _bs.PRIMITIVE_FIELD_FMTS[field_type]
                    except KeyError:
                        raise _bs.BinaryFieldTypeError(
                            binary_field, cls_name,
                            f"Field with non-primitive type `{field_type.__name__}` must have `fmt` metadata.",
                        )
                    metadata = _cm.BinaryMetadata(fmt)

            metadata.finish_metadata(binary_field, field_type, cls_name)
            all_metadata.append(metadata)

        cls._BFIELD_METADATA = tuple(all_metadata)
        cls._BFIELD_INIT = tuple(field.init for field in cls._BINARY_FIELDS)

        cls._BIT_OFFSET_SHIFT_MASK = {}
        run_bit_offset = -1
        run_bit_fmt = ""
        run_bit_fmt_bit_size = 0
        field_struct_index = 0

        for binary_field, metadata in zip(cls._BINARY_FIELDS, all_metadata):
            if metadata.bit_count != -1:
                if run_bit_offset == -1:
                    run_bit_offset = 0
                    run_bit_fmt = metadata.fmt
                    run_bit_fmt_bit_size = 8 * _bs.struct.calcsize(run_bit_fmt)
                elif run_bit_fmt != metadata.fmt or run_bit_offset == run_bit_fmt_bit_size:
                    run_value_count = (run_bit_offset + run_bit_fmt_bit_size - 1) // run_bit_fmt_bit_size
                    field_struct_index += 1
                    cls._STRUCT_METADATA.append_fmt_only(f"{run_value_count}{run_bit_fmt}")
                    run_bit_offset = 0
                    run_bit_fmt = metadata.fmt
                    run_bit_fmt_bit_size = 8 * _bs.struct.calcsize(run_bit_fmt)

                shift = run_bit_offset
                mask = (1 << metadata.bit_count) - 1
                cls._BIT_OFFSET_SHIFT_MASK[binary_field.name] = (shift, mask)
                run_bit_offset += metadata.bit_count

                if run_bit_offset > run_bit_fmt_bit_size:
                    raise _bs.BinaryFieldTypeError(
                        binary_field, cls_name,
                        f"Bit field `{binary_field.name}` overflows its bit field run with fmt {run_bit_fmt}. "
                        f"Maximum bit size of run is {run_bit_fmt_bit_size} bits, but this field pushes the offset "
                        f"to {run_bit_offset}."
                    )

                metadata.set_struct_index(field_struct_index)
                cls._STRUCT_METADATA.skip_field()
            else:
                if run_bit_offset >= 0:
                    # Just finished a run of bit fields.
                    bit_field_size = 8 * _bs.struct.calcsize(run_bit_fmt)
                    run_value_count = (run_bit_offset + bit_field_size - 1) // bit_field_size
                    field_struct_index += 1
                    cls._STRUCT_METADATA.append_fmt_only(f"{run_value_count}{run_bit_fmt}")
                    run_bit_offset = -1  # THE FIX — original never resets this

                metadata.set_struct_index(field_struct_index)
                field_struct_index += metadata.length or 1
                cls._STRUCT_METADATA.append_field_fmt(metadata.fmt)

        if run_bit_offset >= 0:
            # SECOND FIX: a bit-field run that's the very last thing in the
            # struct (no later non-bit field to trigger the flush above)
            # never got appended at all in the original — confirmed via
            # RANDOM_APPEAR_PARAM_ST (100 single-bit Slot fields + one
            # 4-bit pad, nothing after = exactly this shape). Flush it here.
            bit_field_size = 8 * _bs.struct.calcsize(run_bit_fmt)
            run_value_count = (run_bit_offset + bit_field_size - 1) // bit_field_size
            cls._STRUCT_METADATA.append_fmt_only(f"{run_value_count}{run_bit_fmt}")

        for metadata in cls._BFIELD_METADATA:
            if metadata.bit_count != -1:
                cls.IS_SIMPLE = False
                break
            if metadata.length > 0:
                cls.IS_SIMPLE = False
                break
            if metadata.unpack_func is not None or metadata.pack_func is not None:
                cls.IS_SIMPLE = False
                break
            if isinstance(metadata, _bs.BinaryStringMetadata) and metadata.encoding:
                cls.IS_SIMPLE = False
                break

        cls._STRUCT_METADATA.finish()
        cls._BinaryStruct__STRUCT_INITIALIZED = True

    _bs.BinaryStruct._initialize_metadata = _patched_initialize_metadata
except Exception:
    pass

# soulstruct: `Param.to_writer()` packs its own flags1/flags2 header bytes
# with a signed-byte format ("4b") even though their values are genuinely
# unsigned and can exceed 127 (confirmed on a real regulation.bin: ER's
# EquipParamWeapon table has flags1=133) — crashes with "byte format
# requires -128 <= number <= 127" on every real param write, found while
# testing the Params merger end-to-end after the constrata bitfield fixes
# above got loading working. Generic fix: when `BinaryWriter.pack()` hits
# exactly this signed-overflow error for a simple repeated-single-char
# format, retry with each out-of-range value wrapped to its 2's-complement
# equivalent — produces byte-identical output to packing unsigned (verified
# directly: wrapping 133 for "b" gives -123, which packs to the same byte
# as 133 packed as "B"). Covers this bug wherever else it might occur, not
# just this one call site.
try:
    import re as _re2
    import constrata.streams.writer as _cwriter
    _SIGNED_WRAP_MODULUS = {"b": 256, "h": 65536, "i": 2**32, "l": 2**32, "q": 2**64}
    _SIMPLE_FMT_RE = _re2.compile(r"([<>=!@]?)(\d*)([a-zA-Z])$")

    def _wrap_overflow_values(parsed_fmt, values):
        m = _SIMPLE_FMT_RE.fullmatch(parsed_fmt)
        if not m:
            return None
        _, _count, code = m.groups()
        modulus = _SIGNED_WRAP_MODULUS.get(code)
        if modulus is None:
            return None
        half = modulus // 2
        return tuple(v - modulus if isinstance(v, int) and v >= half else v for v in values)

    _orig_writer_pack = _cwriter.BinaryWriter.pack
    def _patched_writer_pack(self, fmt, *values):
        try:
            return _orig_writer_pack(self, fmt, *values)
        except _cwriter.struct.error:
            fixed = _wrap_overflow_values(self.parse_fmt(fmt), values)
            if fixed is None:
                raise
            return _orig_writer_pack(self, fmt, *fixed)
    _cwriter.BinaryWriter.pack = _patched_writer_pack

    _orig_writer_pack_at = _cwriter.BinaryWriter.pack_at
    def _patched_writer_pack_at(self, offset, fmt, *values):
        try:
            return _orig_writer_pack_at(self, offset, fmt, *values)
        except _cwriter.struct.error:
            fixed = _wrap_overflow_values(self.parse_fmt(fmt), values)
            if fixed is None:
                raise
            return _orig_writer_pack_at(self, offset, fmt, *fixed)
    _cwriter.BinaryWriter.pack_at = _patched_writer_pack_at

    # `BinaryStruct.to_writer()`'s final call is `writer.pack_struct(...)`
    # — a separate method (takes a pre-compiled `struct.Struct`, not a
    # format string) that hits the exact same signed/unsigned overflow,
    # but with a MIXED multi-field format (e.g. TAE header's "4b" for a
    # `list[sbyte]` field asserted to `[0,0,0,0xff]` — 0xff/255 doesn't fit
    # a signed byte). The simple single-repeated-char fix above doesn't
    # apply here, so this is a general tokenizer over the full format
    # string, wrapping only the int-valued slots that actually overflow.
    _FMT_TOKEN_RE = _re2.compile(r"(\d*)([xcbB?hHiIlLqQnNefdspP])")

    def _tokenize_struct_fmt(fmt):
        body = fmt[1:] if fmt and fmt[0] in "@=<>!" else fmt
        return [(int(c) if c else 1, code) for c, code in _FMT_TOKEN_RE.findall(body)]

    def _wrap_overflow_values_general(fmt, values):
        result = []
        vi = 0
        for count, code in _tokenize_struct_fmt(fmt):
            if code == "x":
                continue
            if code in ("s", "p"):
                if vi < len(values):
                    result.append(values[vi])
                    vi += 1
                continue
            modulus = _SIGNED_WRAP_MODULUS.get(code)
            for _ in range(count):
                if vi >= len(values):
                    break
                v = values[vi]
                vi += 1
                if modulus is not None and isinstance(v, int) and not isinstance(v, bool) and v >= modulus // 2:
                    result.append(v - modulus)
                else:
                    result.append(v)
        result.extend(values[vi:])
        return tuple(result)

    _orig_pack_struct = _cwriter.BinaryWriter.pack_struct
    def _patched_pack_struct(self, builtin_struct, *values):
        try:
            return _orig_pack_struct(self, builtin_struct, *values)
        except _cwriter.struct.error:
            fixed = _wrap_overflow_values_general(builtin_struct.format, values)
            return _orig_pack_struct(self, builtin_struct, *fixed)
    _cwriter.BinaryWriter.pack_struct = _patched_pack_struct

    _orig_pack_struct_at = _cwriter.BinaryWriter.pack_struct_at
    def _patched_pack_struct_at(self, offset, builtin_struct, *values):
        try:
            return _orig_pack_struct_at(self, offset, builtin_struct, *values)
        except _cwriter.struct.error:
            fixed = _wrap_overflow_values_general(builtin_struct.format, values)
            return _orig_pack_struct_at(self, offset, builtin_struct, *fixed)
    _cwriter.BinaryWriter.pack_struct_at = _patched_pack_struct_at
except Exception:
    pass

# constrata: `BinaryStruct.to_writer()` has the exact same "missing final
# flush" bug as `_initialize_metadata()` above, but in its VALUE-gathering
# loop instead of its FORMAT-building loop — confirmed on the same
# RANDOM_APPEAR_PARAM_ST shape (100 single-bit fields with nothing after):
# the format string correctly has 13 slots (after the fix above), but this
# loop only ever produces 12 packed values, because when a bit-field run is
# the very last thing in the struct, nothing after it triggers the
# "finish previous run" branch that appends the accumulated `run_bits`.
# Reimplemented here verbatim except for that one missing flush at the end.
try:
    _LOGGER = _bs._LOGGER

    def _patched_to_writer(self, writer=None, reserve_obj=None, byte_order=None, long_varints=None):
        if reserve_obj is None:
            reserve_obj = self

        old_byte_order = None
        old_long_varints = None

        if writer is not None:
            if byte_order is not None:
                old_byte_order, writer.byte_order = writer.byte_order, byte_order
            else:
                byte_order = writer.byte_order
            if long_varints is not None:
                old_long_varints, writer.long_varints = writer.long_varints, long_varints
            else:
                long_varints = writer.long_varints
        else:
            byte_order = byte_order or self.DEFAULT_BYTE_ORDER
            writer = _cwriter.BinaryWriter(byte_order, long_varints)

        def restore_writer():
            if old_byte_order is not None:
                writer.byte_order = old_byte_order
            if old_long_varints is not None:
                writer.long_varints = old_long_varints

        cls_name = self.cls_name
        start_offset = writer.position

        field_values = self.get_binary_field_values(include_single_asserted=True)

        try:
            internal_struct, field_offsets, field_sizes = self._STRUCT_METADATA.get_metadata(byte_order, long_varints)
        except KeyError:
            _LOGGER.error(
                f"No struct exists for `{cls_name}` with byte order {byte_order} and long_varints {long_varints}. "
                f"If any 'v' or 'V' fields exist, `long_varints` must be specified."
            )
            raise
        finally:
            restore_writer()

        for (field_name, field_value), field_metadata, field_offset, field_size in zip(
            field_values.items(), self._BFIELD_METADATA, field_offsets, field_sizes, strict=True
        ):
            if field_value is not None:
                continue
            writer.mark_reserved_offset(field_name, field_metadata.fmt, start_offset + field_offset, obj=reserve_obj)
            field_values[field_name] = field_metadata.get_null(field_size)

        struct_input = []
        run_index = -1
        run_bits = 0
        for field, field_type, field_metadata, field_value in zip(
            self._BINARY_FIELDS, self._BFIELD_TYPES, self._BFIELD_METADATA, field_values.values()
        ):
            try:
                field_metadata.validate(field_value)
            finally:
                restore_writer()

            if self.IS_SIMPLE:
                struct_input.append(field_value)
                continue

            packing_value = field_metadata.process_to_pack(field_value, byte_order)

            if field_metadata.length > 0:
                struct_input.extend(packing_value)
            elif field.name in self._BIT_OFFSET_SHIFT_MASK:
                shift, mask = self._BIT_OFFSET_SHIFT_MASK[field.name]
                if packing_value & ~mask:
                    raise _bs.BinaryFieldValueError(
                        f"Field `{cls_name}.{field.name}` value {repr(field_value)} is out of range for "
                        f"bit field with mask {mask:b} (field bit count = {field_metadata.bit_count})."
                    )
                if run_index == -1:
                    run_index = field_metadata.struct_index
                    run_bits = 0
                elif field_metadata.struct_index != run_index:
                    struct_input.append(run_bits)
                    run_index = field_metadata.struct_index
                    run_bits = 0
                run_bits |= (packing_value & mask) << shift
            else:
                if run_index != -1:
                    struct_input.append(run_bits)
                    run_index = -1
                    run_bits = 0
                struct_input.append(packing_value)

        if run_index != -1:
            # THE FIX — a bit-field run that's the very last thing in the
            # struct never got flushed into struct_input in the original.
            struct_input.append(run_bits)

        try:
            writer.pack_struct(internal_struct, *struct_input)
        except Exception as ex:
            _LOGGER.error(
                f"Could not pack struct fmt for `{cls_name}`: {internal_struct.format} (size {internal_struct.size}). "
                f"Error: {ex}"
            )
            raise
        finally:
            restore_writer()

        return writer

    _bs.BinaryStruct.to_writer = _patched_to_writer
except Exception:
    pass

# soulstruct: `_compress_dcx_zstd()` passes both `compression_params=` (a
# `ZstdCompressionParameters` object) and `write_content_size=False`
# directly to `ZstdCompressor()` — newer `zstandard` (0.25.0 here) rejects
# this combination outright ("cannot define compression_params and
# write_content_size"), since that flag now lives on the params object
# itself. Found while writing a real merged regulation.bin end-to-end
# (DCX-compresses the result) after the constrata Params fixes above got
# loading/packing working. Fix: build the params object with
# `write_content_size=False` baked in via `from_level()`, and don't pass it
# again to the compressor.
try:
    import soulstruct.dcx.core as _dcx_core

    def _patched_compress_dcx_zstd(raw_data: bytes, compression_level: int = 15) -> bytes:
        cparams = _dcx_core.zstd.ZstdCompressionParameters.from_level(
            compression_level,
            window_log=16,
            write_content_size=False,
        )
        cctx = _dcx_core.zstd.ZstdCompressor(compression_params=cparams)
        return cctx.compress(raw_data)

    _dcx_core._compress_dcx_zstd = _patched_compress_dcx_zstd
except Exception:
    pass

# soulstruct: `DCXVersionInfo.compression_level` is deliberately `None` for
# `DCX_ZSTD` (its own comment says "not constant for DCX_ZSTD"), but
# `compress()` blindly forwards `version_info.compression_level` straight
# into `DCXHeaderStruct(...)` regardless — so writing ANY ZSTD-compressed
# DCX (which is what every regulation.bin uses) always fails with
# "DCXHeaderStruct cannot fill all fields on its own ... Remaining:
# compression_level". Confirmed the real value real game files actually
# store by decrypting a real regulation.bin's DCX header directly: 21 (not
# the unrelated 15 `_compress_dcx_zstd` defaults to internally — bumped
# that too, for consistency with real files, though zstd decompression
# doesn't actually care what level value the header records).
try:
    def _patched_compress(raw_data: bytes, dcx_type) -> bytes:
        if dcx_type == _dcx_core.DCXType.DCX_EDGE:
            return _dcx_core._compress_dcx_edge(raw_data)

        if dcx_type == _dcx_core.DCXType.DCX_ZSTD:
            compressed = _dcx_core._compress_dcx_zstd(raw_data, compression_level=21)
        elif dcx_type == _dcx_core.DCXType.DCX_KRAK:
            compressed = _dcx_core.oodle.compress(raw_data)
        else:
            compressed = _dcx_core.zlib.compress(raw_data, level=7)

        if dcx_type == _dcx_core.DCXType.DCP_DFLT:
            header = bytes(_dcx_core.DCPHeaderStruct(
                decompressed_size=len(raw_data),
                compressed_size=len(compressed),
            ))
        else:
            version_info = dcx_type.get_version_info()
            compression_level = version_info.compression_level
            if compression_level is None and dcx_type == _dcx_core.DCXType.DCX_ZSTD:
                compression_level = 21  # THE FIX
            header = bytes(_dcx_core.DCXHeaderStruct(
                version1=version_info.version1,
                version2=version_info.version2,
                version3=version_info.version3,
                compression_type=version_info.compression_type,
                decompressed_size=len(raw_data),
                compressed_size=len(compressed),
                compression_level=compression_level,
                version5=version_info.version5,
                version6=version_info.version6,
                version7=version_info.version7,
            ))
            header += b"DCA\0" + b"\x00\x00\x00\x08"
        return header + compressed

    _dcx_core.compress = _patched_compress
    # `soulstruct/dcx/__init__.py` re-exports `compress` by name
    # (`from .core import compress`), and `base_binary_file.py` in turn
    # imports THAT name directly (`from soulstruct.dcx import compress`) —
    # both are separate bound references that won't see the reassignment
    # above. Patch them explicitly so the live call site actually uses it.
    import soulstruct.dcx as _dcx_pkg
    _dcx_pkg.compress = _patched_compress
    import soulstruct.base.base_binary_file as _bbf
    _bbf.compress = _patched_compress
except Exception:
    pass

# soulstruct: `Param.to_writer()` unconditionally fills
# `_short_row_data_offset` with the real writer position — but
# `Param.detect_param_type()` requires that field to be exactly 0 whenever
# a separate, larger `row_data_offset` field is in use (`IntDataOffset` or
# `LongDataOffset` flag set) rather than the short one. Confirmed directly:
# round-tripping the real `ActionButtonParam` table (real ER regulation.bin,
# `OffsetParam`+`LongDataOffset` flags) through `to_writer()` then
# `detect_param_type()` raised "Expected `_row_data_offset` of zero ...
# not: 9640" — found while testing the Params merger's real write path
# end-to-end after all the fixes above got it this far. Reimplemented here
# verbatim except for zeroing that field when the long/int offset is used.
_PARAM_DUPLICATE_ROWS: dict = {}  # id(Param) -> (Param, [(row_id, row), ...])

try:
    import soulstruct.base.params.param as _param_mod
    from soulstruct.utilities.text import pad_chars as _pad_chars

    def _patched_param_to_writer(self, sort: bool = True):
        self.sort()
        # Rows in file order, with any same-ID duplicates kept by the reader patch
        # below placed right after their first copy. Keys are list indices, since a
        # row ID is not unique here.
        dups: dict[int, list] = {}
        for dup_id, dup_row in _PARAM_DUPLICATE_ROWS.get(id(self), (None, ()))[1]:
            if dup_id in self.rows:
                dups.setdefault(dup_id, []).append(dup_row)
        all_rows = []
        for row_id, row in self.rows.items():
            all_rows.append((row_id, row))
            all_rows.extend((row_id, d) for d in dups.get(row_id, ()))
        row_count = len(all_rows)

        byte_order = _bs.ByteOrder.BigEndian if self.big_endian else _bs.ByteOrder.LittleEndian
        writer = _cwriter.BinaryWriter(byte_order=byte_order)

        writer.reserve("row_names_offset", "I", obj=self)
        writer.reserve("_short_row_data_offset", "H", obj=self)
        writer.pack("HHH", self.unknown, self.paramdef_data_version, row_count)

        if self.flags1.OffsetParam:
            writer.pad(4)
            writer.reserve("param_type_offset", "q", obj=self)
            writer.pad(20)
        else:
            writer.append(_pad_chars(
                self.param_type, encoding="ASCII", null_terminate=True, alignment=32, pad=b"\x20")
            )

        writer.pack(
            "4b", -1 if self.big_endian else 0, self.flags1.pack(), self.flags2.pack(), self.paramdef_format_version
        )

        if self.flags1[0] and self.flags1.IntDataOffset:
            writer.reserve("row_data_offset", "i", obj=self)
            writer.pad(12)
            has_long_row_data_offset = True
        elif self.flags1.LongDataOffset:
            writer.reserve("row_data_offset", "q", obj=self)
            writer.pad(8)
            has_long_row_data_offset = True
        else:
            has_long_row_data_offset = False

        for i, (row_id, _row) in enumerate(all_rows):
            writer.pack("i", row_id)
            if self.flags1.LongDataOffset:
                writer.pad(4)
                writer.reserve(f"row_data_offset{i}", "q", obj=self)
                writer.reserve(f"row_name_offset{i}", "q", obj=self)
            else:
                writer.reserve(f"row_data_offset{i}", "i", obj=self)
                writer.reserve(f"row_name_offset{i}", "i", obj=self)

        if has_long_row_data_offset:
            # THE FIX — must stay 0 so `detect_param_type()` knows the real
            # offset lives in the separate int/long `row_data_offset` field.
            writer.fill("_short_row_data_offset", 0, obj=self)
            writer.fill_with_position("row_data_offset", obj=self)
        else:
            writer.fill("_short_row_data_offset", min(writer.position, 2 ** 16 - 1), obj=self)

        for i, (_row_id, row) in enumerate(all_rows):
            writer.fill_with_position(f"row_data_offset{i}", obj=self)
            row.to_writer(writer)

        if self.flags1.OffsetParam:
            writer.fill_with_position("param_type_offset", obj=self)
            writer.append(self.param_type.encode("ASCII") + b"\0")

        writer.fill_with_position("row_names_offset", obj=self)
        for i, (_row_id, row) in enumerate(all_rows):
            packed_name = row.get_packed_name(self.get_name_encoding(self.big_endian, self.flags2))
            if packed_name:
                writer.fill_with_position(f"row_name_offset{i}", obj=self)
                writer.append(packed_name)
            else:
                writer.fill(f"row_name_offset{i}", 0, obj=self)

        return writer

    _param_mod.Param.to_writer = _patched_param_to_writer
except Exception:
    pass

# soulstruct: `Param.from_reader()` reads each row name with `reader.unpack_bytes()`,
# which stops at the first SINGLE zero byte. Elden Ring row names are UTF-16, so
# "Roundtable Hold" (b"R\0o\0...") is cut to b"R", decoding fails silently, and
# the row keeps RawName=b"R". The writer then packs that as b"R\0\0" - three bytes -
# so every later name sits off the 2-byte boundary. The game never reads names and
# runs, but SoulsFormats-based tools do: the item/enemy randomizer reported
# "Param WorldMapLegacyConvParam not found" on the merged Lost in the Sauce + Elden
# Vins regulation.bin (2026-09-24). Only the name read changes: for UTF-16 names
# it reads 2-byte units up to the 2-byte terminator.
try:
    import soulstruct.base.params.param as _param_mod2

    _orig_param_from_reader = _param_mod2.Param.from_reader.__func__

    @classmethod
    def _patched_param_from_reader(cls, reader):
        flags2 = _param_mod2.ParamFlags2(reader.unpack("b", offset=0x2e)[0])
        if flags2.UnicodeRowNames:
            def _unpack_utf16_bytes(length=None, *args, **kwargs):
                if length is not None or args or kwargs:
                    return type(reader).unpack_bytes(reader, length, *args, **kwargs)
                start = reader.position
                chunks = []
                while True:
                    unit = reader.read(2)
                    if len(unit) < 2 or unit == b"\0\0":
                        break
                    chunks.append(unit)
                reader.seek(start)
                return b"".join(chunks)
            reader.unpack_bytes = _unpack_utf16_bytes
            try:
                param = _orig_param_from_reader(cls, reader)
            finally:
                del reader.unpack_bytes
        else:
            param = _orig_param_from_reader(cls, reader)
        _keep_duplicate_rows(cls, reader, param, flags2.UnicodeRowNames)
        return param

    # soulstruct keeps rows in a dict, so a second row with the same ID is dropped
    # ("Repeated param row ID ... Only first will be kept"). Vanilla ER has 26 such
    # rows in RandomAppearParam, 20 of them with DIFFERENT data from the first copy,
    # so every merged regulation.bin lost them. They are kept in _PARAM_DUPLICATE_ROWS
    # (Param has __slots__ and no __weakref__, so they cannot go on the object) and the
    # writer puts each one back right after its first copy, as vanilla orders them.
    # The entry holds the Param itself so its id() is not reused while listed.
    def _keep_duplicate_rows(cls, reader, param, unicode_names):
        import struct as _struct
        data = reader.buffer.getvalue() if hasattr(reader.buffer, "getvalue") else None
        if data is None or len(data) < 0x30:
            return
        bo = ">" if data[0x2C] == 0xFF else "<"
        row_count = _struct.unpack_from(bo + "H", data, 0xA)[0]
        if row_count <= len(param.rows):
            return
        flags1 = _param_mod2.ParamFlags1(data[0x2D])
        long_ptrs = bool(flags1.LongDataOffset)
        table = 0x40 if ((flags1[0] and flags1.IntDataOffset) or flags1.LongDataOffset) else 0x30
        fmt, step = (bo + "iiqq", 24) if long_ptrs else (bo + "iII", 12)
        ptrs = []
        for i in range(row_count):
            p = _struct.unpack_from(fmt, data, table + step * i)
            ptrs.append((p[0], p[2], p[3]) if long_ptrs else p)
        row_size = ptrs[1][1] - ptrs[0][1]
        encoding = param.get_name_encoding(bo == ">", _param_mod2.ParamFlags2(data[0x2E]))
        seen, dups = set(), []
        for row_id, data_offset, name_offset in ptrs:
            if row_id not in seen:
                seen.add(row_id)
                continue
            row = cls.ROW_TYPE.from_bytes(data[data_offset:data_offset + row_size])
            raw_name = b""
            if name_offset:
                end = name_offset
                if unicode_names:
                    while data[end:end + 2] not in (b"\0\0", b""):
                        end += 2
                else:
                    end = data.index(b"\0", name_offset)
                raw_name = data[name_offset:end]
            row.RawName = raw_name
            try:
                row.Name = raw_name.decode(encoding)
            except UnicodeDecodeError:
                row.Name = ""
            dups.append((row_id, row))
        _PARAM_DUPLICATE_ROWS[id(param)] = (param, dups)

    _param_mod2.Param.from_reader = _patched_param_from_reader
except Exception:
    pass

# soulstruct: `ParamRow.get_packed_name()` does `raw_name.rstrip(b"\0")` before adding
# the terminator. For UTF-16 that also strips the zero high byte of the LAST character
# ("Tile" ends b"e\0"), so every name is one byte short and all names after it are
# misaligned. SoulsFormats then runs off the end of the param (EndOfStreamException):
# 91 of 194 params in the merged regulation.bin, every one that has row names.
# Strip whole 2-byte null units instead.
try:
    from soulstruct.base.params.param_row import ParamRow as _ParamRow

    def _patched_get_packed_name(self, encoding: str) -> bytes:
        raw_name = self.Name.encode(encoding) if self.Name else self.RawName
        if encoding.replace("-", "").startswith("utf16"):
            if len(raw_name) % 2:
                raw_name += b"\0"   # RawName read by the old single-byte reader
            while raw_name.endswith(b"\0\0"):
                raw_name = raw_name[:-2]
            return raw_name + b"\0\0" if raw_name else b""
        raw_stripped = raw_name.rstrip(b"\0")
        return raw_stripped + b"\0" if raw_stripped else b""

    _ParamRow.get_packed_name = _patched_get_packed_name
except Exception:
    pass

# soulstruct: TAEHeaderStruct itself has two further bugs beyond the three
# already patched above, confirmed by reconstructing the struct field-by-field
# against a real .anibnd.dcx and Smithbox's TAE3.bt binary template:
#   1. `version` asserts exactly 0x1000C, but the template documents a valid
#      range of 0x1000B-0x1000D — this project's actual game files use 0x1000D.
#   2. The struct is missing one 4-byte zero field (between the existing
#      `_zero` and `flags` fields), and `animation_count_1` needs widening
#      from 4 to 8 bytes — both shift every field read after them otherwise.
# `BinaryStructMeta` always applies `dataclass(slots=True, ...)`, so fields
# can't be added/resized on the existing class after the fact — the struct
# is fully reconstructed here instead (field-for-field identical to
# soulstruct's own, plus the two fixes).
try:
    import soulstruct.base.animations.tae.core as _tae_core
    from constrata.binary_struct import BinaryStruct as _BinStruct
    from constrata.fields import binary as _binary, binary_string as _binary_string, \
        binary_array as _binary_array, binary_pad as _binary_pad
    from soulstruct.utilities.binary import sbyte as _sbyte, byte as _byte, \
        int32 as _int32, int64 as _int64

    class _PatchedTAEHeaderStruct(_BinStruct):
        magic: str = _binary_string(4, asserted="TAE ", init=False)
        _fixed_0: list[_sbyte] = _binary_array(4, asserted=[0x0, 0x0, 0x0, 0xff], init=False)
        version: int = _binary(asserted=(0x1000B, 0x1000C, 0x1000D), init=False)
        file_size: int
        _fixed_1: list[_int64] = _binary_array(4, asserted=[0x40, 0x1, 0x50, 0x80], init=False)
        unk_x30: int
        _zero: _int64 = _binary(asserted=0, init=False)
        _zero_extra: int = _binary(asserted=0, init=False)  # the missing field
        flags: list[_byte] = _binary_array(8)
        _one: _int64 = _binary(asserted=1, init=False)
        tae_id_0: _int32
        animation_count_0: _int32
        animations_offset: _int64
        animation_groups_offset: _int64
        _xa0: _int64 = _binary(asserted=0xa0, init=False)
        animation_count_1: _int64  # widened from int32
        first_animation_offset: _int64
        _one_2: _int64 = _binary(asserted=1, init=False)
        _x90: _int64 = _binary(asserted=0x90, init=False)
        tae_id_1: _int32
        tae_id_2: _int32
        _x50: _int64 = _binary(asserted=0x50, init=False)
        _zero_2: _int64 = _binary(asserted=0, init=False)
        _xb0: _int64 = _binary(asserted=0xb0, init=False)
        skeleton_name_offset: _int64
        sib_name_offset: _int64
        _pad: bytes = _binary_pad(0x2, init=False)

    _tae_core.TAEHeaderStruct = _PatchedTAEHeaderStruct
except Exception:
    pass

# soulstruct: event-data padding fields across many TAE event types (e.g.
# `Unk700`) assert "always zero" but real files sometimes leave non-zero
# garbage there — same root cause as the str/bytes asserted-value bugs
# above, just affecting `BinaryPad()`-constructed fields instead. Since
# padding is by definition not meaningful data, tolerate any value of the
# correct length/type rather than chasing each affected event type
# individually (confirmed scattered across multiple distinct types this
# session, not just one).
try:
    import constrata.metadata as _cm2
    _orig_bm_validate2 = _cm2.BinaryMetadata.validate
    def _pad_tolerant_validate(self, value):
        if (
            getattr(self, "rstrip_null", None) is False
            and len(self.asserted or ()) == 1
            and isinstance(self.asserted[0], bytes)
            and len(set(self.asserted[0])) <= 1  # single repeated byte (or empty) = padding
            and isinstance(value, bytes)
            and len(value) == len(self.asserted[0])
        ):
            return
        return _orig_bm_validate2(self, value)
    _cm2.BinaryMetadata.validate = _pad_tolerant_validate
except Exception:
    pass

# soulstruct: `Unk016` (TAE event type 16) is declared with zero fields,
# but constrata requires at least one field to process a BinaryStruct —
# crashes with "has no binary fields" on the first real file containing
# this (fairly common) event type. Give it a genuine zero-length pad field
# so it satisfies that requirement while still consuming zero real bytes.
try:
    import soulstruct.base.animations.tae.events as _tae_events
    class _PatchedUnk016(_tae_events.Unk016):
        _pad: bytes = _binary_pad(0, init=False)
    _tae_events.Unk016 = _PatchedUnk016
except Exception:
    pass

# soulstruct: `TAEEventType` only catalogs 113 of the 156 event types
# Smithbox's actively-maintained TAE.Template.ER.xml documents (confirmed
# this session) — 43 types are missing outright, and a few more exist in
# the enum but were never given a real data class. `TAEEventType` is a
# stdlib IntEnum (can't have members added after definition), so the two
# reader methods that look event types up are reimplemented here with a
# fallback dict, built once at startup from the bundled XML copy
# (core/mergers/data/TAE.Template.ER.xml). Deliberately does NOT carry over
# the XML's `assert="0"` annotations onto these generated fields — that
# exact over-assertion pattern is responsible for most of the bugs already
# fixed above, so new fields are left unasserted on purpose.
try:
    import xml.etree.ElementTree as _ET
    from soulstruct.utilities.binary import int16 as _int16, uint16 as _uint16, \
        uint32 as _uint32, float32 as _float32

    _TAE_XML_PATH = _pl.Path(__file__).resolve().parent / "core" / "mergers" / "data" / "TAE.Template.ER.xml"
    _TAE_TAG_TO_TYPE = {
        "b": _byte, "u8": _byte, "s8": _sbyte,
        "s16": _int16, "u16": _uint16,
        "s32": _int32, "u32": _uint32,
        "s64": _int64,
        "f32": _float32,
    }
    _EXTRA_TAE_EVENT_TYPES: dict[int, type] = {}
    if _TAE_XML_PATH.exists():
        _tae_xml_root = _ET.parse(_TAE_XML_PATH).getroot()
        for _event_el in _tae_xml_root.findall("event"):
            _event_id = int(_event_el.get("id"))
            try:
                _existing = _tae_core.TAEEventType(_event_id)
                if hasattr(_tae_events, _existing.name):
                    continue  # already fully implemented (enum + real data class)
            except ValueError:
                pass
            _field_types = []
            _all_known = True
            for _child in _event_el:
                if _child.tag not in _TAE_TAG_TO_TYPE:
                    _all_known = False
                    break
                _field_types.append(_TAE_TAG_TO_TYPE[_child.tag])
            if not _all_known:
                continue
            _annotations = {f"unk_x{i:02d}": t for i, t in enumerate(_field_types)}
            _new_cls = type(
                f"Unk{_event_id}", (_tae_events.TAEEventData,),
                {"event_type": _event_id, "__annotations__": _annotations},
            )
            _EXTRA_TAE_EVENT_TYPES[_event_id] = _new_cls

    @classmethod
    def _patched_tae_event_from_reader(cls, reader):
        start_time_offset, end_time_offset, event_data_offset = reader.unpack("qqq")
        start_time = reader.unpack_value("f", offset=start_time_offset)
        end_time = reader.unpack_value("f", offset=end_time_offset)
        with reader.temp_offset(event_data_offset):
            event_type_value = reader.unpack_value("q")
            try:
                event_type = _tae_core.TAEEventType(event_type_value)
                event_data_class = getattr(_tae_events, event_type.name)
            except (ValueError, AttributeError):
                if event_type_value in _EXTRA_TAE_EVENT_TYPES:
                    event_type = event_type_value
                    event_data_class = _EXTRA_TAE_EVENT_TYPES[event_type_value]
                else:
                    raise ValueError(f"Invalid/unknown TAE event type value: {event_type_value}")
            reader.unpack_value("q", asserted=reader.position + 8)
            event_data = event_data_class.from_bytes(reader)
        return cls(event_type=event_type, start_time=start_time, end_time=end_time, event_data=event_data)

    _tae_core.TAEEvent.from_tae_reader = _patched_tae_event_from_reader

    @classmethod
    def _patched_tae_event_group_from_reader(cls, reader, event_offsets_to_indices):
        entry_count, values_offset, type_offset = reader.unpack("qqq")
        reader.unpack_value("q", asserted=0)
        with reader.temp_offset(type_offset):
            event_type_value = reader.unpack_value("q")
            try:
                event_type = _tae_core.TAEEventType(event_type_value)
            except ValueError:
                # `TAEEventGroup`'s own docstring says this field "does not
                # necessarily match" any real per-event type, and nothing
                # downstream builds a typed event-data class from it (unlike
                # individual events) — so there's no reason to require an
                # enum match here at all. By far the dominant TAE parse
                # failure across real game files was event type 765 showing
                # up in groups specifically (493 of ~526 remaining
                # failures) — it isn't in soulstruct's enum OR Smithbox's
                # documented event-type catalog, but since groups never
                # construct event-data from this value, just keep it raw.
                event_type = event_type_value
            reader.unpack_value("q", asserted=0)
        with reader.temp_offset(values_offset):
            header_offsets = reader.unpack(f"{entry_count}i")
            event_indices = [event_offsets_to_indices[offset] for offset in header_offsets]
        return cls(event_type=event_type, event_indices=event_indices)

    _tae_core.TAEEventGroup.from_tae_reader = _patched_tae_event_group_from_reader
except Exception:
    pass

# soulstruct: `TAEAnimation.from_tae_reader`'s `animation_file_reference`
# is read as a 1-byte bool, but the real format (confirmed against
# Smithbox's TAE3.bt AnimFile struct) has it as a full 8-byte quad — every
# field read after it within the same animation was shifted by 7 bytes.
# The method is short and self-contained; reimplemented here verbatim
# except for that one field's width.
try:
    @classmethod
    def _patched_tae_animation_from_reader(cls, reader, encoding):
        animation_id, offset = reader.unpack("qq")
        with reader.temp_offset(offset):
            animation_struct = cls.STRUCT.from_bytes(reader)
            event_offsets_to_indices = {}
            tae_events = []
            with reader.temp_offset(animation_struct.event_headers_offset):
                for event_index in range(animation_struct.event_count):
                    event_offsets_to_indices[reader.position] = event_index
                    tae_events.append(_tae_core.TAEEvent.from_tae_reader(reader))

            event_groups = []
            with reader.temp_offset(animation_struct.event_groups_offset):
                for _ in range(animation_struct.event_group_count):
                    event_groups.append(
                        _tae_core.TAEEventGroup.from_tae_reader(reader, event_offsets_to_indices)
                    )

            with reader.temp_offset(animation_struct.animation_file_offset):
                animation_file_reference = bool(reader.unpack_value("q"))  # was "?" (1 byte)
                reader.unpack_value("q", asserted=reader.position + 8)
                animation_file_name_offset = reader.unpack_value("q")
                animation_file_unk_x18, animation_file_unk_x1c = reader.unpack("ii")
                reader.unpack("qq", asserted=(0, 0))

                try:
                    animation_file_name = reader.unpack_string(
                        offset=animation_file_name_offset, encoding=encoding
                    )
                except ValueError:
                    animation_file_name = ""
                else:
                    if not animation_file_name.endswith((".hkt", ".hkx")):
                        animation_file_name = ""

        return cls(
            animation_id=animation_id,
            events=tae_events,
            event_groups=event_groups,
            is_animation_file_reference=animation_file_reference,
            animation_file_unk_x18=animation_file_unk_x18,
            animation_file_unk_x1c=animation_file_unk_x1c,
            animation_file_name=animation_file_name,
        )

    _tae_core.TAEAnimation.from_tae_reader = _patched_tae_animation_from_reader
except Exception:
    pass

# soulstruct: `TAE.to_writer()` was never implemented at all (`pass`, the
# class docstring literally says `"""TODO: Write methods."""`) — found
# while testing the TAE merger's "free merge" feature (adding an animation
# that only exists in a lower-priority mod): writing the merged TAE back to
# bytes always failed, for every file, regardless of the read-side fixes
# above. This is a from-scratch writer, reverse-engineered field-by-field
# against real game files and cross-checked against Smithbox's TAE3.bt/
# TAE3_EventData.bt templates. It does NOT reproduce vanilla's exact byte
# layout (FromSoft's packer interns shared start/end-time float values and
# orders sections differently) — it writes its own simpler, internally
# self-consistent layout instead, which is sufficient: nothing requires
# byte-for-byte parity with vanilla, only that the existing (fixed) reader
# can parse it back correctly, which is verified directly below and by a
# real round-trip regression sweep.
#
# Layout chosen (all offsets absolute from start of file):
#   [header: 194 bytes, padded to 208]
#   [skeleton_name: 32-byte UTF-16 slot][sib_name: 32-byte UTF-16 slot]
#   [animation pointer table: N * (id:int64, offset:int64)]      <- animations_offset
#   [anim groups: 16-byte header + N * (startID:i32,endID:i32,animOffset:int64)]  <- animation_groups_offset
#       (one trivial 1:1 group per animation, exactly matching what real
#       vanilla files already do — confirmed directly: every group in a
#       real c2010.anibnd.dcx file has startID==endID==that animation's id)
#   [N animation STRUCT blocks, 48 bytes each, contiguous]        <- first_animation_offset
#   for each animation, in order:
#       [event_times: event_count*2 floats]  (no dedup — one float per
#        start/end, unlike vanilla's interning; functionally equivalent)
#       [event_headers: event_count * 24 bytes (start_off, end_off, data_off)]
#       [event_group_headers: event_group_count * 32 bytes]
#       [animation_file block: 48 bytes][animation_file_name: 32-byte slot if present]
#       [event_data blocks: one per event, 16-byte tag+selfref header + payload]
#       [event_group values arrays + type blocks]
try:
    import soulstruct.base.animations.tae.core as _tae_core_w
    from soulstruct.utilities.binary import BinaryWriter as _TAEBinaryWriter

    def _tae_pack_name_slot(s: str, slot_size: int = 32) -> bytes:
        raw = s.encode("utf-16-le") + b"\x00\x00"
        if len(raw) > slot_size:
            raise ValueError(f"TAE name string too long for fixed slot: {s!r}")
        return raw + b"\x00" * (slot_size - len(raw))

    def _tae_animation_extra_size(anim) -> dict:
        """Compute the byte layout (relative offsets + total size) of one
        animation's variable-length 'extra data' section, without writing
        anything yet — needed up front so every animation's absolute base
        offset is known before any animation's bytes are actually packed."""
        n_ev = len(anim.events)
        n_grp = len(anim.event_groups)
        pos = 0
        event_times_rel = pos
        pos += n_ev * 2 * 4
        event_headers_rel = pos
        pos += n_ev * 24
        event_group_headers_rel = pos
        pos += n_grp * 32
        animation_file_rel = pos
        pos += 48
        has_file_name = bool(anim.animation_file_name)
        animation_file_name_rel = pos if has_file_name else 0
        if has_file_name:
            pos += 32
        event_data_rels = []
        for ev in anim.events:
            event_data_rels.append(pos)
            pos += 16 + len(bytes(ev.event_data))
        group_value_rels = []
        group_type_rels = []
        for grp in anim.event_groups:
            group_value_rels.append(pos)
            pos += len(grp.event_indices) * 4
            group_type_rels.append(pos)
            pos += 16
        return dict(
            total_size=pos,
            event_times_rel=event_times_rel,
            event_headers_rel=event_headers_rel,
            event_group_headers_rel=event_group_headers_rel,
            animation_file_rel=animation_file_rel,
            animation_file_name_rel=animation_file_name_rel,
            event_data_rels=event_data_rels,
            group_value_rels=group_value_rels,
            group_type_rels=group_type_rels,
        )

    def _tae_to_writer(self) -> _TAEBinaryWriter:
        writer = _TAEBinaryWriter(byte_order=_bs.ByteOrder.LittleEndian)

        header_size = 208  # 194 rounded up to 16
        skeleton_name_offset = header_size
        sib_name_offset = skeleton_name_offset + 32
        animations_offset = sib_name_offset + 32
        n = len(self.animations)
        animation_groups_offset = animations_offset + n * 16
        anim_struct_block_start = animation_groups_offset + 16 + n * 16

        # Pre-compute every animation's extra-data layout and absolute base.
        layouts = [_tae_animation_extra_size(a) for a in self.animations]
        extra_base = anim_struct_block_start + n * 48
        extra_bases = []
        for layout in layouts:
            extra_bases.append(extra_base)
            extra_base += layout["total_size"]

        first_animation_offset = anim_struct_block_start if n else 0

        # --- Header (file_size patched in at the very end) ---
        header = _tae_core_w.TAEHeaderStruct(
            file_size=0,
            unk_x30=self.unk_x30,
            flags=list(self.flags),
            tae_id_0=self.tae_id,
            animation_count_0=n,
            animations_offset=animations_offset,
            animation_groups_offset=animation_groups_offset,
            animation_count_1=n,
            first_animation_offset=first_animation_offset,
            tae_id_1=self.tae_id,
            tae_id_2=self.tae_id,
            skeleton_name_offset=skeleton_name_offset,
            sib_name_offset=sib_name_offset,
        )
        header.version = 0x1000D
        header_bytes = bytes(header)
        writer.append(header_bytes)
        writer.append(b"\x00" * (header_size - len(header_bytes)))

        writer.append(_tae_pack_name_slot(self.skeleton_name))
        writer.append(_tae_pack_name_slot(self.sib_name))

        # --- Animation pointer table ---
        for i, anim in enumerate(self.animations):
            struct_offset = anim_struct_block_start + i * 48
            writer.pack("qq", anim.animation_id, struct_offset)

        # --- Animation groups (trivial 1:1, matching real vanilla files) ---
        writer.pack("qq", n, writer.position + 16)
        for i, anim in enumerate(self.animations):
            writer.pack("iiq", anim.animation_id, anim.animation_id, animations_offset + i * 16)

        # --- Per-animation STRUCT blocks ---
        for anim, base, layout in zip(self.animations, extra_bases, layouts):
            n_ev = len(anim.events)
            n_grp = len(anim.event_groups)
            writer.pack(
                "qqqqiii i".replace(" ", ""),
                base + layout["event_headers_rel"],
                base + layout["event_group_headers_rel"],
                base + layout["event_times_rel"],
                base + layout["animation_file_rel"],
                n_ev, n_grp, n_ev * 2, 0,
            )

        # --- Per-animation extra data ---
        for anim, base, layout in zip(self.animations, extra_bases, layouts):
            # event_times: one float per start/end, no interning.
            for ev in anim.events:
                writer.pack("ff", ev.start_time, ev.end_time)

            # event_headers (referencing the event_times floats written
            # just above, and the event_data blocks written further below).
            event_times_base = base + layout["event_times_rel"]
            for i, (ev, data_rel) in enumerate(zip(anim.events, layout["event_data_rels"])):
                start_off = event_times_base + i * 8
                end_off = start_off + 4
                writer.pack("qqq", start_off, end_off, base + data_rel)

            # event_group_headers (referencing the values/type blocks
            # written further below).
            for grp, val_rel, type_rel in zip(
                anim.event_groups, layout["group_value_rels"], layout["group_type_rels"]
            ):
                writer.pack("qqqq", len(grp.event_indices), base + val_rel, base + type_rel, 0)

            # animation_file block (+ name string if present).
            has_name = bool(anim.animation_file_name)
            name_offset = base + layout["animation_file_name_rel"] if has_name else 0
            file_block_pos_after_selfref = base + layout["animation_file_rel"] + 16
            writer.pack(
                "qqqiiqq",
                1 if anim.is_animation_file_reference else 0,
                file_block_pos_after_selfref,
                name_offset,
                anim.animation_file_unk_x18,
                anim.animation_file_unk_x1c,
                0, 0,
            )
            if has_name:
                writer.append(_tae_pack_name_slot(anim.animation_file_name))

            # event_data blocks: 8-byte type tag + 8-byte self-referential
            # offset (== position right after BOTH of these 8-byte fields,
            # i.e. where the payload starts) + type-specific payload.
            for ev in anim.events:
                self_ref = writer.position + 16
                writer.pack("qq", int(ev.event_type), self_ref)
                writer.append(bytes(ev.event_data))

            # event_group values arrays (absolute offsets into THIS
            # animation's event_headers table, one per referenced event)
            # + type blocks (type tag + zero pad).
            event_headers_base = base + layout["event_headers_rel"]
            for grp in anim.event_groups:
                for event_index in grp.event_indices:
                    writer.pack("i", event_headers_base + event_index * 24)
                writer.pack("qq", int(grp.event_type), 0)

        file_size = len(bytes(writer))
        # file_size field is a 4-byte int32 at offset 12 in the header —
        # confirmed empirically (not assumed) by packing a marker value
        # and finding its exact byte position in the output.
        writer.pack_at(0x0C, "i", file_size)
        return writer

    _tae_core_w.TAE.to_writer = _tae_to_writer
except Exception:
    pass

# soulstruct: `MSBEntry.reader_to_entry_kwargs()` has a generic, structural
# bug affecting EVERY MSB entry (parts, regions, events, ...) with an
# unused ("None") struct slot: it computes `struct_offset = entry_offset +
# raw_offset_from_file`, then checks `if struct_offset != 0: raise` to
# validate that an unused slot's offset is genuinely absent. But that check
# is comparing the ABSOLUTE offset, not the raw stored value — `entry_offset
# + 0` is only 0 for an entry literally at the start of the file, so this
# check spuriously fails for almost every entry in the file whenever it
# legitimately has a real, correctly-zero unused-struct offset. Confirmed
# directly on a real `m10_00_00_00.msb.dcx`: `MSBMapPointDiscoveryOverrideRegion`
# (whose real format, per Smithbox's `Region.HasTypeData = false`, has no
# subtype data at all — soulstruct's own `STRUCTS["subtype_data"] = None`
# already correctly models this) failed with "Offset for unused struct
# `subtype_data` is non-zero" purely because of this off-by-entry_offset
# arithmetic, not because anything was actually wrong with the file or with
# soulstruct's understanding of the format. Fix: check the raw value before
# adding `entry_offset`, matching how the "used struct, offset == 0" check
# two lines below it already correctly operates on the un-added raw value's
# zero-ness (that check happens to work today only because Python truthiness
# of `entry_offset + raw == 0` is rare by coincidence, not because it's
# correct — same bug, just less likely to misfire for the "used" case).
try:
    import soulstruct.base.maps.msb.msb_entry as _msb_entry

    @classmethod
    def _patched_msb_reader_to_entry_kwargs(cls, reader, entry_offset):
        kwargs = cls.HEADER_STRUCT.reader_to_entry_kwargs(reader, cls, entry_offset)

        for struct_name, struct_type in cls.STRUCTS.items():
            try:
                raw_offset = kwargs.pop(f"{struct_name}_offset")
            except KeyError:
                raise ValueError(f"Struct offset not found for `{struct_name}` in `{cls.__name__}`.")
            if struct_type is None:
                if raw_offset != 0:
                    raise ValueError(f"Offset for unused struct `{struct_name}` in `{cls.__name__}` is non-zero.")
                continue
            if raw_offset == 0:
                raise ValueError(f"Offset for used struct `{struct_name}` in `{cls.__name__}` is 0.")
            reader.seek(entry_offset + raw_offset)
            kwargs |= struct_type.reader_to_entry_kwargs(reader, cls, entry_offset)

        return kwargs

    _msb_entry.MSBEntry.reader_to_entry_kwargs = _patched_msb_reader_to_entry_kwargs
except Exception:
    pass

# soulstruct: every CONCRETE `MSBPart` subtype's `STRUCTS` dict is missing
# one or more keys that `PartHeaderStruct` (shared by every part subtype)
# always emits an offset field for — confirmed systematically, not a
# one-off: `STRUCTS` dicts are fully REPLACED (not merged) by each
# subclass, so any key a subtype's author didn't explicitly re-list
# (including "supertype_data", which every part genuinely uses) silently
# disappears, leaving its offset as an unconsumed kwarg the dataclass
# constructor rejects. Hit this one missing key at a time against a real
# `m10_00_00_00.msb.dcx` (supertype_data, then draw_info_2_data, then
# scene_gparam_data, ...) — rather than keep whack-a-moling each one,
# enumerated `PartHeaderStruct`'s complete field list once and fixed every
# concrete subtype in one pass: any of the 11 known "*_data" offset keys
# missing from a subtype's `STRUCTS` gets added as `None` (matching how
# that subtype's own author already expressed "unused" for the keys they
# DID remember to list) — except "supertype_data", which gets the real
# `PartDataStruct` class, since every part genuinely has that data and its
# total absence across every subtype was clearly just a copy-paste gap
# from the abstract base class's STRUCTS dict (which has it, just under a
# differently-named, non-matching key).
try:
    import soulstruct.eldenring.maps.parts as _msb_parts

    _PART_STRUCT_KEYS_TO_DEFAULTS = {
        "draw_info_1_data": None,
        "draw_info_2_data": None,
        "supertype_data": _msb_parts.PartDataStruct,
        "subtype_data": None,
        "gparam_data": None,
        "scene_gparam_data": None,
        "grass_config_data": None,
        "unk8_data": None,
        "unk9_data": None,
        "tile_load_config_data": None,
        "unk11_data": None,
    }

    for _msb_part_cls_name in dir(_msb_parts):
        _msb_part_cls = getattr(_msb_parts, _msb_part_cls_name)
        if (
            isinstance(_msb_part_cls, type)
            and issubclass(_msb_part_cls, _msb_parts.MSBPart)
            and _msb_part_cls is not _msb_parts.MSBPart
            and _msb_part_cls.STRUCTS  # skip Model classes (empty STRUCTS by design)
        ):
            missing = {
                k: v for k, v in _PART_STRUCT_KEYS_TO_DEFAULTS.items()
                if k not in _msb_part_cls.STRUCTS
            }
            if missing:
                _msb_part_cls.STRUCTS = {**_msb_part_cls.STRUCTS, **missing}
except Exception:
    pass

# soulstruct: `MSBEntrySuperlistHeader._version` is asserted to exactly 73
# — but this single shared header struct is read once per MSB SUPERTYPE
# list (models/events/regions/parts/routes), and a real-file sweep across
# all 1347 vanilla `.msb.dcx` files showed it legitimately takes many
# different values (46, 51, 57, 63, 64, 65, 66, 69, 70, 71, 74, ...)
# depending on which supertype list is being read — clearly not a
# game-patch version number at all (a real version field wouldn't vary
# within a single, unmodified vanilla file depending on which of its own
# five supertype lists you're looking at). Confirmed it's also never
# actually used after being read — `_unpack_supertype_list()` only pops
# `entry_offset_count` and `name_offset`, never `_version` — so the
# assertion was pure validation with no downstream purpose, and a wrong
# one: it was rejecting hundreds of legitimate, unmodified vanilla map
# files before this fix (352+46+39+30+30+22+21+20+18+1+1 = 580 of 1347).
try:
    import soulstruct.eldenring.maps.msb as _msb_module
    from soulstruct.utilities.binary import binary as _msb_binary, long as _msb_long

    # `LAST_READ_VERSION` / `WRITE_VERSION_OVERRIDE`: a real-file sweep (20
    # vanilla maps) showed `_version` is CONSTANT across all 6 supertype
    # lists within one file, but varies file-to-file (46/51/57/63-66/69-71/
    # 73/74 observed) - a genuine per-map format/tool-version stamp, not
    # noise. Removing the assertion (below) fixed reading, but writing needs
    # the real per-file value echoed back, not a hardcoded constant - MSB
    # uses `__slots__` (confirmed: assigning a new attribute on a loaded MSB
    # instance raises AttributeError) so it can't be stashed on the instance
    # itself. `msb_merger.py` captures `LAST_READ_VERSION` immediately after
    # each `MSB.from_path()` call (sequential, one mod at a time - no
    # concurrency risk) and sets `WRITE_VERSION_OVERRIDE` right before the
    # single `result_msb.write()` call for that file.
    _msb_module.LAST_READ_VERSION = None
    _msb_module.WRITE_VERSION_OVERRIDE = None

    class _PatchedMSBEntrySuperlistHeader(_bs.BinaryStruct):
        _version: int = _msb_binary(init=False, default=73)
        entry_offset_count: int
        name_offset: _msb_long

        @classmethod
        def from_object(cls, obj, **field_values):
            instance = super().from_object(obj, **field_values)
            if _msb_module.WRITE_VERSION_OVERRIDE is not None:
                instance._version = _msb_module.WRITE_VERSION_OVERRIDE
            return instance

    _msb_module.MSBEntrySuperlistHeader = _PatchedMSBEntrySuperlistHeader
    _msb_module.MSB.SUPERTYPE_LIST_HEADER = _PatchedMSBEntrySuperlistHeader

    # Bug found while testing the write path (separate from the assertion
    # bug above): `to_writer()` builds a FRESH header struct instance per
    # supertype list via `object_to_writer(self, writer, ...)`, but never
    # fills `_version` if its value is `None` (which it always was, since
    # the previous patch removed the default along with the assertion) -
    # `BinaryWriter.reserve()` keys reservations by `id(obj)`, and these
    # short-lived header instances get garbage-collected and their `id()`
    # reused by CPython between supertype-list iterations, so the second
    # iteration's `reserve("_version", ...)` collided with the first
    # iteration's never-filled reservation at the same reused id - 100%
    # write failure on every real file tested, regardless of content.
    # Restoring a concrete default (`default=73` above) means `_version` is
    # never `None`, so it's packed directly instead of reserved - no
    # collision possible.
    _orig_unpack_supertype_list = _msb_module.MSB._unpack_supertype_list.__func__

    @classmethod
    def _patched_unpack_supertype_list(cls, reader, supertype_name, entry_unpack_func):
        start = reader.position
        header = cls.SUPERTYPE_LIST_HEADER.from_bytes(reader)
        _msb_module.LAST_READ_VERSION = header._version
        reader.seek(start)
        return _orig_unpack_supertype_list(cls, reader, supertype_name, entry_unpack_func)

    _msb_module.MSB._unpack_supertype_list = _patched_unpack_supertype_list
except Exception:
    pass

# soulstruct: `MSBRegion.to_msb_writer()` (eldenring/maps/regions.py) has a
# real bug - it forwards `supertype_index`/`subtype_index` to EVERY struct
# in `self.STRUCTS.items()`, not just `HEADER_STRUCT`. The base class's
# default `to_msb_writer()` (used by every other MSB entry type - parts,
# events, etc.) only passes those two kwargs to the header struct call.
# Only `RegionHeaderStruct` actually declares `supertype_index`/
# `subtype_index` fields; every region subtype's `supertype_data`/
# `subtype_data` struct (e.g. `RegionSupertypeDataStruct`) doesn't, so
# `cls(**field_values)` rejected them outright - confirmed this broke
# writing for every single subtype except `MSBInvasionPointRegion`-style
# ones with no extra structs at all. Region's override exists only for the
# different struct-alignment logic (`aligned` flag below); copied this
# logic verbatim minus the two erroneous kwargs in the loop.
try:
    from soulstruct.eldenring.maps.regions import (
        MSBRegion as _msb_region_cls,
        MSBSoundRegion as _msb_sound_region_cls,
        RegionHeaderStruct as _region_header_cls,
        RegionExtraData as _region_extra_data_cls,
    )
    from soulstruct.eldenring.maps.enums import MSBRegionSubtype as _msb_region_subtype

    # Two more real soulstruct bugs found alongside the kwarg-forwarding one,
    # both confirmed on real vanilla maps (a 20-file sweep across mapstudio):
    #
    # 1. `RegionHeaderStruct.preprocess_write_kwargs()` reserves
    #    `extra_data_offset`, but NOTHING ever fills it - `RegionExtraData`
    #    (map_id/region_unkx_04/region_unkx_0c) is read on load
    #    (`reader_to_entry_kwargs`) but its write-back was simply never
    #    implemented (no equivalent of the `shape`/`unk_shorts_*` writes
    #    already present in `post_write()`). 100% of region writes failed
    #    with "Reserved offsets not filled: ..._extra_data_offset" until
    #    this was added.
    #
    # 2. The per-struct loop unconditionally `continue`s past any
    #    STRUCTS entry whose value is `None` (e.g. `MSBOtherRegion`, which
    #    has no `subtype_data`) WITHOUT filling that offset field - but the
    #    offset is still a real, always-present header field. Real vanilla
    #    files store 0 there to mean "no data"; leaving it reserved/unfilled
    #    fails the same way. Same latent gap exists in the upstream base
    #    `MSBEntry.to_msb_writer()` for any other entry type with a `None`
    #    struct, though no Part/Event subtype in this 20-file sample
    #    happened to trigger it.
    _orig_region_header_post_write = _region_header_cls.post_write.__func__

    @classmethod
    def _patched_region_header_post_write(cls, entry, writer, entry_offset, entry_lists):
        _orig_region_header_post_write(cls, entry, writer, entry_offset, entry_lists)
        extra_offset = writer.position - entry_offset
        writer.fill("extra_data_offset", extra_offset, obj=entry)
        _region_extra_data_cls.object_to_writer(entry, writer)

    _region_header_cls.post_write = _patched_region_header_post_write

    def _patched_region_to_msb_writer(self, writer, supertype_index, subtype_index, entry_lists):
        entry_offset = writer.position
        self.HEADER_STRUCT.kwargs_to_msb_writer(
            self, writer, entry_offset, entry_lists,
            supertype_index=supertype_index, subtype_index=subtype_index,
        )
        aligned = False
        for struct_name, struct_type in self.STRUCTS.items():
            struct_offset = writer.position - entry_offset
            if struct_type is None:
                writer.fill(f"{struct_name}_offset", 0, obj=self)
                continue
            writer.fill(f"{struct_name}_offset", struct_offset, obj=self)
            struct_type.kwargs_to_msb_writer(self, writer, entry_offset, entry_lists)
            if struct_name == "supertype_data" and _msb_region_subtype.MufflingBox <= self.SUBTYPE_ENUM:
                writer.pad_align(8)
                aligned = True
        if not aligned:
            writer.pad_align(8)

    _msb_region_cls.to_msb_writer = _patched_region_to_msb_writer

    # `MSBSoundRegion._child_regions_indices` (up to 16 composite child-region
    # references) is resolved index->object on read (`indices_to_objects` ->
    # `_consume_indices`), but nothing resolves the reverse direction
    # (object->index) before writing - it's a plain `binary_array` field, not
    # one of the auto-resolving `EntryRef`-metadata fields, so it was simply
    # left `None` forever and stayed reserved. `MSBEntry.try_index()` already
    # does exactly this resolution (and already handles the list case) - just
    # never gets called for this specific field.
    _orig_region_to_msb_writer_for_sound = _msb_region_cls.to_msb_writer

    def _patched_sound_region_to_msb_writer(self, writer, supertype_index, subtype_index, entry_lists):
        self._child_regions_indices = self.try_index(entry_lists["POINT_PARAM_ST"], "child_regions")
        _orig_region_to_msb_writer_for_sound(self, writer, supertype_index, subtype_index, entry_lists)

    _msb_sound_region_cls.to_msb_writer = _patched_sound_region_to_msb_writer
except Exception:
    pass

# soulstruct: writing ANY `MSBPart` (the most common MSB entry type by far)
# always raised "MSBPart must have `model_instance_id` set in
# `kwargs_to_msb_writer`" - `PartHeaderStruct.preprocess_write_kwargs()`
# (eldenring/maps/parts.py) requires the caller to have already put
# `model_instance_id` into `kwargs` before the header struct is built, but
# nothing in the actual write path ever does this: `core.py`'s `to_writer()`
# only computes/passes a *count* of parts using each model (`model_instance_
# counts`, an unrelated concept used only for the Models supertype branch
# and a "model not used" warning) - it never touches `model_instance_id`
# itself, and `MSBPart` doesn't override the base entry class's default
# `to_msb_writer()` to supply it either. So every concrete part subtype
# (MSBMapPiece, MSBCharacter, MSBAsset, ...) hit this exact error - 100% of
# Parts writes failed, confirmed across the same 20-file real sweep. Since
# the field's own comment in parts.py says it "[h]opefully doesn't actually
# do anything (I've left it as zero in prior games)", the safe fix is to
# echo back each part's own already-loaded value (round-trip-preserving)
# rather than try to recompute a "real" id from scratch.
try:
    from soulstruct.eldenring.maps.parts import MSBPart as _msb_part_cls

    def _patched_part_to_msb_writer(self, writer, supertype_index, subtype_index, entry_lists):
        entry_offset = writer.position
        self.HEADER_STRUCT.kwargs_to_msb_writer(
            self, writer, entry_offset, entry_lists,
            supertype_index=supertype_index, subtype_index=subtype_index,
            model_instance_id=self.model_instance_id,
        )
        for struct_name, struct_type in self.STRUCTS.items():
            if struct_type is None:
                continue
            struct_offset = writer.position - entry_offset
            writer.fill(f"{struct_name}_offset", struct_offset, obj=self)
            struct_type.kwargs_to_msb_writer(self, writer, entry_offset, entry_lists)

    _msb_part_cls.to_msb_writer = _patched_part_to_msb_writer
except Exception:
    pass

# soulstruct: `PartHeaderStruct.post_write()` (eldenring/maps/parts.py) -
# CRITICAL bug, silently corrupts almost every written part's `name` field.
# It appends only ONE null byte as the name's terminator
# (`entry.name.encode(entry.NAME_ENCODING) + b"\0"`), but `NAME_ENCODING` is
# "utf-16-le" - a proper null TERMINATOR in a 2-byte-per-character encoding
# needs TWO null bytes (one full null character), not one. The one-byte
# terminator misaligns every byte that follows (sib_path) by 1 byte off the
# 2-byte character boundary. The reader (`unpack_string`) scans forward from
# `name_offset` for the first real double-null it finds - since the
# intended terminator is incomplete, it keeps consuming bytes from the
# now-misaligned `sib_path` data (decoded as garbage UTF-16) until it
# stumbles on a coincidental double-zero further in.
#
# This was NOT caught by this session's full 1347-file round-trip sweep,
# which only checked entry counts/re-parse success, not deep field content
# (the natural `_public_fields()`/`vars()` approach can't be used here since
# every MSB entry class is slotted - see msb_merger.py's note on this).
# Confirmed directly on real data after writing this fix: a completely
# unmodified `m45_00_00_00.msb.dcx` write+reload showed 486 of 489 parts
# with a corrupted `.name` after reload (486/489, not all 489, only because
# a small fraction of names happen to have a byte-length parity that
# coincidentally still produces a valid-looking terminator) - this affects
# every Part subtype (MapPiece, Character, Asset, ...), in every file,
# every time a Part is written. The "minimum combined length" padding logic
# in the same method is legitimate and preserved; only the terminator byte
# counts were wrong.
try:
    from soulstruct.eldenring.maps.parts import PartHeaderStruct as _part_header_cls

    @classmethod
    def _patched_part_header_post_write(cls, entry, writer, entry_offset, entry_lists):
        strings_position = writer.position - entry_offset
        writer.fill("name_offset", writer.position - entry_offset, obj=entry)
        writer.append(entry.name.encode(entry.NAME_ENCODING) + b"\0\0")
        writer.fill("sib_path_offset", writer.position - entry_offset, obj=entry)
        packed_sib_path = (entry.sib_path.encode(entry.NAME_ENCODING) + b"\0\0") if entry.sib_path else b"\0\0"
        writer.append(packed_sib_path)
        writer.pad_align(4)
        if writer.position - strings_position < 20:
            writer.pad(0x14 - (writer.position - strings_position))

    _part_header_cls.post_write = _patched_part_header_post_write
except Exception:
    pass

