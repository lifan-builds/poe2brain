# poe2brain

> **中文文档 / Bilibili 用户快速上手：** 请见 [README_ZH.md](README_ZH.md)（支持 Windows 零编译一键安装、中文词缀/升华搜索、本地 PoB2 导入与官方 GGG OAuth 2.1）

**`poe2brain`** is an agent-native Path of Exile 2 (`Patch 0.5.5` / `Forbidden Rites`) intelligence engine for **Windows, macOS, and Linux**. It exposes a single typed Python 3.12+ core simultaneously as a JSON-first CLI (`poe2`) and a Model Context Protocol stdio server (`poe2brain-mcp`, **42** tools in 1:1 parity), giving AI coding/gaming agents and players ground-truth mechanics (English + `zh-CN`/`zh-TW`), `<=50`-char Waystone/Tablet stash regexes, `poe.ninja` + local PoB2 + official GGG OAuth 2.1 character import, real Path of Building 2 calculations & HC rip audits, live market/trade pricing & dump triage, exact expected-cost crafting plans, and validated PoE2 loot filters with CVD/co-op themes, 2 AM tinnitus-safe audio soundpacks, and legacy Filter Doctor auto-healing.

---

## Architecture & Six Pillars

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
  ┌─────────────┬─────────────┬────────────┴────────────┬─────────────┬─────────────┐
  ▼             ▼             ▼                         ▼             ▼             ▼
┌───────────┐ ┌───────────┐ ┌───────────┐             ┌───────────┐ ┌───────────┐ ┌───────────┐
│1.Knowledge│ │2.Headless │ │3.Economy &│             │4.Crafting │ │5.poe.ninja│ │6.Loot     │
│SQLite+FTS5│ │  PoB2     │ │  Trade2   │◄───────────►│  Solver   │ │  Builds & │ │  Filter & │
│0.5.5 Rules│ │LuaJIT RPC │ │Scout+Ninja│             │Pool+MDP+MC│ │  Char PoB │ │  Sound    │
│& Meta 23  │ │& Dewater  │ │+ Official │             │+ Profit   │ │  Importer │ │  Studio   │
└───────────┘ └───────────┘ └───────────┘             └───────────┘ └───────────┘ └───────────┘
```

1. **Knowledge, Ascendancies & Live Ladder Meta (`poe2brain.knowledge`)** — Read-only SQLite + FTS5 index (`data/poe2_0.5.5.sqlite`) over **769** official GGG `KeywordPopups`, **49** Patch `0.5.5` anti-hallucination rules & formulas, **1,711** gems, **435** uniques, **3,219** live PoE2 bases (catching PoE1 / stale `0.1` `Expert ` base traps), **313** socketable augments, **293** crafting levers, and **882** Verisium/Kalguuran runeforge recipes, plus `MetaService` (`TreeData/0_5` **8 playable classes & 23 Ascendancies**, `Abyssal Lich` inheritance, choice notables, PoE1/`1.0` trap guardrails, and live SC/HC/SSF/HCSSF ladder shares + chase demand signals).
2. **Headless Path of Building 2 (`poe2brain.pob`)** — Persistent native `LuaJIT` + `PathOfBuilding-PoE2` (`tree 0_5`) JSON-RPC 2.0 daemon supporting multi-slot builds (`main`, `ref`, `candidate` persisted across CLI calls), `eN...` / `poe.ninja` / `poe2db.tw` / `maxroll.gg` / `pobb.in` / XML / GGG `.build` JSON I/O, **PoB-warrior dewatering audits**, `<10ms` `GetMiscCalculator()` item upgrade sweeps, passive/anoint ranking, and marginal `trade2` stat-weight generation.
3. **Live Economy, Arbitrage & Official Trade2 (`poe2brain.trade`)** — 3-tier pricing router normalized to **`Exalted Orb = 1.0`** combining `api.poe2scout.com` + `poe.ninja` `/poe2/api/economy/exchange/current/overview` (15m TTL disk cache, Reliquary Key vs Unique disambiguation, liquidity & 7d trends) with unauthenticated `pathofexile.com/api/trade2` (`GGGRateLimiter` + `1.0s` buffer), `ArbitrageScanner` (executable `0.5.5` conversions, Alva spreads, online order books/triangles, `profit_ex_per_10k_gold`, and index-lag/bench trap filters), `Exiled-Exchange-2`-backed `Ctrl+C` / `Ctrl+Alt+C` item parsing, **Instant Buyout (`securable` Gold `fee`) vs In-Person (`whisper`)** separation, and price-fixer account deduplication.
4. **Exact Crafting Odds, Policy & Profit Scanner (`poe2brain.crafting`)** — Conditional `ModPoolEngine` (`display_tier = 1` best, tiered orb `min_mod_level` floors `{1, 35, 50, 55, 70}`, Ancient Bone `40` floor, family mutual exclusion, dynamic elemental tag-blocking, 3-draw without-replacement Well of Souls unveil, `Omen of Whittling`, cross-slot/cross-pool `Sinistral/Dextral Crystallisation`, blocking removed `0.4` Homogenising Omens & `0.5` Recombinators) + sunk-cost-disciplined `CraftingPlanner` (`4-mod Fracturing Orb` checkpoints, `p50`/`p80`/`p95` Monte Carlo, buy-vs-craft) + `CraftProfitScanner` ranking profitable crafts against live `trade2` `securable` exit prices.
5. **PoE Ninja Builds Discovery & Live Character Import (`poe2brain.ninja`)** — Live `poe.ninja` `build-index-state` & `snapshot-versions` telemetry across SC/HC/SSF/HCSSF, pure-stdlib `SearchResult` protobuf + `NDIC\x02` dictionary decoder, `<2ms` `<PathOfBuilding2>` XML inspector, multi-facet search over the bundled **353-build `Forbidden Rites` Top-10 Leaderboard corpus** (all **23** Ascendancies), and live `HTTP 200` character / time-machine / multi-site PoB2 share-URL importer directly into `Pob2Bridge`.
6. **Agentic Loot Filter, Visual Theme & Custom Sound Studio (`poe2brain.filters`)** — Self-validating PoE2 `0.5.5` `.filter` compiler & `FilterValidator` enforcing all 7 client-crash traps (`ItemClasses` ID->Name translation, dead/removed `BaseType`s, `HasExplicitMod` spacing, `Continue`-last, `SetFontSize 18..45`, multi-base unique safety), **8 WCAG-verified visual presets** + natural-language `compose_theme` (`--look`), pure-stdlib `44.1 kHz` `16-bit` stereo `.wav` polyphonic synth + hybrid `voice_callout` studio (`.mp3` twin export), `NeverSink` `0.10.4a` auto-healing & restyling, and self-contained interactive HTML drop/audio previews.

---

## Quickstart (Windows, macOS & Linux)

```bash
# 1. Install Python >= 3.12 environment & console entrypoints (poe2, poe2brain-mcp)
uv sync

# 2. Bootstrap headless PathOfBuilding-PoE2 runtime into ~/.cache/poe2brain/pob2
#    - Windows: zero C compiler needed (reuses local PoB2 install or MIT x64 lua51.dll)
#    - macOS / Linux: compiles LuaJIT 2.1 + luautf8 automatically
uv run poe2 bootstrap pob2

# 3. Verify engine, SQLite gamedata DB, and PoB2 runtime readiness
uv run poe2 version --json
```

### Importing Your Own Character (Day-1 Friendly, Zero `POESESSID`)

- **Option 1 — Zero-Auth Local PoB2 Sync (Works Immediately):** If you use Path of Building (PoE2) on desktop, load your latest saved build directly:
  ```bash
  uv run poe2 pob import-local --list
  uv run poe2 pob import-local --slot main
  ```
- **Option 2 — Official GGG OAuth 2.1 PKCE (Same Official Flow as PoB2):**
  ```bash
  uv run poe2 auth login
  uv run poe2 pob import-character --list
  uv run poe2 pob import-character "<CharacterName>" --slot main
  ```
- **Option 3 — `poe.ninja` URL / PoB2 Share Code:**
  ```bash
  uv run poe2 ninja character "<poe.ninja_url>" --load-pob --slot main
  uv run poe2 pob load "<eN..._code_or_pobb.in_url>" --slot main
  ```

---

## Multi-Harness Agent Setup

`poe2brain` ships ready-to-use manifests across major agent harnesses:

- **Antigravity**: Self-contained repo plugin manifest in [plugin.json](plugin.json), workspace MCP config in [.agents/mcp_config.json](.agents/mcp_config.json), and progressive-disclosure skill in [.agents/skills/poe2brain/SKILL.md](.agents/skills/poe2brain/SKILL.md).
- **Claude Code / Cursor**: Workspace stdio MCP config in [.mcp.json](.mcp.json).
- **Devin**: Workspace MCP config in [.devin/mcp_config.json](.devin/mcp_config.json).
- **Shell-Capable Coding Agents (Codex, CLI workflows)**: Invoke `uv --directory /path/to/poe2brain run poe2 ... --json` directly.

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
| **Knowledge** | `poe2_inspect_ascendancy` | `poe2 wiki ascendancy ["<name_or_class>"] [--class <cls>] --json` | List all 23 PoE2 `0.5.5` Ascendancies (8 classes) or inspect notables, choice nodes, and PoE1/`1.0` traps. |
| **Knowledge** | `poe2_meta_ladder` | `poe2 wiki meta [--league "Forbidden Rites"] --json` | Live SC/HC/SSF/HCSSF ladder shares, Ascendancy tier rankings, and liquid chase item demand signals. |
| **Knowledge** | `poe2_waystone_regex` | `poe2 wiki regex [--slot <slot>] [--archetype <csv>] [--avoid <csv>] [--want <csv>] [--tablet-want <csv>] [--min-waystone-drop 120] [--min-pack-size 20] [--item-text "<ctrl_c>"] --json` | Compile verified `<=50`-char PoE2 Waystone brick/safe/juicing & Precursor Tablet stash regexes + `0..6` mod revive schedule. |
| **PoB2** | `poe2_pob_load` | `poe2 pob load <code_url_or_file> [--slot main] --json` | Load `eN...` share code, `poe.ninja` / `poe2db.tw` / `maxroll.gg` / `pobb.in` URL, PoB XML, or GGG `.build` JSON into a persistent named slot. |
| **PoB2** | `poe2_pob_stats` | `poe2 pob stats [--slot main] --json` | Extract `FullDPS`, `TotalEHP`, `Life`, `ES`, `Mana`, `Spirit`, resists, Max Hits, `Block`, `Deflection`, `Evasion`, `Armour`. |
| **PoB2** | `poe2_pob_audit` | `poe2 pob audit [<code_url_or_file>] [--slot main] --json` | **Dewatering audit**: strip PoB-warrior config inflation and return sustainable Pinnacle Boss stats + culprits. |
| **PoB2** | `poe2_pob_compare` | `poe2 pob compare [--slot-a early] [--slot-b main] [--dewater] --json` | Compare two persisted PoB2 slots across DPS/EHP, gear/uniques, skills, passives, and exact `0.5.5` Gold respec cost. |
| **PoB2** | `poe2_pob_hc_audit` | `poe2 pob hc-audit [<code_url_or_file>] [--slot main] [--top-nodes 5] --json` | Audit HC Pinnacle Boss Max Hits, Bleed/Poison ES bypass, Chaos `2x` ES vulnerability, Charms, and top defensive EHP respecs. |
| **PoB2** | `poe2_pob_eval_item` | `poe2 pob eval-item --equip-slot "<slot>" --item-text "<text>" [--slot main] --json` | Non-destructive `GetMiscCalculator()` item swap returning `ItemUpgradeDiff` (`full_dps_delta`, `total_ehp_delta`, `spirit_unreserved_delta`). |
| **PoB2** | `poe2_pob_stat_weights` | `poe2 pob weights --equip-slot "<slot>" [--objective balanced\|dps\|ehp] [--slot main] --json` | Compute marginal PoB2 stat weights (`SlotStatWeights`) and unauthenticated-safe `trade2` filter JSON. |
| **PoB2** | `poe2_pob_suggest_nodes` | `poe2 pob nodes [--mode perPoint\|total\|anoint] [--slot main] [--top 10] --json` | Rank reachable passive notables (`perPoint`/`total`) or full-tree Distilled Emotion Amulet anoints (`anoint`). |
| **PoB2** | `poe2_pob_calc_breakdown` | `poe2 pob calc-breakdown "<query>" [--slot main] [--skill-group <idx>] [--max-mods 40] --json` | Inspect exact PoB2 calculation formulas & `BASE`/`INC`/`MORE`/`FLAG` modifier provenance grouped by source (`CalcBreakdownReport`). |
| **PoB2** | `poe2_pob_supports` | `poe2 pob supports [--slot main] [--group <idx>] [--replace-slot <n>] [--replace-gem "<name>"] [--max-tier <1..3>] [--include-lineage/--no-lineage] [--top 15] --json` | Rank all compatible PoE2 `0.5.5` Support Gems (Tier `I`–`III` + Lineage) by DPS/EHP delta, Mana sustain, Spirit overflow, and Lineage `max_copies=1` conflicts. |
| **PoB2** | `poe2_pob_radius_jewels` | `poe2 pob radius-jewels ["<jewel>"] [--slot main] [--stat FullDPS] [--max-dist 10] [--top 10] --json` | Rank Passive Tree Jewel Sockets × radius rings (`Controlled Metamorphosis` / `Against the Darkness`) by per-point gain & travel points saved. |
| **PoB2** | `poe2_pob_what_if` | `poe2 pob what-if [--slot main] [--equip "Slot=TextOrPath"] [--edit-slot "<slot>" --add-mod "<line>" --remove-mod "<sub>" --quality 20] [--alloc-node <id>] [--dealloc-node <id>] [--config k=v] [--borrow-from <ref> --borrow-slot "<slot>"] [--scenario-matrix] [--apply] --json` | Transactional multi-change PoB2 hypothesis sandbox (`WhatIfReport`) with per-piece attribution, 3-scenario matrix, and automatic rollback. |
| **PoB2** | `poe2_pob_import_local` | `poe2 pob import-local [<name_or_path>] [--dir <builds_dir>] [--slot main] [--list] --json` | Discover or load saved local Path of Building (PoE2) `.xml` builds directly (zero-auth Day-1 sync on Windows/macOS/Linux). |
| **PoB2** | `poe2_pob_import_character` | `poe2 pob import-character [<character_name>] [--realm poe2] [--slot main] [--list] --json` | List or import private/Day-1 PoE2 characters via official GGG OAuth 2.1 PKCE (`poe2 auth login`, zero `POESESSID`). |
| **Economy** | `poe2_currency_price` | `poe2 econ price "<query>" [--league "Forbidden Rites"] --json` | Live `CurrencyQuote` normalized to `{exalted, chaos, divine}` (`Exalted = 1.0`) with 7d trend and volume. |
| **Economy** | `poe2_market_scan` | `poe2 econ scan [--category <cat>] [--min-volume-divine 5.0] [--league "Forbidden Rites"] --json` | High-liquidity exchange movers, numeraire-normalized cross-source spreads, and chanceable Uniques. |
| **Economy** | `poe2_scan_arbitrage` | `poe2 econ arbitrage [--min-volume-divine 2.0] [--max-investment-divine <d>] [--max-gold <g>] [--ladder-start-exalted <ex> --gold-budget <g> --ladder-target-divine <d>] [--live-books/--no-live-books] --json` | Executable `0.5.5` conversions, Alva spreads, order books, `profit_ex_per_10k_gold`, trap warnings, or multi-rung capital & Gold compounding ladder. |
| **Trade** | `poe2_parse_item` | `poe2 trade parse [--file <path> \| --text "<ctrl_c>"] --json` | Parse PoE2 `Ctrl+C` / `Ctrl+Alt+C` text into `ParsedItem` and PoB2 `Item.lua` text. |
| **Trade** | `poe2_price_check` | `poe2 trade check [--file <path> \| --text "<ctrl_c>"] [--securable-only] [--league "Forbidden Rites"] --json` | 3-tier price check splitting **Instant Buyout (`securable`, Gold `fee`)** vs **In-Person (`whisper`)** with fixer warnings. |
| **Trade** | `poe2_triage_dump` | `poe2 trade triage [--file <path> \| --text "<multi_item_dump>"] [--min-exalted 5.0] [--securable-only] [--live-trade/--no-live-trade] --json` | Batch price-check & classify multi-item `Ctrl+C` dumps into `sell_instant`, `craft_finish_ev`, `high_variance_id_or_unveil`, and `salvage_or_vendor`. |
| **Trade** | `poe2_find_upgrades` | `poe2 trade upgrades --equip-slot "<slot>" [--max-exalted 50] [--min-spirit 0] [--securable-only] [--normalize-topoff/--no-normalize-topoff] [--slot main] --json` | Query live `trade2` for slot upgrades, evaluate raw & craft-topped-off (`20%` Quality + Greater Runes) stats in PoB2, and rank by true all-in ROI. |
| **Crafting** | `poe2_craft_odds` | `poe2 craft odds --base "<base>" --ilvl 82 [--lever "<lever>"] [--target "<mod>"] [--omen "<omen>"] [--item-text "<ctrl_c>"] [--protect-existing/--no-protect-existing] --json` | Exact conditional mod-pool hit odds (`CraftActionEval`) with live currency pricing and mid-craft collateral protection. |
| **Crafting** | `poe2_craft_plan` | `poe2 craft plan --base "<base>" --ilvl 82 --target "<mod>"... [--buy-exalted <p>] [--sims 3000] --json` | Optimal state-conditional policy + Monte Carlo cost (`expected_cost_exalted`, `p50`/`p80`/`p95`) + `trade_vs_craft`. |
| **Crafting** | `poe2_scan_craft_profit` | `poe2 craft profit [--budget all\|low\|mid\|high] [--live-trade/--no-live-trade] [--league "Forbidden Rites"] --json` | Rank profitable `0.5.5` crafts with sanitized live input costs, `p50`/`p80` drawdowns, live `securable` exits, and ROI %. |
| **Ninja** | `poe2_ninja_snapshots` | `poe2 ninja snapshots [--league "Forbidden Rites"] --json` | Live `poe.ninja` PoE2 `build-index-state` & `snapshot-versions` across SC/HC/SSF/HCSSF modes and time-machine labels. |
| **Ninja** | `poe2_ninja_search` | `poe2 ninja search [--league "Forbidden Rites"] [--class <cls>] [--ascendancy <asc>] [--skill <sk>] [--support <sup>] [--unique <item>] [--exclude-unique <csv>] [--max-uniques <n>] [--ssf-viable] [--keystone <ks>] [--weapon <w>] [--min-dps <n>] [--min-ehp <n>] [--sort dps\|ehp\|level\|es\|life] --json` | Multi-facet `Forbidden Rites` `poe.ninja` build search with SSF viability ranking, T0 unique exclusions, and DPS/EHP/Life/ES distributions. |
| **Ninja** | `poe2_ninja_character` | `poe2 ninja character <account_or_url> [--name <char>] [--league "Forbidden Rites"] [--timemachine <lbl>] [--probe-timeline] [--load-pob] [--slot main] [--audit] --json` | Import live `poe.ninja` character or PoB2 share URL, binary-search time-machine timeline (`--probe-timeline`), or load + audit in PoB2. |
| **Filter** | `poe2_filter_presets` | `poe2 filter presets --json` | List all 8 WCAG visual theme presets, 6 strictness tiers, 6 custom soundpack styles + musical keys/scales, and local PoE2 filter dir. |
| **Filter** | `poe2_filter_generate` | `poe2 filter generate [--strictness semi_strict] [--theme <id>] [--look "<prompt>"] [--cvd-safe <mode>] [--sound-style <style>] [--warm-night] [--lowpass-hz <hz>] [--callout tier=text] [--pob <code>] [--slot <slot>] [--slots <s1,s2>] [--out <path>] [--preview-html <html_path>] [--install] --json` | Compile a validated `0.5.5` `.filter` with live economy, solo/co-op `--slots` PoB2 context, CVD/custom look, warm-night/spoken `.wav`/`.mp3` soundpack, and HTML preview. |
| **Filter** | `poe2_filter_restyle` | `poe2 filter restyle <source_path_or_neversink_strictness> [--theme <preset>] [--look "<prompt>"] [--cvd-safe <mode>] [--sound-style <style>] [--warm-night] [--callout tier=text] [--pob <code>] [--slot <slot>] [--slots <s1,s2>] [--out <path>] [--preview-html <html_path>] [--install] --json` | Auto-heal `0.5.5` stale `BaseType`s on local or MIT NeverSink `.filter` files, inject economy/co-op PoB2 overrides, and restyle look & sounds. |
| **Filter** | `poe2_filter_validate` | `poe2 filter validate <filter_file_or_text> [--sound-dir <dir>] --json` | Validate any `.filter` file or text against PoE2 `0.5.5` `ItemClasses`, `BaseItemTypes`, grammar bounds, and custom sound files. |
| **Filter** | `poe2_filter_heal` | `poe2 filter heal <filter_file_or_text> [--out <path>] [--sound-dir <dir>] --json` | **Filter Doctor**: auto-heal broken PoE1 / PoE2 `0.1` `.filter` syntax, stale `Expert ` prefixes, singular classes, and dead conditions to `0` errors. |

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
# Rebuild deterministic Patch 0.5.5 SQLite + FTS5 database & poe.ninja corpus
uv run python scripts/sync_gamedata.py
uv run python scripts/sync_ninja_corpus.py

# Lint & run unit + integration test suites
uv run ruff check . scripts
uv run pytest -v

# Include live Forbidden Rites network round-trip tests
POE2_LIVE_TESTS=1 uv run pytest -v
```

---

## Clean-Room Licensing & GGG ToS Boundary

- **MIT Licensed**: See [LICENSE](LICENSE). Bundled lookup data in `data/` derives strictly from MIT-licensed upstream projects (`PathOfBuildingCommunity/PathOfBuilding-PoE2`, `Kvan7/Exiled-Exchange-2` in [data/ee2/LICENSE.Exiled-Exchange-2](data/ee2/LICENSE.Exiled-Exchange-2), `ackness/pobr` in [data/ninja/LICENSE.pobr](data/ninja/LICENSE.pobr), `repoe-fork/poe2` & `thmsndk/vscode.poe2-filter` in [data/filter/LICENSE.filter-data](data/filter/LICENSE.filter-data), plus optional MIT `NeverSinkDev/NeverSink-Filter-for-PoE2` base filter healing).
- **Strictly Out-of-Game & Unauthenticated**: `poe2brain` never reads `POESESSID` or auth cookies, never calls Cloudflare-gated `/api/protected/` routes, never inspects game memory or screen frames, never injects inputs into the Path of Exile 2 client, and paces all public `trade2` requests through `GGGRateLimiter`.
