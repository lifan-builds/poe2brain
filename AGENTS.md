# poe2brain — Agent Engineering Guide

`poe2brain` is an agent-native Path of Exile 2 intelligence engine (`Patch 0.5.5` / `Forbidden Rites`) exposing a single typed Python 3.12+ core across both a JSON-first CLI (`poe2`) and a stdio MCP server (`poe2brain-mcp`).

## Architecture & Module Ownership

- `src/poe2brain/config.py` & `http.py` — Patch/league constants (`POE2_PATCH = "0.5.5"`, `DEFAULT_LEAGUE = "Forbidden Rites"`), runtime cache layout (`~/.cache/poe2brain`, override via `POE2BRAIN_CACHE_DIR`), shared `Poe2HttpClient` with mandatory descriptive `USER_AGENT`, disk TTL cache, and dynamic `GGGRateLimiter` (`X-Rate-Limit-Ip` / `X-Rate-Limit-Ip-State` + `1.0s` buffer).
- `src/poe2brain/models/` — Shared Pydantic v2 schemas (`common.py`, `items.py`, `knowledge.py`, `builds.py`, `trade.py`, `crafting.py`).
- `src/poe2brain/knowledge/` — Read-only SQLite + FTS5 query engine over `data/poe2_0.5.5.sqlite` (built offline by `scripts/sync_gamedata.py` from `repoe-fork/poe2`, `PathOfBuilding-PoE2`, `Exiled-Exchange-2`, `modsview_weights.json.gz`, and `mechanics_facts.json`).
- `src/poe2brain/pob/` — Persistent headless `LuaJIT` + `PathOfBuilding-PoE2` (`~/.cache/poe2brain/pob2`, bootstrapped by `scripts/bootstrap_pob2.sh`) JSON-RPC 2.0 bridge (`lua/pob2_bridge.lua`) supporting multi-slot builds (`main`, `ref`, `candidate`), PoB-warrior dewatering audits, `GetMiscCalculator()` item upgrade sweeps, passive/anoint ranking, and `TradeQueryGenerator` slot weights.
- `src/poe2brain/trade/` — `EconomyService` (`api.poe2scout.com` + `poe.ninja` `/poe2/api/economy/exchange/current/overview`), `Trade2Client` (`pathofexile.com/api/trade2`), and `Exiled-Exchange-2`-backed `Ctrl+C` / `Ctrl+Alt+C` item parser, stat matcher, and `TradeAdvisor`.
- `src/poe2brain/crafting/` — Exact conditional `ModPoolEngine` (`min_mod_level` floors, family mutual exclusion, dynamic elemental tag-blocking) and MDP / Monte Carlo `CraftingPlanner`.
- `src/poe2brain/cli.py` & `mcp_server.py` — Thin Typer CLI (`poe2 ... --json`) and `FastMCP("poe2brain")` (`mcp>=1.6.0,<2.0.0`) surfaces delegating directly to the four domain services.

## Core Repo Invariants

1. **Patch-Stamping & Zero Prose Rot**:
   - Every static gamedata artifact and mechanics rule is explicitly stamped to `POE2_PATCH` (`data/poe2_0.5.5.sqlite`). Rebuild via `scripts/sync_gamedata.py` on patch bumps rather than hand-editing ad-hoc markdown guides or committing hourly market snapshots to git.
   - All mutable runtime caches (`http/`, `pob2/`) live under `get_cache_dir()` (`~/.cache/poe2brain`), never inside tracked repository directories.
2. **Domain Correctness**:
   - **Unit Currency**: Normalize economy and crafting math to **`Exalted = 1.0`** (with Chaos and Divine equivalents). Never default missing/unquoted crafting currencies to `0.0` (`None` = unavailable).
   - **Tier Direction**: Store both `internal_rank_asc` (`1` = lowest ilvl `.datc64` rank) and `display_tier` (`1` = best in-game PoE2 tooltip tier since `0.2.1`); always present `display_tier` to callers.
   - **Active Patch Filtering**: Honor `is_active_drop_in_patch` so dormant PoE1 currencies (`Scouring`, `Alteration`, `Fusing`, `Fossils`), removed `0.4` `Homogenising` Omens, and phased-out `0.5` `Recombinators` are blocked from active league recommendations.
   - **Trade Hygiene**: Distinguish async Gold-fee Instant Buyout (`SaleType.SECURABLE`, `listing.fee`) from `SaleType.WHISPER` listings, deduplicate price-fixer accounts by `account_name`, and never call `poe.ninja`'s dead `/poe2/api/economy/stash/...` (`404`) or `poe2scout.com/api` (HTML) URLs.
3. **Clean-Room MIT Licensing**:
   - This repository is strictly **MIT-licensed**. Vendor or adapt only permissive MIT/BSD/Apache data and code with preserved license attribution (`PathOfBuildingCommunity/PathOfBuilding-PoE2`, `Kvan7/Exiled-Exchange-2` in `data/ee2/LICENSE.Exiled-Exchange-2`, `repoe-fork/poe2`).
   - **Never copy code, schemas, or files from GPL-3.0 or AGPL-3.0 projects** (`exilemoulei/pob4agents`, `ianderse/pob-mcp`, `zgrummons/pob2-mcp`, `maxrenke/pob2-mcp`, `Dboire9/POE2_HTC`).
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
