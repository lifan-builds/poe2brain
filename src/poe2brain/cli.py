from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from poe2brain.config import DEFAULT_LEAGUE
from poe2brain.mcp_server import (
    collect_engine_status,
    delete_pob_slot_snapshot,
    ensure_pob_slot_loaded,
    execute_craft_odds,
    execute_craft_plan,
    execute_find_upgrades,
    export_pob_slot,
    format_domain_error,
    get_build_service,
    get_economy_service,
    get_knowledge_service,
    get_pob_slot_snapshot_path,
    get_trade_advisor,
    normalize_item_text_for_pob2,
    normalize_pob_objective,
    run_bootstrap_script,
    run_sync_gamedata,
    save_pob_slot_snapshot,
)
from poe2brain.trade import parse_clipboard_item, parsed_item_to_pob2_text

app = typer.Typer(
    name="poe2",
    help="poe2brain — Path of Exile 2 (0.5.5 / Forbidden Rites) intelligence CLI.",
    no_args_is_help=True,
)
wiki_app = typer.Typer(
    help="Pillar 1: PoE2 0.5.5 game knowledge, mechanics, gems, bases, uniques, augments.",
    no_args_is_help=True,
)
pob_app = typer.Typer(
    help="Pillar 2: Headless Path of Building 2 builds, dewatering audits, and upgrades.",
    no_args_is_help=True,
)
econ_app = typer.Typer(
    help="Pillar 3a: Live PoE2 currency & unique economy (Exalted = 1.0).", no_args_is_help=True
)
trade_app = typer.Typer(
    help="Pillar 3b: PoE2 Ctrl+C item parser, price-checker, and live upgrade finder.",
    no_args_is_help=True,
)
craft_app = typer.Typer(
    help="Pillar 4: Exact PoE2 0.5.5 mod-pool odds and expected-cost crafting planner.",
    no_args_is_help=True,
)
sync_app = typer.Typer(
    help="Rebuild bundled deterministic PoE2 gamedata artifacts.", no_args_is_help=True
)
bootstrap_app = typer.Typer(help="Bootstrap local headless runtimes.", no_args_is_help=True)

app.add_typer(wiki_app, name="wiki")
app.add_typer(pob_app, name="pob")
app.add_typer(econ_app, name="econ")
app.add_typer(trade_app, name="trade")
app.add_typer(craft_app, name="craft")
app.add_typer(sync_app, name="sync")
app.add_typer(bootstrap_app, name="bootstrap")

console = Console()
err_console = Console(stderr=True)

JsonOpt = Annotated[
    bool,
    typer.Option("--json", help="Emit structured JSON output."),
]


def _emit_json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _run_cli(
    json_output: bool,
    compute_fn: Callable[[], Any],
    render_fn: Callable[[Any], None],
) -> None:
    try:
        result = compute_fn()
    except typer.Exit:
        raise
    except Exception as exc:
        err_payload = format_domain_error(exc)
        if json_output:
            _emit_json(err_payload)
        else:
            err_console.print(
                f"[bold red]Error ({err_payload['error_type']}):[/bold red] {err_payload['error']}"
            )
            if err_payload.get("hint"):
                err_console.print(f"[yellow]Hint:[/yellow] {err_payload['hint']}")
        raise typer.Exit(code=1) from None

    if json_output:
        if hasattr(result, "model_dump"):
            _emit_json(result.model_dump(mode="json"))
        else:
            _emit_json(result)
    else:
        render_fn(result)


def _read_item_input(
    *,
    file_path: Path | None = None,
    text: str | None = None,
    positional: str | None = None,
    allow_empty: bool = False,
) -> str | None:
    if file_path is not None:
        if not file_path.is_file():
            raise FileNotFoundError(f"Item file not found: {file_path}")
        return file_path.read_text(encoding="utf-8")
    if text is not None and text.strip():
        return text
    if positional is not None and positional.strip():
        pos = positional.strip()
        if pos == "-":
            return sys.stdin.read()
        cand = Path(pos).expanduser()
        if "\n" not in pos and len(pos) < 512 and cand.is_file():
            return cand.read_text(encoding="utf-8")
        return positional
    if allow_empty:
        return None
    raise ValueError('Provide item text via --text "<ctrl_c>" or --file <path>.')


@app.callback()
def _root_callback() -> None:
    """Path of Exile 2 intelligence engine for AI agents and power users."""


@app.command("version")
def version_cmd(json_output: JsonOpt = False) -> None:
    """Show poe2brain version, target PoE2 patch, default league, and runtime readiness."""

    def _render(status: Any) -> None:
        pob_label = (
            "ready" if status.pob2_runtime_ready else "missing (run scripts/bootstrap_pob2.sh)"
        )
        db_label = "ready" if status.gamedata_db_ready else "missing (run poe2 sync gamedata)"
        console.print(
            f"[bold cyan]poe2brain[/bold cyan] v{status.version} "
            f"(PoE2 [bold]{status.patch}[/bold] | League: [bold]{status.default_league}[/bold])"
        )
        console.print(f"  PoB2 runtime : {pob_label}")
        console.print(f"  Gamedata DB  : {db_label}")
        console.print(f"  Cache dir    : {status.cache_dir}")

    _run_cli(json_output, collect_engine_status, _render)


# ---------------------------------------------------------------------------
# Pillar 1: Knowledge (`poe2 wiki ...`)
# ---------------------------------------------------------------------------


@wiki_app.command("search")
def wiki_search_cmd(
    query: Annotated[str, typer.Argument(help="Search query across PoE2 0.5.5 gamedata.")],
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            "-c",
            help="Filter category (mechanic, keyword, gem, unique, base, augment, lever).",
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results to return.")] = 12,
    json_output: JsonOpt = False,
) -> None:
    """FTS5 + exact search across PoE2 mechanics, keywords, gems, uniques, bases, and levers."""

    def _compute() -> dict[str, Any]:
        records = get_knowledge_service().search(query, category=category, limit=limit)
        return {
            "query": query,
            "category": category,
            "count": len(records),
            "results": [r.model_dump(mode="json") for r in records],
        }

    def _render(data: dict[str, Any]) -> None:
        table = Table(title=f"PoE2 Knowledge Search: {query!r} ({data['count']} hits)")
        table.add_column("Kind", style="cyan")
        table.add_column("Title", style="bold")
        table.add_column("Summary")
        for row in data["results"]:
            table.add_row(str(row["kind"]), str(row["title"]), str(row["summary"])[:120])
        console.print(table)

    _run_cli(json_output, _compute, _render)


@wiki_app.command("mechanic")
def wiki_mechanic_cmd(
    topic: Annotated[
        str,
        typer.Argument(
            help="Mechanic topic or abbreviation (e.g. conversion, block, CI, ES, support)."
        ),
    ],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max facts/keywords to return.")] = 6,
    json_output: JsonOpt = False,
) -> None:
    """Explain a PoE2 0.5.5 mechanic with anti-hallucination trap warnings and exact formulas."""

    def _compute() -> dict[str, Any]:
        return get_knowledge_service().explain_mechanic(topic, limit=limit).model_dump(mode="json")

    def _render(data: dict[str, Any]) -> None:
        console.print(f"[bold cyan]PoE2 0.5.5 Mechanic:[/bold cyan] {topic}")
        for f in data.get("facts", []):
            console.print(f"\n[bold]{f['title']}[/bold] ({f['slug']})")
            console.print(f"  [red]PoE1/0.1 Trap :[/red] {f['poe1_trap']}")
            console.print(f"  [green]0.5.5 Rule    :[/green] {f['poe2_live_rule']}")
            if f.get("formula_expr"):
                console.print(f"  [cyan]Formula       :[/cyan] {f['formula_expr']}")
        for kw in data.get("keywords", [])[:3]:
            console.print(f"\n[dim][Keyword] {kw['term']}: {kw['definition']}[/dim]")

    _run_cli(json_output, _compute, _render)


@wiki_app.command("gem")
def wiki_gem_cmd(
    name: Annotated[
        str, typer.Argument(help="Active, Spirit, Meta, Support, or Lineage Support gem name.")
    ],
    json_output: JsonOpt = False,
) -> None:
    """Inspect a PoE2 gem, tiered support variants (I/II/III), and Lineage 1-per-char limits."""

    def _compute() -> dict[str, Any]:
        return get_knowledge_service().inspect_gem(name).model_dump(mode="json")

    def _render(data: dict[str, Any]) -> None:
        limit_str = (
            f"max {data['max_copies_per_char']}/char"
            if data.get("max_copies_per_char")
            else "unlimited across skills"
        )
        console.print(f"[bold cyan]{data['name']}[/bold cyan] [{data['gem_kind']}] ({limit_str})")
        if data.get("spirit_cost"):
            console.print(f"  Spirit Cost: {data['spirit_cost']}")
        if data.get("tags"):
            console.print(f"  Tags       : {', '.join(data['tags'])}")
        for ln in data.get("stat_lines", [])[:8]:
            console.print(f"  • {ln}")
        for w in data.get("warnings", []):
            console.print(f"  [yellow]Warning:[/yellow] {w}")

    _run_cli(json_output, _compute, _render)


@wiki_app.command("base")
def wiki_base_cmd(
    name: Annotated[str, typer.Argument(help="PoE2 base item name or class slug.")],
    ilvl: Annotated[int, typer.Option("--ilvl", help="Item level for T1 affix pool check.")] = 82,
    json_output: JsonOpt = False,
) -> None:
    """Inspect a PoE2 base item, implicits, T1 affixes at ilvl, and PoE1/'Expert' trap warnings."""

    def _compute() -> dict[str, Any]:
        return get_knowledge_service().inspect_base_item(name, ilvl=ilvl).model_dump(mode="json")

    def _render(data: dict[str, Any]) -> None:
        status = "LIVE 0.5.5" if data.get("is_live_poe2") else "TRAP / INACTIVE"
        slug_val = data.get("poe2db_slug")
        console.print(
            f"[bold cyan]{data['name']}[/bold cyan] "
            f"({data['item_class']} | slug={slug_val} | {status})"
        )
        if data.get("warning"):
            console.print(f"  [yellow]Trap Warning:[/yellow] {data['warning']}")
        if data.get("suggested_bases"):
            console.print(
                f"  [green]Suggested 0.5.5 Bases:[/green] {', '.join(data['suggested_bases'][:6])}"
            )
        for imp in data.get("implicits", []):
            console.print(f"  Implicit: {imp}")
        for side_key, label in (
            ("top_prefixes", "Top T1 Prefixes"),
            ("top_suffixes", "Top T1 Suffixes"),
        ):
            entries = data.get(side_key) or []
            if entries:
                console.print(f"  [bold]{label} @ ilvl {ilvl}:[/bold]")
                for m in entries[:5]:
                    console.print(
                        f"    [T{m['display_tier']}] (ilvl {m['required_level']}, "
                        f"w={m['weight']}) {m['stat_text']}"
                    )

    _run_cli(json_output, _compute, _render)


@wiki_app.command("unique")
def wiki_unique_cmd(
    name: Annotated[str, typer.Argument(help="PoE2 Unique item name.")],
    json_output: JsonOpt = False,
) -> None:
    """Inspect a PoE2 Unique item's base type, implicits, explicits, and chanceability."""

    def _compute() -> dict[str, Any]:
        return get_knowledge_service().inspect_unique(name).model_dump(mode="json")

    def _render(data: dict[str, Any]) -> None:
        console.print(
            f"[bold magenta]{data['name']}[/bold magenta] — {data['base_type']} "
            f"(chanceable={data.get('is_chanceable')})"
        )
        for imp in data.get("implicits", []):
            console.print(f"  [dim]{imp}[/dim]")
        for exp in data.get("explicits", []):
            console.print(f"  • {exp}")

    _run_cli(json_output, _compute, _render)


@wiki_app.command("augment")
def wiki_augment_cmd(
    query: Annotated[
        str, typer.Argument(help="Rune, Soul Core, Azmeri Idol, or Shaman Bonded query.")
    ],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results to return.")] = 8,
    json_output: JsonOpt = False,
) -> None:
    """Inspect Runes, Soul Cores, Idols, Shaman Bonded effects, and Verisium/Remnant recipes."""

    def _compute() -> dict[str, Any]:
        svc = get_knowledge_service()
        augments = svc.inspect_augment(query, limit=limit)
        recipes = svc.search_runeforge(query, limit=limit)
        return {
            "query": query,
            "augments": [a.model_dump(mode="json") for a in augments],
            "runeforge_recipes": [r.model_dump(mode="json") for r in recipes],
        }

    def _render(data: dict[str, Any]) -> None:
        for aug in data.get("augments", []):
            overw = aug.get("can_overwrite_in_socket")
            console.print(f"[bold cyan]{aug['name']}[/bold cyan] (overwritable={overw})")
            for k, v in (aug.get("slot_effects") or {}).items():
                console.print(f"  [{k}] {', '.join(v) if isinstance(v, list) else v}")
            for k, v in (aug.get("shaman_bonded_effects") or {}).items():
                console.print(f"  [Bonded {k}] {', '.join(v) if isinstance(v, list) else v}")
        for rec in data.get("runeforge_recipes", [])[:5]:
            console.print(f"[green][Runeforge][/green] {rec['output_item']} ({rec['category']})")

    _run_cli(json_output, _compute, _render)


@wiki_app.command("runeforge")
def wiki_runeforge_cmd(
    query: Annotated[str, typer.Argument(help="Verisium Anvil or Kalguuran Remnant recipe query.")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max recipes to return.")] = 12,
    json_output: JsonOpt = False,
) -> None:
    """Search Patch 0.5 Verisium Anvil base transforms and Kalguuran Remnant rune recipes."""

    def _compute() -> dict[str, Any]:
        recipes = get_knowledge_service().search_runeforge(query, limit=limit)
        return {
            "query": query,
            "count": len(recipes),
            "recipes": [r.model_dump(mode="json") for r in recipes],
        }

    def _render(data: dict[str, Any]) -> None:
        for rec in data.get("recipes", []):
            console.print(
                f"[bold cyan]{rec['output_item']}[/bold cyan] "
                f"({rec['category']}): {rec.get('inputs')}"
            )

    _run_cli(json_output, _compute, _render)


# ---------------------------------------------------------------------------
# Pillar 2: Headless PoB2 (`poe2 pob ...`)
# ---------------------------------------------------------------------------


def _render_build_stats(data: dict[str, Any], title: str = "PoB2 Build Stats") -> None:
    cls_str = f"{data.get('class_name') or 'Unknown'}/{data.get('ascendancy_name') or 'None'}"
    console.print(
        f"[bold cyan]{title}[/bold cyan] [{data.get('slot', 'main')}] — "
        f"Lv{data.get('level')} {cls_str} (Main: {data.get('main_skill') or 'None'})"
    )
    console.print(
        f"  FullDPS: [bold green]{data['full_dps']:,.1f}[/bold green] | "
        f"TotalDPS: {data['total_dps']:,.1f} | "
        f"TotalEHP: [bold]{data['total_ehp']:,.1f}[/bold]"
    )
    console.print(
        f"  Pools  : Life {data['life']:,.0f} | ES {data['energy_shield']:,.0f} | "
        f"Mana {data['mana']:,.0f} | "
        f"Spirit {data['spirit_unreserved']}/{data['spirit_total']} unreserved"
    )
    console.print(
        f"  Resists: Fire {data['fire_resist']:.0f}% | Cold {data['cold_resist']:.0f}% | "
        f"Light {data['lightning_resist']:.0f}% | Chaos {data['chaos_resist']:.0f}%"
    )
    console.print(
        f"  Mitig. : Block {data['block_chance']:.1f}% | "
        f"Deflect {data['deflection_chance']:.1f}% | "
        f"Evasion {data['evasion']:,.0f} | Armour {data['armour']:,.0f}"
    )


@pob_app.command("load")
def pob_load_cmd(
    source: Annotated[
        str,
        typer.Argument(
            help="PoB2 eN... share code, pobb.in URL, XML file, or GGG .build JSON file."
        ),
    ],
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    level: Annotated[
        int | None,
        typer.Option("--level", help="Optional character level override for GGG .build JSON."),
    ] = None,
    json_output: JsonOpt = False,
) -> None:
    """Load a PoB2 share code, pobb.in URL, XML, or GGG .build JSON into a persistent named slot."""

    def _compute() -> dict[str, Any]:
        svc = get_build_service()
        stats = svc.load_build(source, slot=slot, level=level)
        snap = save_pob_slot_snapshot(svc, slot)
        out = stats.model_dump(mode="json")
        out["slot"] = slot
        out["snapshot_path"] = str(snap)
        return out

    _run_cli(json_output, _compute, lambda d: _render_build_stats(d, "Loaded PoB2 Build"))


@pob_app.command("stats")
def pob_stats_cmd(
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    json_output: JsonOpt = False,
) -> None:
    """Extract FullDPS, TotalEHP, Life/ES/Mana, Spirit, resistances, Block, and Deflection."""

    def _compute() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        out = svc.get_stats(slot=slot).model_dump(mode="json")
        out["slot"] = slot
        return out

    _run_cli(json_output, _compute, _render_build_stats)


@pob_app.command("audit")
def pob_audit_cmd(
    source: Annotated[
        str | None, typer.Argument(help="Optional build code/URL/file to load before auditing.")
    ] = None,
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    apply_dewater: Annotated[
        bool,
        typer.Option("--apply", help="Persistently strip inflated PoB-warrior flags on the slot."),
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Dewatering audit: strip unsustainable config inflation vs Pinnacle Boss baseline."""

    def _compute() -> dict[str, Any]:
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

    def _render(data: dict[str, Any]) -> None:
        raw_s = data["raw_stats"]
        sus_s = data["sustainable_pinnacle_stats"]
        console.print(
            f"[bold cyan]Dewatering Audit[/bold cyan] [{data['slot_name']}]: "
            f"FullDPS {raw_s['full_dps']:,.1f} -> "
            f"[bold green]{sus_s['full_dps']:,.1f}[/bold green] "
            f"(inflation +{data['dps_inflation_pct']:.1f}%) | "
            f"TotalEHP {raw_s['total_ehp']:,.1f} -> [bold]{sus_s['total_ehp']:,.1f}[/bold] "
            f"(inflation +{data['ehp_inflation_pct']:.1f}%)"
        )
        for sf in data.get("stripped_flags", []):
            console.print(
                f"  • Stripped [bold]{sf['flag']}[/bold]={sf['raw_value']!r} "
                f"(+{sf['dps_inflation_pct']:.1f}% DPS, +{sf['ehp_inflation_pct']:.1f}% EHP)"
            )
        for w in data.get("warnings", []):
            console.print(f"  [yellow]Warning:[/yellow] {w}")

    _run_cli(json_output, _compute, _render)


@pob_app.command("eval-item")
def pob_eval_item_cmd(
    equip_slot: Annotated[
        str,
        typer.Option(
            "--equip-slot", help="PoB2 equipment slot (e.g. 'Weapon 1', 'Body Armour', 'Ring 1')."
        ),
    ],
    item_text: Annotated[
        str | None, typer.Option("--item-text", help="Candidate PoB2 or Ctrl+C item text.")
    ] = None,
    file_path: Annotated[
        Path | None, typer.Option("--file", help="Path to candidate item text file.")
    ] = None,
    price_exalted: Annotated[
        float | None,
        typer.Option("--price-exalted", help="Optional price in Exalted for DPS/Ex ROI."),
    ] = None,
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    json_output: JsonOpt = False,
) -> None:
    """Evaluate a candidate item non-destructively via PoB2 GetMiscCalculator()."""

    def _compute() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        raw_txt = _read_item_input(file_path=file_path, text=item_text)
        assert raw_txt is not None
        pob_txt = normalize_item_text_for_pob2(raw_txt)
        diff = svc.diff_item(
            pob_txt,
            target_slot=equip_slot,
            slot=slot,
            price_exalted=price_exalted,
        )
        return diff.model_dump(mode="json")

    def _render(data: dict[str, Any]) -> None:
        console.print(
            f"[bold cyan]Item Swap ({data['equipment_slot']})[/bold cyan]: "
            f"FullDPS {data['full_dps_before']:,.1f} -> {data['full_dps_after']:,.1f} "
            f"({data['full_dps_pct']:+.1f}%) | "
            f"TotalEHP {data['total_ehp_before']:,.1f} -> {data['total_ehp_after']:,.1f} "
            f"({data['total_ehp_pct']:+.1f}%) | "
            f"Spirit Δ={data['spirit_unreserved_delta']:+d}"
        )

    _run_cli(json_output, _compute, _render)


@pob_app.command("weights")
def pob_weights_cmd(
    equip_slot: Annotated[
        str, typer.Option("--equip-slot", help="PoB2 equipment slot (e.g. 'Weapon 1', 'Amulet').")
    ],
    objective: Annotated[
        str, typer.Option("--objective", help="Weighting objective: balanced, dps, or ehp.")
    ] = "balanced",
    max_exalted: Annotated[
        float | None,
        typer.Option("--max-exalted", help="Optional max Exalted budget in trade2 filter."),
    ] = None,
    securable_only: Annotated[
        bool, typer.Option("--securable-only", help="Restrict filter to Instant Buyout listings.")
    ] = False,
    league: Annotated[str, typer.Option("--league", help="Target PoE2 league.")] = DEFAULT_LEAGUE,
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    json_output: JsonOpt = False,
) -> None:
    """Compute marginal PoB2 stat weights for an equipment slot and generate a trade2 filter."""

    def _compute() -> dict[str, Any]:
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

    def _render(data: dict[str, Any]) -> None:
        table = Table(
            title=f"Marginal Stat Weights — {data['equipment_slot']} ({data['objective']})"
        )
        table.add_column("Stat ID", style="dim")
        table.add_column("Stat")
        table.add_column("Weight", justify="right")
        table.add_column("DPS/unit", justify="right")
        for w in data.get("weights", [])[:15]:
            table.add_row(
                w["stat_id"], w["stat_label"], f"{w['weight']:.3f}", f"{w['dps_per_unit']:.2f}"
            )
        console.print(table)

    _run_cli(json_output, _compute, _render)


@pob_app.command("nodes")
def pob_nodes_cmd(
    mode: Annotated[
        str, typer.Option("--mode", help="Ranking mode: perPoint, total, or anoint.")
    ] = "perPoint",
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    top: Annotated[
        int, typer.Option("--top", help="Number of top notables/anoints to return.")
    ] = 10,
    stat: Annotated[
        str, typer.Option("--stat", help="Target PoB2 stat (FullDPS or TotalEHP).")
    ] = "FullDPS",
    max_dist: Annotated[
        int, typer.Option("--max-dist", help="Max passive pathing distance for perPoint/total.")
    ] = 8,
    include_keystones: Annotated[
        bool, typer.Option("--include-keystones", help="Include Keystones in ranking.")
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Rank reachable passive notables (perPoint/total) or full-tree Amulet anoints (anoint)."""

    def _compute() -> dict[str, Any]:
        if mode not in ("perPoint", "total", "anoint"):
            raise ValueError(f"Invalid --mode {mode!r}; expected perPoint, total, or anoint.")
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        nodes = svc.rank_passive_upgrades(
            slot=slot,
            mode=mode,  # type: ignore[arg-type]
            stat=stat,
            max_path_dist=max_dist,
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

    def _render(data: dict[str, Any]) -> None:
        table = Table(title=f"Top Passive Upgrades ({data['mode']} | {data['stat']})")
        table.add_column("Notable", style="bold cyan")
        table.add_column("Dist", justify="right")
        table.add_column("Full Δ", justify="right")
        table.add_column("Per-Pt %", justify="right")
        for n in data.get("upgrades", []):
            dist = "-" if n["path_dist"] is None else str(n["path_dist"])
            table.add_row(
                n["name"], dist, f"{n['full_path_delta']:+,.1f}", f"{n['per_point_pct']:+.2f}%"
            )
        console.print(table)

    _run_cli(json_output, _compute, _render)


@pob_app.command("export")
def pob_export_cmd(
    fmt: Annotated[
        str, typer.Option("--format", "-f", help="Export format: code (eN...), xml, or ggg_json.")
    ] = "code",
    out_file: Annotated[
        Path | None, typer.Option("--out", "-o", help="Optional output file path.")
    ] = None,
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    json_output: JsonOpt = False,
) -> None:
    """Export a PoB2 build slot as an eN... share code, raw XML, or official GGG .build JSON."""

    def _compute() -> dict[str, Any]:
        if fmt not in ("code", "xml", "ggg_json"):
            raise ValueError(f"Unsupported --format {fmt!r}; expected code, xml, or ggg_json.")
        res = export_pob_slot(slot=slot, fmt=fmt)  # type: ignore[arg-type]
        if res.get("ok") is False:
            raise RuntimeError(res["error"])
        if out_file is not None:
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(res["exported"], encoding="utf-8")
            res["out_file"] = str(out_file)
        return res

    def _render(data: dict[str, Any]) -> None:
        if data.get("out_file"):
            console.print(f"Exported [{data['slot']}] ({data['format']}) -> {data['out_file']}")
        else:
            typer.echo(data["exported"])

    _run_cli(json_output, _compute, _render)


@pob_app.command("equip")
def pob_equip_cmd(
    equip_slot: Annotated[
        str,
        typer.Option("--equip-slot", help="PoB2 equipment slot (e.g. 'Weapon 1', 'Body Armour')."),
    ],
    item_text: Annotated[
        str | None, typer.Option("--item-text", help="PoB2 or Ctrl+C item text.")
    ] = None,
    file_path: Annotated[
        Path | None, typer.Option("--file", help="Path to item text file.")
    ] = None,
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    json_output: JsonOpt = False,
) -> None:
    """Persistently equip an item into a PoB2 build slot and return updated BuildStats."""

    def _compute() -> dict[str, Any]:
        svc = get_build_service()
        ensure_pob_slot_loaded(svc, slot=slot, require_existing=True)
        raw_txt = _read_item_input(file_path=file_path, text=item_text)
        assert raw_txt is not None
        pob_txt = normalize_item_text_for_pob2(raw_txt)
        stats = svc.equip_item(pob_txt, target_slot=equip_slot, slot=slot)
        save_pob_slot_snapshot(svc, slot)
        out = stats.model_dump(mode="json")
        out["slot"] = slot
        out["equipped_slot"] = equip_slot
        return out

    _run_cli(json_output, _compute, lambda d: _render_build_stats(d, f"Equipped {equip_slot}"))


@pob_app.command("slots")
def pob_slots_cmd(
    clone_from: Annotated[
        str | None, typer.Option("--clone-from", help="Source slot to clone.")
    ] = None,
    clone_to: Annotated[
        str | None, typer.Option("--clone-to", help="Target slot for clone.")
    ] = None,
    delete_slot: Annotated[
        str | None, typer.Option("--delete", help="Delete a persisted build slot.")
    ] = None,
    create_slot: Annotated[
        str | None, typer.Option("--create", help="Create/reset a build slot.")
    ] = None,
    class_name: Annotated[
        str | None, typer.Option("--class", help="Class name when creating/resetting a slot.")
    ] = None,
    ascendancy: Annotated[
        str | None,
        typer.Option("--ascendancy", help="Ascendancy name when creating/resetting a slot."),
    ] = None,
    level: Annotated[
        int, typer.Option("--level", help="Character level when creating/resetting a slot.")
    ] = 85,
    gems: Annotated[
        list[str] | None,
        typer.Option("--gems", help="Socket group gem link text to add (repeatable)."),
    ] = None,
    custom_mods: Annotated[
        str | None,
        typer.Option("--custom-mods", help="Custom modifiers string to set on configTab."),
    ] = None,
    config_kv: Annotated[
        list[str] | None,
        typer.Option(
            "--config",
            help="Config key=value override (e.g. useFrenzyCharges=true, overrideShock=50).",
        ),
    ] = None,
    target_slot: Annotated[
        str, typer.Option("--slot", help="Slot for --gems / --custom-mods / --config mutations.")
    ] = "main",
    json_output: JsonOpt = False,
) -> None:
    """List, clone, create, configure, or delete persisted PoB2 build slots."""

    def _compute() -> dict[str, Any]:
        svc = get_build_service()
        if delete_slot:
            delete_pob_slot_snapshot(svc, delete_slot)
            return {"action": "deleted", "slot": delete_slot}

        if create_slot:
            stats = svc.create_character(
                class_name or "Sorceress",
                ascendancy,
                level=level,
                slot=create_slot,
            )
            save_pob_slot_snapshot(svc, create_slot)
            active_target = create_slot
        elif clone_from and clone_to:
            ensure_pob_slot_loaded(svc, slot=clone_from, require_existing=True)
            svc.clone_slot(clone_from, clone_to, activate=False)
            save_pob_slot_snapshot(svc, clone_to)
            active_target = clone_to
        else:
            active_target = target_slot

        if gems or custom_mods is not None or config_kv:
            if not create_slot and not (clone_from and clone_to):
                ensure_pob_slot_loaded(svc, slot=active_target, require_existing=True)
            for idx, gem_group in enumerate(gems or []):
                svc.set_gem_links(
                    gem_group.replace("\\n", "\n"),
                    slot=active_target,
                    include_in_full_dps=True,
                    set_main=(idx == 0),
                )
            cfg_dict: dict[str, Any] = {}
            for pair in config_kv or []:
                if "=" not in pair:
                    raise ValueError(f"Expected key=value for --config, got {pair!r}")
                k, raw_v = pair.split("=", 1)
                low_v = raw_v.strip().lower()
                if low_v == "true":
                    cfg_dict[k.strip()] = True
                elif low_v == "false":
                    cfg_dict[k.strip()] = False
                else:
                    try:
                        cfg_dict[k.strip()] = (
                            int(raw_v) if raw_v.strip().isdigit() else float(raw_v)
                        )
                    except ValueError:
                        cfg_dict[k.strip()] = raw_v.strip()
            if cfg_dict or custom_mods is not None:
                svc.set_config(cfg_dict or None, custom_mods=custom_mods, slot=active_target)
            save_pob_slot_snapshot(svc, active_target)
            stats = svc.get_stats(slot=active_target)
            out = stats.model_dump(mode="json")
            out["slot"] = active_target
            return out

        if create_slot or (clone_from and clone_to):
            out = svc.get_stats(slot=active_target).model_dump(mode="json")
            out["slot"] = active_target
            return out

        slots_dir = get_pob_slot_snapshot_path("main").parent
        saved = sorted(p.stem for p in slots_dir.glob("*.xml")) if slots_dir.is_dir() else []
        return {"persisted_slots": saved, "slots_dir": str(slots_dir)}

    def _render(data: dict[str, Any]) -> None:
        if "persisted_slots" in data:
            joined = ", ".join(data["persisted_slots"]) or "(none)"
            console.print(f"Persisted PoB2 slots ({data['slots_dir']}): {joined}")
        elif "action" in data:
            console.print(f"Slot {data['slot']!r}: {data['action']}")
        else:
            _render_build_stats(data, "Updated PoB2 Slot")

    _run_cli(json_output, _compute, _render)


# ---------------------------------------------------------------------------
# Pillar 3a: Economy (`poe2 econ ...`)
# ---------------------------------------------------------------------------


@econ_app.command("price")
def econ_price_cmd(
    query: Annotated[
        str, typer.Argument(help="Currency, Omen, Essence, Bone, Rune, Gem, or Unique name/slug.")
    ],
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    category: Annotated[
        str | None, typer.Option("--category", help="Optional category filter.")
    ] = None,
    json_output: JsonOpt = False,
) -> None:
    """Look up live PoE2 CurrencyQuote normalized to {exalted, chaos, divine} (Exalted = 1.0)."""

    def _compute() -> dict[str, Any]:
        econ = get_economy_service()
        rates = econ.get_reference_rates(league)
        quote = econ.get_price(query, league=league, category=category)
        if quote is None:
            matches = econ.search_prices(query, league=league, category=category, limit=5)
            if not matches:
                raise KeyError(f"No quote found for {query!r} in {league!r}.")
            quote = matches[0]
        out = quote.model_dump(mode="json")
        out["reference_rates"] = {
            "exalted": 1.0,
            "chaos_exalted": rates.chaos_exalted,
            "divine_exalted": rates.divine_exalted,
        }
        return out

    def _render(data: dict[str, Any]) -> None:
        div_str = f"{data['divine']:.4f} Div" if data.get("divine") is not None else "-"
        chg_str = f"{data['change_7d_pct']:+.1f}%" if data.get("change_7d_pct") is not None else "-"
        console.print(
            f"[bold cyan]{data['name']}[/bold cyan] ({data['league']} | {data['category']}): "
            f"[bold green]{data['exalted']:,.2f} Ex[/bold green] "
            f"({div_str} | 7d: {chg_str} | src: {data['source']})"
        )

    _run_cli(json_output, _compute, _render)


@econ_app.command("rates")
def econ_rates_cmd(
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    json_output: JsonOpt = False,
) -> None:
    """Show live Exalted / Chaos / Divine reference ratios and active PoE2 leagues."""

    def _compute() -> dict[str, Any]:
        econ = get_economy_service()
        rates = econ.get_reference_rates(league)
        leagues = econ.list_leagues()
        return {
            "rates": rates.model_dump(mode="json"),
            "leagues": [lg.model_dump(mode="json") for lg in leagues],
        }

    def _render(data: dict[str, Any]) -> None:
        r = data["rates"]
        console.print(
            f"[bold cyan]{r['league']} Reference Rates[/bold cyan] (Base: Exalted = 1.0): "
            f"1 Divine = [bold green]{r['divine_exalted']:.1f} Ex[/bold green] "
            f"({r['chaos_per_divine']:.2f} Chaos) | "
            f"1 Chaos = [bold]{r['chaos_exalted']:.2f} Ex[/bold]"
        )

    _run_cli(json_output, _compute, _render)


@econ_app.command("scan")
def econ_scan_cmd(
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            help="Optional category filter (ritual, essences, abyss, runes, unique, etc.).",
        ),
    ] = None,
    min_volume_divine: Annotated[
        float, typer.Option("--min-volume-divine", help="Minimum exchange liquidity in Divines.")
    ] = 5.0,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max opportunities to return.")] = 20,
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    json_output: JsonOpt = False,
) -> None:
    """Scan high-liquidity PoE2 exchange movers, cross-source spreads, and chanceable Uniques."""

    def _compute() -> dict[str, Any]:
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

    def _render(data: dict[str, Any]) -> None:
        min_vol = data["min_volume_divine"]
        table = Table(title=f"Market Opportunities — {data['league']} (min vol={min_vol} Div)")
        table.add_column("Kind", style="cyan")
        table.add_column("Item", style="bold")
        table.add_column("Price (Ex)", justify="right")
        table.add_column("Vol (Div)", justify="right")
        table.add_column("7d %", justify="right")
        for opp in data.get("opportunities", []):
            vol_s = f"{opp['volume_divine']:.1f}" if opp.get("volume_divine") is not None else "-"
            chg_s = f"{opp['change_7d_pct']:+.1f}%" if opp.get("change_7d_pct") is not None else "-"
            table.add_row(opp["kind"], opp["name"], f"{opp['exalted']:,.1f}", vol_s, chg_s)
        console.print(table)

    _run_cli(json_output, _compute, _render)


# ---------------------------------------------------------------------------
# Pillar 3b: Trade & Item Clipboard (`poe2 trade ...`)
# ---------------------------------------------------------------------------


@trade_app.command("parse")
def trade_parse_cmd(
    positional: Annotated[str | None, typer.Argument(help="Optional path or Ctrl+C text.")] = None,
    file_path: Annotated[
        Path | None, typer.Option("--file", "-f", help="Path to PoE2 Ctrl+C item file.")
    ] = None,
    text: Annotated[
        str | None, typer.Option("--text", "-t", help="Raw PoE2 Ctrl+C / Ctrl+Alt+C item text.")
    ] = None,
    json_output: JsonOpt = False,
) -> None:
    """Parse PoE2 Ctrl+C / Ctrl+Alt+C clipboard text into ParsedItem and PoB2 text."""

    def _compute() -> dict[str, Any]:
        raw_txt = _read_item_input(file_path=file_path, text=text, positional=positional)
        assert raw_txt is not None
        parsed = parse_clipboard_item(raw_txt)
        out = parsed.model_dump(mode="json")
        out["pob_item_text"] = parsed_item_to_pob2_text(parsed)
        return out

    def _render(data: dict[str, Any]) -> None:
        title = (
            f"{data['name']} {data['base_type']}".strip() if data.get("name") else data["base_type"]
        )
        console.print(
            f"[bold cyan]{title}[/bold cyan] [{data['rarity']}] "
            f"(Class: {data.get('item_class')} | ilvl: {data.get('item_level')} | "
            f"Rune Sockets: {data.get('rune_sockets')})"
        )
        for m in data.get("mods", []):
            console.print(f"  [{m['domain']}] {m['raw_text']}")

    _run_cli(json_output, _compute, _render)


@trade_app.command("check")
def trade_check_cmd(
    positional: Annotated[str | None, typer.Argument(help="Optional path or Ctrl+C text.")] = None,
    file_path: Annotated[
        Path | None, typer.Option("--file", "-f", help="Path to PoE2 Ctrl+C item file.")
    ] = None,
    text: Annotated[
        str | None, typer.Option("--text", "-t", help="Raw PoE2 Ctrl+C / Ctrl+Alt+C item text.")
    ] = None,
    securable_only: Annotated[
        bool,
        typer.Option("--securable-only", help="Restrict to Instant Buyout (Gold fee) listings."),
    ] = False,
    roll_tolerance: Annotated[
        float, typer.Option("--roll-tolerance", help="Minimum affix roll fraction (0.0..1.0).")
    ] = 0.9,
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    json_output: JsonOpt = False,
) -> None:
    """Price-check a PoE2 item: splits Instant Buyout (securable Gold fee) vs whisper listings."""

    def _compute() -> dict[str, Any]:
        raw_txt = _read_item_input(file_path=file_path, text=text, positional=positional)
        assert raw_txt is not None
        res = get_trade_advisor().price_check_item(
            raw_txt,
            league=league,
            roll_tolerance=roll_tolerance,
            securable_only=securable_only,
        )
        return res.model_dump(mode="json")

    def _render(data: dict[str, Any]) -> None:
        dist = data["distribution"]
        console.print(
            f"[bold cyan]Price Check ({data['league']})[/bold cyan] — "
            f"Est: [bold green]{data.get('estimated_exalted')} Ex[/bold green] "
            f"({data['total_matches']} total matches)"
        )
        console.print(
            f"  Instant Buyout (securable): min={dist.get('securable_min_exalted')} Ex | "
            f"median={dist.get('securable_median_exalted')} Ex ({dist['count_securable']} listings)"
        )
        console.print(
            f"  Overall (deduped)         : min={dist.get('min_exalted')} Ex | "
            f"median={dist.get('median_exalted')} Ex ({dist['count_whisper']} whisper)"
        )
        if data.get("trade_url"):
            console.print(f"  Trade URL: {data['trade_url']}")
        for w in data.get("fixer_warnings", []):
            console.print(f"  [yellow]Fixer Warning:[/yellow] {w}")

    _run_cli(json_output, _compute, _render)


@trade_app.command("upgrades")
def trade_upgrades_cmd(
    equip_slot: Annotated[
        str,
        typer.Option(
            "--equip-slot", help="Equipment slot (e.g. 'Weapon 1', 'Body Armour', 'Amulet')."
        ),
    ],
    max_exalted: Annotated[
        float | None, typer.Option("--max-exalted", help="Maximum budget in Exalted Orbs.")
    ] = 50.0,
    min_spirit: Annotated[
        int, typer.Option("--min-spirit", help="Minimum required Spirit on candidate items.")
    ] = 0,
    securable_only: Annotated[
        bool,
        typer.Option("--securable-only", help="Restrict to Instant Buyout (Gold fee) listings."),
    ] = False,
    slot: Annotated[str, typer.Option("--slot", help="Loaded PoB2 build slot name.")] = "main",
    build: Annotated[
        str | None,
        typer.Option("--build", help="Optional PoB2 code/URL/file to load into --slot first."),
    ] = None,
    objective: Annotated[
        str,
        typer.Option(
            "--objective", help="PoB2 ranking objective: FullDPS/dps, TotalEHP/ehp, or balanced."
        ),
    ] = "FullDPS",
    stat: Annotated[
        list[str] | None,
        typer.Option(
            "--stat", help="Standalone natural-language stat filter when searching without PoB2."
        ),
    ] = None,
    no_pob: Annotated[
        bool,
        typer.Option(
            "--no-pob", help="Skip headless PoB2 ranking and return raw trade2 candidates."
        ),
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="Max candidate listings to evaluate.")
    ] = 10,
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    json_output: JsonOpt = False,
) -> None:
    """Query live trade2 for slot upgrades and rank candidates via PoB2 GetMiscCalculator()."""

    def _compute() -> dict[str, Any]:
        return execute_find_upgrades(
            equip_slot=equip_slot,
            max_exalted=max_exalted,
            min_spirit=min_spirit,
            securable_only=securable_only,
            slot=slot,
            build_source=build,
            objective=objective,
            stat_filters=stat,
            no_pob=no_pob,
            limit=limit,
            league=league,
        )

    def _render(data: dict[str, Any]) -> None:
        if "candidates" in data:
            max_ex = data.get("max_price_exalted")
            tot_m = data["total_market_matches"]
            console.print(
                f"[bold cyan]Live PoB2 Upgrades — {data['equipment_slot']}[/bold cyan] "
                f"(budget <= {max_ex} Ex | matches={tot_m})"
            )
            if data.get("trade_url"):
                console.print(f"  Trade URL: {data['trade_url']}")
            for cand in data.get("candidates", [])[:10]:
                lst = cand["listing"]
                diff = cand["upgrade_diff"]
                fee_s = f", Gold fee={lst['fee']}" if lst.get("fee") is not None else ""
                item_lbl = f"{lst.get('item_name') or ''} {lst['base_type']}".strip()
                console.print(
                    f"  #{cand['rank']} [{lst['sale_type']}] {item_lbl} — "
                    f"[bold green]{lst['price_exalted']} Ex[/bold green]{fee_s} | "
                    f"FullDPS {diff['full_dps_pct']:+.1f}% ({diff['full_dps_delta']:+,.1f}) | "
                    f"EHP {diff['total_ehp_pct']:+.1f}%"
                )
        else:
            console.print(
                f"[bold cyan]Trade Candidates — {data.get('equipment_slot')}[/bold cyan] "
                f"({len(data.get('listings', []))} fetched, url={data.get('trade_url')})"
            )
            for lst in data.get("listings", [])[:10]:
                item_lbl = f"{lst.get('item_name') or ''} {lst['base_type']}".strip()
                console.print(
                    f"  • [{lst['sale_type']}] {item_lbl} — {lst['price_exalted']} Ex"
                )

    _run_cli(json_output, _compute, _render)


# ---------------------------------------------------------------------------
# Pillar 4: Crafting (`poe2 craft ...`)
# ---------------------------------------------------------------------------


@craft_app.command("odds")
def craft_odds_cmd(
    base: Annotated[
        str,
        typer.Option(
            "--base",
            help="PoE2 base item name or class slug (e.g. 'Attuned Wand', 'Hexer\\'s Robe').",
        ),
    ],
    ilvl: Annotated[int, typer.Option("--ilvl", help="Item level.")] = 82,
    lever: Annotated[
        str | None, typer.Option("--lever", help="Crafting orb, essence, alloy, or bone name.")
    ] = None,
    target: Annotated[
        list[str] | None,
        typer.Option(
            "--target", help="Target modifier text, family, or 'T1 Spell Damage' (repeatable)."
        ),
    ] = None,
    omen: Annotated[
        list[str] | None,
        typer.Option("--omen", help="Active Ritual/Abyssal Omen modifier (repeatable)."),
    ] = None,
    item_text: Annotated[
        str | None, typer.Option("--item-text", help="Optional starting item Ctrl+C text.")
    ] = None,
    file_path: Annotated[
        Path | None, typer.Option("--file", help="Optional starting item Ctrl+C file.")
    ] = None,
    league: Annotated[
        str, typer.Option("--league", help="PoE2 league for live currency quotes.")
    ] = DEFAULT_LEAGUE,
    offline_prices: Annotated[
        bool,
        typer.Option("--offline-prices", help="Use reference Patch 0.5.5 prices without network."),
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Compute exact PoE2 0.5.5 conditional mod-pool hit odds and expected step cost."""

    def _compute() -> dict[str, Any]:
        raw_item = _read_item_input(file_path=file_path, text=item_text, allow_empty=True)
        return execute_craft_odds(
            base=base,
            ilvl=ilvl,
            lever=lever,
            target=target,
            omen=omen,
            item_text=raw_item,
            league=league,
            offline_prices=offline_prices,
        )

    def _render(data: dict[str, Any]) -> None:
        console.print(
            f"[bold cyan]Crafting Odds — {data['base_type']} "
            f"(ilvl {data['ilvl']}, {data['assumed_rarity']})[/bold cyan] "
            f"[dim]({data['price_source']})[/dim]"
        )
        table = Table()
        table.add_column("Action", style="bold")
        table.add_column("Hit %", justify="right")
        table.add_column("Step (Ex)", justify="right")
        table.add_column("E[Cost] (Ex)", justify="right")
        table.add_column("Weights (Hit/Pool)", justify="right")
        table.add_column("Notes")
        for ev in data.get("evaluations", [])[:10]:
            ec = (
                f"{ev['expected_cost_to_hit_exalted']:,.1f}"
                if ev.get("expected_cost_to_hit_exalted") is not None
                else "-"
            )
            table.add_row(
                ev["action_label"],
                f"{ev['hit_probability'] * 100:.2f}%",
                f"{ev['step_cost_exalted']:,.2f}",
                ec,
                f"{ev['target_weight']}/{ev['eligible_pool_weight']}",
                str(ev.get("notes") or ""),
            )
        console.print(table)

    _run_cli(json_output, _compute, _render)


@craft_app.command("plan")
def craft_plan_cmd(
    base: Annotated[str, typer.Option("--base", help="PoE2 base item name or class slug.")],
    target: Annotated[
        list[str],
        typer.Option("--target", help="Target modifier text, family, or 'T1 ...' (repeatable)."),
    ],
    ilvl: Annotated[int, typer.Option("--ilvl", help="Item level.")] = 82,
    buy_exalted: Annotated[
        float | None,
        typer.Option(
            "--buy-exalted", help="Market buy price in Exalted for buy-vs-craft comparison."
        ),
    ] = None,
    base_cost_exalted: Annotated[
        float,
        typer.Option(
            "--base-cost-exalted", help="Cost of each clean/fractured starting base in Exalted."
        ),
    ] = 0.0,
    sims: Annotated[
        int,
        typer.Option("--sims", help="Monte Carlo trial count for p50/p80/p95 cost percentiles."),
    ] = 3000,
    item_text: Annotated[
        str | None, typer.Option("--item-text", help="Optional starting item Ctrl+C text.")
    ] = None,
    file_path: Annotated[
        Path | None, typer.Option("--file", help="Optional starting item Ctrl+C file.")
    ] = None,
    league: Annotated[
        str, typer.Option("--league", help="PoE2 league for live currency quotes.")
    ] = DEFAULT_LEAGUE,
    offline_prices: Annotated[
        bool,
        typer.Option("--offline-prices", help="Use reference Patch 0.5.5 prices without network."),
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Solve optimal PoE2 0.5.5 crafting policy, Monte Carlo percentiles, and buy-vs-craft."""

    def _compute() -> dict[str, Any]:
        raw_item = _read_item_input(file_path=file_path, text=item_text, allow_empty=True)
        return execute_craft_plan(
            base=base,
            ilvl=ilvl,
            target=target,
            buy_exalted=buy_exalted,
            sims=sims,
            base_cost_exalted=base_cost_exalted,
            item_text=raw_item,
            league=league,
            offline_prices=offline_prices,
        )

    def _render(data: dict[str, Any]) -> None:
        div_s = (
            f" (~{data['expected_cost_divine']:.2f} Div)"
            if data.get("expected_cost_divine") is not None
            else ""
        )
        console.print(
            f"[bold cyan]Crafting Plan — {data['base_type']} "
            f"(ilvl {data['item_level']})[/bold cyan] "
            f"[dim]({data.get('price_source')})[/dim]"
        )
        console.print(f"  Targets       : {', '.join(data['target_descriptions'])}")
        console.print(
            f"  Expected Cost : "
            f"[bold green]{data['expected_cost_exalted']:,.1f} Ex[/bold green]{div_s} "
            f"(p50={data['p50_cost_exalted']:,.1f} Ex | "
            f"p80={data['p80_cost_exalted']:,.1f} Ex | "
            f"p95={data['p95_cost_exalted']:,.1f} Ex)"
        )
        for st in data.get("policy_steps", []):
            prob_pct = st["transition_probability"] * 100
            console.print(
                f"  Step {st['step_index']}: When [{st['when_state']}] -> "
                f"[bold]{st['recommended_action']}[/bold] "
                f"(cost={st['step_cost_exalted']:.2f} Ex, p={prob_pct:.2f}%)"
            )
        tvc = data.get("trade_vs_craft")
        if tvc:
            rec_up = tvc["recommendation"].upper()
            console.print(f"  [bold yellow]Verdict ({rec_up}):[/bold yellow] {tvc['rationale']}")

    _run_cli(json_output, _compute, _render)


# ---------------------------------------------------------------------------
# Maintenance: `poe2 sync gamedata` & `poe2 bootstrap pob2`
# ---------------------------------------------------------------------------


@sync_app.command("gamedata")
def sync_gamedata_cmd(
    out: Annotated[Path | None, typer.Option("--out", help="Output SQLite database path.")] = None,
    json_output: JsonOpt = False,
) -> None:
    """Compile deterministic PoE2 0.5.5 SQLite + FTS5 gamedata database."""
    _run_cli(
        json_output,
        lambda: run_sync_gamedata(out_path=out),
        lambda d: console.print(
            f"[green]Built gamedata DB:[/green] {d['db_path']} ({d['size_bytes']:,} bytes)"
        ),
    )


@bootstrap_app.command("pob2")
def bootstrap_pob2_cmd(
    force: Annotated[
        bool, typer.Option("--force", help="Force re-seed/rebuild of LuaJIT + PathOfBuilding-PoE2.")
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Bootstrap local headless LuaJIT + PathOfBuilding-PoE2 runtime."""
    _run_cli(
        json_output,
        lambda: run_bootstrap_script(force=force),
        lambda d: console.print(d["output"]),
    )


if __name__ == "__main__":
    app()
