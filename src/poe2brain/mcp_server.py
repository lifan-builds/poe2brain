# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import atexit
import base64
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import zlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx

from poe2brain import ATTRIBUTION, LICENSE_SPDX, LICENSE_SUMMARY, __version__
from poe2brain.config import (
    BUNDLED_DATA_DIR,
    CN_FILTER_DIR_HELP,
    CN_IMPORT_HELP,
    DEFAULT_LEAGUE,
    POE2_PATCH,
    REPO_ROOT,
    REPO_URL,
    GameRealm,
    RealmUnavailableError,
    get_cache_dir,
    get_game_realm,
    get_gamedata_db_path,
    get_pob2_paths,
    get_pob_slots_dir,
    require_international,
    set_game_realm,
)
from poe2brain.crafting import (
    CraftingMod,
    CraftingPlanner,
    CraftingState,
    CraftProfitScanner,
    ModPoolEngine,
    RemovedLeverError,
)
from poe2brain.filters import (
    BUILTIN_THEME_PRESETS,
    STRICTNESS_POLICIES,
    FilterGenerator,
    FilterValidator,
    NeverSinkAdapter,
    SoundPackGenerator,
    audit_theme_cvd,
    compose_theme,
    detect_poe2_filter_dir,
    extract_build_filter_context,
    extract_build_filter_context_from_xml,
    get_visual_theme,
    heal_filter_text,
    install_filter_bundle,
    list_visual_themes,
    merge_build_filter_contexts,
    render_filter_preview_html,
)
from poe2brain.filters.preview import resolve_filter_install_dir
from poe2brain.filters.soundpacks import SCALE_INTERVALS
from poe2brain.filters.themes import parse_look_intent
from poe2brain.knowledge import KnowledgeService, MetaService, build_waystone_regex_plan
from poe2brain.models.common import AffixSide, EngineStatus
from poe2brain.models.filters import (
    BuildFilterContext,
    SoundPackManifest,
    SoundPackStyle,
    SoundSynthesisParams,
    StrictnessTier,
    VisualThemeSpec,
)
from poe2brain.models.items import ItemRarity
from poe2brain.ninja import NinjaBuildsService, decode_pob2_code_to_xml
from poe2brain.pob import (
    BuildService,
    LiveSlotUpgradeReport,
    Pob2BridgeError,
    close_default_pob2_bridge,
    get_default_pob2_bridge,
)
from poe2brain.pob.service import _close_default_build_service
from poe2brain.trade import (
    ArbitrageScanner,
    EconomyService,
    Trade2Client,
    TradeAdvisor,
    parse_clipboard_item,
    parsed_item_to_pob2_text,
    split_multi_item_clipboard_dump,
)

_MCP_TOOL_REGISTRY: list[Callable[..., Any]] = []
_MCP_SERVERS: dict[str, Any] = {}


def _mcp_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    _MCP_TOOL_REGISTRY.append(fn)
    return fn


def get_mcp_server(profile: str = "full") -> Any:
    """Lazily import FastMCP and register MCP tools for the full or core profile."""
    from poe2brain.setup_tools import CORE_MCP_TOOL_NAMES, SUPPORTED_MCP_PROFILES

    prof = (profile or "full").strip().lower()
    if prof not in SUPPORTED_MCP_PROFILES:
        raise ValueError(
            f"Unsupported MCP profile {profile!r}; expected one of {SUPPORTED_MCP_PROFILES}"
        )
    if prof == "full":
        existing = globals().get("mcp")
        if existing is not None:
            return existing
    if prof in _MCP_SERVERS:
        return _MCP_SERVERS[prof]
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        "poe2brain",
        instructions=(
            f"{ATTRIBUTION}. License: {LICENSE_SUMMARY}. "
            "Check poe2_status first. If game_realm is null, ask the player to choose "
            "国服 (cn) or 国际服 (international), then call poe2_init with their choice."
        ),
        website_url=REPO_URL,
    )
    server._mcp_server.version = __version__
    for fn in _MCP_TOOL_REGISTRY:
        if prof == "core" and fn.__name__ not in CORE_MCP_TOOL_NAMES:
            continue
        server.tool()(fn)
    _MCP_SERVERS[prof] = server
    if prof == "full":
        globals()["mcp"] = server
    return server


def __getattr__(name: str) -> Any:
    if name == "mcp":
        return get_mcp_server()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_SAFE_SLOT_RE = re.compile(r"[^a-zA-Z0-9._-]+")

_KNOWLEDGE_SVC: KnowledgeService | None = None
_META_SVC: MetaService | None = None
_ECONOMY_SVC: EconomyService | None = None
_TRADE2_CLIENT: Trade2Client | None = None
_TRADE_ADVISOR: TradeAdvisor | None = None
_CRAFTING_PLANNER: CraftingPlanner | None = None
_CRAFT_PROFIT_SCANNER: CraftProfitScanner | None = None
_ARBITRAGE_SCANNER: ArbitrageScanner | None = None
_BUILD_SVC: BuildService | None = None
_NINJA_SVC: NinjaBuildsService | None = None
_FILTER_GEN: FilterGenerator | None = None
_NEVERSINK_ADAPTER: NeverSinkAdapter | None = None
_HYDRATED_SLOTS: dict[tuple[int, int, str], set[str]] = {}


def get_knowledge_service() -> KnowledgeService:
    global _KNOWLEDGE_SVC
    db_path = get_gamedata_db_path()
    if _KNOWLEDGE_SVC is None or _KNOWLEDGE_SVC.db.db_path != db_path:
        _KNOWLEDGE_SVC = KnowledgeService(db=db_path)
    return _KNOWLEDGE_SVC


def _shared_economy_service() -> EconomyService:
    global _ECONOMY_SVC
    if _ECONOMY_SVC is None:
        _ECONOMY_SVC = EconomyService()
    return _ECONOMY_SVC


def get_economy_service() -> EconomyService:
    require_international("Live economy")
    return _shared_economy_service()


def get_trade2_client() -> Trade2Client:
    global _TRADE2_CLIENT
    if _TRADE2_CLIENT is None:
        _TRADE2_CLIENT = Trade2Client(economy=_shared_economy_service())
    return _TRADE2_CLIENT


def get_trade_advisor() -> TradeAdvisor:
    require_international("Trade listings")
    global _TRADE_ADVISOR
    if _TRADE_ADVISOR is None:
        _TRADE_ADVISOR = TradeAdvisor(
            economy=_shared_economy_service(),
            trade2=get_trade2_client(),
        )
    return _TRADE_ADVISOR


def get_crafting_planner() -> CraftingPlanner:
    global _CRAFTING_PLANNER
    db_path = get_gamedata_db_path()
    if _CRAFTING_PLANNER is None or _CRAFTING_PLANNER.engine.db_path != db_path:
        _CRAFTING_PLANNER = CraftingPlanner(ModPoolEngine(db_path=db_path))
    return _CRAFTING_PLANNER


def get_build_service() -> BuildService:
    global _BUILD_SVC
    bridge = get_default_pob2_bridge()
    if _BUILD_SVC is not None and _BUILD_SVC.bridge is not bridge:
        _BUILD_SVC.close()
        _BUILD_SVC = None
    if _BUILD_SVC is None:
        _BUILD_SVC = BuildService(
            bridge=bridge,
            owns_bridge=False,
            trade_client=get_trade2_client(),
            economy_service=_shared_economy_service(),
        )
    return _BUILD_SVC


def get_meta_service() -> MetaService:
    global _META_SVC
    if _META_SVC is None:
        _META_SVC = MetaService(economy_service=_shared_economy_service())
    return _META_SVC


def get_ninja_service() -> NinjaBuildsService:
    require_international("poe.ninja ladder", CN_IMPORT_HELP)
    global _NINJA_SVC
    b_svc = get_build_service()
    m_svc = get_meta_service()
    if _NINJA_SVC is None or _NINJA_SVC._build_service is not b_svc:
        _NINJA_SVC = NinjaBuildsService(
            build_service=b_svc,
            meta_service=m_svc,
        )
    return _NINJA_SVC


def get_filter_generator() -> FilterGenerator:
    global _FILTER_GEN
    econ = _shared_economy_service()
    b_svc = get_build_service()
    if (
        _FILTER_GEN is None
        or _FILTER_GEN.economy_service is not econ
        or _FILTER_GEN.build_service is not b_svc
    ):
        _FILTER_GEN = FilterGenerator(
            economy_service=econ,
            build_service=b_svc,
        )
    return _FILTER_GEN


def get_neversink_adapter() -> NeverSinkAdapter:
    global _NEVERSINK_ADAPTER
    gen = get_filter_generator()
    if _NEVERSINK_ADAPTER is None or _NEVERSINK_ADAPTER.generator is not gen:
        _NEVERSINK_ADAPTER = NeverSinkAdapter(generator=gen)
    return _NEVERSINK_ADAPTER


def get_craft_profit_scanner() -> CraftProfitScanner:
    require_international("Craft profit")
    global _CRAFT_PROFIT_SCANNER
    planner = get_crafting_planner()
    if _CRAFT_PROFIT_SCANNER is None or _CRAFT_PROFIT_SCANNER.planner is not planner:
        _CRAFT_PROFIT_SCANNER = CraftProfitScanner(
            planner=planner,
            economy=_shared_economy_service(),
            trade2=get_trade2_client(),
        )
    return _CRAFT_PROFIT_SCANNER


def get_arbitrage_scanner() -> ArbitrageScanner:
    require_international("Exchange arbitrage")
    global _ARBITRAGE_SCANNER
    k_db = get_knowledge_service().db
    if _ARBITRAGE_SCANNER is None or _ARBITRAGE_SCANNER.db.db_path != k_db.db_path:
        _ARBITRAGE_SCANNER = ArbitrageScanner(
            economy=_shared_economy_service(),
            trade2=get_trade2_client(),
            db=k_db,
        )
    return _ARBITRAGE_SCANNER


def get_pob_slot_snapshot_path(slot: str = "main") -> Path:
    safe = _SAFE_SLOT_RE.sub("_", slot.strip()) or "main"
    return get_pob_slots_dir() / f"{safe}.xml"


def _slot_hydration_bucket(svc: BuildService) -> set[str]:
    key = (id(svc.bridge), svc.bridge.state_epoch, str(get_pob_slots_dir()))
    if key not in _HYDRATED_SLOTS:
        _HYDRATED_SLOTS.clear()
    return _HYDRATED_SLOTS.setdefault(key, set())


def _write_pob_slot_xml_atomic(xml_text: str, slot: str = "main") -> Path:
    snap_path = get_pob_slot_snapshot_path(slot)
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=snap_path.parent, prefix=".tmp_slot_", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(xml_text)
        os.replace(tmp_name, snap_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return snap_path


def save_pob_slot_snapshot(svc: BuildService, slot: str = "main") -> Path:
    xml_text = svc.export_build(slot=slot, fmt="xml")
    snap_path = _write_pob_slot_xml_atomic(xml_text, slot=slot)
    _slot_hydration_bucket(svc).add(slot)
    return snap_path


def delete_pob_slot_snapshot(svc: BuildService | None, slot: str) -> None:
    snap_path = get_pob_slot_snapshot_path(slot)
    if snap_path.is_file():
        snap_path.unlink()
    for bucket in _HYDRATED_SLOTS.values():
        bucket.discard(slot)


def ensure_pob_slot_loaded(
    svc: BuildService,
    slot: str = "main",
    *,
    require_existing: bool = True,
) -> bool:
    hydrated = _slot_hydration_bucket(svc)
    if slot in hydrated:
        return True

    snap_path = get_pob_slot_snapshot_path(slot)
    if snap_path.is_file():
        svc.load_build(snap_path, slot=slot)
        hydrated.add(slot)
        return True

    if require_existing:
        raise ValueError(
            f"No PoB2 build loaded in slot {slot!r}. "
            f"Load one first via poe2_pob_load / 'poe2 pob load <code_url_or_file> --slot {slot}'."
        )
    return False


def normalize_pob_objective(objective: str) -> Literal["FullDPS", "TotalEHP", "balanced"]:
    norm = objective.strip().lower()
    if norm in ("dps", "fulldps", "full_dps", "totaldps", "offence", "offense"):
        return "FullDPS"
    if norm in ("ehp", "totalehp", "total_ehp", "defence", "defense", "tank"):
        return "TotalEHP"
    if norm in ("balanced", "both", "hybrid"):
        return "balanced"
    raise ValueError(
        f"Unsupported objective {objective!r} "
        "(expected 'balanced', 'dps'/'FullDPS', or 'ehp'/'TotalEHP')."
    )


def normalize_item_text_for_pob2(raw_text: str) -> str:
    stripped = raw_text.strip()
    if not stripped:
        raise ValueError("Item text cannot be empty.")
    if "\n--------\n" in stripped or stripped.startswith("Item Class:"):
        parsed = parse_clipboard_item(stripped)
        return parsed_item_to_pob2_text(parsed)
    return stripped


def _coerce_str_list(val: Sequence[str] | str | None) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    return [str(x).strip() for x in val if str(x).strip()]


def _coerce_csv_or_list(val: Sequence[str] | str | None) -> list[str]:
    if val is None:
        return []
    raw_items = [val] if isinstance(val, str) else [str(x) for x in val]
    out: list[str] = []
    for item in raw_items:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


def _parse_callouts_arg(
    callouts: Mapping[str, str] | Sequence[str] | str | None,
) -> dict[str, str]:
    if callouts is None:
        return {}
    if isinstance(callouts, Mapping):
        return {
            str(k).strip(): str(v).strip()
            for k, v in callouts.items()
            if str(k).strip() and str(v).strip()
        }
    raw_entries = [callouts] if isinstance(callouts, str) else [str(x) for x in callouts]
    parsed: dict[str, str] = {}
    for entry in raw_entries:
        s = entry.strip()
        if not s:
            continue
        if "=" not in s:
            raise ValueError(
                f"Expected tier=text for custom callout (e.g. 's_plus=Divine drop!'), got {entry!r}"
            )
        tier_k, phrase = s.split("=", 1)
        if not tier_k.strip() or not phrase.strip():
            raise ValueError(f"Expected non-empty tier=text for custom callout, got {entry!r}")
        parsed[tier_k.strip()] = phrase.strip()
    return parsed


def resolve_live_crafting_prices(
    league: str = DEFAULT_LEAGUE,
    *,
    offline_only: bool = False,
) -> tuple[dict[str, float] | None, str]:
    if get_game_realm() == GameRealm.CN:
        return None, "offline_reference_0.5.5_not_tencent_prices"
    if offline_only:
        return None, "fallback_reference_0.5.5"
    try:
        econ = get_economy_service()
        bulk = econ.get_bulk_crafting_prices(league)
        prices: dict[str, float] = {}
        for slug, quote in bulk.items():
            if quote is not None and quote.exalted > 0:
                prices[slug] = float(quote.exalted)
                prices[quote.name] = float(quote.exalted)
        rates = econ.get_reference_rates(league)
        prices["Exalted Orb"] = 1.0
        if rates.divine_exalted > 0:
            prices["Divine Orb"] = float(rates.divine_exalted)
        if rates.chaos_exalted > 0:
            prices["Chaos Orb"] = float(rates.chaos_exalted)
        if prices:
            return prices, f"live_economy:{league}"
    except Exception:
        pass
    return None, "fallback_reference_0.5.5"


def format_domain_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, RealmUnavailableError):
        return {
            "ok": False, "game_realm": "cn", "error_type": "RealmUnavailableError",
            "error": str(exc), "hint": exc.hint,
        }
    if isinstance(exc, RemovedLeverError):
        return {
            "ok": False,
            "error": str(exc),
            "error_type": "RemovedLeverError",
            "lever_name": getattr(exc, "lever_name", None),
            "patch_note": getattr(exc, "patch_note", None),
            "hint": (
                "Use active Patch 0.5.5 crafting levers: Greater/Perfect Basic Orbs, "
                "Lesser/Normal/Greater Essences (Magic->Rare), Sinistral/Dextral Crystallisation + "
                "Perfect Essence or Verisium Alloy (Rare->Rare), Omen of Whittling, "
                "Fracturing Orb, or Ancient Bone Desecration + Omen of Light."
            ),
        }
    if isinstance(exc, Pob2BridgeError):
        return {
            "ok": False,
            "error": str(exc),
            "error_type": "Pob2BridgeError",
            "hint": (
                "Check build share code or item base name (avoid PoE1 or 0.1 'Expert ' bases) "
                "or run './scripts/bootstrap_pob2.sh' if the PoB2 runtime is missing."
            ),
        }
    if isinstance(exc, LookupError) and not isinstance(exc, KeyError):
        msg_low = str(exc).lower()
        hint = (
            "Use 'poe2 ninja search' / poe2_ninja_search() or "
            "'poe2 ninja snapshots' / poe2_ninja_snapshots() to verify account tag "
            "(Account-1234), character name, and overview snapshot."
            if ("poe.ninja" in msg_low or "overview" in msg_low)
            else (
                "Run 'poe2 wiki ascendancy' / poe2_inspect_ascendancy() with no arguments to list "
                "all 8 playable PoE2 Patch 0.5.5 classes and 23 Ascendancies."
            )
        )
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "hint": hint,
        }
    if isinstance(exc, httpx.HTTPError):
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "hint": "Upstream PoE2 trade/economy HTTP call failed or timed out; retry shortly.",
        }
    return {
        "ok": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
        "hint": "Verify input parameters against 'poe2 wiki' or SKILL.md.",
    }


def _safe_tool_call(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:
        return format_domain_error(exc)


def collect_engine_status() -> EngineStatus:
    pob2 = get_pob2_paths()
    db_path = get_gamedata_db_path()
    realm = get_game_realm()
    return EngineStatus(
        game_realm=realm,
        version=__version__,
        attribution=ATTRIBUTION,
        license=LICENSE_SPDX,
        patch=POE2_PATCH,
        default_league=None if realm == GameRealm.CN else DEFAULT_LEAGUE,
        cache_dir=str(get_cache_dir()),
        bundled_data_dir=str(BUNDLED_DATA_DIR),
        gamedata_db_ready=db_path.is_file(),
        pob2_runtime_ready=pob2.is_ready,
        pob2_luajit_path=(
            str(pob2.luajit_bin)
            if pob2.luajit_bin.is_file()
            else (str(pob2.win_lua_dll) if pob2.win_lua_dll.is_file() else None)
        ),
        pob2_src_dir=str(pob2.src_dir) if pob2.src_dir.is_dir() else None,
    )


def execute_craft_odds(
    *,
    base: str,
    ilvl: int = 82,
    lever: str | None = None,
    target: list[str] | str | None = None,
    omen: list[str] | str | None = None,
    item_text: str | None = None,
    protect_existing_targets: bool = True,
    league: str = DEFAULT_LEAGUE,
    offline_prices: bool = False,
) -> dict[str, Any]:
    planner = get_crafting_planner()
    engine = planner.engine
    omens = _coerce_str_list(omen)
    targets = _coerce_str_list(target)

    # Validate lever & omens upfront so RemovedLeverError surfaces immediately
    lv_entry = engine.resolve_lever(lever) if lever else None
    omen_entries = [engine.resolve_lever(om) for om in omens]
    base_entry = engine.resolve_base(base)

    prices, price_source = resolve_live_crafting_prices(league, offline_only=offline_prices)
    quote_league = None if get_game_realm() == GameRealm.CN else league

    if item_text:
        parsed_item = parse_clipboard_item(item_text)
        state = engine.parse_state_from_item(parsed_item)
    else:
        assumed_rarity = ItemRarity.NORMAL
        if lv_entry is not None:
            if "Transmutation" in lv_entry.name or "Alchemy" in lv_entry.name:
                assumed_rarity = ItemRarity.NORMAL
            elif (
                "Augmentation" in lv_entry.name
                or "Regal" in lv_entry.name
                or lv_entry.category == "essence"
            ):
                assumed_rarity = ItemRarity.MAGIC
            else:
                assumed_rarity = ItemRarity.RARE
        state = CraftingState(base=base_entry, ilvl=ilvl, rarity=assumed_rarity)

        # If evaluating a swap/removal/fracture lever on a synthetic blank Rare state,
        # seed neutral removable filler mod(s) so single-step odds are immediately meaningful.
        if lv_entry is not None and not state.mods:
            needs_removable = (
                "Chaos" in lv_entry.name
                or "Annulment" in lv_entry.name
                or lv_entry.category in ("perfect_or_corrupted_essence", "alloy")
            )
            if needs_removable:
                has_light = any(o.lever_id == "omen_of_light" for o in omen_entries)
                req_side = next(
                    (
                        o.omen_side_constraint
                        for o in omen_entries
                        if o.omen_side_constraint is not None
                    ),
                    AffixSide.PREFIX,
                )
                filler = CraftingMod(
                    mod_id="__synthetic_removable__",
                    mod_type="__synthetic_removable__",
                    family_id="__synthetic_removable__",
                    families=("__synthetic_removable__",),
                    affix_side=req_side,
                    display_tier=9,
                    required_level=1,
                    stat_text="Synthetic removable modifier",
                    is_desecrated=has_light,
                )
                state.mods.append(filler)
            elif "Fracturing" in lv_entry.name:
                # 4-mod Rare with 1 target + 3 fillers = optimal 25% fracture baseline
                target_id = "__target__"
                if targets:
                    sp0 = engine.resolve_target(base_entry.poe2db_slug, targets[0], ilvl)
                    if sp0.matching_entries:
                        state.mods.append(CraftingMod.from_entry(sp0.matching_entries[0]))
                        target_id = sp0.matching_entries[0].mod_id
                while len(state.mods) < 4:
                    idx = len(state.mods)
                    state.mods.append(
                        CraftingMod(
                            mod_id=f"__filler_{idx}__" if idx > 0 else target_id,
                            mod_type=f"__filler_{idx}__",
                            family_id=f"__filler_{idx}__",
                            families=(f"__filler_{idx}__",),
                            affix_side=AffixSide.PREFIX if idx < 2 else AffixSide.SUFFIX,
                            display_tier=8,
                            required_level=1,
                            stat_text=f"Filler modifier #{idx}",
                        )
                    )

    if not targets:
        if lv_entry is not None and lv_entry.forced_mods:
            forced = engine._lookup_forced_mod_entry(base_entry, lv_entry)
            if forced is not None:
                targets = [forced.mod_id]
        if not targets:
            pool_name = (
                "desecrated"
                if (
                    lv_entry is not None
                    and any(b in lv_entry.name for b in ("Jawbone", "Rib", "Collarbone"))
                )
                else "normal"
            )
            t1_mods = [
                m
                for m in engine.get_mod_tiers(base_entry.poe2db_slug, pool_name)
                if m.display_tier == 1 and m.required_level <= ilvl and m.weight > 0
            ][:6]
            if t1_mods:
                evals = []
                for m in t1_mods:
                    step_evals = planner.evaluate_step(
                        base_entry.name,
                        ilvl,
                        [m.mod_id],
                        starting_state=state,
                        lever=lever,
                        omens=omens or None,
                        currency_prices_ex=prices,
                        use_fallback_prices=True,
                        protect_existing_targets=protect_existing_targets,
                    )
                    if step_evals:
                        raw_note = step_evals[0].notes or ""
                        note_str = (
                            f"{m.stat_text} (T1, ilvl {m.required_level}); {raw_note}"
                        ).strip("; ")
                        ev = step_evals[0].model_copy(update={"notes": note_str})
                        evals.append(ev)
                primary = evals[0].model_dump(mode="json") if evals else {}
                return {
                    **primary,
                    "base_type": base_entry.name,
                    "ilvl": ilvl,
                    "league": quote_league,
                    "price_source": price_source,
                    "assumed_rarity": state.rarity.value,
                    "evaluations": [e.model_dump(mode="json") for e in evals],
                }

    if (
        lv_entry is not None
        and "Fracturing" in lv_entry.name
        and targets
        and not item_text
        and len(state.mods) >= 4
    ):
        # Evaluate Fracturing Orb directly against the target mod on the 4-mod Rare
        sp0 = engine.resolve_target(base_entry.poe2db_slug, targets[0], ilvl)
        res_f = engine.evaluate_action(
            state, set(sp0.matching_mod_ids), lv_entry.name, omens or None
        )
        from poe2brain.crafting.planner import PriceBook
        from poe2brain.models.crafting import CraftActionEval

        pb = PriceBook.build(prices, use_fallback_prices=True)
        cost_f = pb.action_cost(res_f.lever, res_f.omens, use_category_fallback=True) or 180.0
        exp_att = (1.0 / res_f.hit_probability) if res_f.hit_probability > 0 else None
        ev_obj = CraftActionEval(
            lever_id=res_f.lever.lever_id,
            action_label=res_f.lever.name,
            step_cost_exalted=round(cost_f, 4),
            hit_probability=round(res_f.hit_probability, 6),
            expected_attempts_to_hit=round(exp_att, 3) if exp_att else None,
            expected_cost_to_hit_exalted=round(cost_f * exp_att, 3) if exp_att else None,
            eligible_pool_weight=res_f.eligible_pool_weight,
            target_weight=res_f.target_weight,
            notes=res_f.notes,
        )
        dumped = ev_obj.model_dump(mode="json")
        return {
            **dumped,
            "base_type": base_entry.name,
            "ilvl": ilvl,
            "league": quote_league,
            "price_source": price_source,
            "assumed_rarity": state.rarity.value,
            "evaluations": [dumped],
        }

    evals = planner.evaluate_step(
        base_entry.name,
        ilvl,
        targets,
        starting_state=state,
        lever=lever,
        omens=omens or None,
        currency_prices_ex=prices,
        use_fallback_prices=True,
        protect_existing_targets=protect_existing_targets,
    )
    eval_dicts = [e.model_dump(mode="json") for e in evals]
    primary = eval_dicts[0] if eval_dicts else {}
    return {
        **primary,
        "base_type": base_entry.name,
        "ilvl": ilvl,
        "league": quote_league,
        "price_source": price_source,
        "assumed_rarity": state.rarity.value,
        "protect_existing_targets": protect_existing_targets,
        "targets": targets,
        "evaluations": eval_dicts,
    }


def execute_craft_plan(
    *,
    base: str,
    ilvl: int = 82,
    target: list[str] | str,
    buy_exalted: float | None = None,
    sims: int = 3000,
    base_cost_exalted: float = 0.0,
    item_text: str | None = None,
    trade_url: str | None = None,
    league: str = DEFAULT_LEAGUE,
    offline_prices: bool = False,
) -> dict[str, Any]:
    planner = get_crafting_planner()
    targets = _coerce_str_list(target)
    if not targets:
        raise ValueError("Provide at least one target modifier via --target / target.")

    # Guard against passing a removed lever name as a target accidentally
    for t in targets:
        t_low = t.lower()
        if "homogenis" in t_low or "homogeniz" in t_low or "recombinat" in t_low:
            planner.engine.resolve_lever(t)

    starting_state = parse_clipboard_item(item_text) if item_text else None
    prices, price_source = resolve_live_crafting_prices(league, offline_only=offline_prices)
    plan = planner.plan_craft(
        base=base,
        ilvl=ilvl,
        target_mods=targets,
        currency_prices_ex=prices,
        starting_state=starting_state,
        base_cost_ex=base_cost_exalted,
        buy_price_ex=buy_exalted,
        trade_url=trade_url,
        league=league,
        n_sims=sims,
        use_fallback_prices=True,
    )
    out = plan.model_dump(mode="json")
    out["price_source"] = price_source
    if get_game_realm() == GameRealm.CN:
        out["league"] = None
    return out


def execute_find_upgrades(
    *,
    equip_slot: str,
    max_exalted: float | None = 50.0,
    min_spirit: int = 0,
    securable_only: bool = True,
    slot: str = "main",
    build_source: str | None = None,
    objective: str = "FullDPS",
    stat_filters: list[str] | None = None,
    no_pob: bool = False,
    normalize_craft_topoff: bool = True,
    limit: int = 10,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    require_international("Live trade upgrades")
    svc = get_build_service()
    if build_source:
        svc.load_build(build_source, slot=slot)
        save_pob_slot_snapshot(svc, slot)

    has_build = (not no_pob) and ensure_pob_slot_loaded(svc, slot=slot, require_existing=False)

    if has_build and not stat_filters:
        metric = normalize_pob_objective(objective)
        report: LiveSlotUpgradeReport = svc.find_live_slot_upgrades(
            equip_slot,
            build_slot=slot,
            metric=metric,
            league=league,
            max_price_exalted=max_exalted,
            min_spirit=min_spirit,
            securable_only=securable_only,
            candidate_limit=limit,
            normalize_craft_topoff=normalize_craft_topoff,
        )
        return report.model_dump(mode="json")

    advisor = get_trade_advisor()
    pc_res = advisor.find_trade_candidates(
        league=league,
        slot_or_category=equip_slot,
        stat_filters_or_weights=stat_filters or None,
        max_price_exalted=max_exalted,
        min_spirit=min_spirit if min_spirit > 0 else None,
        securable_only=securable_only,
        limit=limit,
    )
    out = pc_res.model_dump(mode="json")
    out["mode"] = "trade_only"
    out["equipment_slot"] = equip_slot
    return out


# ---------------------------------------------------------------------------
# MCP Tools (1:1 parity with SKILL.md)
# ---------------------------------------------------------------------------


@_mcp_tool
def poe2_init(realm: GameRealm) -> dict[str, Any]:
    """Save the player's choice of 国服 (cn) or 国际服 (international) for CLI and MCP."""
    set_game_realm(GameRealm(realm))
    return {"game_realm": realm}


@_mcp_tool
def poe2_status() -> dict[str, Any]:
    """Verify PoE2 0.5.5 patch, default league, SQLite DB, and headless PoB2 readiness."""
    return _safe_tool_call(lambda: collect_engine_status().model_dump(mode="json"))


@_mcp_tool
def poe2_search_knowledge(
    query: str,
    category: str | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    """FTS5 + exact search across PoE2 0.5.5 mechanics, keywords, gems, uniques, and bases."""

    def _run() -> dict[str, Any]:
        records = get_knowledge_service().search(query, category=category, limit=limit)
        return {
            "query": query,
            "category": category,
            "count": len(records),
            "results": [r.model_dump(mode="json") for r in records],
        }

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_explain_mechanic(topic: str, limit: int = 4, compact: bool = False) -> dict[str, Any]:
    """Return Patch 0.5.5 MechanicFact (poe1_trap, poe2_live_rule, formula) and GGG keywords."""
    return _safe_tool_call(
        lambda: (
            get_knowledge_service()
            .explain_mechanic(topic, limit=limit, compact=compact)
            .model_dump(mode="json")
        )
    )


@_mcp_tool
def poe2_inspect_gem(name: str) -> dict[str, Any]:
    """Inspect PoE2 active, Spirit, Meta, Tiered Support (I/II/III), and Lineage Support gems."""
    return _safe_tool_call(
        lambda: get_knowledge_service().inspect_gem(name).model_dump(mode="json")
    )


@_mcp_tool
def poe2_inspect_base(name: str, ilvl: int = 82) -> dict[str, Any]:
    """Inspect PoE2 base stats, implicits, T1 (display_tier=1) affixes, and PoE1/'Expert' traps."""
    return _safe_tool_call(
        lambda: get_knowledge_service().inspect_base_item(name, ilvl=ilvl).model_dump(mode="json")
    )


@_mcp_tool
def poe2_inspect_unique(name: str) -> dict[str, Any]:
    """Inspect PoE2 unique item base type, implicits, explicits, and is_chanceable status."""
    return _safe_tool_call(
        lambda: get_knowledge_service().inspect_unique(name).model_dump(mode="json")
    )


@_mcp_tool
def poe2_inspect_augment(query: str, limit: int = 8) -> dict[str, Any]:
    """Inspect PoE2 Runes, Soul Cores, Idols, Shaman Bonded bonuses, and Runeforge recipes."""

    def _run() -> dict[str, Any]:
        svc = get_knowledge_service()
        augments = svc.inspect_augment(query, limit=limit)
        recipes = svc.search_runeforge(query, limit=limit)
        return {
            "query": query,
            "augments": [a.model_dump(mode="json") for a in augments],
            "runeforge_recipes": [r.model_dump(mode="json") for r in recipes],
        }

    return _safe_tool_call(_run)


def execute_inspect_ascendancy(
    *,
    name_or_class: str | None = None,
    class_filter: str | None = None,
) -> dict[str, Any]:
    meta = get_meta_service()
    if name_or_class is not None and name_or_class.strip():
        return meta.inspect_ascendancy(name_or_class.strip()).model_dump(mode="json")
    if class_filter is not None and class_filter.strip():
        ascs = meta.list_ascendancies(class_name=class_filter.strip())
        return {
            "mode": "list",
            "patch": POE2_PATCH,
            "class_filter": class_filter.strip(),
            "count": len(ascs),
            "ascendancies": [a.model_dump(mode="json") for a in ascs],
        }
    classes = meta.list_classes()
    ascs = meta.list_ascendancies()
    return {
        "mode": "list",
        "patch": POE2_PATCH,
        "class_count": len(classes),
        "count": len(ascs),
        "classes": [c.model_dump(mode="json") for c in classes],
        "ascendancies": [a.model_dump(mode="json") for a in ascs],
    }


def execute_meta_ladder(
    *,
    league: str = DEFAULT_LEAGUE,
    top_n: int = 200,
    include_economy_signals: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    require_international("Ladder", CN_IMPORT_HELP)
    report = get_meta_service().get_ladder_meta(
        league=league,
        top_n=top_n,
        include_economy_signals=include_economy_signals,
        force_refresh=force_refresh,
    )
    return report.model_dump(mode="json")


@_mcp_tool
def poe2_inspect_ascendancy(
    name_or_class: str | None = None,
    class_filter: str | None = None,
) -> dict[str, Any]:
    """List all 23 PoE2 0.5.5 Ascendancies (8 classes) or inspect notables, choices, and traps."""
    return _safe_tool_call(
        lambda: execute_inspect_ascendancy(
            name_or_class=name_or_class,
            class_filter=class_filter,
        )
    )


def execute_waystone_regex(
    *,
    slot: str | None = None,
    archetypes: Sequence[str] | str | None = None,
    avoid_keywords: Sequence[str] | str | None = None,
    want_keywords: Sequence[str] | str | None = None,
    tablet_want_keywords: Sequence[str] | str | None = None,
    tablet_preset: str | None = None,
    vendor_preset: str | None = None,
    vendor_want_keywords: Sequence[str] | str | None = None,
    relic_preset: str | None = None,
    relic_want_keywords: Sequence[str] | str | None = None,
    stash_want_keywords: Sequence[str] | str | None = None,
    stash_avoid_keywords: Sequence[str] | str | None = None,
    stash_base_group: str | None = None,
    min_waystone_drop_pct: int = 120,
    min_pack_size_pct: int | None = 20,
    item_text: str | None = None,
    include_dangerous: bool = False,
    max_chars: int = 50,
) -> dict[str, Any]:
    build_payload: dict[str, Any] | None = None
    clean_slot = slot.strip() if (slot and slot.strip()) else None
    if clean_slot:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=clean_slot, require_existing=True)
        insp = svc.inspect_build(slot=clean_slot)
        build_payload = {
            **(insp.get("raw_pob_stats") or {}),
            **insp["stats"].model_dump(mode="json"),
        }

    arch_list = _coerce_csv_or_list(archetypes) or None
    avoid_list = _coerce_csv_or_list(avoid_keywords) or None
    want_list = _coerce_csv_or_list(want_keywords) or None
    tablet_list = _coerce_csv_or_list(tablet_want_keywords) or None
    vendor_list = _coerce_csv_or_list(vendor_want_keywords) or None
    relic_list = _coerce_csv_or_list(relic_want_keywords) or None
    stash_want_list = _coerce_csv_or_list(stash_want_keywords) or None
    stash_avoid_list = _coerce_csv_or_list(stash_avoid_keywords) or None
    clean_item = item_text.strip() if (item_text and item_text.strip()) else None

    plan = build_waystone_regex_plan(
        build=build_payload,
        archetypes=arch_list,
        avoid_keywords=avoid_list,
        want_keywords=want_list,
        tablet_want_keywords=tablet_list,
        tablet_preset=tablet_preset,
        vendor_preset=vendor_preset,
        vendor_want_keywords=vendor_list,
        relic_preset=relic_preset,
        relic_want_keywords=relic_list,
        stash_want_keywords=stash_want_list,
        stash_avoid_keywords=stash_avoid_list,
        stash_base_group=stash_base_group,
        min_waystone_drop_pct=min_waystone_drop_pct,
        min_pack_size_pct=min_pack_size_pct,
        waystone_item=clean_item,
        include_dangerous=include_dangerous,
        max_chars=max_chars,
        db=get_knowledge_service().db,
    )
    out = plan.model_dump(mode="json")
    out["slot"] = clean_slot
    return out


@_mcp_tool
def poe2_meta_ladder(
    league: str = DEFAULT_LEAGUE,
    top_n: int = 200,
    include_economy_signals: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Get live PoE2 SC/HC/SSF/HCSSF ladder shares, Ascendancy tier rankings, and chase demand."""
    return _safe_tool_call(
        lambda: execute_meta_ladder(
            league=league,
            top_n=top_n,
            include_economy_signals=include_economy_signals,
            force_refresh=force_refresh,
        )
    )


@_mcp_tool
def poe2_waystone_regex(
    slot: str | None = None,
    archetypes: list[str] | str | None = None,
    avoid_keywords: list[str] | str | None = None,
    want_keywords: list[str] | str | None = None,
    tablet_want_keywords: list[str] | str | None = None,
    tablet_preset: str | None = None,
    vendor_preset: str | None = None,
    vendor_want_keywords: list[str] | str | None = None,
    relic_preset: str | None = None,
    relic_want_keywords: list[str] | str | None = None,
    stash_want_keywords: list[str] | str | None = None,
    stash_avoid_keywords: list[str] | str | None = None,
    stash_base_group: str | None = None,
    min_waystone_drop_pct: int = 120,
    min_pack_size_pct: int | None = 20,
    item_text: str | None = None,
    include_dangerous: bool = False,
    max_chars: int = 50,
) -> dict[str, Any]:
    """Compile general <=50-char PoE2 Stash, Vendor, Crafting, Tablet, Relic & Waystone regexes."""
    return _safe_tool_call(
        lambda: execute_waystone_regex(
            slot=slot,
            archetypes=archetypes,
            avoid_keywords=avoid_keywords,
            want_keywords=want_keywords,
            tablet_want_keywords=tablet_want_keywords,
            tablet_preset=tablet_preset,
            vendor_preset=vendor_preset,
            vendor_want_keywords=vendor_want_keywords,
            relic_preset=relic_preset,
            relic_want_keywords=relic_want_keywords,
            stash_want_keywords=stash_want_keywords,
            stash_avoid_keywords=stash_avoid_keywords,
            stash_base_group=stash_base_group,
            min_waystone_drop_pct=min_waystone_drop_pct,
            min_pack_size_pct=min_pack_size_pct,
            item_text=item_text,
            include_dangerous=include_dangerous,
            max_chars=max_chars,
        )
    )


@_mcp_tool
def poe2_pob_load(
    source: str,
    slot: str = "main",
    level: int | None = None,
) -> dict[str, Any]:
    """Load a PoB code, converted 国服 PoE2DB page, share URL, XML, or GGG .build JSON."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        stats = svc.load_build(source, slot=slot, level=level)
        save_pob_slot_snapshot(svc, slot)
        out = stats.model_dump(mode="json")
        out["slot"] = slot
        return out

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_pob_stats(slot: str = "main") -> dict[str, Any]:
    """Extract FullDPS, TotalEHP, Life/ES/Mana, Spirit, resists, Block, Deflection, and Armour."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        out = svc.get_stats(slot=slot).model_dump(mode="json")
        out["slot"] = slot
        return out

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_pob_audit(
    source: str | None = None,
    slot: str = "main",
    apply_dewater: bool = False,
) -> dict[str, Any]:
    """Dewatering audit: strip PoB-warrior config inflation vs sustainable Pinnacle stats."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        if source:
            svc.load_build(source, slot=slot)
            save_pob_slot_snapshot(svc, slot)
        else:
            ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        audit = svc.audit_build(slot=slot, apply_dewater=apply_dewater)
        if apply_dewater:
            save_pob_slot_snapshot(svc, slot)
        return audit.model_dump(mode="json")

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_pob_eval_item(
    equip_slot: str,
    item_text: str,
    slot: str = "main",
    price_exalted: float | None = None,
) -> dict[str, Any]:
    """Evaluate a candidate item non-destructively via PoB2 GetMiscCalculator()."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        pob_text = normalize_item_text_for_pob2(item_text)
        diff = svc.diff_item(
            pob_text,
            target_slot=equip_slot,
            slot=slot,
            price_exalted=price_exalted,
        )
        return diff.model_dump(mode="json")

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_pob_stat_weights(
    equip_slot: str,
    objective: str = "balanced",
    slot: str = "main",
    league: str = DEFAULT_LEAGUE,
    max_exalted: float | None = None,
    securable_only: bool = True,
) -> dict[str, Any]:
    """Compute marginal PoB2 stat weights for an equipment slot and build a trade2 filter."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        metric = normalize_pob_objective(objective)
        weights = svc.compute_slot_trade_weights(
            equip_slot,
            build_slot=slot,
            metric=metric,
            league=league,
            max_price_exalted=max_exalted,
            securable_only=securable_only,
        )
        return weights.model_dump(mode="json")

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_pob_suggest_nodes(
    mode: Literal["perPoint", "total", "anoint", "megalomaniac"] = "perPoint",
    slot: str = "main",
    top: int = 10,
    stat: str = "FullDPS",
    max_path_dist: int = 8,
    include_keystones: bool = False,
    max_exalted: float | None = None,
    securable_only: bool = True,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    """Rank reachable notables, Amulet anoints (anoint), or Megalomaniac combos + Trade2 search."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        if mode == "megalomaniac":
            rep = svc.find_megalomaniac_upgrades(
                slot=slot,
                stat=stat,
                top_notables=max(top, 15),
                top_combos=top,
                max_exalted=max_exalted,
                securable_only=securable_only,
                league=league,
            )
            out = rep.model_dump(mode="json")
            out["mode"] = "megalomaniac"
            return out
        nodes = svc.rank_passive_upgrades(
            slot=slot,
            mode=mode,
            stat=stat,
            max_path_dist=max_path_dist,
            top_n=top,
            include_keystones=include_keystones,
        )
        return {
            "slot": slot,
            "mode": mode,
            "stat": stat,
            "count": len(nodes),
            "upgrades": [n.model_dump(mode="json") for n in nodes],
        }

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_pob_optimize_runes(
    slot: str = "main",
    objective: Literal["balanced", "dps", "ehp", "spirit", "rarity"] = "balanced",
    max_budget_exalted: float | None = None,
    league: str = DEFAULT_LEAGUE,
    offline_prices: bool = False,
) -> dict[str, Any]:
    """Solve Resist Tetris & optimal Armour/Weapon Rune and Soul Core sockets in PoB2."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        rep = svc.optimize_rune_sockets(
            slot=slot,
            objective=objective,
            max_budget_exalted=max_budget_exalted,
            league=league,
            offline_prices=offline_prices,
        )
        return rep.model_dump(mode="json")

    return _safe_tool_call(_run)


def execute_pob_compare(
    *,
    slot_a: str = "early",
    slot_b: str = "main",
    dewater: bool = False,
) -> dict[str, Any]:
    svc = get_build_service()
    ensure_pob_slot_loaded(svc, slot=slot_a, require_existing=True)
    ensure_pob_slot_loaded(svc, slot=slot_b, require_existing=True)
    comp = svc.compare_slots(slot_a=slot_a, slot_b=slot_b, dewater=dewater)
    return comp.model_dump(mode="json")


def execute_pob_hc_audit(
    *,
    source: str | None = None,
    slot: str = "main",
    top_nodes: int = 5,
    apply_dewater: bool = False,
) -> dict[str, Any]:
    svc = get_build_service()
    if source and source.strip():
        svc.load_build(source.strip(), slot=slot)
        save_pob_slot_snapshot(svc, slot)
    else:
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
    report = svc.audit_hc_rip_risks(
        slot=slot,
        apply_dewater=apply_dewater,
        top_defensive_nodes=top_nodes,
    )
    if apply_dewater:
        save_pob_slot_snapshot(svc, slot)
    return report.model_dump(mode="json")


@_mcp_tool
def poe2_pob_compare(
    slot_a: str = "early",
    slot_b: str = "main",
    dewater: bool = False,
) -> dict[str, Any]:
    """Compare two loaded PoB2 slots across DPS/EHP, gear, skills, passives, and Gold respec."""
    return _safe_tool_call(
        lambda: execute_pob_compare(
            slot_a=slot_a,
            slot_b=slot_b,
            dewater=dewater,
        )
    )


@_mcp_tool
def poe2_pob_hc_audit(
    source: str | None = None,
    slot: str = "main",
    top_nodes: int = 5,
    apply_dewater: bool = False,
) -> dict[str, Any]:
    """Audit Hardcore Pinnacle Boss one-shot Max Hits, Bleed/Poison ES bypass, & Gold respecs."""
    return _safe_tool_call(
        lambda: execute_pob_hc_audit(
            source=source,
            slot=slot,
            top_nodes=top_nodes,
            apply_dewater=apply_dewater,
        )
    )


def parse_config_kv_pairs(
    pairs: Mapping[str, Any] | Sequence[str] | str | None,
) -> dict[str, Any]:
    if pairs is None:
        return {}
    if isinstance(pairs, Mapping):
        return {str(k): v for k, v in pairs.items()}
    items = [pairs] if isinstance(pairs, str) else list(pairs)
    out: dict[str, Any] = {}
    for pair in items:
        if "=" not in pair:
            raise ValueError(f"Expected key=value config override, got {pair!r}")
        k, raw_v = pair.split("=", 1)
        low_v = raw_v.strip().lower()
        if low_v == "true":
            out[k.strip()] = True
        elif low_v == "false":
            out[k.strip()] = False
        else:
            try:
                out[k.strip()] = int(raw_v.strip()) if "." not in raw_v else float(raw_v.strip())
            except ValueError:
                out[k.strip()] = raw_v.strip()
    return out


def execute_pob_calc_breakdown(
    *,
    query: str,
    slot: str = "main",
    skill_group: int | None = None,
    max_mods: int = 40,
) -> dict[str, Any]:
    svc = get_build_service()
    ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
    report = svc.explain_calc(
        query,
        slot=slot,
        skill_group=skill_group,
        max_mods=max_mods,
    )
    return report.model_dump(mode="json")


def execute_pob_supports(
    *,
    slot: str = "main",
    group_index: int | None = None,
    replace_gem_slot: int | None = None,
    replace_gem: str | None = None,
    best_swap: bool = False,
    dewater: bool = False,
    max_tier: int | None = None,
    include_lineage: bool = True,
    exclude_lineage_conflicts: bool = False,
    top_n: int = 15,
) -> dict[str, Any]:
    svc = get_build_service()
    ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
    report = svc.rank_support_gems(
        slot=slot,
        group_index=group_index,
        replace_gem_slot=replace_gem_slot,
        replace_gem=replace_gem,
        best_swap=best_swap,
        dewater=dewater,
        max_tier=max_tier,
        include_lineage=include_lineage,
        exclude_lineage_conflicts=exclude_lineage_conflicts,
        top_n=top_n,
    )
    return report.model_dump(mode="json")


def execute_pob_radius_jewels(
    *,
    jewel_name: str = "Controlled Metamorphosis",
    slot: str = "main",
    stat: str = "FullDPS",
    max_path_dist: int = 10,
    top_n: int = 10,
) -> dict[str, Any]:
    svc = get_build_service()
    ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
    report = svc.rank_radius_jewels(
        jewel_name=jewel_name,
        slot=slot,
        stat=stat,
        max_path_dist=max_path_dist,
        top_n=top_n,
    )
    return report.model_dump(mode="json")


def execute_pob_what_if(
    *,
    slot: str = "main",
    equip_items: Mapping[str, str] | Sequence[str] | None = None,
    edit_slot: str | None = None,
    add_mod_lines: Sequence[str] | str | None = None,
    remove_mod_substrings: Sequence[str] | str | None = None,
    set_quality: int | None = None,
    alloc_node_ids: Sequence[int] | None = None,
    dealloc_node_ids: Sequence[int] | None = None,
    set_config: Mapping[str, Any] | Sequence[str] | str | None = None,
    borrow_from_slot: str | None = None,
    borrow_slots: Sequence[str] | str | None = None,
    borrow_main_skill: bool = False,
    include_scenario_matrix: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    svc = get_build_service()
    ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
    if borrow_from_slot:
        ensure_pob_slot_loaded(svc, slot=borrow_from_slot, require_existing=True)

    resolved_equips: dict[str, str] = {}
    if isinstance(equip_items, Mapping):
        for eq_s, raw_txt in equip_items.items():
            resolved_equips[str(eq_s)] = normalize_item_text_for_pob2(str(raw_txt))
    elif equip_items:
        for entry in equip_items:
            if "=" not in entry:
                raise ValueError(f"Expected 'Slot=ItemTextOrPath' for equip_items, got {entry!r}")
            eq_s, rhs = entry.split("=", 1)
            rhs_str = rhs.strip()
            cand_p = Path(rhs_str).expanduser()
            if "\n" not in rhs_str and cand_p.is_file():
                rhs_str = cand_p.read_text(encoding="utf-8")
            resolved_equips[eq_s.strip()] = normalize_item_text_for_pob2(
                rhs_str.replace("\\n", "\n")
            )

    report = svc.what_if(
        slot=slot,
        equip_items=resolved_equips or None,
        edit_slot=edit_slot,
        add_mod_lines=_coerce_str_list(add_mod_lines) or None,
        remove_mod_substrings=_coerce_str_list(remove_mod_substrings) or None,
        set_quality=set_quality,
        alloc_node_ids=list(alloc_node_ids) if alloc_node_ids else None,
        dealloc_node_ids=list(dealloc_node_ids) if dealloc_node_ids else None,
        set_config=parse_config_kv_pairs(set_config) or None,
        borrow_from_slot=borrow_from_slot,
        borrow_slots=_coerce_csv_or_list(borrow_slots) or None,
        borrow_main_skill=borrow_main_skill,
        include_scenario_matrix=include_scenario_matrix,
        apply=apply,
    )
    if apply:
        save_pob_slot_snapshot(svc, slot)
    return report.model_dump(mode="json")


@_mcp_tool
def poe2_pob_calc_breakdown(
    query: str = "FullDPS",
    slot: str = "main",
    skill_group: int | None = None,
    max_mods: int = 40,
) -> dict[str, Any]:
    """Explain exact PoB2 calculation formulas & modifier provenance (BASE/INC/MORE/FLAG)."""
    return _safe_tool_call(
        lambda: execute_pob_calc_breakdown(
            query=query,
            slot=slot,
            skill_group=skill_group,
            max_mods=max_mods,
        )
    )


@_mcp_tool
def poe2_pob_supports(
    slot: str = "main",
    group_index: int | None = None,
    replace_gem_slot: int | None = None,
    replace_gem: str | None = None,
    best_swap: bool = False,
    dewater: bool = False,
    max_tier: int | None = None,
    include_lineage: bool = True,
    exclude_lineage_conflicts: bool = False,
    top_n: int = 15,
) -> dict[str, Any]:
    """Rank compatible PoE2 0.5.5 Support Gems (Tier I-III + Lineage) with Mana/Spirit checks."""
    return _safe_tool_call(
        lambda: execute_pob_supports(
            slot=slot,
            group_index=group_index,
            replace_gem_slot=replace_gem_slot,
            replace_gem=replace_gem,
            best_swap=best_swap,
            dewater=dewater,
            max_tier=max_tier,
            include_lineage=include_lineage,
            exclude_lineage_conflicts=exclude_lineage_conflicts,
            top_n=top_n,
        )
    )


@_mcp_tool
def poe2_pob_radius_jewels(
    jewel_name: str = "Controlled Metamorphosis",
    slot: str = "main",
    stat: str = "FullDPS",
    max_path_dist: int = 10,
    top_n: int = 10,
) -> dict[str, Any]:
    """Rank Passive Tree Jewel Sockets & rings for Controlled Metamorphosis / Against Darkness."""
    return _safe_tool_call(
        lambda: execute_pob_radius_jewels(
            jewel_name=jewel_name,
            slot=slot,
            stat=stat,
            max_path_dist=max_path_dist,
            top_n=top_n,
        )
    )


@_mcp_tool
def poe2_pob_what_if(
    slot: str = "main",
    equip_items: dict[str, str] | list[str] | None = None,
    edit_slot: str | None = None,
    add_mod_lines: list[str] | str | None = None,
    remove_mod_substrings: list[str] | str | None = None,
    set_quality: int | None = None,
    alloc_node_ids: list[int] | None = None,
    dealloc_node_ids: list[int] | None = None,
    set_config: dict[str, Any] | list[str] | str | None = None,
    borrow_from_slot: str | None = None,
    borrow_slots: list[str] | str | None = None,
    borrow_main_skill: bool = False,
    include_scenario_matrix: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Run compound PoB2 what-if simulations (multi-slot, mod edits, nodes, ref borrowing)."""
    return _safe_tool_call(
        lambda: execute_pob_what_if(
            slot=slot,
            equip_items=equip_items,
            edit_slot=edit_slot,
            add_mod_lines=add_mod_lines,
            remove_mod_substrings=remove_mod_substrings,
            set_quality=set_quality,
            alloc_node_ids=alloc_node_ids,
            dealloc_node_ids=dealloc_node_ids,
            set_config=set_config,
            borrow_from_slot=borrow_from_slot,
            borrow_slots=borrow_slots,
            borrow_main_skill=borrow_main_skill,
            include_scenario_matrix=include_scenario_matrix,
            apply=apply,
        )
    )


def execute_pob_import_local(
    *,
    name_or_path: str | None = None,
    builds_dir: str | None = None,
    slot: str = "main",
    list_only: bool = False,
) -> dict[str, Any]:
    from poe2brain.pob.local_sync import discover_local_pob2_builds, resolve_local_pob2_build

    dir_path = Path(builds_dir).expanduser() if builds_dir else None
    if list_only:
        return discover_local_pob2_builds(builds_dir=dir_path).model_dump(mode="json")

    target_xml = resolve_local_pob2_build(name_or_path, builds_dir=dir_path)
    svc = get_build_service()
    stats = svc.load_build(target_xml, slot=slot, name=target_xml.stem)
    save_pob_slot_snapshot(svc, slot=slot)
    return {
        "slot": slot,
        "source_path": str(target_xml),
        "build_name": target_xml.stem,
        "stats": stats.model_dump(mode="json"),
    }


def execute_pob_import_character(
    *,
    character_name: str | None = None,
    realm: str = "poe2",
    slot: str = "main",
    list_only: bool = False,
) -> dict[str, Any]:
    from poe2brain.pob.oauth import GggOAuthClient

    if get_game_realm() == GameRealm.CN:
        from poe2brain.pob.cn import cn_export_name

        if list_only or not character_name:
            return {"realm": "cn", "instructions": CN_IMPORT_HELP}
        source = character_name.strip()
        if "://" in source:
            if not cn_export_name(source):
                raise ValueError(CN_IMPORT_HELP)
        else:
            try:
                source = decode_pob2_code_to_xml(source)
            except ValueError:
                raise ValueError(CN_IMPORT_HELP) from None
        svc = get_build_service()
        stats = svc.load_build(source, slot=slot)
        save_pob_slot_snapshot(svc, slot)
        return {"realm": "cn", "slot": slot, "stats": stats.model_dump(mode="json")}

    oauth = GggOAuthClient()
    if list_only or not character_name:
        chars = oauth.list_characters(realm=realm)
        return {
            "realm": realm,
            "count": len(chars),
            "characters": [c.model_dump(mode="json") for c in chars],
        }

    raw_json = oauth.fetch_character_json(character_name, realm=realm)
    svc = get_build_service()
    stats = svc.import_ggg_character_json(raw_json, slot=slot, name=character_name)
    save_pob_slot_snapshot(svc, slot=slot)
    return {
        "slot": slot,
        "character_name": character_name,
        "realm": realm,
        "stats": stats.model_dump(mode="json"),
    }


@_mcp_tool
def poe2_pob_import_local(
    name_or_path: str | None = None,
    builds_dir: str | None = None,
    slot: str = "main",
    list_only: bool = False,
) -> dict[str, Any]:
    """Discover or load saved local Path of Building (PoE2) .xml builds without OAuth."""
    return _safe_tool_call(
        lambda: execute_pob_import_local(
            name_or_path=name_or_path,
            builds_dir=builds_dir,
            slot=slot,
            list_only=list_only,
        )
    )


@_mcp_tool
def poe2_pob_import_character(
    character_name: str | None = None,
    realm: str = "poe2",
    slot: str = "main",
    list_only: bool = False,
) -> dict[str, Any]:
    """Import a converted 国服 character page, PoB Code, or GGG account character."""
    return _safe_tool_call(
        lambda: execute_pob_import_character(
            character_name=character_name,
            realm=realm,
            slot=slot,
            list_only=list_only,
        )
    )


def export_pob_slot(
    slot: str = "main",
    fmt: Literal["code", "xml", "ggg_json"] = "code",
) -> dict[str, Any]:
    """Export a PoB2 build slot as an eN... share code, raw XML, or GGG .build JSON."""

    def _run() -> dict[str, Any]:
        if fmt in ("xml", "code"):
            snap_path = get_pob_slot_snapshot_path(slot)
            if snap_path.is_file():
                xml_text = snap_path.read_text(encoding="utf-8")
                if fmt == "xml":
                    return {"slot": slot, "format": fmt, "exported": xml_text}
                compressed = zlib.compress(xml_text.encode("utf-8"), 9)
                code = base64.urlsafe_b64encode(compressed).decode("ascii")
                return {"slot": slot, "format": fmt, "exported": code}
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        exported = svc.export_build(slot=slot, fmt=fmt)
        return {"slot": slot, "format": fmt, "exported": exported}

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_currency_price(
    query: str,
    league: str = DEFAULT_LEAGUE,
    category: str | None = None,
) -> dict[str, Any]:
    """Get live PoE2 CurrencyQuote in {exalted, chaos, divine} (Exalted=1.0) with trend & volume."""

    def _run() -> dict[str, Any]:
        econ = get_economy_service()
        rates = econ.get_reference_rates(league)
        if query.strip().lower() in ("rates", "reference", "all"):
            return rates.model_dump(mode="json")
        quote = econ.get_price(query, league=league, category=category)
        if quote is None:
            matches = econ.search_prices(query, league=league, category=category, limit=5)
            if not matches:
                raise KeyError(f"No market quote found for {query!r} in {league!r}.")
            quote = matches[0]
        out = quote.model_dump(mode="json")
        out["reference_rates"] = {
            "exalted": 1.0,
            "chaos_exalted": rates.chaos_exalted,
            "divine_exalted": rates.divine_exalted,
        }
        return out

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_market_scan(
    category: str | None = None,
    min_volume_divine: float = 5.0,
    league: str = DEFAULT_LEAGUE,
    limit: int = 20,
) -> dict[str, Any]:
    """Scan high-liquidity PoE2 exchange movers, cross-source spreads, and chanceable Uniques."""

    def _run() -> dict[str, Any]:
        econ = get_economy_service()
        rates = econ.get_reference_rates(league)
        opps = econ.scan_market_opportunities(
            league=league,
            category=category,
            min_volume_divine=min_volume_divine,
            limit=limit,
        )
        return {
            "league": league,
            "category": category,
            "min_volume_divine": min_volume_divine,
            "divine_exalted": rates.divine_exalted,
            "chaos_exalted": rates.chaos_exalted,
            "count": len(opps),
            "opportunities": [o.model_dump(mode="json") for o in opps],
        }

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_boss_ev(
    league: str = DEFAULT_LEAGUE,
    encounter: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Compare live PoE2 0.5.5 Pinnacle Boss & Trial entry costs vs chase unique drops."""

    def _run() -> dict[str, Any]:
        econ = get_economy_service()
        return econ.compare_boss_ev(
            league=league,
            encounter=encounter,
            force_refresh=force_refresh,
        )

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_farming_strategy(
    stage: Literal["day1", "early", "mid", "endgame"] = "early",
    budget: str | float | int | None = None,
    focus: Literal["balanced", "steady", "jackpot", "speed"] = "balanced",
    slot: str | None = None,
    league: str = DEFAULT_LEAGUE,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Rank Patch 0.5.5 league-start & endgame Atlas farming loops with live prices,
    Atlas Masters, Tablet/Waystone setups, and optional PoB2 build-fit scoring.
    """

    def _run() -> dict[str, Any]:
        from poe2brain.trade.farming import recommend_farming_strategies

        econ = get_economy_service()
        budget_ex: float | None = None
        if budget is not None:
            parsed = parse_unified_budget(budget)
            if parsed is not None:
                amt, unit = parsed
                if unit == "div":
                    rates = econ.get_reference_rates(league, force_refresh=force_refresh)
                    budget_ex = amt * max(1.0, float(rates.divine_exalted or 450.0))
                else:
                    budget_ex = amt

        b_stats = None
        if slot:
            b_svc = get_build_service()
            ensure_pob_slot_loaded(b_svc, slot=slot, require_existing=True)
            b_stats = b_svc.audit_build(slot=slot).sustainable_pinnacle_stats

        return recommend_farming_strategies(
            econ,
            league=league,
            stage=stage,
            budget_exalted=budget_ex,
            focus=focus,
            build_stats=b_stats,
            slot_name=slot,
            force_refresh=force_refresh,
        )

    return _safe_tool_call(_run)


_BUDGET_AMOUNT_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(d|div|divine|divines|ex|exalt|exalted|exalteds)?\s*$",
    re.IGNORECASE,
)


def parse_unified_budget(
    raw: str | float | int | None,
) -> tuple[float, Literal["ex", "div"]] | None:
    """Parse a unified budget (`'500'`, `'500ex'`, `'5d'`, `'5div'`) into `(amount, unit)`."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val <= 0:
            raise ValueError(f"Budget must be positive, got {raw!r}.")
        return val, "ex"
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    m = _BUDGET_AMOUNT_RE.match(cleaned)
    if not m:
        raise ValueError(
            f"Unsupported budget format {raw!r} (expected e.g. '500', '500ex', '5d', or '5div')."
        )
    val = float(m.group(1))
    if val <= 0:
        raise ValueError(f"Budget must be positive, got {raw!r}.")
    unit_raw = (m.group(2) or "ex").lower()
    unit: Literal["ex", "div"] = "div" if unit_raw.startswith("d") else "ex"
    return val, unit


def execute_scan_arbitrage(
    *,
    budget: str | float | None = None,
    gold: int | None = None,
    min_volume_divine: float = 2.0,
    min_roi_pct: float = 5.0,
    max_investment_divine: float | None = None,
    max_gold_per_cycle: int | None = None,
    starting_exalted: float | None = None,
    gold_budget: int | None = None,
    target_divine: float | None = None,
    live_books: bool = True,
    include_encounter_gated: bool = True,
    limit: int = 25,
    force_refresh: bool = False,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    scanner = get_arbitrage_scanner()
    parsed_budget = parse_unified_budget(budget)
    is_ladder = starting_exalted is not None or gold_budget is not None or target_divine is not None

    if is_ladder:
        eff_start_ex = starting_exalted
        if eff_start_ex is None and parsed_budget is not None:
            amt, unit = parsed_budget
            if unit == "div":
                rates = scanner.economy.get_reference_rates(league)
                div_ex = float(rates.divine_exalted) if rates.divine_exalted > 0 else 450.0
                eff_start_ex = amt * div_ex
            else:
                eff_start_ex = amt
        if eff_start_ex is None:
            eff_start_ex = 250.0
        eff_gold = (
            gold_budget if gold_budget is not None else (gold if gold is not None else 80_000)
        )
        eff_target_div = target_divine if target_divine is not None else 50.0
        ladder = scanner.build_compounding_ladder(
            starting_exalted=eff_start_ex,
            gold_budget=eff_gold,
            target_divine=eff_target_div,
            league=league,
            min_volume_divine=min(min_volume_divine, 0.8),
            include_live_exchange=False,
            force_refresh=force_refresh,
        )
        return ladder.model_dump(mode="json")

    eff_max_inv_div = max_investment_divine
    if eff_max_inv_div is None and parsed_budget is not None:
        amt, unit = parsed_budget
        if unit == "div":
            eff_max_inv_div = amt
        else:
            rates = scanner.economy.get_reference_rates(league)
            div_ex = float(rates.divine_exalted) if rates.divine_exalted > 0 else 450.0
            eff_max_inv_div = round(amt / div_ex, 4)

    eff_max_gold = max_gold_per_cycle if max_gold_per_cycle is not None else gold

    report = scanner.scan_all(
        league=league,
        min_volume_divine=min_volume_divine,
        min_roi_pct=min_roi_pct,
        max_investment_divine=eff_max_inv_div,
        max_gold_per_cycle=eff_max_gold,
        include_live_exchange=live_books,
        include_encounter_gated=include_encounter_gated,
        limit=limit,
        force_refresh=force_refresh,
    )
    return report.model_dump(mode="json")


@_mcp_tool
def poe2_scan_arbitrage(
    budget: str | float | None = None,
    gold: int | None = None,
    min_volume_divine: float = 2.0,
    min_roi_pct: float = 5.0,
    max_investment_divine: float | None = None,
    max_gold_per_cycle: int | None = None,
    starting_exalted: float | None = None,
    gold_budget: int | None = None,
    target_divine: float | None = None,
    live_books: bool = True,
    include_encounter_gated: bool = True,
    limit: int = 25,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    """Scan PoE2 0.5.5 arbitrage plays or build a Gold ladder (unified budget '500'/'5d' + gold)."""
    return _safe_tool_call(
        lambda: execute_scan_arbitrage(
            budget=budget,
            gold=gold,
            min_volume_divine=min_volume_divine,
            min_roi_pct=min_roi_pct,
            max_investment_divine=max_investment_divine,
            max_gold_per_cycle=max_gold_per_cycle,
            starting_exalted=starting_exalted,
            gold_budget=gold_budget,
            target_divine=target_divine,
            live_books=live_books,
            include_encounter_gated=include_encounter_gated,
            limit=limit,
            league=league,
        )
    )


@_mcp_tool
def poe2_parse_item(item_text: str) -> dict[str, Any]:
    """Parse PoE2 Ctrl+C / Ctrl+Alt+C clipboard text into ParsedItem plus PoB2-ready item text."""

    def _run() -> dict[str, Any]:
        parsed = parse_clipboard_item(item_text)
        out = parsed.model_dump(mode="json")
        out["pob_item_text"] = parsed_item_to_pob2_text(parsed)
        return out

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_price_check(
    item_text: str,
    securable_only: bool = True,
    league: str = DEFAULT_LEAGUE,
    roll_tolerance: float = 0.9,
) -> dict[str, Any]:
    """Price-check a PoE2 item across live exchange and trade2 listings with fixer warnings."""

    def _run() -> dict[str, Any]:
        res = get_trade_advisor().price_check_item(
            item_text,
            league=league,
            roll_tolerance=roll_tolerance,
            securable_only=securable_only,
        )
        return res.model_dump(mode="json")

    return _safe_tool_call(_run)


@_mcp_tool
def poe2_find_upgrades(
    equip_slot: str,
    max_exalted: float | None = 50.0,
    min_spirit: int = 0,
    securable_only: bool = True,
    slot: str = "main",
    build_source: str | None = None,
    objective: str = "FullDPS",
    league: str = DEFAULT_LEAGUE,
    limit: int = 10,
    no_pob: bool = False,
    normalize_craft_topoff: bool = True,
    stat_filters: list[str] | None = None,
) -> dict[str, Any]:
    """Find live trade2 slot upgrades and rank via headless PoB2 GetMiscCalculator()."""
    return _safe_tool_call(
        lambda: execute_find_upgrades(
            equip_slot=equip_slot,
            max_exalted=max_exalted,
            min_spirit=min_spirit,
            securable_only=securable_only,
            slot=slot,
            build_source=build_source,
            objective=objective,
            stat_filters=stat_filters,
            no_pob=no_pob,
            normalize_craft_topoff=normalize_craft_topoff,
            limit=limit,
            league=league,
        )
    )


def execute_triage_dump(
    *,
    items: Sequence[str] | str,
    min_exalted: float = 5.0,
    roll_tolerance: float = 0.9,
    securable_only: bool = True,
    live_trade: bool = True,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    split_list = split_multi_item_clipboard_dump(items)
    report = get_trade_advisor().triage_clipboard_dump(
        split_list,
        league=league,
        min_sell_exalted=min_exalted,
        roll_tolerance=roll_tolerance,
        securable_only=securable_only,
        live_trade=live_trade,
    )
    return report.model_dump(mode="json")


@_mcp_tool
def poe2_triage_dump(
    items: list[str] | str,
    min_exalted: float = 5.0,
    securable_only: bool = True,
    live_trade: bool = True,
    roll_tolerance: float = 0.9,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    """Batch price-check & classify multi-item PoE2 Ctrl+C dump into sell/craft/unveil/salvage."""
    return _safe_tool_call(
        lambda: execute_triage_dump(
            items=items,
            min_exalted=min_exalted,
            roll_tolerance=roll_tolerance,
            securable_only=securable_only,
            live_trade=live_trade,
            league=league,
        )
    )


@_mcp_tool
def poe2_craft_odds(
    base: str,
    ilvl: int = 82,
    lever: str | None = None,
    target: list[str] | str | None = None,
    omen: list[str] | str | None = None,
    item_text: str | None = None,
    protect_existing_targets: bool = True,
    offline_prices: bool = False,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    """Compute exact PoE2 0.5.5 conditional mod-pool hit odds and step cost (CraftActionEval)."""
    return _safe_tool_call(
        lambda: execute_craft_odds(
            base=base,
            ilvl=ilvl,
            lever=lever,
            target=target,
            omen=omen,
            item_text=item_text,
            protect_existing_targets=protect_existing_targets,
            offline_prices=offline_prices,
            league=league,
        )
    )


@_mcp_tool
def poe2_craft_plan(
    base: str,
    target: list[str] | str,
    ilvl: int = 82,
    buy_exalted: float | None = None,
    sims: int = 3000,
    base_cost_exalted: float = 0.0,
    item_text: str | None = None,
    offline_prices: bool = False,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    """Solve optimal PoE2 0.5.5 crafting policy, Monte Carlo p50/p80/p95 cost, and buy-vs-craft."""
    return _safe_tool_call(
        lambda: execute_craft_plan(
            base=base,
            ilvl=ilvl,
            target=target,
            buy_exalted=buy_exalted,
            sims=sims,
            base_cost_exalted=base_cost_exalted,
            item_text=item_text,
            offline_prices=offline_prices,
            league=league,
        )
    )


def execute_scan_craft_profit(
    *,
    budget: str = "all",
    max_budget_divine: float | None = None,
    live_trade: bool = True,
    offline_prices: bool = False,
    max_trade_queries: int = 8,
    sims: int = 3000,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    norm_budget = budget.strip().lower()
    if norm_budget not in ("all", "low", "mid", "high"):
        raise ValueError(
            f"Unsupported budget tier {budget!r} (expected 'all', 'low', 'mid', or 'high')."
        )
    tier_arg: Literal["low", "mid", "high"] | None = (
        None if norm_budget == "all" else norm_budget  # type: ignore[assignment]
    )
    report = get_craft_profit_scanner().scan(
        league=league,
        budget_tier=tier_arg,
        max_budget_divine=max_budget_divine,
        live_economy=not offline_prices,
        live_trade=(live_trade and not offline_prices),
        max_trade_queries=max_trade_queries,
        n_sims=sims,
    )
    return report.model_dump(mode="json")


@_mcp_tool
def poe2_scan_craft_profit(
    budget: Literal["all", "low", "mid", "high"] = "all",
    max_budget_divine: float | None = None,
    live_trade: bool = True,
    offline_prices: bool = False,
    max_trade_queries: int = 8,
    sims: int = 3000,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    """Scan & rank profitable PoE2 0.5.5 crafts with live costs, p50/p80, exit prices, and ROI."""
    return _safe_tool_call(
        lambda: execute_scan_craft_profit(
            budget=budget,
            max_budget_divine=max_budget_divine,
            live_trade=live_trade,
            offline_prices=offline_prices,
            max_trade_queries=max_trade_queries,
            sims=sims,
            league=league,
        )
    )


# ---------------------------------------------------------------------------
# PoE Ninja Builds (`poe2_ninja_*`) & Loot Filter Studio (`poe2_filter_*`)
# ---------------------------------------------------------------------------


def execute_ninja_sync(
    *,
    league: str = DEFAULT_LEAGUE,
    force_refresh: bool = True,
) -> dict[str, Any]:
    return (
        get_ninja_service()
        .sync_build_index(league=league, force_refresh=force_refresh)
        .model_dump(mode="json")
    )


def execute_ninja_snapshots(
    *,
    league: str = DEFAULT_LEAGUE,
    force_refresh: bool = False,
) -> dict[str, Any]:
    return (
        get_ninja_service()
        .get_snapshot_state(league=league, force_refresh=force_refresh)
        .model_dump(mode="json")
    )


def execute_ninja_search(
    *,
    league: str = DEFAULT_LEAGUE,
    preset: str | None = None,
    class_name: str | None = None,
    ascendancy: str | None = None,
    skill: str | None = None,
    support_gem: str | None = None,
    unique_item: str | None = None,
    exclude_uniques: Sequence[str] | str | None = None,
    max_uniques: int | None = None,
    keystone: str | None = None,
    weapon_type: str | None = None,
    min_dps: float | None = None,
    min_ehp: float | None = None,
    sort_by: str | None = None,
    ssf_viable_first: bool = False,
    limit: int = 15,
    refresh_live_top: int = 0,
    include_pob_code: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    norm_sort: str | None = None
    if sort_by is not None and sort_by.strip():
        norm_sort = sort_by.strip().lower()
        if norm_sort not in ("dps", "ehp", "level", "es", "life", "sleeper", "mobility"):
            raise ValueError(
                f"Unsupported sort_by {sort_by!r} "
                "(expected 'dps', 'ehp', 'level', 'es', 'life', 'sleeper', or 'mobility')."
            )
    excl_list = _coerce_csv_or_list(exclude_uniques) or None
    report = get_ninja_service().search_builds(
        league=league,
        preset=preset,  # type: ignore[arg-type]
        class_name=class_name,
        ascendancy=ascendancy,
        skill=skill,
        support_gem=support_gem,
        unique_item=unique_item,
        exclude_uniques=excl_list,
        max_uniques=max_uniques,
        keystone=keystone,
        weapon_type=weapon_type,
        min_dps=min_dps,
        min_ehp=min_ehp,
        sort_by=norm_sort,  # type: ignore[arg-type]
        ssf_viable_first=ssf_viable_first,
        limit=limit,
        refresh_live_top=refresh_live_top,
        include_pob_code=include_pob_code,
        force_refresh=force_refresh,
    )
    return report.model_dump(mode="json")


def execute_ninja_character(
    *,
    account_or_url: str,
    character: str | None = None,
    league: str = DEFAULT_LEAGUE,
    timemachine: str | None = None,
    probe_timeline: bool = False,
    load_into_pob: bool = False,
    slot: str = "main",
    audit: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    ninja_svc = get_ninja_service()
    if probe_timeline:
        timeline = ninja_svc.probe_character_timeline(
            account_or_url,
            character=character,
            league=league,
            force_refresh=force_refresh,
        )
        return timeline.model_dump(mode="json")

    imp = ninja_svc.fetch_character(
        account_or_url,
        character=character,
        league=league,
        timemachine=timemachine,
        load_into_pob=load_into_pob,
        slot=slot,
        audit=audit,
        force_refresh=force_refresh,
    )
    out = imp.model_dump(mode="json")
    if imp.loaded_into_pob and imp.pob_slot:
        b_svc = ninja_svc._ensure_build_service()
        snap = save_pob_slot_snapshot(b_svc, imp.pob_slot)
        out["snapshot_path"] = str(snap)
    return out


_NEVERSINK_STRICTNESS_ALIASES: dict[str, StrictnessTier] = {
    "soft": StrictnessTier.SOFT,
    "0_soft": StrictnessTier.SOFT,
    "regular": StrictnessTier.REGULAR,
    "1_regular": StrictnessTier.REGULAR,
    "semi_strict": StrictnessTier.SEMI_STRICT,
    "semistrict": StrictnessTier.SEMI_STRICT,
    "2_semi_strict": StrictnessTier.SEMI_STRICT,
    "neversink": StrictnessTier.SEMI_STRICT,
    "strict": StrictnessTier.STRICT,
    "3_strict": StrictnessTier.STRICT,
    "very_strict": StrictnessTier.VERY_STRICT,
    "verystrict": StrictnessTier.VERY_STRICT,
    "4_very_strict": StrictnessTier.VERY_STRICT,
    "uber_strict": StrictnessTier.UBER_STRICT,
    "uberstrict": StrictnessTier.UBER_STRICT,
    "5_uber_strict": StrictnessTier.UBER_STRICT,
    "uber_plus_strict": StrictnessTier.UBER_STRICT,
    "6_uber_plus_strict": StrictnessTier.UBER_STRICT,
}

_SOUND_STYLE_DESCRIPTIONS: dict[SoundPackStyle, str] = {
    SoundPackStyle.CELESTIAL_CHIME: (
        "5-voice stereo-panned FM crystalline bell arpeggio with detuned shimmer."
    ),
    SoundPackStyle.RITUAL_GONG: (
        "Inharmonic bronze gong partials with sub-bass transient punch and stereo beating."
    ),
    SoundPackStyle.CYBER_SYNTH: (
        "Detuned band-limited sawtooth synthwave lead with octave sub-bass."
    ),
    SoundPackStyle.ARCADE_RETRO: (
        "25%/50% duty-cycle chiptune pulse arpeggio with triangle sub-bass."
    ),
    SoundPackStyle.ZEN_MINIMAL: (
        "Low-fatigue Rhodes-style electric piano tine ticks for marathon mapping."
    ),
    SoundPackStyle.VOICE_CALLOUT: (
        "Instant 140ms FM chime attack mixed with auto-ducked spoken tier callouts."
    ),
}


def _resolve_strictness_tier(raw: str) -> StrictnessTier:
    cleaned = raw.strip().lower()
    if cleaned.startswith("neversink:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    norm = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    if norm in _NEVERSINK_STRICTNESS_ALIASES:
        return _NEVERSINK_STRICTNESS_ALIASES[norm]
    valid = ", ".join(t.value for t in StrictnessTier)
    raise ValueError(f"Unsupported strictness {raw!r} (expected one of: {valid}).")


def _normalize_cvd_flag(cvd_safe: str | bool | None) -> str | bool | None:
    if cvd_safe is None or cvd_safe is False:
        return None
    if cvd_safe is True:
        return True
    s_val = str(cvd_safe).strip().lower()
    if s_val in ("", "false", "0", "none", "off"):
        return None
    if s_val in ("true", "1", "yes", "on", "cvd", "colorblind", "colorblind_safe"):
        return True
    if s_val in ("deuteranopia", "protanopia", "tritanopia"):
        return s_val
    raise ValueError(
        f"Unsupported cvd_safe {cvd_safe!r} "
        "(expected 'deuteranopia', 'protanopia', 'tritanopia', or true)."
    )


def _resolve_visual_theme(
    theme: str | None,
    look: str | None,
    *,
    cvd_safe: str | bool | None = None,
    default_preset: str | None = "forbidden_abyss",
) -> VisualThemeSpec | None:
    eff_cvd = _normalize_cvd_flag(cvd_safe)
    has_look = bool(look is not None and look.strip())
    if has_look or eff_cvd is not None:
        base_p: str | None = None
        if theme and theme.strip() and theme.strip().lower() != "none":
            norm_base = theme.strip().lower().replace("-", "_")
            base_p = norm_base if norm_base in BUILTIN_THEME_PRESETS else get_visual_theme(theme).id
        return compose_theme(
            look.strip() if (look and look.strip()) else None,
            base_preset=base_p,
            cvd_safe=eff_cvd,
        )
    if theme is not None and theme.strip():
        if theme.strip().lower() == "none":
            return None
        return get_visual_theme(theme)
    if default_preset is not None:
        return get_visual_theme(default_preset)
    return None


def _try_decode_pob_source_to_xml_fast(raw_source: str) -> str | None:
    """Resolve a PoB2 code, XML string, file path, or remote URL to XML without booting LuaJIT."""
    trimmed = raw_source.strip()
    if not trimmed or trimmed.startswith("{"):
        return None
    if (
        len(trimmed) < 512
        and "\n" not in trimmed
        and not trimmed.startswith(("<", "http://", "https://"))
        and Path(trimmed).expanduser().is_file()
    ):
        trimmed = Path(trimmed).expanduser().read_text(encoding="utf-8").strip()
        if trimmed.startswith("{"):
            return None
    if trimmed.lower().startswith(("http://", "https://")):
        ninja_svc = NinjaBuildsService(meta_service=get_meta_service())
        try:
            imp = ninja_svc.fetch_character(trimmed, load_into_pob=False)
            return decode_pob2_code_to_xml(imp.pob_code)
        except Exception:
            return None
        finally:
            ninja_svc.close()
    try:
        return decode_pob2_code_to_xml(trimmed)
    except ValueError:
        return None


def _extract_slot_filter_context_fast(s_name: str) -> BuildFilterContext:
    """Extract BuildFilterContext for a persisted slot without booting LuaJIT when possible."""
    snap_path = get_pob_slot_snapshot_path(s_name)
    if snap_path.is_file():
        xml_text = snap_path.read_text(encoding="utf-8")
        if "<PlayerStat" in xml_text:
            return extract_build_filter_context_from_xml(xml_text)

    if _BUILD_SVC is not None and (_BUILD_SVC.bridge.is_running or _BUILD_SVC.bridge.is_hibernated):
        ensure_pob_slot_loaded(_BUILD_SVC, slot=s_name, require_existing=True)
        return extract_build_filter_context(_BUILD_SVC, slot=s_name)

    if snap_path.is_file():
        b_svc = get_build_service()
        ensure_pob_slot_loaded(b_svc, slot=s_name, require_existing=True)
        return extract_build_filter_context(b_svc, slot=s_name)

    raise ValueError(
        f"No PoB2 build loaded in slot {s_name!r}. "
        f"Load one first via poe2_pob_load / 'poe2 pob load <code_url_or_file> --slot {s_name}'."
    )


def _resolve_build_filter_context(
    *,
    pob: str | None,
    slot: str | None,
    slots: Sequence[str] | str | None = None,
) -> BuildFilterContext | None:
    contexts: list[BuildFilterContext] = []
    extra_slots = _coerce_csv_or_list(slots)

    if pob is not None and pob.strip():
        target_slot = slot.strip() if (slot and slot.strip() and "," not in slot) else "main"
        fast_xml = _try_decode_pob_source_to_xml_fast(pob.strip())
        if fast_xml is not None and "<PlayerStat" in fast_xml:
            _write_pob_slot_xml_atomic(fast_xml, slot=target_slot)
            for bucket in _HYDRATED_SLOTS.values():
                bucket.discard(target_slot)
            contexts.append(extract_build_filter_context_from_xml(fast_xml))
        else:
            b_svc = get_build_service()
            b_svc.load_build(pob.strip(), slot=target_slot)
            save_pob_slot_snapshot(b_svc, target_slot)
            contexts.append(extract_build_filter_context(b_svc, slot=target_slot))
        for s_name in extra_slots:
            if s_name == target_slot:
                continue
            contexts.append(_extract_slot_filter_context_fast(s_name))
    else:
        combined_slots = list(dict.fromkeys([*_coerce_csv_or_list(slot), *extra_slots]))
        for s_name in combined_slots:
            contexts.append(_extract_slot_filter_context_fast(s_name))

    if not contexts:
        return None
    if len(contexts) == 1:
        return contexts[0]
    return merge_build_filter_contexts(contexts)


def _maybe_synthesize_sound_pack(
    *,
    sound_style: str | None,
    musical_key: str = "D",
    scale: str = "lydian",
    warm_night_mode: bool = False,
    lowpass_hz: float | None = None,
    custom_callouts: Mapping[str, str] | Sequence[str] | str | None = None,
    voice_language: str = "en",
    sound_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    preview_html: str | Path | None = None,
    install: bool = False,
    install_dir: str | Path | None = None,
) -> SoundPackManifest | None:
    if sound_style is not None and sound_style.strip().lower() == "none":
        return None

    callout_map = _parse_callouts_arg(custom_callouts)

    if sound_style is None or not sound_style.strip():
        if callout_map:
            style_enum = SoundPackStyle.VOICE_CALLOUT
        elif (
            warm_night_mode
            or lowpass_hz is not None
            or sound_dir is not None
            or preview_html is not None
            or install
        ):
            style_enum = SoundPackStyle.CELESTIAL_CHIME
        else:
            return None
    else:
        norm_style = sound_style.strip().lower().replace("-", "_")
        try:
            style_enum = SoundPackStyle(norm_style)
        except ValueError as exc:
            valid = ", ".join([s.value for s in SoundPackStyle] + ["none"])
            raise ValueError(
                f"Unsupported sound_style {sound_style!r} (expected one of: {valid})."
            ) from exc

    norm_scale = scale.strip().lower()
    if norm_scale not in SCALE_INTERVALS:
        valid_scales = ", ".join(SCALE_INTERVALS)
        raise ValueError(f"Unsupported scale {scale!r} (expected one of: {valid_scales}).")

    params = SoundSynthesisParams(
        musical_key=musical_key.strip().upper() or "D",
        scale=norm_scale,  # type: ignore[arg-type]
        warm_night_mode=bool(warm_night_mode),
        lowpass_hz=lowpass_hz,
        voice_language="zh" if voice_language == "zh" else "en",
        custom_callouts=callout_map,
    )

    pack_id: str | None = None
    if sound_dir is not None:
        sd = Path(sound_dir).expanduser()
        if sd.name.startswith("poe2brain_"):
            output_root = sd.parent
            pack_id = sd.name
        else:
            output_root = sd
    elif out_path is not None:
        output_root = Path(out_path).expanduser().parent
    elif install:
        output_root = (
            Path(install_dir).expanduser() if install_dir is not None else detect_poe2_filter_dir()
        )
    else:
        output_root = get_cache_dir() / "soundpacks"

    return SoundPackGenerator().generate_pack(
        output_root,
        style=style_enum,
        pack_id=pack_id,
        params=params,
        custom_callouts=callout_map or None,
        warm_night_mode=bool(warm_night_mode),
        lowpass_hz=lowpass_hz,
    )


def execute_filter_presets() -> dict[str, Any]:
    cn = get_game_realm() == GameRealm.CN
    filter_dir = None if cn else detect_poe2_filter_dir()
    themes = list_visual_themes()
    return {
        "patch": POE2_PATCH,
        "default_league": None if cn else DEFAULT_LEAGUE,
        "detected_poe2_filter_dir": str(filter_dir) if filter_dir else None,
        "detected_poe2_filter_dir_exists": bool(filter_dir and filter_dir.is_dir()),
        **({"game_realm": "cn", "install_hint": CN_FILTER_DIR_HELP} if cn else {}),
        "theme_count": len(themes),
        "visual_themes": [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "min_contrast_ratio": t.min_contrast_ratio,
                "s_plus_style": {
                    "font_size": t.tiers["s_plus"].font_size,
                    "text_hex": t.tiers["s_plus"].text_color.to_hex(),
                    "border_hex": t.tiers["s_plus"].border_color.to_hex(),
                    "background_hex": t.tiers["s_plus"].background_color.to_hex(),
                    "contrast_ratio": t.tiers["s_plus"].contrast_ratio,
                },
            }
            for t in themes
        ],
        "strictness_count": len(StrictnessTier),
        "strictness_tiers": [
            {
                "id": st.value,
                "min_currency_ex": pol.min_currency_ex,
                "min_endgame_waystone_tier": pol.min_endgame_waystone_tier,
                "min_gold_stack_endgame": pol.min_gold_stack_endgame,
                "min_uncut_gem_level": pol.min_uncut_gem_level,
                "min_unid_tier_generic": pol.min_unid_tier_generic,
                "min_unid_tier_build": pol.min_unid_tier_build,
                "hide_endgame_normal_magic_gear": pol.hide_endgame_normal_magic_gear,
                "hide_endgame_untiered_rares": pol.hide_endgame_untiered_rares,
            }
            for st, pol in STRICTNESS_POLICIES.items()
        ],
        "sound_style_count": len(SoundPackStyle),
        "sound_styles": [
            {
                "id": s.value,
                "description": _SOUND_STYLE_DESCRIPTIONS[s],
            }
            for s in SoundPackStyle
        ],
        "musical_keys": ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"],
        "synth_scales": list(SCALE_INTERVALS.keys()),
    }


def execute_filter_generate(
    *,
    strictness: str = "semi_strict",
    theme: str | None = None,
    look: str | None = None,
    cvd_safe: str | bool | None = None,
    sound_style: str | None = None,
    musical_key: str = "D",
    scale: str = "lydian",
    warm_night_mode: bool = False,
    lowpass_hz: float | None = None,
    custom_callouts: Mapping[str, str] | Sequence[str] | str | None = None,
    pob: str | None = None,
    slot: str | None = None,
    slots: Sequence[str] | str | None = None,
    out_path: str | Path | None = None,
    sound_dir: str | Path | None = None,
    preview_html: str | Path | None = None,
    install: bool = False,
    install_dir: str | Path | None = None,
    filter_name: str | None = None,
    league: str = DEFAULT_LEAGUE,
    offline: bool = False,
) -> dict[str, Any]:
    cn = get_game_realm() == GameRealm.CN
    target_dir = resolve_filter_install_dir(install_dir) if install else None
    strict_tier = _resolve_strictness_tier(strictness)
    look_intent = parse_look_intent(look)
    eff_cvd = _normalize_cvd_flag(cvd_safe if cvd_safe is not None else look_intent["cvd_safe"])
    eff_warm_night = bool(warm_night_mode or look_intent["warm_night_mode"])
    eff_sound_style = sound_style if sound_style is not None else look_intent["sound_style"]
    resolved_theme = _resolve_visual_theme(theme, look, cvd_safe=eff_cvd) or get_visual_theme(
        "forbidden_abyss"
    )
    build_ctx = _resolve_build_filter_context(pob=pob, slot=slot, slots=slots)
    sound_pack = _maybe_synthesize_sound_pack(
        sound_style=eff_sound_style,
        musical_key=musical_key,
        scale=scale,
        warm_night_mode=eff_warm_night,
        lowpass_hz=lowpass_hz,
        custom_callouts=custom_callouts,
        voice_language=look_intent.get("voice_language", "en"),
        sound_dir=sound_dir,
        out_path=out_path,
        preview_html=preview_html,
        install=install,
        install_dir=target_dir,
    )

    resolved_out = Path(out_path).expanduser() if out_path is not None else None
    eff_name = (
        filter_name
        or (resolved_out.stem if resolved_out is not None else None)
        or f"poe2brain_{strict_tier.value}"
    )

    gen = get_filter_generator()
    result = gen.generate(
        filter_name=eff_name,
        strictness=strict_tier,
        theme=resolved_theme,
        sound_pack=sound_pack,
        league=league,
        use_live_economy=not offline and not cn,
        build_context=build_ctx,
        output_path=resolved_out,
    )

    if cn:
        result.league = None
    if sound_pack is not None:
        result.validation = gen.validator.validate_text(
            result.filter_text,
            filter_dir=Path(sound_pack.pack_dir).parent,
        )

    if install:
        install_filter_bundle(result, target_dir=target_dir, sound_pack=sound_pack)

    if preview_html is not None:
        render_filter_preview_html(
            resolved_theme,
            result=result,
            sound_pack=sound_pack,
            output_path=Path(preview_html).expanduser(),
        )

    out = result.model_dump(mode="json")
    if cn:
        out.update(game_realm="cn", price_source="offline_reference_0.5.5_not_tencent_prices")
    out["theme"] = {
        "id": resolved_theme.id,
        "name": resolved_theme.name,
        "style_prompt": resolved_theme.style_prompt,
        "min_contrast_ratio": resolved_theme.min_contrast_ratio,
    }
    if (
        eff_cvd is not None
        or resolved_theme.id == "colorblind_safe"
        or resolved_theme.base_preset == "colorblind_safe"
    ):
        out["cvd_audit"] = audit_theme_cvd(resolved_theme).model_dump(mode="json")
    out["sound_pack"] = sound_pack.model_dump(mode="json") if sound_pack is not None else None
    return out


def execute_filter_restyle(
    *,
    source: str,
    theme: str | None = None,
    look: str | None = None,
    cvd_safe: str | bool | None = None,
    sound_style: str | None = None,
    musical_key: str = "D",
    scale: str = "lydian",
    warm_night_mode: bool = False,
    lowpass_hz: float | None = None,
    custom_callouts: Mapping[str, str] | Sequence[str] | str | None = None,
    pob: str | None = None,
    slot: str | None = None,
    slots: Sequence[str] | str | None = None,
    out_path: str | Path | None = None,
    sound_dir: str | Path | None = None,
    preview_html: str | Path | None = None,
    install: bool = False,
    install_dir: str | Path | None = None,
    filter_name: str | None = None,
    league: str = DEFAULT_LEAGUE,
    offline: bool = False,
    base_only: bool = False,
) -> dict[str, Any]:
    cn = get_game_realm() == GameRealm.CN
    target_dir = resolve_filter_install_dir(install_dir) if install else None
    base_only = base_only or cn
    raw_src = source.strip()
    if not raw_src:
        raise ValueError("source filter path or NeverSink strictness cannot be empty.")

    source_text: str | None = None
    source_path: Path | None = None
    inferred_strictness = StrictnessTier.SEMI_STRICT

    if "\n" in raw_src or raw_src.lstrip().startswith(("Show", "Hide", "Minimal")):
        source_text = source
    else:
        cand_path = Path(raw_src).expanduser()
        if cand_path.is_file():
            source_path = cand_path
            fname_norm = re.sub(r"[^a-z0-9]+", "_", cand_path.stem.lower())
            for alias_key, st_val in sorted(
                _NEVERSINK_STRICTNESS_ALIASES.items(),
                key=lambda kv: len(kv[0]),
                reverse=True,
            ):
                if alias_key in fname_norm:
                    inferred_strictness = st_val
                    break
        else:
            norm_src = re.sub(
                r"[^a-z0-9]+",
                "_",
                raw_src.lower().removeprefix("neversink:"),
            ).strip("_")
            if norm_src in _NEVERSINK_STRICTNESS_ALIASES:
                inferred_strictness = _NEVERSINK_STRICTNESS_ALIASES[norm_src]
            elif raw_src.endswith(".filter") or "/" in raw_src or "\\" in raw_src:
                raise FileNotFoundError(f"Source filter file not found: {raw_src}")
            else:
                inferred_strictness = _resolve_strictness_tier(raw_src)

    look_intent = parse_look_intent(look)
    eff_cvd = _normalize_cvd_flag(cvd_safe if cvd_safe is not None else look_intent["cvd_safe"])
    eff_warm_night = bool(warm_night_mode or look_intent["warm_night_mode"])
    eff_sound_style = sound_style if sound_style is not None else look_intent["sound_style"]
    resolved_theme = _resolve_visual_theme(
        theme, look, cvd_safe=eff_cvd, default_preset=None if base_only else "forbidden_abyss"
    )
    if base_only and not (
        eff_sound_style or custom_callouts or eff_warm_night or lowpass_hz is not None
    ):
        eff_sound_style = "none"
    build_ctx = _resolve_build_filter_context(pob=pob, slot=slot, slots=slots)
    sound_pack = _maybe_synthesize_sound_pack(
        sound_style=eff_sound_style,
        musical_key=musical_key,
        scale=scale,
        warm_night_mode=eff_warm_night,
        lowpass_hz=lowpass_hz,
        custom_callouts=custom_callouts,
        voice_language=look_intent.get("voice_language", "en"),
        sound_dir=sound_dir,
        out_path=out_path,
        preview_html=preview_html,
        install=install,
        install_dir=target_dir,
    )

    resolved_out = Path(out_path).expanduser() if out_path is not None else None
    eff_name = (
        filter_name
        or (resolved_out.stem if resolved_out is not None else None)
        or f"poe2brain_neversink_{inferred_strictness.value}"
    )

    adapter = get_neversink_adapter()
    result = adapter.restyle_and_heal(
        source_filter_text=source_text,
        source_filter_path=source_path,
        strictness=inferred_strictness,
        theme=resolved_theme,
        sound_pack=sound_pack,
        base_only=base_only,
        league=league,
        use_live_economy=not offline and not cn,
        build_context=build_ctx,
        filter_name=eff_name,
        output_path=resolved_out,
    )

    if cn:
        result.league = None
    if sound_pack is not None:
        result.validation = adapter.validator.validate_text(
            result.filter_text,
            filter_dir=Path(sound_pack.pack_dir).parent,
        )

    if install:
        install_filter_bundle(result, target_dir=target_dir, sound_pack=sound_pack)

    if preview_html is not None:
        preview_theme = resolved_theme or get_visual_theme("forbidden_abyss")
        render_filter_preview_html(
            preview_theme,
            result=result,
            sound_pack=sound_pack,
            output_path=Path(preview_html).expanduser(),
        )

    out = result.model_dump(mode="json")
    if cn:
        out.update(game_realm="cn", price_source="source_filter_priorities")
    if resolved_theme is not None and (
        eff_cvd is not None
        or resolved_theme.id == "colorblind_safe"
        or resolved_theme.base_preset == "colorblind_safe"
    ):
        out["cvd_audit"] = audit_theme_cvd(resolved_theme).model_dump(mode="json")
    out["sound_pack"] = sound_pack.model_dump(mode="json") if sound_pack is not None else None
    return out


def execute_filter_heal(
    *,
    filter_file_or_text: str,
    out_path: str | Path | None = None,
    sound_dir: str | Path | None = None,
) -> dict[str, Any]:
    if not filter_file_or_text or not filter_file_or_text.strip():
        raise ValueError("filter_file_or_text cannot be empty.")

    stripped = filter_file_or_text.strip()
    source_path: Path | None = None
    base_dir: Path | None = None

    if "\n" not in stripped and len(stripped) < 1024:
        cand = Path(stripped).expanduser()
        if cand.is_file():
            source_path = cand
            base_dir = cand.parent
            text = cand.read_text(encoding="utf-8")
        elif stripped.endswith(".filter") or "/" in stripped or "\\" in stripped:
            raise FileNotFoundError(f"Filter file not found: {stripped}")
        else:
            text = filter_file_or_text
    else:
        text = filter_file_or_text

    resolved_out = Path(out_path).expanduser() if out_path is not None else None
    resolved_dir = resolved_out.parent if resolved_out is not None else base_dir
    if sound_dir is not None:
        sd = Path(sound_dir).expanduser()
        if (sd / "manifest.json").is_file() and sd.name.startswith("poe2brain_"):
            resolved_dir = sd.parent
        else:
            resolved_dir = sd

    report = heal_filter_text(text, filter_dir=resolved_dir)
    if resolved_out is not None:
        resolved_out.parent.mkdir(parents=True, exist_ok=True)
        resolved_out.write_text(report.healed_text, encoding="utf-8")

    out = report.model_dump(mode="json")
    out["source_path"] = str(source_path) if source_path is not None else None
    out["out_path"] = str(resolved_out) if resolved_out is not None else None
    out["filter_dir"] = str(resolved_dir) if resolved_dir is not None else None
    return out


def execute_filter_validate(
    *,
    filter_file_or_text: str,
    sound_dir: str | Path | None = None,
) -> dict[str, Any]:
    if not filter_file_or_text or not filter_file_or_text.strip():
        raise ValueError("filter_file_or_text cannot be empty.")

    stripped = filter_file_or_text.strip()
    source_path: Path | None = None
    base_dir: Path | None = None

    if "\n" not in stripped and len(stripped) < 1024:
        cand = Path(stripped).expanduser()
        if cand.is_file():
            source_path = cand
            base_dir = cand.parent
            text = cand.read_text(encoding="utf-8")
        elif stripped.endswith(".filter") or "/" in stripped or "\\" in stripped:
            raise FileNotFoundError(f"Filter file not found: {stripped}")
        else:
            text = filter_file_or_text
    else:
        text = filter_file_or_text

    resolved_dir = base_dir
    if sound_dir is not None:
        sd = Path(sound_dir).expanduser()
        if (sd / "manifest.json").is_file() and sd.name.startswith("poe2brain_"):
            resolved_dir = sd.parent
        else:
            resolved_dir = sd

    report = FilterValidator().validate_text(text, filter_dir=resolved_dir)
    out = report.model_dump(mode="json")
    out["source_path"] = str(source_path) if source_path is not None else None
    out["filter_dir"] = str(resolved_dir) if resolved_dir is not None else None
    return out


@_mcp_tool
def poe2_ninja_snapshots(
    league: str = DEFAULT_LEAGUE,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Query live poe.ninja PoE2 build-index-state & snapshot-versions across SC/HC/SSF/HCSSF."""
    return _safe_tool_call(
        lambda: execute_ninja_snapshots(league=league, force_refresh=force_refresh)
    )


@_mcp_tool
def poe2_ninja_search(
    league: str = DEFAULT_LEAGUE,
    preset: Literal["sleeper", "zoom_mapper", "league_start", "boss_killer", "hc_tank"]
    | None = None,
    class_name: str | None = None,
    ascendancy: str | None = None,
    skill: str | None = None,
    support_gem: str | None = None,
    unique_item: str | None = None,
    exclude_uniques: list[str] | str | None = None,
    max_uniques: int | None = None,
    keystone: str | None = None,
    weapon_type: str | None = None,
    min_dps: float | None = None,
    min_ehp: float | None = None,
    sort_by: Literal["dps", "ehp", "level", "es", "life", "sleeper", "mobility"] | None = None,
    ssf_viable_first: bool = False,
    limit: int = 15,
    refresh_live_top: int = 0,
    include_pob_code: bool = False,
) -> dict[str, Any]:
    """Search PoE2 Forbidden Rites builds with presets, !negation, and pre-audited PoB2 metrics."""
    return _safe_tool_call(
        lambda: execute_ninja_search(
            league=league,
            preset=preset,
            class_name=class_name,
            ascendancy=ascendancy,
            skill=skill,
            support_gem=support_gem,
            unique_item=unique_item,
            exclude_uniques=exclude_uniques,
            max_uniques=max_uniques,
            keystone=keystone,
            weapon_type=weapon_type,
            min_dps=min_dps,
            min_ehp=min_ehp,
            sort_by=sort_by,
            ssf_viable_first=ssf_viable_first,
            limit=limit,
            refresh_live_top=refresh_live_top,
            include_pob_code=include_pob_code,
        )
    )


@_mcp_tool
def poe2_ninja_character(
    account_or_url: str,
    character: str | None = None,
    league: str = DEFAULT_LEAGUE,
    timemachine: str | None = None,
    probe_timeline: bool = False,
    load_into_pob: bool = False,
    slot: str = "main",
    audit: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Import a live poe.ninja character, probe time-machine timeline, or load/audit in PoB2."""
    return _safe_tool_call(
        lambda: execute_ninja_character(
            account_or_url=account_or_url,
            character=character,
            league=league,
            timemachine=timemachine,
            probe_timeline=probe_timeline,
            load_into_pob=load_into_pob,
            slot=slot,
            audit=audit,
            force_refresh=force_refresh,
        )
    )


@_mcp_tool
def poe2_filter_presets() -> dict[str, Any]:
    """List PoE2 0.5.5 visual theme presets, strictnesses, soundpack styles, and filter folder."""
    return _safe_tool_call(execute_filter_presets)


@_mcp_tool
def poe2_filter_generate(
    strictness: str = "semi_strict",
    theme: str | None = None,
    look: str | None = None,
    cvd_safe: str | bool | None = None,
    sound_style: str | None = None,
    musical_key: str = "D",
    scale: str = "lydian",
    warm_night_mode: bool = False,
    lowpass_hz: float | None = None,
    custom_callouts: dict[str, str] | list[str] | None = None,
    pob: str | None = None,
    slot: str | None = None,
    slots: list[str] | str | None = None,
    out_path: str | None = None,
    sound_dir: str | None = None,
    preview_html: str | None = None,
    install: bool = False,
    install_dir: str | None = None,
    filter_name: str | None = None,
    league: str = DEFAULT_LEAGUE,
    offline: bool = False,
) -> dict[str, Any]:
    """Generate a native filter from scratch on request. Usually recommend poe2_filter_restyle."""
    return _safe_tool_call(
        lambda: execute_filter_generate(
            strictness=strictness,
            theme=theme,
            look=look,
            cvd_safe=cvd_safe,
            sound_style=sound_style,
            musical_key=musical_key,
            scale=scale,
            warm_night_mode=warm_night_mode,
            lowpass_hz=lowpass_hz,
            custom_callouts=custom_callouts,
            pob=pob,
            slot=slot,
            slots=slots,
            out_path=out_path,
            sound_dir=sound_dir,
            preview_html=preview_html,
            install=install,
            install_dir=install_dir,
            filter_name=filter_name,
            league=league,
            offline=offline,
        )
    )


@_mcp_tool
def poe2_filter_restyle(
    source: str,
    theme: str | None = None,
    look: str | None = None,
    cvd_safe: str | bool | None = None,
    sound_style: str | None = None,
    musical_key: str = "D",
    scale: str = "lydian",
    warm_night_mode: bool = False,
    lowpass_hz: float | None = None,
    custom_callouts: dict[str, str] | list[str] | None = None,
    pob: str | None = None,
    slot: str | None = None,
    slots: list[str] | str | None = None,
    out_path: str | None = None,
    sound_dir: str | None = None,
    preview_html: str | None = None,
    install: bool = False,
    install_dir: str | None = None,
    filter_name: str | None = None,
    league: str = DEFAULT_LEAGUE,
    offline: bool = False,
    base_only: bool = False,
) -> dict[str, Any]:
    """Recommended for creating filters. Branch from a local .filter or NeverSink 'semi_strict'.

    Start with base_only=True to keep the base's priorities, colors and sounds. Pass a look to
    customize its style. International economy and PoB2 rules are available with base_only=False.
    """
    return _safe_tool_call(
        lambda: execute_filter_restyle(
            source=source,
            base_only=base_only,
            theme=theme,
            look=look,
            cvd_safe=cvd_safe,
            sound_style=sound_style,
            musical_key=musical_key,
            scale=scale,
            warm_night_mode=warm_night_mode,
            lowpass_hz=lowpass_hz,
            custom_callouts=custom_callouts,
            pob=pob,
            slot=slot,
            slots=slots,
            out_path=out_path,
            sound_dir=sound_dir,
            preview_html=preview_html,
            install=install,
            install_dir=install_dir,
            filter_name=filter_name,
            league=league,
            offline=offline,
        )
    )


@_mcp_tool
def poe2_filter_validate(
    filter_file_or_text: str,
    sound_dir: str | None = None,
) -> dict[str, Any]:
    """Validate a PoE2 0.5.5 .filter file or text against live BaseTypes, Classes, and grammar."""
    return _safe_tool_call(
        lambda: execute_filter_validate(
            filter_file_or_text=filter_file_or_text,
            sound_dir=sound_dir,
        )
    )


@_mcp_tool
def poe2_filter_heal(
    filter_file_or_text: str,
    out_path: str | None = None,
    sound_dir: str | None = None,
) -> dict[str, Any]:
    """Doctor & auto-heal broken PoE1 / PoE2 0.1 .filter files or text to 0 Patch 0.5.5 errors."""
    return _safe_tool_call(
        lambda: execute_filter_heal(
            filter_file_or_text=filter_file_or_text,
            out_path=out_path,
            sound_dir=sound_dir,
        )
    )


def run_bootstrap_script(
    *,
    force: bool = False,
    cn: bool = False,
    mirror: str | None = None,
) -> dict[str, Any]:
    from poe2brain.pob.bootstrap import bootstrap_pob2_runtime

    paths = bootstrap_pob2_runtime(force=force, cn=cn, mirror=mirror)
    status = collect_engine_status()
    return {
        "ok": True,
        "output": f"PoB2 runtime ready at {paths.root_dir} (src={paths.src_dir})",
        "status": status.model_dump(mode="json"),
    }


def run_sync_gamedata(*, out_path: Path | None = None) -> dict[str, Any]:
    script = REPO_ROOT / "scripts" / "sync_gamedata.py"
    target = out_path or get_gamedata_db_path()
    cmd = [sys.executable, str(script), "--out", str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return {
        "ok": True,
        "db_path": str(target),
        "size_bytes": target.stat().st_size if target.is_file() else 0,
        "output": proc.stdout.strip(),
    }


def close_pob_services() -> None:
    """Close cached PoB2 build services and the default LuaJIT bridge singleton."""
    global _BUILD_SVC
    if _BUILD_SVC is not None:
        try:
            _BUILD_SVC.close()
        except Exception:
            pass
        _BUILD_SVC = None
    _HYDRATED_SLOTS.clear()
    _close_default_build_service()
    close_default_pob2_bridge()


def close_services() -> None:
    """Close cached singleton services (including the LuaJIT Pob2Bridge) cleanly."""
    global _KNOWLEDGE_SVC, _META_SVC, _ECONOMY_SVC, _TRADE2_CLIENT, _TRADE_ADVISOR
    global _CRAFTING_PLANNER, _CRAFT_PROFIT_SCANNER, _ARBITRAGE_SCANNER
    global _BUILD_SVC, _NINJA_SVC, _FILTER_GEN, _NEVERSINK_ADAPTER

    for attr_name in (
        "_BUILD_SVC",
        "_NINJA_SVC",
        "_CRAFT_PROFIT_SCANNER",
        "_ARBITRAGE_SCANNER",
        "_META_SVC",
        "_TRADE2_CLIENT",
        "_ECONOMY_SVC",
    ):
        obj = globals().get(attr_name)
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass
            globals()[attr_name] = None

    if _CRAFTING_PLANNER is not None:
        try:
            _CRAFTING_PLANNER.engine.close()
        except Exception:
            pass
        _CRAFTING_PLANNER = None

    if _KNOWLEDGE_SVC is not None:
        try:
            _KNOWLEDGE_SVC.db.close()
        except Exception:
            pass
        _KNOWLEDGE_SVC = None

    _TRADE_ADVISOR = None
    _FILTER_GEN = None
    _NEVERSINK_ADAPTER = None
    _HYDRATED_SLOTS.clear()
    _close_default_build_service()
    close_default_pob2_bridge()


atexit.register(close_services)


def run_with_service_cleanup(entry_fn: Callable[[], Any]) -> None:
    """Run an entrypoint with SIGTERM + finally cleanup of cached services."""
    is_main_thread = threading.current_thread() is threading.main_thread()
    prev_sigterm: Any = None
    if is_main_thread:

        def _handle_sigterm(signum: int, _frame: object) -> None:
            close_services()
            raise SystemExit(128 + signum)

        prev_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        entry_fn()
    finally:
        close_services()
        if is_main_thread and prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)


def main(argv: list[str] | None = None) -> None:
    import argparse

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(prog="poe2brain-mcp")
    parser.add_argument(
        "--profile",
        choices=("full", "core"),
        default=os.environ.get("POE2BRAIN_MCP_PROFILE", "full").strip().lower() or "full",
        help=(
            "MCP tool profile: 'full' (all 46 tools) or 'core' "
            "(a smaller tool set for DeepSeek/Qwen/small models)."
        ),
    )
    parsed = parser.parse_args(argv)
    run_with_service_cleanup(get_mcp_server(profile=parsed.profile).run)


if __name__ == "__main__":
    main()
