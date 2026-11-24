from __future__ import annotations

import atexit
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote as url_quote

from pydantic import Field

from poe2brain.config import (
    DEFAULT_LEAGUE,
    GGG_TRADE2_BASE_URL,
    GGG_TRADE2_WEB_BASE_URL,
)
from poe2brain.http import Poe2HttpClient
from poe2brain.models.builds import (
    BuildStats,
    DewateredAudit,
    ItemUpgradeDiff,
    SlotStatWeights,
    StatWeightEntry,
    StrippedPobFlag,
)
from poe2brain.models.common import Poe2Model, SaleType
from poe2brain.models.trade import TradeListing
from poe2brain.pob.bridge import Pob2Bridge, Pob2BridgeError

_POBBIN_URL_RE = re.compile(
    r"^https?://(?:www\.)?pobb\.in/(?:u/[^/]+/)?([A-Za-z0-9_-]+)(?:/raw)?/?$"
)


class PassiveNodeUpgrade(Poe2Model):
    node_id: int
    string_id: str | None = None
    name: str
    node_type: str = "Notable"
    stats: list[str] = Field(default_factory=list)
    path_dist: int | None = None
    path_node_ids: list[int] = Field(default_factory=list)
    single_node_delta: float = 0.0
    single_node_pct: float = 0.0
    full_path_delta: float = 0.0
    full_path_pct: float = 0.0
    per_point_delta: float = 0.0
    per_point_pct: float = 0.0


class LiveUpgradeCandidate(Poe2Model):
    rank: int
    listing: TradeListing
    upgrade_diff: ItemUpgradeDiff
    dps_gain_per_exalted: float | None = None
    ehp_gain_per_exalted: float | None = None
    warnings: list[str] = Field(default_factory=list)


class LiveSlotUpgradeReport(Poe2Model):
    league: str = DEFAULT_LEAGUE
    build_slot: str = "main"
    equipment_slot: str
    metric: str = "FullDPS"
    max_price_exalted: float | None = None
    securable_only: bool = False
    query_id: str | None = None
    trade_url: str | None = None
    total_market_matches: int = 0
    slot_weights: SlotStatWeights
    candidates: list[LiveUpgradeCandidate] = Field(default_factory=list)


def _active_skill_names(skills_block: Mapping[str, Any] | None) -> tuple[str | None, list[str]]:
    if not isinstance(skills_block, Mapping):
        return None, []
    main_skill: str | None = None
    names: list[str] = []
    for grp in skills_block.get("socketGroups") or []:
        if not isinstance(grp, Mapping) or not grp.get("enabled", True):
            continue
        act_name = grp.get("mainActiveSkillName")
        if isinstance(act_name, str) and act_name:
            if act_name not in names:
                names.append(act_name)
            if grp.get("isMainSkill"):
                main_skill = act_name
        else:
            for gem in grp.get("gems") or []:
                if (
                    isinstance(gem, Mapping)
                    and gem.get("enabled", True)
                    and not gem.get("isSupport")
                    and isinstance(gem.get("name"), str)
                ):
                    gname = gem["name"]
                    if gname not in names:
                        names.append(gname)
                    if grp.get("isMainSkill") and main_skill is None:
                        main_skill = gname
    if main_skill is None and names:
        main_skill = names[0]
    return main_skill, names


def _to_build_stats(
    stats_dict: Mapping[str, Any],
    *,
    character: Mapping[str, Any] | None = None,
    skills: Mapping[str, Any] | None = None,
    tree: Mapping[str, Any] | None = None,
) -> BuildStats:
    main_skill, active_skills = _active_skill_names(skills)
    alloc_count = 0
    if isinstance(tree, Mapping) and isinstance(tree.get("allocatedCount"), int):
        alloc_count = int(tree["allocatedCount"])
    elif isinstance(character, Mapping) and isinstance(character.get("passivePointsUsed"), int):
        alloc_count = int(character["passivePointsUsed"])

    deflect_val = float(
        stats_dict.get(
            "deflection_chance",
            stats_dict.get("DeflectChance", stats_dict.get("Deflection", 0.0)),
        )
        or 0.0
    )
    eff_block = float(
        stats_dict.get("EffectiveBlockChance", stats_dict.get("BlockChance", 0.0)) or 0.0
    )

    payload: dict[str, Any] = {
        **dict(stats_dict),
        "class_name": character.get("className") if isinstance(character, Mapping) else None,
        "ascendancy_name": (
            character.get("ascendancyName") if isinstance(character, Mapping) else None
        ),
        "level": int(character.get("level", 1)) if isinstance(character, Mapping) else 1,
        "main_skill": main_skill,
        "spirit_total": int(round(float(stats_dict.get("Spirit", 0) or 0))),
        "spirit_reserved": int(round(float(stats_dict.get("SpiritReserved", 0) or 0))),
        "spirit_unreserved": int(round(float(stats_dict.get("SpiritUnreserved", 0) or 0))),
        "block_chance": eff_block,
        "deflection_chance": deflect_val,
        "allocated_nodes": alloc_count,
        "active_skills": active_skills,
    }
    return BuildStats.model_validate(payload)


def _format_gem_paste_line(raw_gem: str) -> str:
    trimmed = raw_gem.strip()
    if not trimmed:
        return ""
    # Already in PoB socket-group format ("Spark 20/20  1" or "Slot: Amulet")
    if trimmed.lower().startswith("slot:") or re.search(r"\d+/\d+\s+\d+$", trimmed):
        return trimmed
    # Match optional trailing "20/20" or "Lv20"
    m = re.match(r"^(.*?)\s+(\d+)/(\d+)$", trimmed)
    if m:
        return f"{m.group(1).strip()} {m.group(2)}/{m.group(3)}  1"
    m_lvl = re.match(r"^(.*?)\s+(?:lv|lvl|level)\s*(\d+)$", trimmed, flags=re.IGNORECASE)
    if m_lvl:
        return f"{m_lvl.group(1).strip()} {m_lvl.group(2)}/0  1"
    return f"{trimmed} 20/0  1"


def _to_item_upgrade_diff(
    raw: Mapping[str, Any],
    *,
    item_text: str | None = None,
    price_exalted: float | None = None,
) -> ItemUpgradeDiff:
    before = raw.get("before") or {}
    after = raw.get("after") or {}
    delta = raw.get("delta") or {}
    pct = raw.get("pctChange") or {}

    full_before = float(before.get("FullDPS") or before.get("EffectiveDPS") or 0.0)
    full_after = float(after.get("FullDPS") or after.get("EffectiveDPS") or 0.0)
    full_delta = full_after - full_before
    full_pct = float(pct.get("FullDPS") or pct.get("EffectiveDPS") or 0.0)

    dps_per_ex: float | None = None
    if price_exalted is not None and price_exalted > 0:
        dps_per_ex = round(full_delta / price_exalted, 4)

    cand_info = raw.get("candidate") or {}
    label = raw.get("label") or cand_info.get("name") or raw.get("id")

    return ItemUpgradeDiff(
        equipment_slot=str(raw.get("targetSlot") or "Unknown"),
        candidate_label=str(label) if label else None,
        candidate_item_text=item_text,
        price_exalted=price_exalted,
        full_dps_before=full_before,
        full_dps_after=full_after,
        full_dps_delta=full_delta,
        full_dps_pct=full_pct,
        total_dps_before=float(before.get("TotalDPS") or 0.0),
        total_dps_after=float(after.get("TotalDPS") or 0.0),
        total_dps_pct=float(pct.get("TotalDPS") or 0.0),
        total_ehp_before=float(before.get("TotalEHP") or 0.0),
        total_ehp_after=float(after.get("TotalEHP") or 0.0),
        total_ehp_delta=float(delta.get("TotalEHP") or 0.0),
        total_ehp_pct=float(pct.get("TotalEHP") or 0.0),
        life_delta=float(delta.get("Life") or 0.0),
        es_delta=float(delta.get("EnergyShield") or 0.0),
        mana_delta=float(delta.get("Mana") or 0.0),
        spirit_unreserved_delta=int(round(float(delta.get("SpiritUnreserved") or 0.0))),
        resist_deltas={
            "fire": float(delta.get("FireResist") or 0.0),
            "cold": float(delta.get("ColdResist") or 0.0),
            "lightning": float(delta.get("LightningResist") or 0.0),
            "chaos": float(delta.get("ChaosResist") or 0.0),
        },
        dps_per_exalted=dps_per_ex,
    )


def _fallback_fetch_entry_to_pob2_text(entry: Mapping[str, Any]) -> str:
    """Lazy-imports canonical trade2_fetch_to_pob2_text, with minimal inline fallback."""
    try:
        from poe2brain.trade.item_parser import trade2_fetch_to_pob2_text

        return trade2_fetch_to_pob2_text(dict(entry))
    except Exception:
        item = entry.get("item") or {}
        rarity = str(item.get("rarity") or "Rare").upper()
        name = str(item.get("name") or "").strip()
        base_type = str(item.get("baseType") or item.get("typeLine") or "Attuned Wand").strip()
        lines = [f"Rarity: {rarity}"]
        if name:
            lines.append(name)
        lines.append(base_type)
        lines.append("Implicits: 0")
        for group_key in ("implicitMods", "runeMods", "fracturedMods", "explicitMods"):
            for mod in item.get(group_key) or []:
                desc = mod.get("description") if isinstance(mod, Mapping) else str(mod)
                if desc:
                    cleaned = re.sub(
                        r"\[([^\]|]+)\|?([^\]]*)\]", lambda m: m.group(2) or m.group(1), str(desc)
                    )
                    lines.append(cleaned)
        return "\n".join(lines)


class BuildService:
    """High-level PoE2 Build & Path of Building 2 intelligence service."""

    def __init__(
        self,
        *,
        bridge: Pob2Bridge | None = None,
        http_client: Poe2HttpClient | None = None,
        trade_client: Any = None,
        economy_service: Any = None,
    ) -> None:
        self._owns_bridge = bridge is None
        self.bridge = bridge or Pob2Bridge()
        self._owns_http = http_client is None
        self.http_client = http_client or Poe2HttpClient()
        self._trade_client = trade_client
        self._economy_service = economy_service

    def close(self) -> None:
        if self._owns_bridge:
            self.bridge.close()
        if self._owns_http:
            self.http_client.close()

    def __enter__(self) -> BuildService:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 1. Multi-slot lifecycle
    # ------------------------------------------------------------------

    def list_slots(self) -> dict[str, Any]:
        return dict(self.bridge.call("list_slots"))

    def switch_slot(self, slot: str, *, create_if_missing: bool = True) -> BuildStats:
        self.bridge.call("switch_slot", {"slot": slot, "createIfMissing": create_if_missing})
        return self.get_stats(slot=slot)

    def clone_slot(
        self,
        source: str = "main",
        target: str = "candidate",
        *,
        activate: bool = False,
    ) -> dict[str, Any]:
        return dict(
            self.bridge.call(
                "clone_slot",
                {"source": source, "target": target, "activate": activate},
            )
        )

    def reset_slot(
        self,
        slot: str = "main",
        *,
        class_name: str | None = None,
        ascendancy: str | None = None,
        level: int | None = None,
        name: str | None = None,
    ) -> BuildStats:
        params: dict[str, Any] = {"slot": slot}
        if class_name is not None:
            params["className"] = class_name
        if ascendancy is not None:
            params["ascendancy"] = ascendancy
        if level is not None:
            params["level"] = level
        if name is not None:
            params["name"] = name
        self.bridge.call("reset_slot", params)
        return self.get_stats(slot=slot)

    def delete_slot(self, slot: str) -> dict[str, Any]:
        return dict(self.bridge.call("delete_slot", {"slot": slot}))

    def undo(self, slot: str = "main") -> BuildStats:
        self.bridge.call("undo", {"slot": slot})
        return self.get_stats(slot=slot)

    def redo(self, slot: str = "main") -> BuildStats:
        self.bridge.call("redo", {"slot": slot})
        return self.get_stats(slot=slot)

    # ------------------------------------------------------------------
    # 2. Universal build loader & exporter
    # ------------------------------------------------------------------

    def _fetch_pobbin_code(self, url: str) -> str:
        match = _POBBIN_URL_RE.match(url.strip())
        if not match:
            raise ValueError(f"Unsupported pobb.in URL format: {url!r}")
        paste_id = match.group(1)
        raw_url = f"https://pobb.in/{paste_id}/raw"
        resp = self.http_client.sync_client.get(raw_url, headers={"Accept": "text/plain, */*"})
        resp.raise_for_status()
        code = resp.text.strip()
        if not code:
            raise ValueError(f"Empty share code returned from {raw_url}")
        return code

    def _fetch_remote_poe2_build_xml(self, url: str) -> tuple[str, str | None]:
        import tempfile

        from poe2brain.ninja.service import NinjaBuildsService
        from poe2brain.ninja.xml_inspector import decode_pob2_code_to_xml

        is_mocked = self.http_client._sync_transport is not None
        if is_mocked:
            with tempfile.TemporaryDirectory(prefix="poe2brain_mock_url_") as tmp_dir:
                ninja_svc = NinjaBuildsService(
                    http_client=self.http_client,
                    cache_dir=Path(tmp_dir),
                    min_uncached_char_interval_seconds=0.0,
                )
                imp = ninja_svc.fetch_character(url, load_into_pob=False, force_refresh=True)
        else:
            ninja_svc = NinjaBuildsService(http_client=self.http_client)
            imp = ninja_svc.fetch_character(url, load_into_pob=False)
        return decode_pob2_code_to_xml(imp.pob_code), imp.character

    def load_build(
        self,
        source: str | Path,
        *,
        slot: str = "main",
        name: str | None = None,
        level: int | None = None,
    ) -> BuildStats:
        text: str
        inferred_name = name
        if isinstance(source, Path):
            if not source.is_file():
                raise FileNotFoundError(f"Build file does not exist: {source}")
            text = source.read_text(encoding="utf-8")
            inferred_name = inferred_name or source.stem
        else:
            raw_str = str(source).strip()
            if not raw_str:
                raise ValueError("Build source cannot be empty")
            if _POBBIN_URL_RE.match(raw_str):
                text = self._fetch_pobbin_code(raw_str)
            elif raw_str.lower().startswith(("https://", "http://")):
                text, char_name = self._fetch_remote_poe2_build_xml(raw_str)
                inferred_name = inferred_name or char_name
            elif (
                len(raw_str) < 512
                and "\n" not in raw_str
                and not raw_str.startswith("<")
                and not raw_str.startswith("{")
                and Path(raw_str).expanduser().is_file()
            ):
                p = Path(raw_str).expanduser()
                text = p.read_text(encoding="utf-8")
                inferred_name = inferred_name or p.stem
            else:
                text = raw_str

        stripped = text.strip()
        if stripped.startswith("<") or "<PathOfBuilding" in stripped[:500]:
            self.bridge.call(
                "load_build_xml",
                {"slot": slot, "xml": stripped, "name": inferred_name},
            )
        elif stripped.startswith("{"):
            params: dict[str, Any] = {"slot": slot, "json": stripped}
            if level is not None:
                params["level"] = level
            self.bridge.call("load_ggg_build", params)
        else:
            self.bridge.call(
                "load_build_code",
                {"slot": slot, "code": stripped, "name": inferred_name},
            )
        return self.get_stats(slot=slot)

    def export_build(
        self,
        slot: str = "main",
        *,
        fmt: Literal["code", "xml", "ggg_json"] = "code",
        name: str | None = None,
        author: str | None = None,
    ) -> str:
        if fmt == "code":
            res = self.bridge.call("export_build_code", {"slot": slot})
            return str(res["code"])
        if fmt == "xml":
            res = self.bridge.call("export_build_xml", {"slot": slot})
            return str(res["xml"])
        if fmt == "ggg_json":
            params: dict[str, Any] = {"slot": slot}
            if name is not None:
                params["name"] = name
            if author is not None:
                params["author"] = author
            res = self.bridge.call("export_ggg_build", params)
            return str(res["json"])
        raise ValueError(
            f"Unsupported export format {fmt!r} (expected 'code', 'xml', or 'ggg_json')"
        )

    # ------------------------------------------------------------------
    # 3. Build inspection & editing
    # ------------------------------------------------------------------

    def get_stats(self, slot: str = "main") -> BuildStats:
        summary = self.bridge.call("get_build_summary", {"slot": slot, "includeItemRaw": False})
        return _to_build_stats(
            summary.get("stats") or {},
            character=summary.get("character"),
            skills=summary.get("skills"),
            tree=summary.get("tree"),
        )

    def inspect_build(
        self,
        slot: str = "main",
        *,
        include_item_raw: bool = False,
    ) -> dict[str, Any]:
        raw = self.bridge.call(
            "get_build_summary",
            {"slot": slot, "includeItemRaw": include_item_raw},
        )
        typed_stats = _to_build_stats(
            raw.get("stats") or {},
            character=raw.get("character"),
            skills=raw.get("skills"),
            tree=raw.get("tree"),
        )
        return {
            "slot": raw.get("slot", slot),
            "rev": raw.get("rev", 1),
            "character": raw.get("character") or {},
            "stats": typed_stats,
            "raw_pob_stats": raw.get("stats") or {},
            "skills": raw.get("skills") or {},
            "tree": raw.get("tree") or {},
            "items": raw.get("items") or [],
            "config": raw.get("config") or {},
        }

    def list_classes(self) -> list[dict[str, Any]]:
        res = self.bridge.call("list_classes")
        return list(res.get("classes") or [])

    def create_character(
        self,
        class_name: str,
        ascendancy: str | None = None,
        *,
        level: int = 1,
        slot: str = "main",
        name: str | None = None,
    ) -> BuildStats:
        return self.reset_slot(
            slot=slot,
            class_name=class_name,
            ascendancy=ascendancy,
            level=level,
            name=name,
        )

    def equip_item(
        self,
        item_text: str,
        *,
        target_slot: str | None = None,
        slot: str = "main",
        active: bool | None = None,
    ) -> BuildStats:
        params: dict[str, Any] = {"slot": slot, "itemText": item_text}
        if target_slot is not None:
            params["targetSlot"] = target_slot
        if active is not None:
            params["active"] = active
        self.bridge.call("equip_item", params)
        return self.get_stats(slot=slot)

    def unequip_item(self, target_slot: str, *, slot: str = "main") -> BuildStats:
        self.bridge.call("unequip_item", {"slot": slot, "targetSlot": target_slot})
        return self.get_stats(slot=slot)

    def set_gem_links(
        self,
        gems_or_paste_text: str | Sequence[str],
        *,
        slot: str = "main",
        label: str | None = None,
        slot_name: str | None = None,
        include_in_full_dps: bool = True,
        set_main: bool = True,
    ) -> BuildStats:
        if isinstance(gems_or_paste_text, str):
            lines = [_format_gem_paste_line(ln) for ln in gems_or_paste_text.splitlines()]
        else:
            lines = [_format_gem_paste_line(str(g)) for g in gems_or_paste_text]
        paste_text = "\n".join(ln for ln in lines if ln) + "\n"
        params: dict[str, Any] = {
            "slot": slot,
            "text": paste_text,
            "includeInFullDPS": include_in_full_dps,
            "setMain": set_main,
        }
        if label is not None:
            params["label"] = label
        if slot_name is not None:
            params["slotName"] = slot_name
        self.bridge.call("paste_socket_group", params)
        return self.get_stats(slot=slot)

    def list_valid_supports(
        self, slot: str = "main", *, group_index: int = 1
    ) -> list[dict[str, Any]]:
        res = self.bridge.call("list_valid_supports", {"slot": slot, "groupIndex": group_index})
        return list(res.get("supports") or [])

    def set_config(
        self,
        values: Mapping[str, Any] | None = None,
        *,
        custom_mods: str | None = None,
        reset_keys: Sequence[str] | None = None,
        slot: str = "main",
    ) -> BuildStats:
        params: dict[str, Any] = {"slot": slot}
        if values is not None:
            params["values"] = dict(values)
        if custom_mods is not None:
            params["customMods"] = custom_mods
        if reset_keys is not None:
            params["resetKeys"] = list(reset_keys)
        self.bridge.call("set_config", params)
        return self.get_stats(slot=slot)

    def alloc_passive_nodes(
        self,
        node_ids: Sequence[int],
        *,
        slot: str = "main",
        connect_path: bool = True,
    ) -> BuildStats:
        self.bridge.call(
            "alloc_nodes",
            {"slot": slot, "nodeIds": [int(n) for n in node_ids], "connectPath": connect_path},
        )
        return self.get_stats(slot=slot)

    def dealloc_passive_nodes(self, node_ids: Sequence[int], *, slot: str = "main") -> BuildStats:
        self.bridge.call(
            "dealloc_nodes",
            {"slot": slot, "nodeIds": [int(n) for n in node_ids]},
        )
        return self.get_stats(slot=slot)

    def search_passive_nodes(
        self,
        query: str = "",
        *,
        node_type: str | None = None,
        limit: int = 25,
        slot: str = "main",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"slot": slot, "query": query, "limit": limit}
        if node_type is not None:
            params["nodeType"] = node_type
        res = self.bridge.call("search_nodes", params)
        return list(res.get("nodes") or [])

    # ------------------------------------------------------------------
    # 4. Dewatering / PoB-Warrior Audit
    # ------------------------------------------------------------------

    def audit_build(
        self,
        slot: str = "main",
        *,
        apply_dewater: bool = False,
        keep: Sequence[str] | None = None,
    ) -> DewateredAudit:
        params: dict[str, Any] = {"slot": slot, "apply": apply_dewater}
        if keep:
            params["keep"] = list(keep)
        res = self.bridge.call(
            "audit_build",
            params,
            timeout=max(self.bridge.request_timeout, 120.0),
        )

        char_info = res.get("character")
        skills_info = res.get("skills")
        tree_info = res.get("tree")

        raw_stats = _to_build_stats(
            res.get("rawStats") or {},
            character=char_info,
            skills=skills_info,
            tree=tree_info,
        )
        sust_stats = _to_build_stats(
            res.get("sustainableStats") or {},
            character=char_info,
            skills=skills_info,
            tree=tree_info,
        )

        stripped: list[StrippedPobFlag] = []
        for c in res.get("culprits") or []:
            stripped.append(
                StrippedPobFlag(
                    flag=str(c.get("key") or c.get("id") or "unknown"),
                    raw_value=c.get("originalValue"),
                    dps_inflation_pct=round(float(c.get("dpsInflationPct") or 0.0), 2),
                    ehp_inflation_pct=round(float(c.get("ehpInflationPct") or 0.0), 2),
                    reason=str(c.get("label") or c.get("category") or ""),
                )
            )

        warnings: list[str] = []
        total_dps_inf = float(res.get("totalDpsInflationPct") or 0.0)
        total_ehp_inf = float(res.get("totalEhpInflationPct") or 0.0)
        if total_dps_inf >= 25.0:
            warnings.append(
                f"Configured DPS is inflated by +{total_dps_inf:.1f}% above "
                "sustainable Pinnacle Boss baseline."
            )
        if total_ehp_inf >= 25.0:
            warnings.append(
                f"Configured EHP is inflated by +{total_ehp_inf:.1f}% above "
                "sustainable Pinnacle Boss baseline."
            )
        for res_name, val in (
            ("Fire", sust_stats.fire_resist),
            ("Cold", sust_stats.cold_resist),
            ("Lightning", sust_stats.lightning_resist),
        ):
            if sust_stats.level >= 65 and val < 75.0:
                warnings.append(
                    f"Endgame {res_name} Resistance is uncapped "
                    f"({val:.0f}% < 75%) under -60% penalty."
                )
        if sust_stats.spirit_unreserved < 0:
            warnings.append(
                f"Spirit is over-reserved ({sust_stats.spirit_reserved}/{sust_stats.spirit_total})."
            )
        if not apply_dewater and not res.get("restoredVerified", True):
            warnings.append("Warning: Post-audit snapshot verification drifted.")

        return DewateredAudit(
            slot_name=slot,
            raw_stats=raw_stats,
            sustainable_pinnacle_stats=sust_stats,
            dps_inflation_pct=round(total_dps_inf, 2),
            ehp_inflation_pct=round(total_ehp_inf, 2),
            stripped_flags=stripped,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # 5. Single & Batch Candidate Item Upgrade Diffs
    # ------------------------------------------------------------------

    def diff_item(
        self,
        item_text: str,
        *,
        target_slot: str | None = None,
        slot: str = "main",
        label: str | None = None,
        price_exalted: float | None = None,
    ) -> ItemUpgradeDiff:
        params: dict[str, Any] = {"slot": slot, "itemText": item_text}
        if target_slot is not None:
            params["targetSlot"] = target_slot
        if label is not None:
            params["label"] = label
        raw = self.bridge.call("diff_item", params)
        return _to_item_upgrade_diff(raw, item_text=item_text, price_exalted=price_exalted)

    def diff_items_batch(
        self,
        items: Sequence[Mapping[str, Any] | str],
        *,
        target_slot: str | None = None,
        slot: str = "main",
    ) -> list[ItemUpgradeDiff]:
        normalized: list[dict[str, Any]] = []
        prices_by_idx: dict[int, float | None] = {}
        texts_by_idx: dict[int, str] = {}

        for idx, entry in enumerate(items, start=1):
            if isinstance(entry, str):
                item_txt = entry
                req: dict[str, Any] = {"id": str(idx), "itemText": item_txt}
                if target_slot is not None:
                    req["targetSlot"] = target_slot
                prices_by_idx[idx] = None
                texts_by_idx[idx] = item_txt
            else:
                item_txt = str(
                    entry.get("itemText")
                    or entry.get("item_text")
                    or entry.get("pob_item_text")
                    or ""
                )
                req = {
                    "id": str(entry.get("id") or idx),
                    "itemText": item_txt,
                }
                lbl = entry.get("label") or entry.get("candidate_label")
                if lbl is not None:
                    req["label"] = str(lbl)
                tslot = entry.get("targetSlot") or entry.get("target_slot") or target_slot
                if tslot is not None:
                    req["targetSlot"] = str(tslot)
                raw_price = entry.get("price_exalted")
                prices_by_idx[idx] = float(raw_price) if raw_price is not None else None
                texts_by_idx[idx] = item_txt
            normalized.append(req)

        res = self.bridge.call("diff_items_batch", {"slot": slot, "items": normalized})
        diffs: list[ItemUpgradeDiff] = []
        for idx, r in enumerate(res.get("results") or [], start=1):
            if not isinstance(r, Mapping) or not r.get("ok"):
                continue
            diffs.append(
                _to_item_upgrade_diff(
                    r,
                    item_text=texts_by_idx.get(idx),
                    price_exalted=prices_by_idx.get(idx),
                )
            )
        return diffs

    # ------------------------------------------------------------------
    # 6. Passive Node Diff & Notable / Anoint Ranking
    # ------------------------------------------------------------------

    def rank_passive_upgrades(
        self,
        slot: str = "main",
        *,
        mode: Literal["perPoint", "total", "anoint"] = "perPoint",
        stat: str = "FullDPS",
        max_path_dist: int = 8,
        top_n: int = 15,
        include_keystones: bool = False,
    ) -> list[PassiveNodeUpgrade]:
        res = self.bridge.call(
            "rank_passive_notables",
            {
                "slot": slot,
                "sortBy": mode,
                "stat": stat,
                "maxPathDist": max_path_dist,
                "topN": top_n,
                "includeKeystones": include_keystones,
            },
            timeout=max(self.bridge.request_timeout, 120.0),
        )
        out: list[PassiveNodeUpgrade] = []
        for row in res.get("notables") or []:
            if not isinstance(row, Mapping):
                continue
            out.append(
                PassiveNodeUpgrade(
                    node_id=int(row["nodeId"]),
                    string_id=str(row["stringId"]) if row.get("stringId") else None,
                    name=str(row.get("name") or ""),
                    node_type=str(row.get("type") or "Notable"),
                    stats=[str(s) for s in (row.get("stats") or [])],
                    path_dist=int(row["pathDist"])
                    if isinstance(row.get("pathDist"), int)
                    else None,
                    path_node_ids=[int(pid) for pid in (row.get("pathNodeIds") or [])],
                    single_node_delta=float(row.get("singleNodeDelta") or 0.0),
                    single_node_pct=float(row.get("singleNodePct") or 0.0),
                    full_path_delta=float(row.get("fullPathDelta") or 0.0),
                    full_path_pct=float(row.get("fullPathPct") or 0.0),
                    per_point_delta=float(row.get("perPointDelta") or 0.0),
                    per_point_pct=float(row.get("perPointPct") or 0.0),
                )
            )
        return out

    def diff_passive_nodes(
        self,
        *,
        add_node_ids: Sequence[int] | None = None,
        remove_node_ids: Sequence[int] | None = None,
        slot: str = "main",
        include_path: bool = False,
        include_dependents: bool = False,
    ) -> dict[str, Any]:
        return dict(
            self.bridge.call(
                "diff_passive_nodes",
                {
                    "slot": slot,
                    "addNodeIds": [int(n) for n in (add_node_ids or [])],
                    "removeNodeIds": [int(n) for n in (remove_node_ids or [])],
                    "includePath": include_path,
                    "includeDependents": include_dependents,
                },
            )
        )

    # ------------------------------------------------------------------
    # 7. Slot Marginal Stat Weights & Live Closed-Loop Upgrade Finder
    # ------------------------------------------------------------------

    def compute_slot_trade_weights(
        self,
        slot_name: str,
        *,
        build_slot: str = "main",
        metric: Literal["FullDPS", "TotalEHP", "balanced"] = "FullDPS",
        league: str = DEFAULT_LEAGUE,
        max_price_exalted: float | None = None,
        securable_only: bool = False,
        min_rune_sockets: int = 0,
        top_n: int = 35,
    ) -> SlotStatWeights:
        del league  # Reserved for economy-adjusted filters if needed
        if metric == "FullDPS":
            stat_weights = [{"stat": "FullDPS", "weightMult": 1.0}]
        elif metric == "TotalEHP":
            stat_weights = [{"stat": "TotalEHP", "weightMult": 1.0}]
        elif metric == "balanced":
            stat_weights = [
                {"stat": "FullDPS", "weightMult": 1.0},
                {"stat": "TotalEHP", "weightMult": 0.6},
            ]
        else:
            raise ValueError(
                f"Unsupported metric {metric!r} (expected FullDPS, TotalEHP, balanced)"
            )

        params: dict[str, Any] = {
            "slot": build_slot,
            "targetSlot": slot_name,
            "statWeights": stat_weights,
            "statusOption": "securable" if securable_only else "available",
            "sockets": min_rune_sockets,
            "topN": top_n,
        }
        if max_price_exalted is not None and max_price_exalted > 0:
            params["maxPrice"] = max_price_exalted

        res = self.bridge.call("slot_stat_weights", params)
        base_summary = self.get_stats(slot=build_slot)
        base_dps = max(base_summary.full_dps, base_summary.total_dps, 1.0)
        base_ehp = max(base_summary.total_ehp, 1.0)

        entries: list[StatWeightEntry] = []
        count_filters: list[dict[str, Any]] = []
        raw_weights = [w for w in (res.get("weights") or []) if isinstance(w, Mapping)]
        for w in raw_weights:
            wt = float(w.get("weight") or 0.0)
            mean_diff = float(w.get("meanStatDiff") or 0.0)
            # PoB2 TradeQueryGenerator scales ratio by 1000: weight = (delta_ratio * 1000) / value
            dps_unit = (wt / 1000.0) * base_dps if metric in ("FullDPS", "balanced") else 0.0
            ehp_unit = (wt / 1000.0) * base_ehp if metric in ("TotalEHP", "balanced") else 0.0
            stat_id = str(w["tradeModId"])
            entries.append(
                StatWeightEntry(
                    stat_id=stat_id,
                    stat_label=str(w.get("text") or stat_id),
                    weight=round(wt, 4),
                    dps_per_unit=round(dps_unit, 3),
                    ehp_per_unit=round(ehp_unit, 3),
                )
            )
            if wt > 0 and len(count_filters) < 8:
                midpoint = abs(mean_diff / wt) if abs(wt) > 1e-6 else 1.0
                min_roll = max(1, int(round(midpoint * 0.55)))
                count_filters.append({"id": stat_id, "value": {"min": min_roll}})

        # Note: GGG's unauthenticated trade2 API rejects `type: "weight"` (complexity >= 54
        # requires POESESSID login), whereas `type: "count"` with PoB2-derived midpoint min
        # thresholds succeeds with HTTP 200 unauthenticated and feeds real candidates into
        # PoB2's exact GetMiscCalculator() ranker.
        raw_payload = res.get("trade2Payload")
        raw_query = (
            dict(raw_payload["query"])
            if isinstance(raw_payload, Mapping) and isinstance(raw_payload.get("query"), Mapping)
            else {}
        )
        raw_filters = dict(raw_query.get("filters") or {})
        clean_filters = {
            k: v
            for k, v in raw_filters.items()
            if k in ("type_filters", "trade_filters", "equipment_filters", "req_filters")
        }
        min_count = 3 if len(count_filters) >= 5 else (2 if len(count_filters) >= 2 else 1)
        sort_spec = (
            {"price": "desc"}
            if (max_price_exalted is not None and max_price_exalted > 0)
            else {"price": "asc"}
        )
        unauth_trade2_payload: dict[str, Any] = (
            {
                "query": {
                    "status": raw_query.get(
                        "status",
                        {"option": "securable" if securable_only else "available"},
                    ),
                    "filters": clean_filters,
                    "stats": (
                        [
                            {
                                "type": "count",
                                "value": {"min": min_count},
                                "filters": count_filters,
                            }
                        ]
                        if count_filters
                        else []
                    ),
                },
                "sort": sort_spec,
            }
            if raw_query
            else {}
        )

        return SlotStatWeights(
            equipment_slot=slot_name,
            objective=metric,
            weights=entries,
            trade2_weighted_filter=unauth_trade2_payload,
        )

    def _resolve_currency_to_exalted(self, currency: str, amount: float, league: str) -> float:
        cur = currency.strip().lower()
        if cur in ("exalted", "exalt", "ex", "exalted-orb"):
            return amount
        econ = self._economy_service
        if econ is None:
            try:
                from poe2brain.trade.economy import EconomyService

                self._economy_service = EconomyService(client=self.http_client)
                econ = self._economy_service
            except Exception:
                econ = None
        if econ is not None:
            try:
                rates = econ.get_reference_rates(league)
                div_ex = float(getattr(rates, "divine_in_exalted", 450.0) or 450.0)
                chaos_ex = float(getattr(rates, "chaos_in_exalted", 35.0) or 35.0)
                if cur in ("divine", "div", "divine-orb"):
                    return amount * div_ex
                if cur in ("chaos", "c", "chaos-orb"):
                    return amount * chaos_ex
                quote = econ.get_price(cur, league)
                if quote is not None and getattr(quote, "exalted", None):
                    return amount * float(quote.exalted)
            except Exception:
                pass
        # Conservative fallback ratios if offline
        fallback = {"divine": 450.0, "chaos": 35.0, "regal": 0.25, "vaal": 1.0, "alch": 0.5}
        return amount * fallback.get(cur, 1.0)

    def _execute_trade2_weighted_search(
        self,
        *,
        league: str,
        payload: dict[str, Any],
        limit: int,
    ) -> tuple[str | None, str | None, int, list[TradeListing]]:
        # 1. Use injected or lazy Trade2Client if available and compatible
        tclient = self._trade_client
        if tclient is None:
            try:
                from poe2brain.trade.trade2_client import Trade2Client

                self._trade_client = Trade2Client(client=self.http_client)
                tclient = self._trade_client
            except Exception:
                tclient = None

        if tclient is not None:
            for method_name in ("search_and_fetch", "price_check_query", "search_items"):
                fn = getattr(tclient, method_name, None)
                if callable(fn):
                    try:
                        res = fn(payload, league=league, limit=limit)
                    except TypeError:
                        try:
                            res = fn(league, payload, limit=limit)
                        except TypeError:
                            continue
                    if hasattr(res, "listings"):
                        listings = list(res.listings)
                        for lst in listings:
                            if not lst.pob_item_text and getattr(lst, "raw_entry", None):
                                lst.pob_item_text = _fallback_fetch_entry_to_pob2_text(
                                    lst.raw_entry
                                )
                        return (
                            getattr(res, "query_id", None),
                            getattr(res, "trade_url", None),
                            int(getattr(res, "total_matches", len(listings)) or 0),
                            listings[:limit],
                        )

        # 2. Direct official GGG Trade2 search + batched fetch via shared Poe2HttpClient
        encoded_league = url_quote(league, safe="")
        search_url = f"{GGG_TRADE2_BASE_URL}/search/poe2/{encoded_league}"
        search_resp = self.http_client.post_json(search_url, json_body=payload)
        query_id = str(search_resp.get("id") or "") or None
        total_matches = int(search_resp.get("total") or 0)
        result_ids = [str(rid) for rid in (search_resp.get("result") or [])]
        trade_url = f"{GGG_TRADE2_WEB_BASE_URL}/{encoded_league}/{query_id}" if query_id else None
        if not result_ids:
            return query_id, trade_url, total_matches, []

        # Fetch up to 2x limit (capped at 20 = 2 chunks) so account deduplication leaves `limit`
        target_ids = result_ids[: min(len(result_ids), max(limit * 2, 10), 20)]
        raw_entries: list[dict[str, Any]] = []
        for offset in range(0, len(target_ids), 10):
            chunk = target_ids[offset : offset + 10]
            fetch_url = f"{GGG_TRADE2_BASE_URL}/fetch/{','.join(chunk)}"
            params = {"realm": "poe2"}
            if query_id:
                params["query"] = query_id
            fetch_resp = self.http_client.get_json(fetch_url, params=params)
            for row in fetch_resp.get("result") or []:
                if isinstance(row, dict):
                    raw_entries.append(row)

        seen_accounts: set[str] = set()
        listings: list[TradeListing] = []
        for entry in raw_entries:
            listing_info = entry.get("listing") or {}
            item_info = entry.get("item") or {}
            acct_info = listing_info.get("account") or {}
            acct_name = str(acct_info.get("name") or "unknown")
            if acct_name in seen_accounts:
                continue
            seen_accounts.add(acct_name)

            price_info = listing_info.get("price") or {}
            p_amount = float(price_info.get("amount") or 0.0)
            p_curr = str(price_info.get("currency") or "exalted")
            p_exalted = (
                self._resolve_currency_to_exalted(p_curr, p_amount, league) if p_amount > 0 else 0.0
            )
            fee_raw = listing_info.get("fee")
            fee_int = int(fee_raw) if isinstance(fee_raw, (int, float)) else None
            whisper = listing_info.get("whisper")
            sale_type = (
                SaleType.SECURABLE
                if fee_int is not None
                else (SaleType.WHISPER if whisper else SaleType.UNKNOWN)
            )
            pob_text = _fallback_fetch_entry_to_pob2_text(entry)

            mod_lines: list[str] = []
            for mgroup in ("implicitMods", "runeMods", "fracturedMods", "explicitMods"):
                for m in item_info.get(mgroup) or []:
                    desc = m.get("description") if isinstance(m, Mapping) else str(m)
                    if desc:
                        mod_lines.append(
                            re.sub(
                                r"\[([^\]|]+)\|?([^\]]*)\]",
                                lambda match: match.group(2) or match.group(1),
                                str(desc),
                            )
                        )

            listings.append(
                TradeListing(
                    listing_id=str(entry.get("id") or ""),
                    item_name=str(item_info.get("name") or "") or None,
                    base_type=str(
                        item_info.get("baseType") or item_info.get("typeLine") or "Unknown"
                    ),
                    item_level=(
                        int(item_info["ilvl"]) if isinstance(item_info.get("ilvl"), int) else None
                    ),
                    corrupted=bool(item_info.get("corrupted")),
                    price_amount=p_amount,
                    price_currency=p_curr,
                    price_exalted=round(p_exalted, 2),
                    sale_type=sale_type,
                    fee=fee_int,
                    whisper=str(whisper) if whisper else None,
                    account_name=acct_name,
                    online=acct_info.get("online") is not None,
                    indexed_at=str(listing_info.get("indexed") or "") or None,
                    pob_item_text=pob_text,
                    mod_lines=mod_lines,
                )
            )
            if len(listings) >= limit:
                break

        return query_id, trade_url, total_matches, listings

    def find_live_slot_upgrades(
        self,
        slot_name: str,
        *,
        build_slot: str = "main",
        metric: Literal["FullDPS", "TotalEHP", "balanced"] = "FullDPS",
        league: str = DEFAULT_LEAGUE,
        max_price_exalted: float | None = None,
        securable_only: bool = False,
        min_rune_sockets: int = 0,
        candidate_limit: int = 10,
        sort_by_value: bool = False,
    ) -> LiveSlotUpgradeReport:
        slot_weights = self.compute_slot_trade_weights(
            slot_name,
            build_slot=build_slot,
            metric=metric,
            league=league,
            max_price_exalted=max_price_exalted,
            securable_only=securable_only,
            min_rune_sockets=min_rune_sockets,
        )
        payload = dict(slot_weights.trade2_weighted_filter)
        if not payload or not payload.get("query"):
            raise Pob2BridgeError(
                f"PoB2 could not generate a weighted trade2 query for slot {slot_name!r}"
            )

        query_id, trade_url, total_matches, listings = self._execute_trade2_weighted_search(
            league=league,
            payload=payload,
            limit=candidate_limit,
        )
        if not listings:
            return LiveSlotUpgradeReport(
                league=league,
                build_slot=build_slot,
                equipment_slot=slot_name,
                metric=metric,
                max_price_exalted=max_price_exalted,
                securable_only=securable_only,
                query_id=query_id,
                trade_url=trade_url,
                total_market_matches=total_matches,
                slot_weights=slot_weights,
                candidates=[],
            )

        batch_inputs: list[dict[str, Any]] = []
        listings_by_id: dict[str, TradeListing] = {}
        for idx, lst in enumerate(listings, start=1):
            lid = lst.listing_id or f"cand_{idx}"
            listings_by_id[lid] = lst
            cand_label = f"{lst.item_name + ' ' if lst.item_name else ''}{lst.base_type}".strip()
            batch_inputs.append(
                {
                    "id": lid,
                    "label": cand_label,
                    "targetSlot": slot_name,
                    "itemText": lst.pob_item_text or "",
                }
            )

        raw_batch = self.bridge.call(
            "diff_items_batch",
            {"slot": build_slot, "items": batch_inputs},
        )

        scored: list[tuple[float, LiveUpgradeCandidate]] = []
        for raw_res in raw_batch.get("results") or []:
            if not isinstance(raw_res, Mapping) or not raw_res.get("ok"):
                continue
            lid = str(raw_res.get("id") or "")
            lst = listings_by_id.get(lid)
            if lst is None:
                continue
            price_ex = lst.price_exalted if lst.price_exalted > 0 else None
            diff_model = _to_item_upgrade_diff(
                raw_res,
                item_text=lst.pob_item_text,
                price_exalted=price_ex,
            )
            ehp_per_ex = (
                round(diff_model.total_ehp_delta / price_ex, 4)
                if price_ex is not None and price_ex > 0
                else None
            )
            warns = [str(w) for w in (raw_res.get("warnings") or [])]

            if sort_by_value and diff_model.dps_per_exalted is not None:
                score = diff_model.dps_per_exalted
            elif metric == "TotalEHP":
                score = diff_model.total_ehp_pct
            elif metric == "balanced":
                score = diff_model.full_dps_pct + 0.6 * diff_model.total_ehp_pct
            else:
                score = diff_model.full_dps_delta

            scored.append(
                (
                    score,
                    LiveUpgradeCandidate(
                        rank=0,
                        listing=lst,
                        upgrade_diff=diff_model,
                        dps_gain_per_exalted=diff_model.dps_per_exalted,
                        ehp_gain_per_exalted=ehp_per_ex,
                        warnings=warns,
                    ),
                )
            )

        scored.sort(key=lambda pair: pair[0], reverse=True)
        ranked_candidates: list[LiveUpgradeCandidate] = []
        for rank_idx, (_, cand) in enumerate(scored, start=1):
            cand.rank = rank_idx
            ranked_candidates.append(cand)

        return LiveSlotUpgradeReport(
            league=league,
            build_slot=build_slot,
            equipment_slot=slot_name,
            metric=metric,
            max_price_exalted=max_price_exalted,
            securable_only=securable_only,
            query_id=query_id,
            trade_url=trade_url,
            total_market_matches=total_matches,
            slot_weights=slot_weights,
            candidates=ranked_candidates,
        )


_DEFAULT_SERVICE: BuildService | None = None


def get_default_build_service() -> BuildService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = BuildService()
    return _DEFAULT_SERVICE


def _close_default_build_service() -> None:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is not None:
        try:
            _DEFAULT_SERVICE.close()
        except Exception:
            pass
        _DEFAULT_SERVICE = None


atexit.register(_close_default_build_service)
