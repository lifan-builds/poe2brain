from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP

from poe2brain import __version__
from poe2brain.config import (
    BUNDLED_DATA_DIR,
    DEFAULT_LEAGUE,
    POE2_PATCH,
    REPO_ROOT,
    get_cache_dir,
    get_gamedata_db_path,
    get_pob2_paths,
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
    compose_theme,
    detect_poe2_filter_dir,
    extract_build_filter_context,
    get_visual_theme,
    install_filter_bundle,
    list_visual_themes,
    render_filter_preview_html,
)
from poe2brain.filters.soundpacks import SCALE_INTERVALS
from poe2brain.knowledge import KnowledgeService, MetaService
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
from poe2brain.ninja import NinjaBuildsService
from poe2brain.pob import (
    BuildService,
    LiveSlotUpgradeReport,
    LiveUpgradeCandidate,
    Pob2Bridge,
    Pob2BridgeError,
)
from poe2brain.trade import (
    ArbitrageScanner,
    EconomyService,
    Trade2Client,
    TradeAdvisor,
    parse_clipboard_item,
    parsed_item_to_pob2_text,
    resolve_natural_stat,
    resolve_slot_or_category,
)

mcp = FastMCP("poe2brain")

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
_HYDRATED_SLOTS: dict[tuple[int, str], set[str]] = {}


def get_knowledge_service() -> KnowledgeService:
    global _KNOWLEDGE_SVC
    db_path = get_gamedata_db_path()
    if _KNOWLEDGE_SVC is None or _KNOWLEDGE_SVC.db.db_path != db_path:
        _KNOWLEDGE_SVC = KnowledgeService(db=db_path)
    return _KNOWLEDGE_SVC


def get_economy_service() -> EconomyService:
    global _ECONOMY_SVC
    if _ECONOMY_SVC is None:
        _ECONOMY_SVC = EconomyService()
    return _ECONOMY_SVC


def get_trade2_client() -> Trade2Client:
    global _TRADE2_CLIENT
    if _TRADE2_CLIENT is None:
        _TRADE2_CLIENT = Trade2Client(economy=get_economy_service())
    return _TRADE2_CLIENT


def get_trade_advisor() -> TradeAdvisor:
    global _TRADE_ADVISOR
    if _TRADE_ADVISOR is None:
        _TRADE_ADVISOR = TradeAdvisor(
            economy=get_economy_service(),
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
    if _BUILD_SVC is None:
        _BUILD_SVC = BuildService(
            bridge=Pob2Bridge(request_timeout=120.0),
            trade_client=get_trade2_client(),
            economy_service=get_economy_service(),
        )
    return _BUILD_SVC


def get_meta_service() -> MetaService:
    global _META_SVC
    if _META_SVC is None:
        _META_SVC = MetaService(economy_service=get_economy_service())
    return _META_SVC


def get_ninja_service() -> NinjaBuildsService:
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
    econ = get_economy_service()
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
    global _CRAFT_PROFIT_SCANNER
    planner = get_crafting_planner()
    if _CRAFT_PROFIT_SCANNER is None or _CRAFT_PROFIT_SCANNER.planner is not planner:
        _CRAFT_PROFIT_SCANNER = CraftProfitScanner(
            planner=planner,
            economy=get_economy_service(),
            trade2=get_trade2_client(),
        )
    return _CRAFT_PROFIT_SCANNER


def get_arbitrage_scanner() -> ArbitrageScanner:
    global _ARBITRAGE_SCANNER
    k_db = get_knowledge_service().db
    if _ARBITRAGE_SCANNER is None or _ARBITRAGE_SCANNER.db.db_path != k_db.db_path:
        _ARBITRAGE_SCANNER = ArbitrageScanner(
            economy=get_economy_service(),
            trade2=get_trade2_client(),
            db=k_db,
        )
    return _ARBITRAGE_SCANNER


def get_pob_slot_snapshot_path(slot: str = "main") -> Path:
    safe = _SAFE_SLOT_RE.sub("_", slot.strip()) or "main"
    return get_cache_dir() / "pob_slots" / f"{safe}.xml"


def _slot_hydration_bucket(svc: BuildService) -> set[str]:
    key = (id(svc), str(get_cache_dir()))
    return _HYDRATED_SLOTS.setdefault(key, set())


def save_pob_slot_snapshot(svc: BuildService, slot: str = "main") -> Path:
    snap_path = get_pob_slot_snapshot_path(slot)
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    xml_text = svc.export_build(slot=slot, fmt="xml")
    fd, tmp_name = tempfile.mkstemp(dir=snap_path.parent, prefix=".tmp_slot_", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(xml_text)
        os.replace(tmp_name, snap_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    _slot_hydration_bucket(svc).add(slot)
    return snap_path


def delete_pob_slot_snapshot(svc: BuildService, slot: str) -> None:
    snap_path = get_pob_slot_snapshot_path(slot)
    if snap_path.is_file():
        snap_path.unlink()
    _slot_hydration_bucket(svc).discard(slot)


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


def _coerce_str_list(val: list[str] | str | None) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    return [str(x).strip() for x in val if str(x).strip()]


def resolve_live_crafting_prices(
    league: str = DEFAULT_LEAGUE,
    *,
    offline_only: bool = False,
) -> tuple[dict[str, float] | None, str]:
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
    return EngineStatus(
        version=__version__,
        patch=POE2_PATCH,
        default_league=DEFAULT_LEAGUE,
        cache_dir=str(get_cache_dir()),
        bundled_data_dir=str(BUNDLED_DATA_DIR),
        gamedata_db_ready=db_path.is_file(),
        pob2_runtime_ready=pob2.is_ready,
        pob2_luajit_path=str(pob2.luajit_bin) if pob2.luajit_bin.is_file() else None,
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
                    "league": league,
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
            "league": league,
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
    )
    eval_dicts = [e.model_dump(mode="json") for e in evals]
    primary = eval_dicts[0] if eval_dicts else {}
    return {
        **primary,
        "base_type": base_entry.name,
        "ilvl": ilvl,
        "league": league,
        "price_source": price_source,
        "assumed_rarity": state.rarity.value,
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
    return out


def execute_find_upgrades(
    *,
    equip_slot: str,
    max_exalted: float | None = 50.0,
    min_spirit: int = 0,
    securable_only: bool = False,
    slot: str = "main",
    build_source: str | None = None,
    objective: str = "FullDPS",
    stat_filters: list[str] | None = None,
    no_pob: bool = False,
    limit: int = 10,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    svc = get_build_service()
    if build_source:
        svc.load_build(build_source, slot=slot)
        save_pob_slot_snapshot(svc, slot)

    has_build = (not no_pob) and ensure_pob_slot_loaded(svc, slot=slot, require_existing=False)

    if has_build and not stat_filters:
        metric = normalize_pob_objective(objective)
        if min_spirit <= 0:
            report: LiveSlotUpgradeReport = svc.find_live_slot_upgrades(
                equip_slot,
                build_slot=slot,
                metric=metric,
                league=league,
                max_price_exalted=max_exalted,
                securable_only=securable_only,
                candidate_limit=limit,
            )
            return report.model_dump(mode="json")

        # Inject min_spirit filter onto PoB2 weighted search payload before fetching & scoring
        slot_weights = svc.compute_slot_trade_weights(
            equip_slot,
            build_slot=slot,
            metric=metric,
            league=league,
            max_price_exalted=max_exalted,
            securable_only=securable_only,
        )
        payload = dict(slot_weights.trade2_weighted_filter)
        query_block = dict(payload.get("query") or {})
        cat = resolve_slot_or_category(equip_slot) or ""
        if cat == "weapon.sceptre":
            filters_block = dict(query_block.get("filters") or {})
            eq_block = dict(filters_block.get("equipment_filters") or {})
            eq_filters = dict(eq_block.get("filters") or {})
            eq_filters["spirit"] = {"min": min_spirit}
            eq_block["filters"] = eq_filters
            filters_block["equipment_filters"] = eq_block
            query_block["filters"] = filters_block
        else:
            spirit_cands = resolve_natural_stat(
                "spirit", category=cat or "accessory.amulet", limit=1
            )
            if spirit_cands and spirit_cands[0].trade_ids:
                stats_list = list(query_block.get("stats") or [])
                stats_list.append(
                    {
                        "type": "count",
                        "value": {"min": 1},
                        "filters": [
                            {"id": tid, "value": {"min": min_spirit}}
                            for tid in spirit_cands[0].trade_ids
                        ],
                    }
                )
                query_block["stats"] = stats_list
        payload["query"] = query_block

        query_id, trade_url, total_matches, listings = svc._execute_trade2_weighted_search(
            league=league,
            payload=payload,
            limit=limit,
        )
        batch_inputs = [
            {
                "id": lst.listing_id or f"cand_{idx}",
                "label": f"{lst.item_name + ' ' if lst.item_name else ''}{lst.base_type}".strip(),
                "targetSlot": equip_slot,
                "itemText": lst.pob_item_text or "",
                "price_exalted": lst.price_exalted,
            }
            for idx, lst in enumerate(listings, start=1)
        ]
        diffs = svc.diff_items_batch(batch_inputs, target_slot=equip_slot, slot=slot)
        listings_by_txt = {lst.pob_item_text: lst for lst in listings if lst.pob_item_text}
        cands: list[LiveUpgradeCandidate] = []
        for idx, diff in enumerate(diffs, start=1):
            matched_lst = listings_by_txt.get(diff.candidate_item_text) or listings[idx - 1]
            cands.append(
                LiveUpgradeCandidate(
                    rank=idx,
                    listing=matched_lst,
                    upgrade_diff=diff,
                    dps_gain_per_exalted=diff.dps_per_exalted,
                )
            )
        cands.sort(key=lambda c: c.upgrade_diff.full_dps_delta, reverse=True)
        for rank_idx, c in enumerate(cands, start=1):
            c.rank = rank_idx
        return LiveSlotUpgradeReport(
            league=league,
            build_slot=slot,
            equipment_slot=equip_slot,
            metric=metric,
            max_price_exalted=max_exalted,
            securable_only=securable_only,
            query_id=query_id,
            trade_url=trade_url,
            total_market_matches=total_matches,
            slot_weights=slot_weights,
            candidates=cands,
        ).model_dump(mode="json")

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


@mcp.tool()
def poe2_status() -> dict[str, Any]:
    """Verify PoE2 0.5.5 patch, default league, SQLite DB, and headless PoB2 readiness."""
    return _safe_tool_call(lambda: collect_engine_status().model_dump(mode="json"))


@mcp.tool()
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


@mcp.tool()
def poe2_explain_mechanic(topic: str, limit: int = 6) -> dict[str, Any]:
    """Return Patch 0.5.5 MechanicFact (poe1_trap, poe2_live_rule, formula) and GGG keywords."""
    return _safe_tool_call(
        lambda: get_knowledge_service().explain_mechanic(topic, limit=limit).model_dump(mode="json")
    )


@mcp.tool()
def poe2_inspect_gem(name: str) -> dict[str, Any]:
    """Inspect PoE2 active, Spirit, Meta, Tiered Support (I/II/III), and Lineage Support gems."""
    return _safe_tool_call(
        lambda: get_knowledge_service().inspect_gem(name).model_dump(mode="json")
    )


@mcp.tool()
def poe2_inspect_base(name: str, ilvl: int = 82) -> dict[str, Any]:
    """Inspect PoE2 base stats, implicits, T1 (display_tier=1) affixes, and PoE1/'Expert' traps."""
    return _safe_tool_call(
        lambda: get_knowledge_service().inspect_base_item(name, ilvl=ilvl).model_dump(mode="json")
    )


@mcp.tool()
def poe2_inspect_unique(name: str) -> dict[str, Any]:
    """Inspect PoE2 unique item base type, implicits, explicits, and is_chanceable status."""
    return _safe_tool_call(
        lambda: get_knowledge_service().inspect_unique(name).model_dump(mode="json")
    )


@mcp.tool()
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
    report = get_meta_service().get_ladder_meta(
        league=league,
        top_n=top_n,
        include_economy_signals=include_economy_signals,
        force_refresh=force_refresh,
    )
    return report.model_dump(mode="json")


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def poe2_pob_load(
    source: str,
    slot: str = "main",
    level: int | None = None,
) -> dict[str, Any]:
    """Load PoB2 eN... code, pobb.in URL, XML, or GGG .build JSON and return BuildStats."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        stats = svc.load_build(source, slot=slot, level=level)
        save_pob_slot_snapshot(svc, slot)
        out = stats.model_dump(mode="json")
        out["slot"] = slot
        return out

    return _safe_tool_call(_run)


@mcp.tool()
def poe2_pob_stats(slot: str = "main") -> dict[str, Any]:
    """Extract FullDPS, TotalEHP, Life/ES/Mana, Spirit, resists, Block, Deflection, and Armour."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        out = svc.get_stats(slot=slot).model_dump(mode="json")
        out["slot"] = slot
        return out

    return _safe_tool_call(_run)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def poe2_pob_stat_weights(
    equip_slot: str,
    objective: str = "balanced",
    slot: str = "main",
    league: str = DEFAULT_LEAGUE,
    max_exalted: float | None = None,
    securable_only: bool = False,
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


@mcp.tool()
def poe2_pob_suggest_nodes(
    mode: Literal["perPoint", "total", "anoint"] = "perPoint",
    slot: str = "main",
    top: int = 10,
    stat: str = "FullDPS",
    max_path_dist: int = 8,
    include_keystones: bool = False,
) -> dict[str, Any]:
    """Rank reachable passive notables (perPoint/total) or full-tree Amulet anoints (anoint)."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
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


def export_pob_slot(
    slot: str = "main",
    fmt: Literal["code", "xml", "ggg_json"] = "code",
) -> dict[str, Any]:
    """Export a PoB2 build slot as an eN... share code, raw XML, or GGG .build JSON."""

    def _run() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        exported = svc.export_build(slot=slot, fmt=fmt)
        return {"slot": slot, "format": fmt, "exported": exported}

    return _safe_tool_call(_run)


@mcp.tool()
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


@mcp.tool()
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


def execute_scan_arbitrage(
    *,
    min_volume_divine: float = 2.0,
    min_roi_pct: float = 5.0,
    live_books: bool = True,
    include_encounter_gated: bool = True,
    limit: int = 25,
    force_refresh: bool = False,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    report = get_arbitrage_scanner().scan_all(
        league=league,
        min_volume_divine=min_volume_divine,
        min_roi_pct=min_roi_pct,
        include_live_exchange=live_books,
        include_encounter_gated=include_encounter_gated,
        limit=limit,
        force_refresh=force_refresh,
    )
    return report.model_dump(mode="json")


@mcp.tool()
def poe2_scan_arbitrage(
    min_volume_divine: float = 2.0,
    min_roi_pct: float = 5.0,
    live_books: bool = True,
    include_encounter_gated: bool = True,
    limit: int = 25,
    league: str = DEFAULT_LEAGUE,
) -> dict[str, Any]:
    """Scan executable PoE2 0.5.5 conversions, Alva spreads, order books, Gold ROI, and traps."""
    return _safe_tool_call(
        lambda: execute_scan_arbitrage(
            min_volume_divine=min_volume_divine,
            min_roi_pct=min_roi_pct,
            live_books=live_books,
            include_encounter_gated=include_encounter_gated,
            limit=limit,
            league=league,
        )
    )


@mcp.tool()
def poe2_parse_item(item_text: str) -> dict[str, Any]:
    """Parse PoE2 Ctrl+C / Ctrl+Alt+C clipboard text into ParsedItem plus PoB2-ready item text."""

    def _run() -> dict[str, Any]:
        parsed = parse_clipboard_item(item_text)
        out = parsed.model_dump(mode="json")
        out["pob_item_text"] = parsed_item_to_pob2_text(parsed)
        return out

    return _safe_tool_call(_run)


@mcp.tool()
def poe2_price_check(
    item_text: str,
    securable_only: bool = False,
    league: str = DEFAULT_LEAGUE,
    roll_tolerance: float = 0.9,
) -> dict[str, Any]:
    """Price-check PoE2 item: splits Instant Buyout (securable Gold fee) vs whisper listings."""

    def _run() -> dict[str, Any]:
        res = get_trade_advisor().price_check_item(
            item_text,
            league=league,
            roll_tolerance=roll_tolerance,
            securable_only=securable_only,
        )
        return res.model_dump(mode="json")

    return _safe_tool_call(_run)


@mcp.tool()
def poe2_find_upgrades(
    equip_slot: str,
    max_exalted: float | None = 50.0,
    min_spirit: int = 0,
    securable_only: bool = False,
    slot: str = "main",
    objective: str = "FullDPS",
    league: str = DEFAULT_LEAGUE,
    limit: int = 10,
    no_pob: bool = False,
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
            objective=objective,
            stat_filters=stat_filters,
            no_pob=no_pob,
            limit=limit,
            league=league,
        )
    )


@mcp.tool()
def poe2_craft_odds(
    base: str,
    ilvl: int = 82,
    lever: str | None = None,
    target: list[str] | str | None = None,
    omen: list[str] | str | None = None,
    item_text: str | None = None,
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
            league=league,
        )
    )


@mcp.tool()
def poe2_craft_plan(
    base: str,
    target: list[str] | str,
    ilvl: int = 82,
    buy_exalted: float | None = None,
    sims: int = 3000,
    base_cost_exalted: float = 0.0,
    item_text: str | None = None,
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


@mcp.tool()
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
    class_name: str | None = None,
    ascendancy: str | None = None,
    skill: str | None = None,
    support_gem: str | None = None,
    unique_item: str | None = None,
    keystone: str | None = None,
    weapon_type: str | None = None,
    min_dps: float | None = None,
    min_ehp: float | None = None,
    sort_by: str = "dps",
    limit: int = 15,
    refresh_live_top: int = 0,
    include_pob_code: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    norm_sort = sort_by.strip().lower()
    if norm_sort not in ("dps", "ehp", "level", "es", "life"):
        raise ValueError(
            f"Unsupported sort_by {sort_by!r} (expected 'dps', 'ehp', 'level', 'es', or 'life')."
        )
    report = get_ninja_service().search_builds(
        league=league,
        class_name=class_name,
        ascendancy=ascendancy,
        skill=skill,
        support_gem=support_gem,
        unique_item=unique_item,
        keystone=keystone,
        weapon_type=weapon_type,
        min_dps=min_dps,
        min_ehp=min_ehp,
        sort_by=norm_sort,  # type: ignore[arg-type]
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
    load_into_pob: bool = False,
    slot: str = "main",
    audit: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    ninja_svc = get_ninja_service()
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


def _resolve_visual_theme(theme: str | None, look: str | None) -> VisualThemeSpec | None:
    if look is not None and look.strip():
        norm_base = theme.strip().lower().replace("-", "_") if theme else None
        base_p = norm_base if norm_base in BUILTIN_THEME_PRESETS else None
        return compose_theme(look.strip(), base_preset=base_p)
    if theme is None or not theme.strip() or theme.strip().lower() == "none":
        return None
    return get_visual_theme(theme)


def _resolve_build_filter_context(
    *,
    pob: str | None,
    slot: str | None,
) -> BuildFilterContext | None:
    if pob is not None and pob.strip():
        b_svc = get_build_service()
        target_slot = slot.strip() if (slot and slot.strip()) else "main"
        b_svc.load_build(pob.strip(), slot=target_slot)
        save_pob_slot_snapshot(b_svc, target_slot)
        return extract_build_filter_context(b_svc, slot=target_slot)
    if slot is not None and slot.strip():
        b_svc = get_build_service()
        target_slot = slot.strip()
        ensure_pob_slot_loaded(b_svc, slot=target_slot, require_existing=True)
        return extract_build_filter_context(b_svc, slot=target_slot)
    return None


def _maybe_synthesize_sound_pack(
    *,
    sound_style: str | None,
    musical_key: str = "D",
    scale: str = "lydian",
    sound_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    preview_html: str | Path | None = None,
    install: bool = False,
    install_dir: str | Path | None = None,
) -> SoundPackManifest | None:
    if sound_style is not None and sound_style.strip().lower() == "none":
        return None

    if sound_style is None or not sound_style.strip():
        if sound_dir is None and preview_html is None and not install:
            return None
        style_enum = SoundPackStyle.CELESTIAL_CHIME
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
    )


def execute_filter_presets() -> dict[str, Any]:
    filter_dir = detect_poe2_filter_dir()
    themes = list_visual_themes()
    return {
        "patch": POE2_PATCH,
        "default_league": DEFAULT_LEAGUE,
        "detected_poe2_filter_dir": str(filter_dir),
        "detected_poe2_filter_dir_exists": filter_dir.is_dir(),
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
    theme: str = "forbidden_abyss",
    look: str | None = None,
    sound_style: str | None = None,
    musical_key: str = "D",
    scale: str = "lydian",
    pob: str | None = None,
    slot: str | None = None,
    out_path: str | Path | None = None,
    sound_dir: str | Path | None = None,
    preview_html: str | Path | None = None,
    install: bool = False,
    install_dir: str | Path | None = None,
    filter_name: str | None = None,
    league: str = DEFAULT_LEAGUE,
    offline: bool = False,
) -> dict[str, Any]:
    strict_tier = _resolve_strictness_tier(strictness)
    resolved_theme = _resolve_visual_theme(theme, look) or get_visual_theme("forbidden_abyss")
    build_ctx = _resolve_build_filter_context(pob=pob, slot=slot)
    sound_pack = _maybe_synthesize_sound_pack(
        sound_style=sound_style,
        musical_key=musical_key,
        scale=scale,
        sound_dir=sound_dir,
        out_path=out_path,
        preview_html=preview_html,
        install=install,
        install_dir=install_dir,
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
        use_live_economy=not offline,
        build_context=build_ctx,
        output_path=resolved_out,
    )

    if sound_pack is not None and sound_dir is not None:
        result.validation = gen.validator.validate_text(
            result.filter_text,
            filter_dir=Path(sound_pack.pack_dir).parent,
        )

    if install:
        target_dir = Path(install_dir).expanduser() if install_dir is not None else None
        install_filter_bundle(result, target_dir=target_dir, sound_pack=sound_pack)

    if preview_html is not None:
        render_filter_preview_html(
            resolved_theme,
            result=result,
            sound_pack=sound_pack,
            output_path=Path(preview_html).expanduser(),
        )

    out = result.model_dump(mode="json")
    out["theme"] = {
        "id": resolved_theme.id,
        "name": resolved_theme.name,
        "style_prompt": resolved_theme.style_prompt,
        "min_contrast_ratio": resolved_theme.min_contrast_ratio,
    }
    out["sound_pack"] = sound_pack.model_dump(mode="json") if sound_pack is not None else None
    return out


def execute_filter_restyle(
    *,
    source: str,
    theme: str | None = "forbidden_abyss",
    look: str | None = None,
    sound_style: str | None = None,
    musical_key: str = "D",
    scale: str = "lydian",
    pob: str | None = None,
    slot: str | None = None,
    out_path: str | Path | None = None,
    sound_dir: str | Path | None = None,
    preview_html: str | Path | None = None,
    install: bool = False,
    install_dir: str | Path | None = None,
    filter_name: str | None = None,
    league: str = DEFAULT_LEAGUE,
    offline: bool = False,
) -> dict[str, Any]:
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

    resolved_theme = _resolve_visual_theme(theme, look)
    build_ctx = _resolve_build_filter_context(pob=pob, slot=slot)
    sound_pack = _maybe_synthesize_sound_pack(
        sound_style=sound_style,
        musical_key=musical_key,
        scale=scale,
        sound_dir=sound_dir,
        out_path=out_path,
        preview_html=preview_html,
        install=install,
        install_dir=install_dir,
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
        league=league,
        use_live_economy=not offline,
        build_context=build_ctx,
        filter_name=eff_name,
        output_path=resolved_out,
    )

    if sound_pack is not None and sound_dir is not None:
        result.validation = adapter.validator.validate_text(
            result.filter_text,
            filter_dir=Path(sound_pack.pack_dir).parent,
        )

    if install:
        target_dir = Path(install_dir).expanduser() if install_dir is not None else None
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
    out["sound_pack"] = sound_pack.model_dump(mode="json") if sound_pack is not None else None
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


@mcp.tool()
def poe2_ninja_snapshots(
    league: str = DEFAULT_LEAGUE,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Query live poe.ninja PoE2 build-index-state & snapshot-versions across SC/HC/SSF/HCSSF."""
    return _safe_tool_call(
        lambda: execute_ninja_snapshots(league=league, force_refresh=force_refresh)
    )


@mcp.tool()
def poe2_ninja_search(
    league: str = DEFAULT_LEAGUE,
    class_name: str | None = None,
    ascendancy: str | None = None,
    skill: str | None = None,
    support_gem: str | None = None,
    unique_item: str | None = None,
    keystone: str | None = None,
    weapon_type: str | None = None,
    min_dps: float | None = None,
    min_ehp: float | None = None,
    sort_by: Literal["dps", "ehp", "level", "es", "life"] = "dps",
    limit: int = 15,
    refresh_live_top: int = 0,
    include_pob_code: bool = False,
) -> dict[str, Any]:
    """Search PoE2 Forbidden Rites poe.ninja builds across class/ascendancy/skill/unique/stats."""
    return _safe_tool_call(
        lambda: execute_ninja_search(
            league=league,
            class_name=class_name,
            ascendancy=ascendancy,
            skill=skill,
            support_gem=support_gem,
            unique_item=unique_item,
            keystone=keystone,
            weapon_type=weapon_type,
            min_dps=min_dps,
            min_ehp=min_ehp,
            sort_by=sort_by,
            limit=limit,
            refresh_live_top=refresh_live_top,
            include_pob_code=include_pob_code,
        )
    )


@mcp.tool()
def poe2_ninja_character(
    account_or_url: str,
    character: str | None = None,
    league: str = DEFAULT_LEAGUE,
    timemachine: str | None = None,
    load_into_pob: bool = False,
    slot: str = "main",
    audit: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Import a live poe.ninja character or PoB2 URL, inspect XML, and optionally load/audit."""
    return _safe_tool_call(
        lambda: execute_ninja_character(
            account_or_url=account_or_url,
            character=character,
            league=league,
            timemachine=timemachine,
            load_into_pob=load_into_pob,
            slot=slot,
            audit=audit,
            force_refresh=force_refresh,
        )
    )


@mcp.tool()
def poe2_filter_presets() -> dict[str, Any]:
    """List PoE2 0.5.5 visual theme presets, strictnesses, soundpack styles, and filter folder."""
    return _safe_tool_call(execute_filter_presets)


@mcp.tool()
def poe2_filter_generate(
    strictness: str = "semi_strict",
    theme: str = "forbidden_abyss",
    look: str | None = None,
    sound_style: str | None = None,
    musical_key: str = "D",
    scale: str = "lydian",
    pob: str | None = None,
    slot: str | None = None,
    out_path: str | None = None,
    sound_dir: str | None = None,
    preview_html: str | None = None,
    install: bool = False,
    install_dir: str | None = None,
    filter_name: str | None = None,
    league: str = DEFAULT_LEAGUE,
    offline: bool = False,
) -> dict[str, Any]:
    """Compile a validated PoE2 0.5.5 .filter + custom theme look + .wav/.mp3 soundpack + HTML."""
    return _safe_tool_call(
        lambda: execute_filter_generate(
            strictness=strictness,
            theme=theme,
            look=look,
            sound_style=sound_style,
            musical_key=musical_key,
            scale=scale,
            pob=pob,
            slot=slot,
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


@mcp.tool()
def poe2_filter_restyle(
    source: str,
    theme: str | None = "forbidden_abyss",
    look: str | None = None,
    sound_style: str | None = None,
    musical_key: str = "D",
    scale: str = "lydian",
    pob: str | None = None,
    slot: str | None = None,
    out_path: str | None = None,
    sound_dir: str | None = None,
    preview_html: str | None = None,
    install: bool = False,
    install_dir: str | None = None,
    filter_name: str | None = None,
    league: str = DEFAULT_LEAGUE,
    offline: bool = False,
) -> dict[str, Any]:
    """Auto-heal 0.5.5 stale BaseTypes, inject economy/PoB2 overrides, and restyle a .filter."""
    return _safe_tool_call(
        lambda: execute_filter_restyle(
            source=source,
            theme=theme,
            look=look,
            sound_style=sound_style,
            musical_key=musical_key,
            scale=scale,
            pob=pob,
            slot=slot,
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


@mcp.tool()
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


def run_bootstrap_script(*, force: bool = False) -> dict[str, Any]:
    script = REPO_ROOT / "scripts" / "bootstrap_pob2.sh"
    cmd = [str(script)] + (["--force"] if force else [])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    status = collect_engine_status()
    return {
        "ok": True,
        "output": proc.stdout.strip(),
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
