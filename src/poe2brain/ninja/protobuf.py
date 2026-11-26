# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Zero-dependency wire codec for poe.ninja SearchResult protobuf & NDIC v2 dictionaries."""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence

from poe2brain.models.ninja import (
    ProtobufDictionaryRef,
    ProtobufDimensionCount,
    ProtobufSearchResult,
)

NDIC_MAGIC: bytes = b"NDIC"
NDIC_HEADER_BYTES: int = 36


def read_varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    """Decode a base-128 unsigned varint from `buf[pos:]`."""
    value = 0
    n = len(buf)
    for shift in range(0, 70, 7):
        if pos >= n:
            raise ValueError("Truncated protobuf varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, pos
    raise ValueError("Oversized protobuf varint (>10 bytes)")


def encode_varint(value: int) -> bytes:
    """Encode a signed/unsigned 64-bit integer as a protobuf varint."""
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _to_signed_int32_or_int64(raw_uint: int) -> int:
    if raw_uint >= (1 << 63):
        return raw_uint - (1 << 64)
    if (1 << 31) <= raw_uint < (1 << 32):
        return raw_uint - (1 << 32)
    return raw_uint


def read_packed_varints(buf: bytes | memoryview) -> list[int]:
    """Unpack a length-delimited `packed=true` varint field."""
    values: list[int] = []
    pos = 0
    n = len(buf)
    while pos < n:
        raw, pos = read_varint(buf, pos)
        values.append(_to_signed_int32_or_int64(raw))
    return values


def parse_protobuf_fields(buf: bytes) -> dict[int, list[tuple[int, int | bytes]]]:
    """Parse raw protobuf wire fields into `{field_number: [(wire_type, raw_value), ...]}`."""
    fields: dict[int, list[tuple[int, int | bytes]]] = {}
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = read_varint(buf, pos)
        field_no = tag >> 3
        wire_type = tag & 0x07
        if field_no <= 0:
            raise ValueError(f"Invalid protobuf field number {field_no}")

        if wire_type == 0:
            v, pos = read_varint(buf, pos)
            fields.setdefault(field_no, []).append((wire_type, v))
        elif wire_type == 1:
            if pos + 8 > n:
                raise ValueError("Truncated protobuf fixed64 field")
            fields.setdefault(field_no, []).append((wire_type, buf[pos : pos + 8]))
            pos += 8
        elif wire_type == 2:
            size, pos = read_varint(buf, pos)
            if pos + size > n:
                raise ValueError("Truncated protobuf length-delimited field")
            fields.setdefault(field_no, []).append((wire_type, buf[pos : pos + size]))
            pos += size
        elif wire_type == 5:
            if pos + 4 > n:
                raise ValueError("Truncated protobuf fixed32 field")
            fields.setdefault(field_no, []).append((wire_type, buf[pos : pos + 4]))
            pos += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")
    return fields


def decode_ndic_dictionary(blob: bytes) -> list[str]:
    """Decode a `poe.ninja` `NDIC` v2 binary dictionary (or legacy protobuf string table).

    `NDIC` v2 layout (little-endian):
    - `0..4`:   magic `b"NDIC"`
    - `4..8`:   uint32 version (`2`)
    - `12..16`: uint32 string count
    - `28..32`: uint32 sync table entry count (`8` bytes each)
    - `32..36`: uint32 varint byte-length section size
    - `36 .. 36 + sync*8`: sync entries
    - `len_pos .. len_pos + lengths_bytes`: `count` varint UTF-8 byte lengths
    - `txt_pos .. EOF`: concatenated UTF-8 strings
    """
    if blob[:4] != NDIC_MAGIC:
        fields = parse_protobuf_fields(blob)
        return [
            raw.decode("utf-8")
            for wt, raw in fields.get(2, [])
            if wt == 2 and isinstance(raw, bytes)
        ]

    if len(blob) < NDIC_HEADER_BYTES:
        raise ValueError(f"Truncated NDIC header ({len(blob)} < {NDIC_HEADER_BYTES} bytes)")
    version = struct.unpack_from("<I", blob, 4)[0]
    if version != 2:
        raise ValueError(f"Unsupported NDIC dictionary version {version} (expected 2)")

    count = struct.unpack_from("<I", blob, 12)[0]
    sync_entries, lengths_bytes = struct.unpack_from("<II", blob, 28)
    len_pos = NDIC_HEADER_BYTES + sync_entries * 8
    len_end = len_pos + lengths_bytes
    txt_pos = len_end

    if len_end > len(blob):
        raise ValueError("Truncated NDIC length table")

    values: list[str] = []
    for _ in range(count):
        str_len, len_pos = read_varint(blob, len_pos)
        if len_pos > len_end or txt_pos + str_len > len(blob):
            raise ValueError("Truncated NDIC UTF-8 string entry")
        values.append(blob[txt_pos : txt_pos + str_len].decode("utf-8"))
        txt_pos += str_len

    if len_pos != len_end or txt_pos != len(blob):
        raise ValueError("Trailing bytes in NDIC dictionary payload")
    return values


def encode_ndic_dictionary(values: Sequence[str]) -> bytes:
    """Encode strings into a deterministic `NDIC` v2 container (used in offline tests)."""
    encoded_strs = [s.encode("utf-8") for s in values]
    lengths_blob = b"".join(encode_varint(len(b)) for b in encoded_strs)
    texts_blob = b"".join(encoded_strs)
    header = struct.pack(
        "<4sIIIIIIII",
        NDIC_MAGIC,
        2,
        0,
        len(encoded_strs),
        0,
        0,
        0,
        0,
        len(lengths_blob),
    )
    return header + lengths_blob + texts_blob


def _first_str(fields: Mapping[int, list[tuple[int, int | bytes]]], field_no: int) -> str | None:
    for wt, val in fields.get(field_no, ()):
        if wt == 2 and isinstance(val, bytes):
            return val.decode("utf-8")
    return None


def _first_int(fields: Mapping[int, list[tuple[int, int | bytes]]], field_no: int) -> int | None:
    for wt, val in fields.get(field_no, ()):
        if wt == 0 and isinstance(val, int):
            return _to_signed_int32_or_int64(val)
    return None


def _read_int_series(
    fields: Mapping[int, list[tuple[int, int | bytes]]], field_no: int
) -> list[int]:
    out: list[int] = []
    for wt, val in fields.get(field_no, ()):
        if wt == 2 and isinstance(val, bytes):
            out.extend(read_packed_varints(val))
        elif wt == 0 and isinstance(val, int):
            out.append(_to_signed_int32_or_int64(val))
    return out


def _read_float_series(
    fields: Mapping[int, list[tuple[int, int | bytes]]], field_no: int
) -> list[float]:
    out: list[float] = []
    for wt, val in fields.get(field_no, ()):
        if wt == 2 and isinstance(val, bytes) and len(val) % 8 == 0:
            count = len(val) // 8
            out.extend(struct.unpack(f"<{count}d", val))
        elif wt == 1 and isinstance(val, bytes):
            out.append(struct.unpack("<d", val)[0])
        elif wt == 5 and isinstance(val, bytes):
            out.append(struct.unpack("<f", val)[0])
    return out


def decode_search_result(
    blob: bytes,
    dictionaries: Mapping[str, Sequence[str]] | None = None,
) -> ProtobufSearchResult:
    """Decode `poe.ninja`'s `NinjaSearchResult` protobuf wire format.

    Wire layout:
    - Outer wrapper `field 1` (`bytes`) -> `SearchResult` (or unwrapped `SearchResult` directly)
    - `SearchResult`:
      - `field 1` (`varint`): `total` matching builds
      - `field 2` (repeated `Dimension`): `1=id`, `2=dictionary_id`, `3=Count{1=key, 2=count}`
      - `field 6` (repeated `DictionaryReference`): `1=id`, `2=hash`, `3=properties_hash`
      - `field 12` (repeated `Column`):
        - `1=id` (`"name"`, `"account"`, `"class"`, `"level"`, `"dps"`, `"ehp"`, ...)
        - `6=int_values` (packed varint; resolved via `dictionary_id` if `11` is set)
        - `7=string_values`
        - `8=double_values` (optional float/double metrics)
        - `10=nulls` (packed varint null row indices)
        - `11=dictionary_id`
        - `13=row_count`
        - `14=row_ids` (packed varint row indices for sparse columns)
    """
    dicts = dictionaries or {}
    outer = parse_protobuf_fields(blob)
    if (
        len(outer) == 1
        and 1 in outer
        and len(outer[1]) == 1
        and outer[1][0][0] == 2
        and isinstance(outer[1][0][1], bytes)
    ):
        sr = parse_protobuf_fields(outer[1][0][1])
    else:
        sr = outer

    total = _first_int(sr, 1) or 0

    dict_refs: list[ProtobufDictionaryRef] = []
    for wt, raw in sr.get(6, ()):
        if wt == 2 and isinstance(raw, bytes):
            dm = parse_protobuf_fields(raw)
            did = _first_str(dm, 1) or ""
            dhash = _first_str(dm, 2) or ""
            phash = _first_str(dm, 3)
            if did and dhash:
                dict_refs.append(ProtobufDictionaryRef(id=did, hash=dhash, properties_hash=phash))

    dimensions: dict[str, list[ProtobufDimensionCount]] = {}
    for wt, raw in sr.get(2, ()):
        if wt != 2 or not isinstance(raw, bytes):
            continue
        dm = parse_protobuf_fields(raw)
        dim_id = _first_str(dm, 1) or ""
        dict_id = _first_str(dm, 2) or ""
        vocab = dicts.get(dict_id, ())
        counts: list[ProtobufDimensionCount] = []
        for cwt, craw in dm.get(3, ()):
            if cwt == 2 and isinstance(craw, bytes):
                cm = parse_protobuf_fields(craw)
                key = _first_int(cm, 1) or 0
                cnt = _first_int(cm, 2) or 0
                label = vocab[key] if 0 <= key < len(vocab) else str(key)
                counts.append(ProtobufDimensionCount(key=key, label=label, count=cnt))
        dimensions[dim_id] = counts

    columns: dict[str, list[str | int | float | None]] = {}
    inferred_rows = 0
    for wt, raw in sr.get(12, ()):
        if wt != 2 or not isinstance(raw, bytes):
            continue
        cm = parse_protobuf_fields(raw)
        col_id = _first_str(cm, 1)
        if not col_id:
            continue
        dict_id = _first_str(cm, 11)
        row_count = _first_int(cm, 13)
        ints = _read_int_series(cm, 6)
        strs = [v.decode("utf-8") for swt, v in cm.get(7, ()) if swt == 2 and isinstance(v, bytes)]
        floats = _read_float_series(cm, 8)
        null_set = set(_read_int_series(cm, 10))
        row_ids = _read_int_series(cm, 14)

        vocab = dicts.get(dict_id or "", ())
        raw_vals: list[str | int | float]
        if strs:
            raw_vals = list(strs)
        elif floats and not ints:
            raw_vals = list(floats)
        else:
            raw_vals = [vocab[i] if (dict_id and 0 <= i < len(vocab)) else i for i in ints]

        total_len = row_count if (row_count is not None and row_count > 0) else len(raw_vals)
        if row_ids:
            total_len = max(total_len, max(row_ids, default=-1) + 1)
        if null_set:
            total_len = max(total_len, max(null_set) + 1)

        aligned: list[str | int | float | None] = [None] * total_len
        if row_ids and len(row_ids) == len(raw_vals):
            for r_idx, val in zip(row_ids, raw_vals, strict=False):
                if 0 <= r_idx < total_len and r_idx not in null_set:
                    aligned[r_idx] = val
        else:
            val_iter = iter(raw_vals)
            for r_idx in range(total_len):
                if r_idx in null_set:
                    aligned[r_idx] = None
                else:
                    try:
                        aligned[r_idx] = next(val_iter)
                    except StopIteration:
                        break

        columns[col_id] = aligned
        inferred_rows = max(inferred_rows, len(aligned))

    roster: list[dict[str, str | int | float | None]] = []
    names = columns.get("name") or []
    accounts = columns.get("account") or []
    row_total = min(len(names), len(accounts)) if (names and accounts) else inferred_rows
    for idx in range(row_total):
        row_dict: dict[str, str | int | float | None] = {}
        for col_name, col_vals in columns.items():
            row_dict[col_name] = col_vals[idx] if idx < len(col_vals) else None
        roster.append(row_dict)

    return ProtobufSearchResult(
        total=total,
        dictionaries=dict_refs,
        dimensions=dimensions,
        columns=columns,
        roster=roster,
    )
