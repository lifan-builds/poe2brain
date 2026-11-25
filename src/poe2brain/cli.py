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
    close_pob_services,
    collect_engine_status,
    delete_pob_slot_snapshot,
    ensure_pob_slot_loaded,
    execute_craft_odds,
    execute_craft_plan,
    execute_filter_generate,
    execute_filter_heal,
    execute_filter_presets,
    execute_filter_restyle,
    execute_filter_validate,
    execute_find_upgrades,
    execute_inspect_ascendancy,
    execute_meta_ladder,
    execute_ninja_character,
    execute_ninja_search,
    execute_ninja_snapshots,
    execute_pob_calc_breakdown,
    execute_pob_compare,
    execute_pob_hc_audit,
    execute_pob_import_character,
    execute_pob_import_local,
    execute_pob_radius_jewels,
    execute_pob_supports,
    execute_pob_what_if,
    execute_scan_arbitrage,
    execute_scan_craft_profit,
    execute_triage_dump,
    execute_waystone_regex,
    export_pob_slot,
    format_domain_error,
    get_build_service,
    get_economy_service,
    get_knowledge_service,
    get_pob_slot_snapshot_path,
    get_trade_advisor,
    normalize_item_text_for_pob2,
    normalize_pob_objective,
    parse_config_kv_pairs,
    run_bootstrap_script,
    run_sync_gamedata,
    run_with_service_cleanup,
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
ninja_app = typer.Typer(
    help="Pillar 5: PoE Ninja PoE2 builds search, snapshots, and live PoB2 character import.",
    no_args_is_help=True,
)
filter_app = typer.Typer(
    help="Pillar 6: PoE2 0.5.5 loot filter compiler, theme composer, custom soundpacks, & healing.",
    no_args_is_help=True,
)
auth_app = typer.Typer(
    help="Official GGG OAuth 2.1 PKCE login for private/Day-1 PoE2 character imports.",
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
app.add_typer(ninja_app, name="ninja")
app.add_typer(filter_app, name="filter")
app.add_typer(auth_app, name="auth")
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
                    f"[bold red]Error ({err_payload['error_type']}):[/bold red] "
                    f"{err_payload['error']}"
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
    finally:
        close_pob_services()


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


@wiki_app.command("ascendancy")
def wiki_ascendancy_cmd(
    name_or_class: Annotated[
        str | None,
        typer.Argument(
            help="Ascendancy or base class name (omit to list all 23 PoE2 0.5.5 Ascendancies)."
        ),
    ] = None,
    class_filter: Annotated[
        str | None,
        typer.Option("--class", "-c", help="Optional base class filter when listing Ascendancies."),
    ] = None,
    json_output: JsonOpt = False,
) -> None:
    """List all 23 PoE2 0.5.5 Ascendancies or inspect notables, choice nodes, and PoE1 traps."""

    def _compute() -> dict[str, Any]:
        return execute_inspect_ascendancy(
            name_or_class=name_or_class,
            class_filter=class_filter,
        )

    def _render(data: dict[str, Any]) -> None:
        if data.get("mode") == "list":
            table = Table(title=f"PoE2 {data.get('patch')} Ascendancies ({data['count']})")
            table.add_column("Class", style="cyan")
            table.add_column("Ascendancy", style="bold")
            table.add_column("Summary")
            for asc in data.get("ascendancies", []):
                summary_txt = str(asc.get("archetype_summary") or asc.get("summary") or "")
                table.add_row(
                    str(asc.get("class_name")),
                    str(asc.get("name")),
                    summary_txt[:96],
                )
            console.print(table)
            return

        for asc in data.get("ascendancies", []):
            console.print(
                f"[bold cyan]{asc['name']}[/bold cyan] ({asc['class_name']}) — "
                f"{len(asc.get('notables', []))} notables"
            )
            summary_txt = asc.get("archetype_summary") or asc.get("summary")
            if summary_txt:
                console.print(f"  [dim]{summary_txt}[/dim]")
            for nt in asc.get("notables", []):
                stats_str = "; ".join(nt.get("stats") or [])
                console.print(f"  • [bold]{nt['name']}[/bold]: {stats_str}")
                for opt in nt.get("choice_options") or []:
                    opt_stats = "; ".join(opt.get("stats") or [])
                    console.print(f"      ↳ [cyan]{opt['name']}[/cyan]: {opt_stats}")

    _run_cli(json_output, _compute, _render)


@wiki_app.command("meta")
def wiki_meta_cmd(
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    top_n: Annotated[
        int, typer.Option("--top-n", help="Top ladder characters to inspect when available.")
    ] = 200,
    no_economy: Annotated[
        bool, typer.Option("--no-economy", help="Skip live chase item demand signal enrichment.")
    ] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass 30-minute ladder telemetry cache.")
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Show live PoE2 SC/HC/SSF/HCSSF ladder shares, Ascendancy tier rankings, and chase demand."""

    def _compute() -> dict[str, Any]:
        return execute_meta_ladder(
            league=league,
            top_n=top_n,
            include_economy_signals=not no_economy,
            force_refresh=refresh,
        )

    def _render(data: dict[str, Any]) -> None:
        div_s = f" | 1 Div = {data['divine_exalted']:.1f} Ex" if data.get("divine_exalted") else ""
        table = Table(title=f"PoE2 {data['patch']} Ladder Meta — {data['league']}{div_s}")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Tier", style="bold yellow")
        table.add_column("Ascendancy", style="bold cyan")
        table.add_column("Class")
        table.add_column("SC %", justify="right")
        table.add_column("HC %", justify="right")
        table.add_column("SSF %", justify="right")
        table.add_column("Score", justify="right")
        for row in data.get("ascendancy_rankings", [])[:15]:
            table.add_row(
                str(row["rank"]),
                str(row["tier"]),
                str(row["ascendancy_name"]),
                str(row["class_name"]),
                f"{row['sc_share_pct']:.2f}%",
                f"{row['hc_share_pct']:.2f}%",
                f"{row['ssf_share_pct']:.2f}%",
                f"{row['composite_score']:.1f}",
            )
        console.print(table)

    _run_cli(json_output, _compute, _render)


@wiki_app.command("regex")
def wiki_regex_cmd(
    slot: Annotated[
        str | None,
        typer.Option("--slot", help="Optional persisted PoB2 slot to auto-detect build hazards."),
    ] = None,
    archetype: Annotated[
        list[str] | None,
        typer.Option(
            "--archetype",
            help="Explicit archetype CSV/tags (ci, archmage_mom, leech, crit, hybrid_es_life).",
        ),
    ] = None,
    avoid: Annotated[
        list[str] | None,
        typer.Option("--avoid", help="Waystone mod keywords to treat as bricks (CSV/repeatable)."),
    ] = None,
    want: Annotated[
        list[str] | None,
        typer.Option("--want", help="Desired Waystone juicing keywords (CSV/repeatable)."),
    ] = None,
    tablet_want: Annotated[
        list[str] | None,
        typer.Option(
            "--tablet-want", help="Desired Precursor Tablet mod keywords (CSV/repeatable)."
        ),
    ] = None,
    min_waystone_drop: Annotated[
        int, typer.Option("--min-waystone-drop", help="Minimum +Waystone Drop Chance %.")
    ] = 120,
    min_pack_size: Annotated[
        int | None, typer.Option("--min-pack-size", help="Minimum +Pack Size % (or omit).")
    ] = 20,
    item_text: Annotated[
        str | None, typer.Option("--item-text", help="Optional Waystone Ctrl+C text to inspect.")
    ] = None,
    file_path: Annotated[
        Path | None, typer.Option("--file", help="Optional Waystone Ctrl+C file to inspect.")
    ] = None,
    include_dangerous: Annotated[
        bool, typer.Option("--include-dangerous", help="Include dangerous non-brick mods in regex.")
    ] = False,
    max_chars: Annotated[
        int, typer.Option("--max-chars", help="Maximum PoE2 Stash Search regex length (<= 50).")
    ] = 50,
    json_output: JsonOpt = False,
) -> None:
    """Compile <=50-char PoE2 Waystone brick/safe/juicing & Precursor Tablet stash regexes."""

    def _compute() -> dict[str, Any]:
        raw_ws = _read_item_input(file_path=file_path, text=item_text, allow_empty=True)
        return execute_waystone_regex(
            slot=slot,
            archetypes=archetype,
            avoid_keywords=avoid,
            want_keywords=want,
            tablet_want_keywords=tablet_want,
            min_waystone_drop_pct=min_waystone_drop,
            min_pack_size_pct=min_pack_size,
            item_text=raw_ws,
            include_dangerous=include_dangerous,
            max_chars=max_chars,
        )

    def _render(data: dict[str, Any]) -> None:
        arch_s = ", ".join(data.get("detected_archetypes") or []) or "default_mapper"
        console.print(f"[bold cyan]PoE2 0.5.5 Stash Regex Plan[/bold cyan] (archetypes: {arch_s})")
        for key, label in (
            ("brick_regex", "Brick Highlight"),
            ("safe_runnable_regex", "Safe Runnable"),
            ("juicing_regex", "Waystone Juicing"),
            ("tablet_regex", "Tablet Rolling"),
        ):
            rx = data[key]
            console.print(
                f"  [bold]{label:<17}[/bold]: [green]{rx['regex']}[/green] "
                f"({rx['char_length']}/{rx['max_chars']} chars)"
            )
        if data.get("item_check"):
            ic = data["item_check"]
            verdict = "[red]BRICK[/red]" if ic["is_brick"] else "[green]SAFE[/green]"
            console.print(
                f"  Item Check       : {ic['waystone_name']} -> {verdict} "
                f"(revives={ic['revives_available']})"
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


@pob_app.command("compare")
def pob_compare_cmd(
    slot_a: Annotated[
        str, typer.Option("--slot-a", help="Baseline/earlier persisted PoB2 slot name.")
    ] = "early",
    slot_b: Annotated[
        str, typer.Option("--slot-b", help="Target/current persisted PoB2 slot name.")
    ] = "main",
    dewater: Annotated[
        bool,
        typer.Option("--dewater", help="Compare sustainable Pinnacle Boss dewatered stats."),
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Compare two persisted PoB2 slots across DPS/EHP, gear, skills, passives, and Gold respec."""

    def _compute() -> dict[str, Any]:
        return execute_pob_compare(slot_a=slot_a, slot_b=slot_b, dewater=dewater)

    def _render(data: dict[str, Any]) -> None:
        respec = data["respec_estimate"]
        console.print(
            f"[bold cyan]PoB2 Slot Comparison[/bold cyan] [{data['slot_a']} -> {data['slot_b']}] "
            f"(Lv Δ={data['level_delta']:+d} | dewatered={data['dewatered']})"
        )
        console.print(
            f"  Effective DPS : {data['effective_dps_before']:,.1f} -> "
            f"[bold green]{data['effective_dps_after']:,.1f}[/bold green] "
            f"({data['effective_dps_pct']:+.1f}%)"
        )
        console.print(
            f"  Total EHP     : {data['total_ehp_before']:,.1f} -> "
            f"[bold]{data['total_ehp_after']:,.1f}[/bold] ({data['total_ehp_pct']:+.1f}%)"
        )
        if data.get("added_uniques"):
            console.print(f"  Added Uniques : {', '.join(data['added_uniques'])}")
        console.print(
            f"  Gold Respec   : [yellow]{respec['total_gold_cost']:,} Gold[/yellow] "
            f"({respec['refunded_passive_points']} passives, "
            f"{respec['refunded_ascendancy_points']} Ascendancy)"
        )

    _run_cli(json_output, _compute, _render)


@pob_app.command("hc-audit")
def pob_hc_audit_cmd(
    source: Annotated[
        str | None, typer.Argument(help="Optional build code/URL/file to load before HC auditing.")
    ] = None,
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    top_nodes: Annotated[
        int, typer.Option("--top-nodes", help="Top defensive EHP passive clusters to rank.")
    ] = 5,
    apply_dewater: Annotated[
        bool,
        typer.Option("--apply", help="Persistently strip inflated PoB-warrior flags on the slot."),
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Audit HC Pinnacle Boss Max Hits, Bleed/Poison ES bypass, Chaos 2x ES, & Gold respecs."""

    def _compute() -> dict[str, Any]:
        return execute_pob_hc_audit(
            source=source,
            slot=slot,
            top_nodes=top_nodes,
            apply_dewater=apply_dewater,
        )

    def _render(data: dict[str, Any]) -> None:
        mh = data["max_hits"]
        ready_lbl = (
            "[bold green]READY[/bold green]"
            if data["pinnacle_ready"]
            else "[bold red]AT RISK[/bold red]"
        )
        console.print(
            f"[bold cyan]HC Pinnacle Rip Audit[/bold cyan] [{data['slot_name']}]: {ready_lbl} "
            f"(Phys={mh.get('physical', 0):,.0f} | Chaos={mh.get('chaos', 0):,.0f} | "
            f"Light={mh.get('lightning', 0):,.0f})"
        )
        for r in data.get("rip_risks", []):
            col = "red" if r["severity"] in ("critical", "high") else "yellow"
            console.print(
                f"  [{col}][{r['severity'].upper()}][/{col}] {r['title']} — {r['summary']}"
            )
        for nd in data.get("top_defensive_passives", []):
            console.print(
                f"  • Respec -> [bold]{nd['name']}[/bold] ({nd['path_dist']} pts, "
                f"{nd['ehp_pct']:+.2f}% EHP, {nd['respec_gold_cost']:,} Gold)"
            )

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


@pob_app.command("calc-breakdown")
def pob_calc_breakdown_cmd(
    query: Annotated[
        str,
        typer.Argument(
            help="PoB2 stat or alias (e.g. FullDPS, ManaCost, EnergyShield, CritChance, Spirit)."
        ),
    ] = "FullDPS",
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    skill_group: Annotated[
        int | None,
        typer.Option("--skill-group", help="Optional 1-based socket group override."),
    ] = None,
    max_mods: Annotated[
        int, typer.Option("--max-mods", help="Maximum modifier provenance rows to return.")
    ] = 40,
    json_output: JsonOpt = False,
) -> None:
    """Explain exact PoB2 calculation formulas & modifier provenance (BASE/INC/MORE/FLAG)."""

    def _compute() -> dict[str, Any]:
        return execute_pob_calc_breakdown(
            query=query,
            slot=slot,
            skill_group=skill_group,
            max_mods=max_mods,
        )

    def _render(data: dict[str, Any]) -> None:
        console.print(
            f"[bold cyan]PoB2 Calc Breakdown — {data['query']}[/bold cyan] "
            f"[{data['slot']} | Group #{data['skill_group']}: {data['active_skill_name']} | "
            f"mods={data['modifier_count_total']}]"
        )
        for w in data.get("warnings", []):
            console.print(f"  [yellow]Warning:[/yellow] {w}")
        for st in data.get("matched_stats", [])[:6]:
            console.print(f"  • [bold]{st['key']}[/bold] = {st['value']}")
            for ln in st.get("lines", [])[:4]:
                console.print(f"      {ln}")
        if data.get("source_totals"):
            table = Table(title="Modifier Totals by Source")
            table.add_column("Source", style="bold cyan")
            table.add_column("Count", justify="right")
            table.add_column("BASE", justify="right")
            table.add_column("INC %", justify="right")
            table.add_column("MORE %", justify="right")
            for s in data["source_totals"]:
                table.add_row(
                    s["source_type"],
                    str(s["mod_count"]),
                    f"{s['base_total']:,.2f}",
                    f"{s['inc_total']:+.1f}%",
                    f"{s['more_pct']:+.2f}%",
                )
            console.print(table)

    _run_cli(json_output, _compute, _render)


@pob_app.command("supports")
def pob_supports_cmd(
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    group_index: Annotated[
        int | None,
        typer.Option("--group", help="1-based socket group index (defaults to mainSkill)."),
    ] = None,
    replace_gem_slot: Annotated[
        int | None,
        typer.Option(
            "--replace-slot",
            help="1-based support ordinal to replace instead of appending.",
        ),
    ] = None,
    replace_gem: Annotated[
        str | None,
        typer.Option("--replace-gem", help="Existing support gem name substring to swap out."),
    ] = None,
    max_tier: Annotated[
        int | None,
        typer.Option("--max-tier", help="Maximum Uncut Support Gem Tier (1-5)."),
    ] = None,
    include_lineage: Annotated[
        bool,
        typer.Option("--lineage/--no-lineage", help="Include PoE2 Lineage Support Gems."),
    ] = True,
    exclude_lineage_conflicts: Annotated[
        bool,
        typer.Option(
            "--exclude-conflicts",
            help="Hide Lineage Supports already socketed in another skill group (copy=1).",
        ),
    ] = False,
    top: Annotated[
        int, typer.Option("--top", help="Number of top support gem candidates to return.")
    ] = 15,
    json_output: JsonOpt = False,
) -> None:
    """Rank compatible PoE2 0.5.5 Support Gems (Tier I-III + Lineage) with Mana/Spirit checks."""

    def _compute() -> dict[str, Any]:
        return execute_pob_supports(
            slot=slot,
            group_index=group_index,
            replace_gem_slot=replace_gem_slot,
            replace_gem=replace_gem,
            max_tier=max_tier,
            include_lineage=include_lineage,
            exclude_lineage_conflicts=exclude_lineage_conflicts,
            top_n=top,
        )

    def _render(data: dict[str, Any]) -> None:
        cur_supps = ", ".join(data.get("current_supports", [])) or "none"
        table = Table(
            title=(
                f"Support Gem Ranking — {data['active_skill_label']} "
                f"(Group #{data['group_index']} | current={cur_supps})"
            )
        )
        table.add_column("Support Gem", style="bold cyan")
        table.add_column("Tier", justify="right")
        table.add_column("DPS %", justify="right")
        table.add_column("Mana", justify="right")
        table.add_column("Notes")
        for c in data.get("candidates", []):
            t_lbl = "Lineage" if c["is_lineage"] else f"T{c['support_tier'] or '?'}"
            notes: list[str] = []
            if c.get("already_socketed"):
                notes.append("equipped")
            if c.get("replaces_gem"):
                notes.append(f"replaces {c['replaces_gem']}")
            if c.get("lineage_conflict_skill"):
                notes.append(f"CONFLICT:{c['lineage_conflict_skill']}")
            if not c.get("mana_sustainable_after", True):
                notes.append("mana-unsustainable")
            if c.get("spirit_overflow"):
                notes.append("spirit-overflow")
            if not c.get("attributes_met_after", True):
                notes.append("attr-short")
            table.add_row(
                c["name"],
                t_lbl,
                f"{c['dps_pct']:+.2f}%",
                f"{c['mana_cost_after']:.1f}",
                ", ".join(notes) or "ok",
            )
        console.print(table)

    _run_cli(json_output, _compute, _render)


@pob_app.command("radius-jewels")
def pob_radius_jewels_cmd(
    jewel_name: Annotated[
        str,
        typer.Option(
            "--jewel",
            help="Radius jewel type ('Controlled Metamorphosis' or 'Against the Darkness').",
        ),
    ] = "Controlled Metamorphosis",
    slot: Annotated[str, typer.Option("--slot", help="Named PoB2 build slot.")] = "main",
    stat: Annotated[
        str, typer.Option("--stat", help="Target PoB2 stat (FullDPS or TotalEHP).")
    ] = "FullDPS",
    max_dist: Annotated[
        int,
        typer.Option(
            "--max-dist",
            help="Maximum passive pathing distance to unallocated sockets.",
        ),
    ] = 10,
    top: Annotated[
        int, typer.Option("--top", help="Number of top socket × ring combinations to return.")
    ] = 10,
    json_output: JsonOpt = False,
) -> None:
    """Rank Passive Tree Jewel Sockets & rings for Controlled Metamorphosis / Against Darkness."""

    def _compute() -> dict[str, Any]:
        return execute_pob_radius_jewels(
            jewel_name=jewel_name,
            slot=slot,
            stat=stat,
            max_path_dist=max_dist,
            top_n=top,
        )

    def _render(data: dict[str, Any]) -> None:
        table = Table(title=f"Radius Jewel Optimizer — {data['jewel_name']} ({data['stat']})")
        table.add_column("Socket", style="bold cyan")
        table.add_column("Ring")
        table.add_column("Pts", justify="right")
        table.add_column("Gain %", justify="right")
        table.add_column("%/Pt", justify="right")
        table.add_column("Notables")
        for c in data.get("candidates", []):
            table.add_row(
                f"#{c['socket_node_id']}{' (alloc)' if c['socket_allocated'] else ''}",
                c["ring_label"],
                str(c["total_points_spent"]),
                f"{c['pct']:+.2f}%",
                f"{c['per_point_pct']:+.2f}%",
                ", ".join(c.get("ring_notables", [])[:4]),
            )
        console.print(table)

    _run_cli(json_output, _compute, _render)


@pob_app.command("what-if")
def pob_what_if_cmd(
    slot: Annotated[str, typer.Option("--slot", help="Target PoB2 build slot.")] = "main",
    equip: Annotated[
        list[str] | None,
        typer.Option(
            "--equip",
            help="Equip override as 'Slot=ItemTextOrFilePath' (repeatable across multiple slots).",
        ),
    ] = None,
    edit_slot: Annotated[
        str | None,
        typer.Option("--edit-slot", help="Existing equipped slot to edit in place."),
    ] = None,
    add_mod: Annotated[
        list[str] | None,
        typer.Option(
            "--add-mod",
            help="Explicit modifier line to append to --edit-slot (repeatable).",
        ),
    ] = None,
    remove_mod: Annotated[
        list[str] | None,
        typer.Option(
            "--remove-mod",
            help="Substring of explicit mod line(s) to strip on --edit-slot.",
        ),
    ] = None,
    set_quality: Annotated[
        int | None,
        typer.Option("--set-quality", help="Override Quality % on --edit-slot."),
    ] = None,
    alloc_node: Annotated[
        list[int] | None,
        typer.Option("--alloc-node", help="Passive node ID to allocate (repeatable)."),
    ] = None,
    dealloc_node: Annotated[
        list[int] | None,
        typer.Option("--dealloc-node", help="Passive node ID to deallocate (repeatable)."),
    ] = None,
    config_kv: Annotated[
        list[str] | None,
        typer.Option("--config", help="Config key=value override (repeatable)."),
    ] = None,
    borrow_from: Annotated[
        str | None,
        typer.Option("--borrow-from", help="Reference PoB2 slot to transplant pieces from."),
    ] = None,
    borrow_slot: Annotated[
        list[str] | None,
        typer.Option(
            "--borrow-slot",
            help="Equipment slot to transplant from --borrow-from (repeatable).",
        ),
    ] = None,
    borrow_main_skill: Annotated[
        bool,
        typer.Option(
            "--borrow-main-skill",
            help="Also transplant mainSkill gem links from --borrow-from.",
        ),
    ] = False,
    scenario_matrix: Annotated[
        bool,
        typer.Option(
            "--scenario-matrix",
            help="Evaluate across configured, sustainable_pinnacle, and mapping_clear scenarios.",
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Persistently commit the simulated changes to --slot."),
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Run compound PoB2 what-if simulations (multi-slot, mod edits, passives, ref borrowing)."""

    def _compute() -> dict[str, Any]:
        return execute_pob_what_if(
            slot=slot,
            equip_items=equip,
            edit_slot=edit_slot,
            add_mod_lines=add_mod,
            remove_mod_substrings=remove_mod,
            set_quality=set_quality,
            alloc_node_ids=alloc_node,
            dealloc_node_ids=dealloc_node,
            set_config=config_kv,
            borrow_from_slot=borrow_from,
            borrow_slots=borrow_slot,
            borrow_main_skill=borrow_main_skill,
            include_scenario_matrix=scenario_matrix,
            apply=apply,
        )

    def _render(data: dict[str, Any]) -> None:
        base_st = data["baseline_stats"]
        after_st = data["after_stats"]
        sp_delta = int(after_st["spirit_unreserved"]) - int(base_st["spirit_unreserved"])
        console.print(
            f"[bold cyan]PoB2 What-If Simulation[/bold cyan] "
            f"[{data['slot']} | applied={data['applied']}]"
        )
        console.print(
            f"  Full DPS  : {base_st['full_dps']:,.1f} -> "
            f"[bold green]{after_st['full_dps']:,.1f}[/bold green] "
            f"({data['effective_dps_pct']:+.2f}%)"
        )
        console.print(
            f"  Total EHP : {base_st['total_ehp']:,.1f} -> "
            f"[bold]{after_st['total_ehp']:,.1f}[/bold] "
            f"({data['total_ehp_pct']:+.2f}%) | Spirit Δ={sp_delta:+d}"
        )
        for attr in data.get("piece_attributions", []):
            console.print(
                f"    • {attr['label']}: DPS {attr['effective_dps_pct']:+.2f}%, "
                f"EHP {attr['total_ehp_pct']:+.2f}%"
            )
        for sc in data.get("scenario_matrix", []):
            console.print(
                f"    [{sc['scenario']}] DPS={sc['effective_dps']:,.1f} | "
                f"EHP={sc['total_ehp']:,.1f}"
            )

    _run_cli(json_output, _compute, _render)


@pob_app.command("import-local")
def pob_import_local_cmd(
    name_or_path: Annotated[
        str | None,
        typer.Argument(
            help="Build name substring or .xml file path (omit to load the newest PoB2 build)."
        ),
    ] = None,
    builds_dir: Annotated[
        Path | None,
        typer.Option("--dir", help="Override local Path of Building (PoE2)/Builds directory."),
    ] = None,
    slot: Annotated[str, typer.Option("--slot", help="Target PoB2 build slot.")] = "main",
    list_only: Annotated[
        bool,
        typer.Option("--list", help="List detected local PoB2 .xml builds without loading."),
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Discover or load saved local Path of Building (PoE2) .xml builds (zero-auth)."""

    def _compute() -> dict[str, Any]:
        return execute_pob_import_local(
            name_or_path=name_or_path,
            builds_dir=str(builds_dir) if builds_dir is not None else None,
            slot=slot,
            list_only=list_only,
        )

    def _render(data: dict[str, Any]) -> None:
        if list_only:
            table = Table(title="Local Path of Building (PoE2) Builds")
            table.add_column("Name", style="bold cyan")
            table.add_column("Class / Ascendancy")
            table.add_column("Lv", justify="right")
            table.add_column("Skill")
            table.add_column("Modified")
            for b in data.get("builds", []):
                asc = b.get("ascendancy_name") or b.get("class_name") or "?"
                table.add_row(
                    str(b["name"]),
                    str(asc),
                    str(b.get("level") or "-"),
                    str(b.get("main_skill") or "-"),
                    str(b["modified_at"])[:19],
                )
            console.print(table)
            return
        st = data["stats"]
        console.print(
            f"[bold green]Loaded local PoB2 build[/bold green] {data['build_name']} -> "
            f"[{data['slot']}] ({st.get('ascendancy_name') or st.get('class_name')} "
            f"Lv{st.get('level')}, DPS={st.get('effective_dps', 0):,.1f}, "
            f"EHP={st.get('total_ehp', 0):,.1f})"
        )

    _run_cli(json_output, _compute, _render)


@pob_app.command("import-character")
def pob_import_character_cmd(
    character_name: Annotated[
        str | None,
        typer.Argument(help="PoE2 character name to import (omit to list account characters)."),
    ] = None,
    realm: Annotated[str, typer.Option("--realm", help="GGG realm (default: poe2).")] = "poe2",
    slot: Annotated[str, typer.Option("--slot", help="Target PoB2 build slot.")] = "main",
    list_only: Annotated[
        bool,
        typer.Option("--list", help="List characters on your GGG OAuth account."),
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """List or import official PoE2 characters via GGG OAuth 2.1 PKCE (run 'poe2 auth login')."""

    def _compute() -> dict[str, Any]:
        return execute_pob_import_character(
            character_name=character_name,
            realm=realm,
            slot=slot,
            list_only=list_only,
        )

    def _render(data: dict[str, Any]) -> None:
        if "characters" in data:
            table = Table(title=f"GGG Account Characters ({data['realm']})")
            table.add_column("Name", style="bold cyan")
            table.add_column("Class")
            table.add_column("Lv", justify="right")
            table.add_column("League")
            for c in data["characters"]:
                table.add_row(c["name"], c["class_name"], str(c["level"]), c["league"])
            console.print(table)
            return
        st = data["stats"]
        console.print(
            f"[bold green]Imported GGG character[/bold green] {data['character_name']} -> "
            f"[{data['slot']}] ({st.get('ascendancy_name') or st.get('class_name')} "
            f"Lv{st.get('level')}, DPS={st.get('effective_dps', 0):,.1f}, "
            f"EHP={st.get('total_ehp', 0):,.1f})"
        )

    _run_cli(json_output, _compute, _render)


@auth_app.command("login")
def auth_login_cmd(
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait for browser OAuth callback.")
    ] = 120.0,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print authorize URL without opening a browser.")
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Authenticate with GGG's official OAuth 2.1 PKCE portal (no POESESSID)."""
    from poe2brain.pob.oauth import GggOAuthClient

    _run_cli(
        json_output,
        lambda: GggOAuthClient()
        .login(timeout=timeout, open_browser=not no_browser)
        .model_dump(mode="json"),
        lambda d: console.print(
            f"[bold green]GGG OAuth authenticated[/bold green] "
            f"(client={d['client_id']}, scopes={', '.join(d.get('scopes', []))})"
        ),
    )


@auth_app.command("status")
def auth_status_cmd(json_output: JsonOpt = False) -> None:
    """Show current GGG OAuth 2.1 login status and token expiry."""
    from poe2brain.pob.oauth import GggOAuthClient

    def _render(d: dict[str, Any]) -> None:
        state = (
            "[green]authenticated[/green]"
            if d["authenticated"]
            else "[yellow]not logged in[/yellow]"
        )
        console.print(f"GGG OAuth 2.1: {state} (client_id={d['client_id']})")
        if d.get("note"):
            console.print(f"  {d['note']}")

    _run_cli(
        json_output,
        lambda: GggOAuthClient().status().model_dump(mode="json"),
        _render,
    )


@auth_app.command("logout")
def auth_logout_cmd(json_output: JsonOpt = False) -> None:
    """Delete locally stored GGG OAuth 2.1 tokens."""
    from poe2brain.pob.oauth import GggOAuthClient

    _run_cli(
        json_output,
        lambda: GggOAuthClient().logout(),
        lambda d: console.print(f"Logged out (removed token file: {d['had_saved_token']})."),
    )


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
        if delete_slot:
            delete_pob_slot_snapshot(None, delete_slot)
            return {"action": "deleted", "slot": delete_slot}

        if (
            not create_slot
            and not (clone_from and clone_to)
            and not gems
            and custom_mods is None
            and not config_kv
        ):
            slots_dir = get_pob_slot_snapshot_path("main").parent
            saved = sorted(p.stem for p in slots_dir.glob("*.xml")) if slots_dir.is_dir() else []
            return {"persisted_slots": saved, "slots_dir": str(slots_dir)}

        svc = get_build_service()

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
            cfg_dict = parse_config_kv_pairs(config_kv)
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


@econ_app.command("arbitrage")
def econ_arbitrage_cmd(
    min_volume_divine: Annotated[
        float,
        typer.Option("--min-volume-divine", help="Minimum leg liquidity in Divines."),
    ] = 2.0,
    min_roi_pct: Annotated[
        float, typer.Option("--min-roi-pct", help="Minimum gross ROI percentage.")
    ] = 5.0,
    max_investment_divine: Annotated[
        float | None,
        typer.Option("--max-investment-divine", help="Maximum per-cycle capital in Divines."),
    ] = None,
    max_gold: Annotated[
        int | None,
        typer.Option("--max-gold", help="Maximum Alva Gold tax per cycle."),
    ] = None,
    ladder_start_exalted: Annotated[
        float | None,
        typer.Option(
            "--ladder-start-exalted",
            help="Build a multi-rung compounding ladder starting from this Exalted bankroll.",
        ),
    ] = None,
    gold_budget: Annotated[
        int | None,
        typer.Option("--gold-budget", help="Total Gold budget for compounding ladder mode."),
    ] = None,
    ladder_target_divine: Annotated[
        float | None,
        typer.Option("--ladder-target-divine", help="Target bankroll in Divines for ladder mode."),
    ] = None,
    live_books: Annotated[
        bool,
        typer.Option(
            "--live-books/--no-live-books",
            help="Probe live online /api/trade2/exchange order books and triangular cycles.",
        ),
    ] = True,
    encounter_gated: Annotated[
        bool,
        typer.Option(
            "--encounter-gated/--no-encounter-gated",
            help="Include encounter-gated Kalguuran Remnant recipes alongside hideout plays.",
        ),
    ] = True,
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="Maximum arbitrage opportunities to return.")
    ] = 25,
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    json_output: JsonOpt = False,
) -> None:
    """Scan PoE2 0.5.5 conversions/spreads or build a Gold-disciplined compounding ladder."""

    def _compute() -> dict[str, Any]:
        return execute_scan_arbitrage(
            min_volume_divine=min_volume_divine,
            min_roi_pct=min_roi_pct,
            max_investment_divine=max_investment_divine,
            max_gold_per_cycle=max_gold,
            starting_exalted=ladder_start_exalted,
            gold_budget=gold_budget,
            target_divine=ladder_target_divine,
            live_books=live_books,
            include_encounter_gated=encounter_gated,
            limit=limit,
            league=league,
        )

    def _render(data: dict[str, Any]) -> None:
        if "rungs" in data:
            table = Table(
                title=(
                    f"PoE2 Compounding Ladder — {data['league']} "
                    f"({data['starting_exalted']:.0f} Ex -> "
                    f"{data['final_bankroll_divine']:.2f} Div | "
                    f"Gold {data['total_gold_spent']:,}/{data['gold_budget']:,})"
                )
            )
            table.add_column("Rung", justify="right", style="cyan")
            table.add_column("Play", style="bold")
            table.add_column("Cycles", justify="right")
            table.add_column("End (Div)", justify="right", style="green")
            table.add_column("Gold Used", justify="right")
            for rg in data.get("rungs", []):
                table.add_row(
                    str(rg["rung_index"]),
                    str(rg["title"])[:60],
                    f"{rg['cycles_executed']}x",
                    f"{rg['ending_bankroll_divine']:.2f}",
                    f"{rg['gold_spent_on_rung']:,}",
                )
            console.print(table)
            return

        table = Table(
            title=(
                f"PoE2 Arbitrage Scanner — {data['league']} "
                f"(1 Div = {data['divine_exalted']:.1f} Ex, "
                f"1 Chaos = {data['chaos_exalted']:.2f} Ex)"
            )
        )
        table.add_column("Mechanism", style="cyan")
        table.add_column("Title", style="bold")
        table.add_column("Profit (Div)", justify="right", style="green")
        table.add_column("ROI %", justify="right")
        table.add_column("Ex / 10k Gold", justify="right")
        for opp in data.get("opportunities", [])[:15]:
            table.add_row(
                str(opp["mechanism"]),
                str(opp["title"])[:64],
                f"{opp['gross_profit_divine']:+.2f}",
                f"{opp['roi_pct']:+.1f}%",
                f"{opp['profit_ex_per_10k_gold']:,.1f}",
            )
        console.print(table)
        for trap in data.get("trap_warnings", [])[:5]:
            console.print(
                f"  [yellow]Trap ({trap['kind']} — {trap['item_or_pair']}):[/yellow] "
                f"{trap['explanation']}"
            )

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
    normalize_topoff: Annotated[
        bool,
        typer.Option(
            "--normalize-topoff/--no-normalize-topoff",
            help="Normalize uncrafted listings to +20% Quality & open Rune sockets.",
        ),
    ] = True,
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
            normalize_craft_topoff=normalize_topoff,
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
                topoff_s = ""
                if cand.get("top_off_applied"):
                    all_in = cand.get("effective_total_cost_exalted")
                    steps = ", ".join(cand["top_off_applied"])
                    topoff_s = f" | Top-off ({steps}) @ {all_in} Ex"
                console.print(
                    f"  #{cand['rank']} [{lst['sale_type']}] {item_lbl} — "
                    f"[bold green]{lst['price_exalted']} Ex[/bold green]{fee_s} | "
                    f"FullDPS {diff['full_dps_pct']:+.1f}% ({diff['full_dps_delta']:+,.1f}) | "
                    f"EHP {diff['total_ehp_pct']:+.1f}%{topoff_s}"
                )
        else:
            console.print(
                f"[bold cyan]Trade Candidates — {data.get('equipment_slot')}[/bold cyan] "
                f"({len(data.get('listings', []))} fetched, url={data.get('trade_url')})"
            )
            for lst in data.get("listings", [])[:10]:
                item_lbl = f"{lst.get('item_name') or ''} {lst['base_type']}".strip()
                console.print(f"  • [{lst['sale_type']}] {item_lbl} — {lst['price_exalted']} Ex")

    _run_cli(json_output, _compute, _render)


@trade_app.command("triage")
def trade_triage_cmd(
    positional: Annotated[
        str | None, typer.Argument(help="Optional path or multi-item Ctrl+C dump text.")
    ] = None,
    file_path: Annotated[
        Path | None, typer.Option("--file", "-f", help="Path to multi-item PoE2 Ctrl+C dump file.")
    ] = None,
    text: Annotated[
        str | None, typer.Option("--text", "-t", help="Multi-item PoE2 Ctrl+C clipboard dump text.")
    ] = None,
    min_exalted: Annotated[
        float,
        typer.Option("--min-exalted", help="Minimum Exalted threshold before salvage/vendor."),
    ] = 5.0,
    securable_only: Annotated[
        bool,
        typer.Option("--securable-only", help="Restrict trade2 queries to Instant Buyouts."),
    ] = False,
    live_trade: Annotated[
        bool,
        typer.Option(
            "--live-trade/--no-live-trade",
            help="Query live trade2 for non-currency equipment/gems/tablets.",
        ),
    ] = True,
    roll_tolerance: Annotated[
        float, typer.Option("--roll-tolerance", help="Minimum affix roll fraction (0.0..1.0).")
    ] = 0.9,
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    json_output: JsonOpt = False,
) -> None:
    """Batch price-check & classify a dump tab of mixed PoE2 Ctrl+C items into 4 action buckets."""

    def _compute() -> dict[str, Any]:
        raw_dump = _read_item_input(file_path=file_path, text=text, positional=positional)
        assert raw_dump is not None
        return execute_triage_dump(
            items=raw_dump,
            min_exalted=min_exalted,
            roll_tolerance=roll_tolerance,
            securable_only=securable_only,
            live_trade=live_trade,
            league=league,
        )

    def _render(data: dict[str, Any]) -> None:
        div_lbl = (
            f" (~{data['total_estimated_divine']:.2f} Div)"
            if data.get("total_estimated_divine") is not None
            else ""
        )
        table = Table(
            title=(
                f"Quad-Tab Dump Triage — {data['league']} "
                f"({data['total_items']} items | "
                f"Total Est: {data['total_estimated_exalted']:,.1f} Ex{div_lbl})"
            )
        )
        table.add_column("#", justify="right", style="dim")
        table.add_column("Bucket", style="cyan")
        table.add_column("Item", style="bold")
        table.add_column("Est (Ex)", justify="right", style="green")
        table.add_column("Next Step")
        for ent in data.get("entries", []):
            est_s = (
                f"{ent['estimated_total_exalted']:,.1f}"
                if ent.get("estimated_total_exalted") is not None
                else "-"
            )
            table.add_row(
                str(ent["index"] + 1),
                str(ent["bucket"]),
                str(ent["item_label"])[:46],
                est_s,
                str(ent["recommended_next_step"])[:72],
            )
        console.print(table)

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
    protect_existing: Annotated[
        bool,
        typer.Option(
            "--protect-existing/--no-protect-existing",
            help="Auto-protect existing high-tier mods on --item-text against collateral bricks.",
        ),
    ] = True,
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
            protect_existing_targets=protect_existing,
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


@craft_app.command("profit")
def craft_profit_cmd(
    budget: Annotated[
        str,
        typer.Option("--budget", help="Budget tier filter: all, low (<5d), mid (5-40d), or high."),
    ] = "all",
    max_divine: Annotated[
        float | None,
        typer.Option("--max-divine", help="Optional maximum expected craft cost in Divines."),
    ] = None,
    live_trade: Annotated[
        bool,
        typer.Option(
            "--live-trade/--no-live-trade",
            help="Query live trade2 Instant Buyout (securable) exit & base quotes.",
        ),
    ] = True,
    offline_prices: Annotated[
        bool,
        typer.Option("--offline-prices", help="Use offline Patch 0.5.5 reference quotes."),
    ] = False,
    max_trade_queries: Annotated[
        int, typer.Option("--max-trade-queries", help="Max paced live trade2 queries per scan.")
    ] = 8,
    sims: Annotated[
        int, typer.Option("--sims", help="Monte Carlo trial count for p50/p80/p95 drawdowns.")
    ] = 3000,
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    json_output: JsonOpt = False,
) -> None:
    """Scan & rank profitable PoE2 0.5.5 crafts with live input costs, exit prices, and recipes."""

    def _compute() -> dict[str, Any]:
        return execute_scan_craft_profit(
            budget=budget,
            max_budget_divine=max_divine,
            live_trade=live_trade,
            offline_prices=offline_prices,
            max_trade_queries=max_trade_queries,
            sims=sims,
            league=league,
        )

    def _render(data: dict[str, Any]) -> None:
        table = Table(
            title=(
                f"Profitable Crafts — {data['league']} "
                f"(1 Div = {data['divine_rate_ex']:.1f} Ex | "
                f"profitable={data['profitable_count']}/{data['total_recipes_evaluated']})"
            )
        )
        table.add_column("#", justify="right", style="dim")
        table.add_column("Tier", style="cyan")
        table.add_column("Craft", style="bold")
        table.add_column("Cost (Div)", justify="right")
        table.add_column("Exit (Div)", justify="right")
        table.add_column("Net (Div)", justify="right", style="green")
        table.add_column("ROI %", justify="right")
        for ev in data.get("evaluations", []):
            table.add_row(
                str(ev["rank"]),
                str(ev["budget_tier"]),
                str(ev["name"])[:54],
                f"{ev['expected_cost_divine']:.2f}",
                f"{ev['exit_price_divine']:.2f}",
                f"{ev['profit_divine']:+.2f}",
                f"{ev['expected_roi_pct']:+.1f}%",
            )
        console.print(table)

    _run_cli(json_output, _compute, _render)


# ---------------------------------------------------------------------------
# Pillar 5: PoE Ninja Builds (`poe2 ninja ...`)
# ---------------------------------------------------------------------------


@ninja_app.command("snapshots")
def ninja_snapshots_cmd(
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass snapshot telemetry cache.")
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Show live poe.ninja PoE2 build-index-state & snapshot-versions across SC/HC/SSF/HCSSF."""

    def _compute() -> dict[str, Any]:
        return execute_ninja_snapshots(league=league, force_refresh=refresh)

    def _render(data: dict[str, Any]) -> None:
        table = Table(
            title=f"poe.ninja PoE2 {data['patch']} Build Snapshots ({data['requested_league']})"
        )
        table.add_column("Mode", style="cyan")
        table.add_column("League", style="bold")
        table.add_column("Indexed", justify="right", style="green")
        table.add_column("Overview Slug")
        table.add_column("Tree")
        table.add_column("Time Machine")
        for mode_key, snap in (data.get("modes") or {}).items():
            tms = snap.get("time_machine_labels") or []
            tm_summary = f"{len(tms)} ({tms[0]}..{tms[-1]})" if len(tms) >= 2 else ", ".join(tms)
            table.add_row(
                mode_key,
                str(snap.get("league_name")),
                f"{snap.get('total_characters', 0):,}",
                str(snap.get("snapshot_name") or "-"),
                str(snap.get("passive_tree") or "-"),
                tm_summary or "-",
            )
        console.print(table)

    _run_cli(json_output, _compute, _render)


@ninja_app.command("search")
def ninja_search_cmd(
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    class_name: Annotated[
        str | None, typer.Option("--class", help="Base class or Ascendancy filter.")
    ] = None,
    ascendancy: Annotated[
        str | None, typer.Option("--ascendancy", help="Ascendancy name or alias filter.")
    ] = None,
    skill: Annotated[
        str | None, typer.Option("--skill", help="Main or active skill gem filter.")
    ] = None,
    support: Annotated[
        str | None, typer.Option("--support", help="Support or Lineage Support gem filter.")
    ] = None,
    unique: Annotated[
        str | None, typer.Option("--unique", help="Equipped Unique item filter.")
    ] = None,
    exclude_unique: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-unique",
            help="Exclude builds equipping these Uniques (CSV/repeatable, e.g. Temporalis).",
        ),
    ] = None,
    max_uniques: Annotated[
        int | None,
        typer.Option("--max-uniques", help="Maximum total equipped Uniques per build."),
    ] = None,
    keystone: Annotated[
        str | None, typer.Option("--keystone", help="Allocated Keystone or Ascendancy notable.")
    ] = None,
    weapon: Annotated[
        str | None, typer.Option("--weapon", help="Weapon/offhand type filter (e.g. Quarterstaff).")
    ] = None,
    min_dps: Annotated[
        float | None, typer.Option("--min-dps", help="Minimum Effective DPS floor.")
    ] = None,
    min_ehp: Annotated[
        float | None, typer.Option("--min-ehp", help="Minimum Total EHP floor.")
    ] = None,
    sort_by: Annotated[
        str, typer.Option("--sort", help="Sort metric: dps, ehp, level, es, or life.")
    ] = "dps",
    ssf_viable: Annotated[
        bool,
        typer.Option("--ssf-viable", help="Prioritize SSF-achievable low-unique builds first."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max build rows to return.")] = 15,
    refresh_live: Annotated[
        int,
        typer.Option(
            "--refresh-live",
            help="Live-refresh top N matched characters via poe.ninja PoB endpoint.",
        ),
    ] = 0,
    include_pob_code: Annotated[
        bool, typer.Option("--include-pob-code", help="Include full eN... PoB2 code per build row.")
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Search PoE2 Forbidden Rites poe.ninja builds across class/ascendancy/skill/unique/SSF."""

    def _compute() -> dict[str, Any]:
        return execute_ninja_search(
            league=league,
            class_name=class_name,
            ascendancy=ascendancy,
            skill=skill,
            support_gem=support,
            unique_item=unique,
            exclude_uniques=exclude_unique,
            max_uniques=max_uniques,
            keystone=keystone,
            weapon_type=weapon,
            min_dps=min_dps,
            min_ehp=min_ehp,
            sort_by=sort_by,
            ssf_viable_first=ssf_viable,
            limit=limit,
            refresh_live_top=refresh_live,
            include_pob_code=include_pob_code,
        )

    def _render(data: dict[str, Any]) -> None:
        table = Table(
            title=(
                f"poe.ninja Builds — {data['league']} "
                f"(matched {data['matched_builds']}/{data['corpus_total_builds']} | "
                f"sort={data['sort_by']})"
            )
        )
        table.add_column("Character", style="bold cyan")
        table.add_column("Ascendancy")
        table.add_column("Lv", justify="right")
        table.add_column("Main Skill")
        table.add_column("DPS", justify="right", style="green")
        table.add_column("EHP", justify="right")
        table.add_column("ES / Life", justify="right")
        for b in data.get("builds", []):
            table.add_row(
                f"{b['character']} ({b['account']})",
                str(b.get("ascendancy_name") or b["class_name"]),
                str(b["level"]),
                str(b.get("main_skill") or "-"),
                f"{b['effective_dps']:,.0f}",
                f"{b['total_ehp']:,.0f}",
                f"{b['energy_shield']:,.0f} / {b['life']:,.0f}",
            )
        console.print(table)

    _run_cli(json_output, _compute, _render)


@ninja_app.command("character")
def ninja_character_cmd(
    account_or_url: Annotated[
        str,
        typer.Argument(
            help="poe.ninja character URL, PoB share URL (ninja/poe2db/maxroll/pobb.in), or acct."
        ),
    ],
    name: Annotated[
        str | None,
        typer.Option("--name", "--character", help="Character name when passing account tag."),
    ] = None,
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    timemachine: Annotated[
        str | None,
        typer.Option("--timemachine", help="Optional time-machine label (e.g. week-3, day-5)."),
    ] = None,
    probe_timeline: Annotated[
        bool,
        typer.Option(
            "--probe-timeline",
            help="Binary-search available weekly time-machine snapshots for this character.",
        ),
    ] = False,
    load_pob: Annotated[
        bool,
        typer.Option("--load-pob", help="Load imported character into persistent PoB2 --slot."),
    ] = False,
    slot: Annotated[str, typer.Option("--slot", help="Target PoB2 build slot name.")] = "main",
    audit: Annotated[
        bool,
        typer.Option("--audit", help="Run headless PoB2 dewatering audit on the character."),
    ] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass 30-minute character PoB cache.")
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Import a live poe.ninja character, probe time-machine timeline, or load/audit in PoB2."""

    def _compute() -> dict[str, Any]:
        return execute_ninja_character(
            account_or_url=account_or_url,
            character=name,
            league=league,
            timemachine=timemachine,
            probe_timeline=probe_timeline,
            load_into_pob=load_pob,
            slot=slot,
            audit=audit,
            force_refresh=refresh,
        )

    def _render(data: dict[str, Any]) -> None:
        if "probed_points" in data:
            console.print(
                f"[bold cyan]poe.ninja Character Timeline[/bold cyan] — "
                f"{data['character']} ({data['account']}) | "
                f"earliest={data.get('earliest_available_label')} | "
                f"latest={data.get('latest_available_label')}"
            )
            for pt in data.get("probed_points", []):
                asc_or_cls = pt.get("ascendancy_name") or pt.get("class_name")
                status_s = (
                    f"[green]Lv{pt.get('level')} {asc_or_cls}[/green]"
                    if pt["available"]
                    else f"[dim]absent ({pt.get('http_status')})[/dim]"
                )
                console.print(f"  • {pt['label']:<10}: {status_s}")
            return
        cls_lbl = f"{data['class_name']}/{data.get('ascendancy_name') or 'None'}"
        ident = (
            f"{data['character']} ({data['account']})"
            if data.get("character")
            else data["requested_source"]
        )
        console.print(
            f"[bold cyan]{ident}[/bold cyan] — Lv{data['level']} {cls_lbl} "
            f"(Main: {data.get('main_skill') or 'None'} | src: {data['source_kind']})"
        )
        if data.get("unique_items"):
            console.print(f"  Uniques  : {', '.join(data['unique_items'][:8])}")
        if data.get("keystones"):
            console.print(f"  Keystones: {', '.join(data['keystones'][:8])}")
        if data.get("pob_stats"):
            _render_build_stats(
                {**data["pob_stats"], "slot": data.get("pob_slot") or slot},
                "Headless PoB2 Stats",
            )
        if data.get("dewatered_audit"):
            aud = data["dewatered_audit"]
            console.print(
                f"  Audit    : DPS inflation +{aud['dps_inflation_pct']:.1f}% | "
                f"EHP inflation +{aud['ehp_inflation_pct']:.1f}%"
            )

    _run_cli(json_output, _compute, _render)


# ---------------------------------------------------------------------------
# Pillar 6: Loot Filter, Visual Theme & Sound Studio (`poe2 filter ...`)
# ---------------------------------------------------------------------------


def _render_filter_compile_summary(data: dict[str, Any], label: str) -> None:
    val = data["validation"]
    console.print(
        f"[bold cyan]{label}[/bold cyan] — [bold]{data['filter_name']}[/bold] "
        f"(strictness={data['strictness']} | theme={data['theme_id']} | "
        f"sound={data.get('sound_pack_id') or 'builtin_poe2'})"
    )
    console.print(
        f"  Validation: valid=[bold green]{val['valid']}[/bold green] "
        f"({val['total_blocks']} blocks, {val['error_count']} errors, "
        f"{val['warning_count']} warnings)"
    )
    if data.get("healed_basetypes"):
        console.print(f"  Healed    : {', '.join(data['healed_basetypes'])}")
    if data.get("filter_path"):
        console.print(f"  Filter    : {data['filter_path']}")
    if data.get("sound_pack"):
        console.print(f"  Soundpack : {data['sound_pack']['pack_dir']}")
    if data.get("preview_html_path"):
        console.print(f"  Preview   : {data['preview_html_path']}")
    if data.get("installed_dir"):
        console.print(f"  Installed : {data['installed_dir']}")


@filter_app.command("presets")
def filter_presets_cmd(json_output: JsonOpt = False) -> None:
    """List PoE2 0.5.5 visual theme presets, strictness tiers, soundpack styles, and filter dir."""

    def _render(data: dict[str, Any]) -> None:
        console.print(
            f"[bold cyan]PoE2 {data['patch']} Filter Studio Presets[/bold cyan] "
            f"(PoE2 folder: {data['detected_poe2_filter_dir']})"
        )
        t_table = Table(title=f"Visual Themes ({data['theme_count']})")
        t_table.add_column("ID", style="cyan")
        t_table.add_column("Name", style="bold")
        t_table.add_column("S+ Contrast", justify="right")
        t_table.add_column("Description")
        for t in data["visual_themes"]:
            t_table.add_row(
                t["id"],
                t["name"],
                f"{t['s_plus_style']['contrast_ratio']}:1",
                t["description"][:78],
            )
        console.print(t_table)

        s_table = Table(title=f"Soundpack Styles ({data['sound_style_count']})")
        s_table.add_column("Style", style="magenta")
        s_table.add_column("Description")
        for s in data["sound_styles"]:
            s_table.add_row(s["id"], s["description"])
        console.print(s_table)

    _run_cli(json_output, execute_filter_presets, _render)


@filter_app.command("generate")
def filter_generate_cmd(
    strictness: Annotated[
        str,
        typer.Option(
            "--strictness",
            help="Strictness: soft, regular, semi_strict, strict, very_strict, uber_strict.",
        ),
    ] = "semi_strict",
    theme: Annotated[
        str | None,
        typer.Option(
            "--theme",
            help="Visual theme preset ID (defaults to forbidden_abyss unless --look is set).",
        ),
    ] = None,
    look: Annotated[
        str | None,
        typer.Option("--look", help="Natural-language visual theme customization prompt."),
    ] = None,
    cvd_safe: Annotated[
        str | None,
        typer.Option(
            "--cvd-safe",
            help="Colorblind-safe mode: deuteranopia, protanopia, tritanopia, or true.",
        ),
    ] = None,
    sound_style: Annotated[
        str | None,
        typer.Option(
            "--sound-style",
            help=(
                "Sound pack style: celestial_chime, ritual_gong, cyber_synth, arcade_retro, "
                "zen_minimal, voice_callout, or none."
            ),
        ),
    ] = None,
    musical_key: Annotated[
        str, typer.Option("--musical-key", help="Musical root key for synth pack (e.g. D, F#, A).")
    ] = "D",
    scale: Annotated[
        str,
        typer.Option(
            "--scale", help="Musical scale: lydian, major, minor, pentatonic, phrygian, dorian."
        ),
    ] = "lydian",
    warm_night: Annotated[
        bool,
        typer.Option(
            "--warm-night",
            help="2 AM tinnitus-safe psychoacoustic low-pass mode (<1.5% energy >2.5kHz).",
        ),
    ] = False,
    lowpass_hz: Annotated[
        float | None,
        typer.Option("--lowpass-hz", help="Custom Butterworth low-pass cutoff frequency in Hz."),
    ] = None,
    callout: Annotated[
        list[str] | None,
        typer.Option(
            "--callout",
            help="Custom spoken tier callout as tier=text (repeatable, e.g. s_plus='Divine!').",
        ),
    ] = None,
    pob: Annotated[
        str | None,
        typer.Option("--pob", help="Optional PoB2 share code, URL, or XML for build tailoring."),
    ] = None,
    slot: Annotated[
        str | None,
        typer.Option("--slot", help="Optional persisted PoB2 slot name (e.g. main) to tailor to."),
    ] = None,
    slots: Annotated[
        str | None,
        typer.Option("--slots", help="Comma-separated persisted PoB2 slots for co-op party union."),
    ] = None,
    out_path: Annotated[
        Path | None, typer.Option("--out", "-o", help="Output .filter file path.")
    ] = None,
    sound_dir: Annotated[
        Path | None, typer.Option("--sound-dir", help="Output root directory for custom soundpack.")
    ] = None,
    preview_html: Annotated[
        Path | None,
        typer.Option("--preview-html", help="Output path for interactive HTML drop/audio preview."),
    ] = None,
    install: Annotated[
        bool, typer.Option("--install", help="Install .filter and soundpack into PoE2 folder.")
    ] = False,
    install_dir: Annotated[
        Path | None,
        typer.Option("--install-dir", help="Override target PoE2 folder when using --install."),
    ] = None,
    filter_name: Annotated[
        str | None, typer.Option("--name", help="Optional filter display/file stem.")
    ] = None,
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use offline Patch 0.5.5 currency/unique baselines.")
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Compile a validated PoE2 0.5.5 .filter, CVD/custom look, .wav/.mp3 soundpack, & preview."""

    def _compute() -> dict[str, Any]:
        return execute_filter_generate(
            strictness=strictness,
            theme=theme,
            look=look,
            cvd_safe=cvd_safe,
            sound_style=sound_style,
            musical_key=musical_key,
            scale=scale,
            warm_night_mode=warm_night,
            lowpass_hz=lowpass_hz,
            custom_callouts=callout,
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

    _run_cli(
        json_output,
        _compute,
        lambda d: _render_filter_compile_summary(d, "Compiled PoE2 Filter"),
    )


@filter_app.command("restyle")
def filter_restyle_cmd(
    source: Annotated[
        str,
        typer.Argument(
            help="Local .filter file path or NeverSink strictness (e.g. semi_strict, strict)."
        ),
    ],
    theme: Annotated[
        str | None,
        typer.Option(
            "--theme",
            help="Visual theme preset ID, or 'none' (defaults to forbidden_abyss unless --look).",
        ),
    ] = None,
    look: Annotated[
        str | None,
        typer.Option("--look", help="Natural-language visual theme customization prompt."),
    ] = None,
    cvd_safe: Annotated[
        str | None,
        typer.Option(
            "--cvd-safe",
            help="Colorblind-safe mode: deuteranopia, protanopia, tritanopia, or true.",
        ),
    ] = None,
    sound_style: Annotated[
        str | None,
        typer.Option("--sound-style", help="Sound pack style (or 'none')."),
    ] = None,
    musical_key: Annotated[
        str, typer.Option("--musical-key", help="Musical root key for synth pack.")
    ] = "D",
    scale: Annotated[str, typer.Option("--scale", help="Musical scale for synth pack.")] = "lydian",
    warm_night: Annotated[
        bool,
        typer.Option(
            "--warm-night",
            help="2 AM tinnitus-safe psychoacoustic low-pass mode (<1.5% energy >2.5kHz).",
        ),
    ] = False,
    lowpass_hz: Annotated[
        float | None,
        typer.Option("--lowpass-hz", help="Custom Butterworth low-pass cutoff frequency in Hz."),
    ] = None,
    callout: Annotated[
        list[str] | None,
        typer.Option(
            "--callout",
            help="Custom spoken tier callout as tier=text (repeatable, e.g. s_plus='Divine!').",
        ),
    ] = None,
    pob: Annotated[
        str | None,
        typer.Option("--pob", help="Optional PoB2 code/URL/file for build override injection."),
    ] = None,
    slot: Annotated[
        str | None,
        typer.Option("--slot", help="Optional persisted PoB2 slot for build override injection."),
    ] = None,
    slots: Annotated[
        str | None,
        typer.Option("--slots", help="Comma-separated persisted PoB2 slots for co-op party union."),
    ] = None,
    out_path: Annotated[
        Path | None, typer.Option("--out", "-o", help="Output .filter file path.")
    ] = None,
    sound_dir: Annotated[
        Path | None, typer.Option("--sound-dir", help="Output root directory for custom soundpack.")
    ] = None,
    preview_html: Annotated[
        Path | None,
        typer.Option("--preview-html", help="Output path for interactive HTML preview."),
    ] = None,
    install: Annotated[
        bool, typer.Option("--install", help="Install healed/restyled bundle into PoE2 folder.")
    ] = False,
    install_dir: Annotated[
        Path | None,
        typer.Option("--install-dir", help="Override target PoE2 folder when using --install."),
    ] = None,
    filter_name: Annotated[
        str | None, typer.Option("--name", help="Optional filter display/file stem.")
    ] = None,
    league: Annotated[str, typer.Option("--league", help="PoE2 league name.")] = DEFAULT_LEAGUE,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use offline Patch 0.5.5 economy baselines.")
    ] = False,
    json_output: JsonOpt = False,
) -> None:
    """Auto-heal 0.5.5 stale BaseTypes, inject economy/co-op PoB2 overrides, & restyle a .filter."""

    def _compute() -> dict[str, Any]:
        return execute_filter_restyle(
            source=source,
            theme=theme,
            look=look,
            cvd_safe=cvd_safe,
            sound_style=sound_style,
            musical_key=musical_key,
            scale=scale,
            warm_night_mode=warm_night,
            lowpass_hz=lowpass_hz,
            custom_callouts=callout,
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

    _run_cli(
        json_output,
        _compute,
        lambda d: _render_filter_compile_summary(d, "Healed & Restyled PoE2 Filter"),
    )


@filter_app.command("validate")
def filter_validate_cmd(
    filter_file_or_text: Annotated[
        str, typer.Argument(help="Path to .filter file or inline .filter text.")
    ],
    sound_dir: Annotated[
        Path | None,
        typer.Option("--sound-dir", help="Optional directory to verify CustomAlertSound files."),
    ] = None,
    json_output: JsonOpt = False,
) -> None:
    """Validate a PoE2 0.5.5 .filter file or text against live BaseTypes, Classes, and grammar."""

    def _compute() -> dict[str, Any]:
        return execute_filter_validate(
            filter_file_or_text=filter_file_or_text,
            sound_dir=sound_dir,
        )

    def _render(data: dict[str, Any]) -> None:
        badge = (
            "[bold green]VALID[/bold green]" if data["valid"] else "[bold red]INVALID[/bold red]"
        )
        console.print(
            f"PoE2 {data['patch']} Filter Validation: {badge} "
            f"({data['total_blocks']} blocks | {data['error_count']} errors | "
            f"{data['warning_count']} warnings)"
        )
        for diag in data.get("diagnostics", [])[:15]:
            col = "red" if diag["severity"] == "error" else "yellow"
            console.print(
                f"  [{col}][{diag['severity'].upper()} L{diag['line']} {diag['code']}][/{col}] "
                f"{diag['message']}"
            )

    _run_cli(json_output, _compute, _render)


@filter_app.command("heal")
def filter_heal_cmd(
    filter_file_or_text: Annotated[
        str, typer.Argument(help="Path to broken PoE1/0.1 .filter file or inline .filter text.")
    ],
    out_path: Annotated[
        Path | None, typer.Option("--out", "-o", help="Optional output path for healed .filter.")
    ] = None,
    sound_dir: Annotated[
        Path | None,
        typer.Option("--sound-dir", help="Optional directory to verify CustomAlertSound files."),
    ] = None,
    json_output: JsonOpt = False,
) -> None:
    """Filter Doctor: auto-heal broken PoE1 / PoE2 0.1 .filter syntax, classes, & BaseTypes."""

    def _compute() -> dict[str, Any]:
        return execute_filter_heal(
            filter_file_or_text=filter_file_or_text,
            out_path=out_path,
            sound_dir=sound_dir,
        )

    def _render(data: dict[str, Any]) -> None:
        val = data["validation"]
        badge = (
            "[bold green]HEALED (0 ERRORS)[/bold green]"
            if data["valid"]
            else "[bold red]NEEDS REVIEW[/bold red]"
        )
        console.print(
            f"PoE2 {data['patch']} Filter Doctor: {badge} "
            f"(errors {data['original_error_count']} -> {val['error_count']} | "
            f"fixes={len(data.get('fixes_applied') or [])} | pruned={data['pruned_blocks']})"
        )
        for fx in (data.get("fixes_applied") or [])[:12]:
            console.print(f"  • {fx}")
        if data.get("out_path"):
            console.print(f"  Wrote healed filter -> {data['out_path']}")

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


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    run_with_service_cleanup(app)


if __name__ == "__main__":
    main()
