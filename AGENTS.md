# poe2brain — Agent Engineering Guide

`poe2brain` is an agent-native Path of Exile 2 intelligence engine (`Patch 0.5.5` / `Forbidden Rites`) exposing a single typed Python 3.12+ core across both a JSON-first CLI (`poe2`) and a stdio MCP server (`poe2brain-mcp`).

## Architecture & Module Ownership

- `src/poe2brain/config.py` & `http.py` — Patch/league constants (`POE2_PATCH = "0.5.5"`, `DEFAULT_LEAGUE = "Forbidden Rites"`), runtime cache layout (`~/.cache/poe2brain`, override via `POE2BRAIN_CACHE_DIR`), shared `Poe2HttpClient` with mandatory descriptive `USER_AGENT`, disk TTL cache, and dynamic `GGGRateLimiter` (`X-Rate-Limit-Ip` / `X-Rate-Limit-Ip-State` + `1.0s` buffer).
- `src/poe2brain/models/` — Shared Pydantic v2 schemas (`common.py`, `items.py`, `knowledge.py`, `builds.py`, `trade.py`, `crafting.py`, `ninja.py`, `filters.py`).
- `src/poe2brain/knowledge/` — Read-only SQLite + FTS5 query engine over `data/poe2_0.5.5.sqlite` (built offline by `scripts/sync_gamedata.py` from `repoe-fork/poe2`, `PathOfBuilding-PoE2`, `Exiled-Exchange-2`, `modsview_weights.json.gz`, and `mechanics_facts.json`), plus `meta.py` (`MetaService`: `TreeData/0_5` 8 playable classes & 23 Ascendancies, `Abyssal Lich` inheritance, choice notables, PoE1/`1.0` trap guardrails, and 30m TTL SC/HC/SSF/HCSSF ladder shares + chase demand signals).
- `src/poe2brain/pob/` — Persistent headless `LuaJIT` + `PathOfBuilding-PoE2` (`~/.cache/poe2brain/pob2`, bootstrapped by `scripts/bootstrap_pob2.sh`) JSON-RPC 2.0 bridge (`lua/pob2_bridge.lua`) supporting multi-slot builds (`main`, `ref`, `candidate`), universal `eN...` / `poe.ninja` / `poe2db.tw` / `maxroll.gg` / `pobb.in` / XML / GGG `.build` JSON loading, PoB-warrior dewatering audits, `GetMiscCalculator()` item upgrade sweeps, passive/anoint ranking, and `TradeQueryGenerator` slot weights (`{"max": max_price_exalted}` Exalted-Equivalent).
- `src/poe2brain/trade/` — `EconomyService` (`api.poe2scout.com` + `poe.ninja` `/poe2/api/economy/exchange/current/overview` with Reliquary Key vs Unique disambiguation and thin-volume outlier sanitization), `ArbitrageScanner` (`arbitrage.py`: deterministic `0.5.5` conversions, Alva spreads, online `/api/trade2/exchange` order books/triangles, `profit_ex_per_10k_gold`, and index-lag/bench trap filters), `Trade2Client` (`pathofexile.com/api/trade2`), and `Exiled-Exchange-2`-backed `Ctrl+C` / `Ctrl+Alt+C` item parser, stat matcher, and `TradeAdvisor`.
- `src/poe2brain/crafting/` — Exact conditional `ModPoolEngine` (`min_mod_level` floors, family mutual exclusion, dynamic elemental tag-blocking, cross-slot/cross-pool Essence/Alloy hydration), sunk-cost-disciplined MDP / Monte Carlo `CraftingPlanner` (`Crystallisation` swaps & `4-mod Fracturing Orb` checkpoints), and `CraftProfitScanner` (`profit.py`).
- `src/poe2brain/ninja/` — `NinjaBuildsService` (`build-index-state` + `snapshot-versions` + live `HTTP 200` `/poe2/pob/raw/overview/code` character/time-machine/multi-site PoB2 importer), pure-stdlib `SearchResult` protobuf + `NDIC\x02` dictionary codec (`protobuf.py`), `<2ms` `<PathOfBuilding2>` XML inspector (`xml_inspector.py`), and deterministic 353-build `Forbidden Rites` leaderboard corpus (`data/ninja/forbidden_rites_0.5.5_builds.json.gz`, built by `scripts/sync_ninja_corpus.py`).
- `src/poe2brain/filters/` — Self-validating PoE2 `0.5.5` `.filter` compiler (`generator.py`), lossless AST & 7-trap `FilterValidator` (`grammar.py`, `validator.py` over `data/filter/item_classes_0.5.5.json`), 8 WCAG-verified visual presets + natural-language `compose_theme` (`themes.py`), stdlib `44.1 kHz` `16-bit` stereo `.wav` synth + hybrid `voice_callout` studio (`soundpacks.py`), `NeverSinkAdapter` `0.10.4a` auto-healer/restyler (`neversink.py`), and HTML drop/audio preview + OS bundle installer (`preview.py`).
- `src/poe2brain/cli.py` & `mcp_server.py` — Thin Typer CLI (`poe2 ... --json`) and `FastMCP("poe2brain")` (`mcp>=1.6.0,<2.0.0`, 31 tools in 1:1 parity) surfaces delegating directly to the six domain modules.

## Core Repo Invariants

1. **Patch-Stamping & Zero Prose Rot**:
   - Every static gamedata artifact and mechanics rule is explicitly stamped to `POE2_PATCH` (`data/poe2_0.5.5.sqlite`). Rebuild via `scripts/sync_gamedata.py` on patch bumps rather than hand-editing ad-hoc markdown guides or committing hourly market snapshots to git.
   - All mutable runtime caches (`http/`, `pob2/`) live under `get_cache_dir()` (`~/.cache/poe2brain`), never inside tracked repository directories.
2. **Domain Correctness**:
   - **Unit Currency**: Normalize economy and crafting math to **`Exalted = 1.0`** (with Chaos and Divine equivalents). Never default missing/unquoted crafting currencies to `0.0` (`None` = unavailable).
   - **Tier Direction**: Store both `internal_rank_asc` (`1` = lowest ilvl `.datc64` rank) and `display_tier` (`1` = best in-game PoE2 tooltip tier since `0.2.1`); always present `display_tier` to callers.
   - **Active Patch Filtering**: Honor `is_active_drop_in_patch` so dormant PoE1 currencies (`Scouring`, `Alteration`, `Fusing`, `Fossils`), removed `0.4` `Homogenising` Omens, and phased-out `0.5` `Recombinators` are blocked from active league recommendations.
   - **Trade & Filter Hygiene**: Distinguish async Gold-fee Instant Buyout (`SaleType.SECURABLE`, `listing.fee`) from `SaleType.WHISPER` listings, deduplicate price-fixer accounts by `account_name`, never call `poe.ninja`'s dead `/poe2/api/economy/stash/...` (`404`), Cloudflare-gated `/api/protected/...` (`403`), or `poe2scout.com/api` (HTML) URLs, translate SQLite `item_class` IDs through `to_filter_class()` before emitting `.filter` blocks, and never write to the real `~/Library/Application Support/Path of Exile 2` directory during automated tests.
3. **Clean-Room MIT Licensing**:
   - This repository is strictly **MIT-licensed**. Vendor or adapt only permissive MIT/BSD/Apache data and code with preserved license attribution (`PathOfBuildingCommunity/PathOfBuilding-PoE2`, `Kvan7/Exiled-Exchange-2` in `data/ee2/LICENSE.Exiled-Exchange-2`, `ackness/pobr` in `data/ninja/LICENSE.pobr`, `repoe-fork/poe2` & `thmsndk/vscode.poe2-filter` in `data/filter/LICENSE.filter-data`, and `NeverSinkDev/NeverSink-Filter-for-PoE2`).
   - **Never copy code, schemas, or files from GPL-3.0 or AGPL-3.0 projects** (`exilemoulei/pob4agents`, `ianderse/pob-mcp`, `zgrummons/pob2-mcp`, `maxrenke/pob2-mcp`, `Dboire9/POE2_HTC`, `NeverSinkDev/PrototypingLab`, `WyrmGoldsmith/Path-of-Exile-2-Helper-AIO`).
4. **Non-Automation & GGG ToS Boundary**:
   - `poe2brain` is strictly an **out-of-game calculation, clipboard analysis, and public-API advisor**.
   - Never read or store `POESESSID` / session cookies, inspect game memory, OCR/poll live gameplay frames, inject mouse/keyboard inputs into the Path of Exile 2 client, send automated trade whispers, or automate item purchases. Always pace `trade2` requests through `GGGRateLimiter`.
5. **Multi-Harness Parity**:
   - Keep `.agents/mcp_config.json` (Antigravity), `.mcp.json` (Claude Code / Cursor), `.devin/mcp_config.json` (Devin), `plugin.json` (Antigravity plugin manifest — never hand-edit `~/.gemini/config/config.json` or include unknown top-level fields), and `.agents/skills/poe2brain/` synchronized (`/Users/lfan/Project/agent/scripts/harness-sync.py verify`).

## Setup & Verification

```bash
# 1. Bootstrap native macOS ARM64 LuaJIT + PathOfBuilding-PoE2 runtime (~1.7s if cached)
./scripts/bootstrap_pob2.sh

# 2. Rebuild deterministic Patch 0.5.5 SQLite + FTS5 gamedata database (~2.5s offline)
uv run python scripts/sync_gamedata.py

# 3. Lint, test, and verify CLI/MCP readiness
uv run ruff check .
uv run pytest -v
uv run poe2 version --json
```
