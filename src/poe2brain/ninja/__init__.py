# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from poe2brain.ninja.protobuf import (
    decode_ndic_dictionary,
    decode_search_result,
    encode_ndic_dictionary,
    encode_varint,
    parse_protobuf_fields,
    read_packed_varints,
    read_varint,
)
from poe2brain.ninja.service import (
    POENINJA_BUILD_INDEX_URL,
    POENINJA_CHAR_POB_RAW_URL,
    POENINJA_DICTIONARY_BASE_URL,
    POENINJA_INDEX_STATE_URL,
    POENINJA_SNAPSHOT_VERSIONS_URL,
    NinjaBuildsService,
)
from poe2brain.ninja.xml_inspector import (
    InspectedPob2Build,
    decode_pob2_code_to_xml,
    inspect_pob2_xml_or_code,
)

__all__ = [
    "InspectedPob2Build",
    "NinjaBuildsService",
    "POENINJA_BUILD_INDEX_URL",
    "POENINJA_CHAR_POB_RAW_URL",
    "POENINJA_DICTIONARY_BASE_URL",
    "POENINJA_INDEX_STATE_URL",
    "POENINJA_SNAPSHOT_VERSIONS_URL",
    "decode_ndic_dictionary",
    "decode_pob2_code_to_xml",
    "decode_search_result",
    "encode_ndic_dictionary",
    "encode_varint",
    "inspect_pob2_xml_or_code",
    "parse_protobuf_fields",
    "read_packed_varints",
    "read_varint",
]
