# poe2brain

**`poe2brain`** is an agent-native Path of Exile 2 (`Patch 0.5.5` / `Forbidden Rites`) intelligence engine. It exposes a single typed Python 3.12+ core simultaneously as a JSON-first CLI (`poe2`) and a Model Context Protocol stdio server (`poe2brain-mcp`), giving AI coding/gaming agents and power users ground-truth mechanics, real Path of Building 2 calculations, live market/trade pricing, and exact expected-cost crafting plans.

---

## Architecture & Four Pillars

```text
                       ┌──────────────────────────────────────┐
                       │      Agent Skills & MCP Configs      │
                       │ .agents/skills/poe2brain/SKILL.md    │
                       │ plugin.json | .mcp.json | .devin/    │
                       └──────────┬───────────────────┬───────┘
                                  │                   │
                     CLI (--json) ▼                   ▼ MCP (stdio)
                       ┌──────────────────┐ ┌──────────────────┐
                       │  poe2 (Typer)    │ │  poe2brain-mcp   │
                       └────────┬─────────┘ └─────────┬────────┘
                                └──────────┬──────────┘
                                           ▼
        ┌──────────────────┬───────────────┴───────────────┬──────────────────┐
        ▼                  ▼                               ▼                  ▼
┌───────────────┐  ┌───────────────┐               ┌───────────────┐  ┌───────────────┐
│ 1. Knowledge  │  │ 2. Headless   │               │ 3. Economy &  │  │ 4. Crafting   │
│ SQLite + FTS5 │  │    PoB2       │               │    Trade2     │  │    Solver     │
│ 0.5.5 Rules   │  │ LuaJIT Daemon │◄─────────────►│ Scout + Ninja │─►│ Pool + Policy │
│ & Gamedata    │  │ & Dewatering  │  Item/Weight  │ + Official    │  │ + Monte Carlo │
└───────────────┘  └───────────────┘   Closed Loop └───────────────┘  └───────────────┘
```

1. **Knowledge & Anti-Hallucination (`poe2brain.knowledge`)** — Read-only SQLite + FTS5 index (`data/poe2_0.5.5.sqlite`) over **769** official GGG `KeywordPopups`, **49** Patch `0.5.5` anti-hallucination rules & formulas, **1,711** gems, **435** uniques, **3,219** live PoE2 bases (catching PoE1 / stale `0.1` `Expert ` base traps), **313** socketable augments, **293** crafting levers, and **882** Verisium/Kalguuran runeforge recipes.
2. **Headless Path of Building 2 (`poe2brain.pob`)** — Persistent native `LuaJIT` + `PathOfBuilding-PoE2` (`tree 0_5`) JSON-RPC 2.0 daemon supporting multi-slot builds (`main`, `ref`, `candidate` persisted across CLI calls), `eN...` / `pobb.in` / XML / GGG `.build` JSON I/O, **PoB-warrior dewatering audits**, `<10ms` `GetMiscCalculator()` item upgrade sweeps, passive/anoint ranking, and marginal `trade2` stat-weight generation.
3. **Live Economy & Official Trade2 (`poe2brain.trade`)** — 3-tier pricing router normalized to **`Exalted Orb = 1.0`** combining `api.poe2scout.com` + `poe.ninja` `/poe2/api/economy/exchange/current/overview` (15m TTL disk cache, liquidity & 7d trends) with unauthenticated `pathofexile.com/api/trade2` (`GGGRateLimiter` + `1.0s` buffer), `Exiled-Exchange-2`-backed `Ctrl+C` / `Ctrl+Alt+C` item parsing, **Instant Buyout (`securable` Gold `fee`) vs In-Person (`whisper`)** separation, and price-fixer account deduplication.
4. **Exact Crafting Odds & Cost Planner (`poe2brain.crafting`)** — Conditional `ModPoolEngine` (`display_tier = 1` best, tiered orb `min_mod_level` floors `{1, 35, 50, 55, 70}`, Ancient Bone `40` floor, family mutual exclusion, dynamic elemental tag-blocking, 3-draw without-replacement Well of Souls unveil, `Omen of Whittling`, `Sinistral/Dextral Crystallisation`, blocking removed `0.4` Homogenising Omens & `0.5` Recombinators) + state-conditional policy & Monte Carlo (`p50`/`p80`/`p95`) `CraftingPlanner` with live buy-vs-craft verdicts.

---

## Quickstart

```bash
# 1. Install Python >= 3.12 environment & console entrypoints (poe2, poe2brain-mcp)
uv sync

# 2. Bootstrap native LuaJIT + PathOfBuilding-PoE2 into ~/.cache/poe2brain/pob2 (<2s if cached)
./scripts/bootstrap_pob2.sh

# 3. Verify engine, SQLite gamedata DB, and PoB2 runtime readiness
uv run poe2 version --json
```

---

## Multi-Harness Agent Setup

`poe2brain` ships ready-to-use manifests across major agent harnesses:

- **Antigravity**: Self-contained repo plugin manifest in [plugin.json](plugin.json), workspace MCP config in [.agents/mcp_config.json](.agents/mcp_config.json), and progressive-disclosure skill in [.agents/skills/poe2brain/SKILL.md](.agents/skills/poe2brain/SKILL.md).
- **Claude Code / Cursor**: Workspace stdio MCP config in [.mcp.json](.mcp.json).
- **Devin**: Workspace MCP config in [.devin/mcp_config.json](.devin/mcp_config.json).
- **Shell-Capable Coding Agents (Codex, CLI workflows)**: Invoke `uv --directory /Users/lfan/Project/poe2brain run poe2 ... --json` directly.

To run the stdio MCP server manually:

```bash
uv run poe2brain-mcp
```

---

## 1:1 MCP Tool & CLI Command Reference

Every MCP tool maps 1:1 to a `poe2 ... --json` CLI command over the exact same domain services:

| Pillar | MCP Tool (`poe2brain`) | CLI Command (`uv run ...`) | Purpose |
| :--- | :--- | :--- | :--- |
| **Health** | `poe2_status` | `poe2 version --json` | Verify patch (`0.5.5`), default league (`Forbidden Rites`), SQLite DB, and headless PoB2 readiness. |
| **Knowledge** | `poe2_search_knowledge` | `poe2 wiki search "<query>" [--category <cat>] [--limit 12] --json` | FTS5 + exact search across 769 GGG `KeywordPopups`, 49 anti-hallucination rules, 1,711 gems, 435 uniques, 3,219 live bases, 313 augments, 293 levers, and 882 runeforge recipes. |
| **Knowledge** | `poe2_explain_mechanic` | `poe2 wiki mechanic "<topic>" --json` | Authoritative `MechanicFact` (`poe1_trap`, `poe2_live_rule`, `formula_expr`) + official GGG keyword popups. |
| **Knowledge** | `poe2_inspect_gem` | `poe2 wiki gem "<name>" --json` | Active, Spirit, Meta, Tiered Support (`I`/`II`/`III`), and `lineage_support` (`max_copies_per_char=1`) gem inspection. |
| **Knowledge** | `poe2_inspect_base` | `poe2 wiki base "<name>" [--ilvl 82] --json` | Base properties, implicits, `display_tier=1` affixes at `ilvl`, and PoE1 / stale `Expert ` trap warnings. |
| **Knowledge** | `poe2_inspect_unique` | `poe2 wiki unique "<name>" --json` | Unique base type, implicits, explicits, and `is_chanceable` status. |
| **Knowledge** | `poe2_inspect_augment` | `poe2 wiki augment "<query>" --json` | Runes, Soul Cores, Azmeri Idols, Shaman `Bonded:` bonuses, and Verisium / Kalguuran Remnant recipes. |
| **PoB2** | `poe2_pob_load` | `poe2 pob load <code_url_or_file> [--slot main] --json` | Load `eN...` share code, `pobb.in/<id>` URL, PoB XML, or GGG `.build` JSON into a persistent named slot. |
| **PoB2** | `poe2_pob_stats` | `poe2 pob stats [--slot main] --json` | Extract `FullDPS`, `TotalEHP`, `Life`, `ES`, `Mana`, `Spirit`, resists, `Block`, `Deflection`, `Evasion`, `Armour`. |
| **PoB2** | `poe2_pob_audit` | `poe2 pob audit [<code_url_or_file>] [--slot main] --json` | **Dewatering audit**: strip PoB-warrior config inflation and return sustainable Pinnacle Boss stats + culprits. |
| **PoB2** | `poe2_pob_eval_item` | `poe2 pob eval-item --equip-slot "<slot>" --item-text "<text>" [--slot main] --json` | Non-destructive `GetMiscCalculator()` item swap returning `ItemUpgradeDiff` (`full_dps_delta`, `total_ehp_delta`, `spirit_unreserved_delta`). |
| **PoB2** | `poe2_pob_stat_weights` | `poe2 pob weights --equip-slot "<slot>" [--objective balanced\|dps\|ehp] [--slot main] --json` | Compute marginal PoB2 stat weights (`SlotStatWeights`) and unauthenticated-safe `trade2` filter JSON. |
| **PoB2** | `poe2_pob_suggest_nodes` | `poe2 pob nodes [--mode perPoint\|total\|anoint] [--slot main] [--top 10] --json` | Rank reachable passive notables (`perPoint`/`total`) or full-tree Distilled Emotion Amulet anoints (`anoint`). |
| **Economy** | `poe2_currency_price` | `poe2 econ price "<query>" [--league "Forbidden Rites"] --json` | Live `CurrencyQuote` normalized to `{exalted, chaos, divine}` (`Exalted = 1.0`) with 7d trend and volume. |
| **Economy** | `poe2_market_scan` | `poe2 econ scan [--category <cat>] [--min-volume-divine 5.0] [--league "Forbidden Rites"] --json` | High-liquidity exchange movers, cross-source spreads, and chanceable Uniques. |
| **Trade** | `poe2_parse_item` | `poe2 trade parse [--file <path> \| --text "<ctrl_c>"] --json` | Parse PoE2 `Ctrl+C` / `Ctrl+Alt+C` text into `ParsedItem` and PoB2 `Item.lua` text. |
| **Trade** | `poe2_price_check` | `poe2 trade check [--file <path> \| --text "<ctrl_c>"] [--securable-only] [--league "Forbidden Rites"] --json` | 3-tier price check splitting **Instant Buyout (`securable`, Gold `fee`)** vs **In-Person (`whisper`)** with fixer warnings. |
| **Trade** | `poe2_find_upgrades` | `poe2 trade upgrades --equip-slot "<slot>" [--max-exalted 50] [--min-spirit 0] [--securable-only] [--slot main] --json` | Query live `trade2` for slot upgrades, convert fetched items to PoB2 text, and rank via `GetMiscCalculator()`. |
| **Crafting** | `poe2_craft_odds` | `poe2 craft odds --base "<base>" --ilvl 82 --lever "<lever>" [--target "<mod>"] [--omen "<omen>"] --json` | Exact conditional mod-pool hit odds (`CraftActionEval`) with live currency pricing. |
| **Crafting** | `poe2_craft_plan` | `poe2 craft plan --base "<base>" --ilvl 82 --target "<mod>"... [--buy-exalted <p>] [--sims 3000] --json` | Optimal state-conditional policy + Monte Carlo cost (`expected_cost_exalted`, `p50`/`p80`/`p95`) + `trade_vs_craft`. |

### Additional CLI Utility Subcommands

- `poe2 wiki runeforge "<query>" [--limit 12] --json` — Search Verisium Anvil base transforms and Kalguuran Remnant rune recipes directly.
- `poe2 pob export [--format code|xml|ggg_json] [--out <path>] [--slot main] --json` — Export a persisted build slot to share code, XML, or official GGG `.build` JSON.
- `poe2 pob equip --equip-slot "<slot>" [--item-text "<text>" | --file <path>] [--slot main] --json` — Persistently equip an item into a saved PoB2 slot.
- `poe2 pob slots [--create <slot> --class <cls> --ascendancy <asc>] [--clone-from <a> --clone-to <b>] [--delete <slot>] [--gems "<links>"] [--config key=val] [--custom-mods "<mods>"] --json` — Manage and mutate persisted CLI PoB2 slots (`~/.cache/poe2brain/pob_slots`).
- `poe2 econ rates [--league "Forbidden Rites"] --json` — Inspect live `Exalted : Chaos : Divine` reference rates and active PoE2 leagues.
- `poe2 sync gamedata [--out <path>] --json` — Deterministically rebuild `data/poe2_0.5.5.sqlite` from MIT upstream sources and bundled weights.
- `poe2 bootstrap pob2 [--force] --json` — Seed or compile the local `LuaJIT` + `PathOfBuilding-PoE2` runtime.

---

## Development & Verification

```bash
# Rebuild deterministic Patch 0.5.5 SQLite + FTS5 database
uv run python scripts/sync_gamedata.py

# Lint & run unit + integration test suites
uv run ruff check .
uv run pytest -v

# Include live Forbidden Rites network round-trip tests
POE2_LIVE_TESTS=1 uv run pytest -v
```

---

## Clean-Room Licensing & GGG ToS Boundary

- **MIT Licensed**: See [LICENSE](LICENSE). Bundled lookup data in `data/` derives strictly from MIT-licensed upstream projects (`PathOfBuildingCommunity/PathOfBuilding-PoE2`, `Kvan7/Exiled-Exchange-2` in [data/ee2/LICENSE.Exiled-Exchange-2](data/ee2/LICENSE.Exiled-Exchange-2), and `repoe-fork/poe2`).
- **Strictly Out-of-Game & Unauthenticated**: `poe2brain` never reads `POESESSID` or auth cookies, never inspects game memory or screen frames, never injects inputs into the Path of Exile 2 client, and paces all public `trade2` requests through `GGGRateLimiter`.
