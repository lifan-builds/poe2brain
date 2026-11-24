---
name: poe2brain
description: >-
  Operate accurately in Path of Exile 2 (Patch 0.5.5 / Forbidden Rites) across
  verified gamedata, 23 Ascendancies & live ladder meta, poe.ninja build discovery &
  character import, headless Path of Building 2 (PoB2) build audits and upgrades, live
  trade2, currency economy & arbitrage scanning, exact expected-cost / profit-ranked
  crafting plans, and validated PoE2 loot filters with custom visual themes and audio
  soundpacks. Activate whenever a prompt touches PoE2 builds, poe.ninja, classes,
  ascendancies, ladder meta, PoB2 codes/URLs, skills, gems, passives, items, clipboard
  price-checks, trade searches, currencies, arbitrage, omens, essences, desecration,
  crafting, loot filters (.filter / NeverSink / FilterBlade), filter themes, or drop sounds.
---

# PoE2Brain (`0.5.5` / `Forbidden Rites`)

Use `poe2brain` (`poe2` CLI `--json` and `poe2brain` MCP server) as the single source of truth for Path of Exile 2. Never rely on pretrained LLM memory for PoE2 mechanics, base types, support gem limits, currency ratios, or crafting levers.

## Surface Routing: `poe2` CLI vs `poe2brain` MCP

Both surfaces call the same typed Python engine in `/Users/lfan/Project/poe2brain`.
- **In shell-capable coding harnesses (Antigravity, Claude Code, Codex, Devin):** Prefer `uv --directory /Users/lfan/Project/poe2brain run poe2 ... --json` when batching calls, piping item text from files, or filtering large outputs with `jq`.
- **For single-turn lookups or chat-only harnesses:** Call the matching `poe2brain` MCP tool directly.

| Pillar | MCP Tool (`poe2brain`) | CLI Command (`uv --directory /Users/lfan/Project/poe2brain run ...`) | Purpose & Typed Output |
| :--- | :--- | :--- | :--- |
| **Health** | `poe2_status` | `poe2 version --json` | Verify patch (`0.5.5`), default league (`Forbidden Rites`), SQLite DB (`data/poe2_0.5.5.sqlite`), and headless PoB2 runtime (`~/.cache/poe2brain/pob2`). |
| **Knowledge** | `poe2_search_knowledge` | `poe2 wiki search "<query>" [--category <cat>] [--limit 12] --json` | FTS5 + exact search across 769 GGG `KeywordPopups`, 49 anti-hallucination rules, 1,711 gems, 435 uniques, 3,219 live bases, 313 augments, 293 crafting levers, and 882 runeforge recipes. |
| **Knowledge** | `poe2_explain_mechanic` | `poe2 wiki mechanic "<topic>" --json` | Authoritative `MechanicFact` (`poe1_trap`, `poe2_live_rule`, `formula_expr`) + official GGG keyword popups. |
| **Knowledge** | `poe2_inspect_gem` | `poe2 wiki gem "<name>" --json` | Active, Spirit, Meta, Tiered Support (`I`/`II`/`III`), and `lineage_support` (`max_copies_per_char=1`) gem stats and tag compatibility. |
| **Knowledge** | `poe2_inspect_base` | `poe2 wiki base "<name>" [--ilvl 82] --json` | Base properties, implicits, `poe2db_slug`, top `display_tier=1` affixes at `ilvl`, and automatic PoE1 / stale `Expert ` base-name correction. |
| **Knowledge** | `poe2_inspect_unique` | `poe2 wiki unique "<name>" --json` | Unique base type, implicits, explicits, and `is_chanceable` status. |
| **Knowledge** | `poe2_inspect_augment` | `poe2 wiki augment "<query>" --json` | Runes, Soul Cores, Azmeri Idols, Shaman Bonded bonuses, and Patch `0.5` Verisium Anvil / Kalguuran Remnant recipes. |
| **Knowledge** | `poe2_inspect_ascendancy` | `poe2 wiki ascendancy ["<name_or_class>"] [--class <cls>] --json` | List all 23 PoE2 `0.5.5` Ascendancies across 8 classes, or inspect full notables, multiple-choice nodes, `Abyssal Lich` inheritance, and PoE1/`1.0` trap errors. |
| **Knowledge** | `poe2_meta_ladder` | `poe2 wiki meta [--league "Forbidden Rites"] --json` | Live SC/HC/SSF/HCSSF ladder shares (`142k+` indexed characters), composite Ascendancy tier rankings, and liquid chase item / Lineage Support demand signals. |
| **PoB2** | `poe2_pob_load` | `poe2 pob load <code_url_or_file> [--slot main] --json` | Load `eN...` share code, `poe.ninja` / `poe2db.tw` / `maxroll.gg` / `pobb.in` URL, PoB XML, or GGG `.build` JSON into named slot (`main`, `ref`, `candidate`) and return `BuildStats`. |
| **PoB2** | `poe2_pob_stats` | `poe2 pob stats [--slot main] --json` | Extract `FullDPS`, `TotalEHP`, `Life`, `ES`, `Mana`, `Spirit` (`total`/`reserved`/`unreserved`), resists, `Block`, `Deflection`, `Evasion`, `Armour`. |
| **PoB2** | `poe2_pob_audit` | `poe2 pob audit [<code_url_or_file>] [--slot main] --json` | **Dewatering audit**: strip PoB-warrior config inflation and return `DewateredAudit` (`raw_stats` vs `sustainable_pinnacle_stats` + `stripped_flags`). |
| **PoB2** | `poe2_pob_eval_item` | `poe2 pob eval-item --equip-slot "<slot>" --item-text "<text>" [--slot main] --json` | Non-destructive `GetMiscCalculator()` swap returning `ItemUpgradeDiff` (`full_dps_delta`, `total_ehp_delta`, `spirit_unreserved_delta`, `resist_deltas`). |
| **PoB2** | `poe2_pob_stat_weights` | `poe2 pob weights --equip-slot "<slot>" [--objective balanced\|dps\|ehp] [--slot main] --json` | Compute exact marginal stat weights (`SlotStatWeights`, works on occupied or empty slots) and compile an unauthenticated-safe `trade2` `type: "count"` query (`trade2_weighted_filter`). |
| **PoB2** | `poe2_pob_suggest_nodes` | `poe2 pob nodes [--mode perPoint\|total\|anoint] [--slot main] [--top 10] --json` | Rank highest-impact reachable passive notables (`perPoint`/`total`) or full-tree Amulet Distilled Emotion anoints (`anoint`). |
| **Economy** | `poe2_currency_price` | `poe2 econ price "<query>" [--league "Forbidden Rites"] --json` | Live `CurrencyQuote` normalized to `{exalted, chaos, divine}` (`Exalted = 1.0`) with 7d trend and `poe.ninja` exchange volume. |
| **Economy** | `poe2_market_scan` | `poe2 econ scan [--category <cat>] [--min-volume-divine 5.0] [--league "Forbidden Rites"] --json` | High-liquidity exchange movers, numeraire-normalized cross-source spreads, and high-value chanceable Uniques. |
| **Economy** | `poe2_scan_arbitrage` | `poe2 econ arbitrage [--min-volume-divine 2.0] [--live-books/--no-live-books] [--league "Forbidden Rites"] --json` | Executable `0.5.5` hideout conversions, fragment/splinter sets, Alva limit-order spreads, online order books/triangles, `profit_ex_per_10k_gold`, and trap warnings. |
| **Trade** | `poe2_parse_item` | `poe2 trade parse [--file <path> \| --text "<ctrl_c>"] --json` | Parse PoE2 `Ctrl+C` / `Ctrl+Alt+C` text into `ParsedItem` (Spirit, Catalyst quality, Rune/Gem sockets, Fractured, Desecrated, Sanctified, Waystone revives). |
| **Trade** | `poe2_price_check` | `poe2 trade check [--file <path> \| --text "<ctrl_c>"] [--securable-only] [--league "Forbidden Rites"] --json` | 3-tier price check returning `PriceCheckResult`: splits **Instant Buyout (`securable`, Gold `fee`)** vs **In-Person (`whisper`)**, deduplicates fixer accounts, and emits clickable `trade_url`. |
| **Trade** | `poe2_find_upgrades` | `poe2 trade upgrades --equip-slot "<slot>" [--max-exalted 50] [--min-spirit 0] [--securable-only] [--slot main] --json` | Query `trade2` using PoB2 slot weights, convert top listings via `trade2_fetch_to_pob2_text`, and rank real upgrades by `full_dps_delta` and `dps_per_exalted`. |
| **Crafting** | `poe2_craft_odds` | `poe2 craft odds --base "<base>" --ilvl 82 --lever "<lever>" [--target "<mod>"] [--omen "<omen>"] --json` | Exact conditional mod-pool hit odds (`CraftActionEval`) honoring ilvl, orb `min_mod_level` floors, family blocking, and dynamic elemental tag-blocking. |
| **Crafting** | `poe2_craft_plan` | `poe2 craft plan --base "<base>" --ilvl 82 --target "<mod>"... [--buy-exalted <p>] [--sims 3000] --json` | Optimal state-conditional crafting policy + Monte Carlo cost (`expected_cost_exalted`, `p50`/`p80`/`p95`) + `trade_vs_craft` recommendation. |
| **Crafting** | `poe2_scan_craft_profit` | `poe2 craft profit [--budget all\|low\|mid\|high] [--live-trade/--no-live-trade] [--league "Forbidden Rites"] --json` | Rank profitable `0.5.5` crafts across low/mid/high budgets comparing sanitized live currency inputs + `p50`/`p80` drawdowns against live `trade2` `securable` exit prices. |
| **Ninja** | `poe2_ninja_snapshots` | `poe2 ninja snapshots [--league "Forbidden Rites"] --json` | Live `poe.ninja` PoE2 `build-index-state` & `snapshot-versions` across SC/HC/SSF/HCSSF modes, character totals, class shares, and time-machine labels. |
| **Ninja** | `poe2_ninja_search` | `poe2 ninja search [--league "Forbidden Rites"] [--class <cls>] [--ascendancy <asc>] [--skill <sk>] [--support <sup>] [--unique <item>] [--keystone <ks>] [--weapon <w>] [--min-dps <n>] [--min-ehp <n>] [--sort dps\|ehp\|level\|es\|life] [--limit 15] [--refresh-live 0] --json` | Multi-facet `Forbidden Rites` `poe.ninja` build search with top skill/support/unique/keystone/weapon facets and `min`/`median`/`p90`/`max` DPS/EHP/Life/ES distributions. |
| **Ninja** | `poe2_ninja_character` | `poe2 ninja character <account_or_url> [--name <char>] [--league "Forbidden Rites"] [--timemachine <lbl>] [--load-pob] [--slot main] [--audit] --json` | Fetch live `poe.ninja` character (or `poe.ninja`/`poe2db`/`maxroll`/`pobb.in` share URL), inspect `<PathOfBuilding2>` XML, and optionally load + persist into PoB2 `--slot` and run dewatering audit. |
| **Filter** | `poe2_filter_presets` | `poe2 filter presets --json` | List all 8 WCAG-verified visual theme presets, 6 strictness tiers, 6 custom soundpack styles + musical keys/scales, and local PoE2 filter directory (`detect_poe2_filter_dir()`). |
| **Filter** | `poe2_filter_generate` | `poe2 filter generate [--strictness semi_strict] [--theme forbidden_abyss] [--look "<prompt>"] [--sound-style <style>] [--musical-key D] [--scale lydian] [--pob <code>] [--slot <slot>] [--out <path>] [--sound-dir <dir>] [--preview-html <html_path>] [--install] --json` | Compile a validated `0.5.5` `.filter` from live `Forbidden Rites` economy + optional PoB2 build context, compose custom look & `.wav`/`.mp3` soundpack, render HTML preview, and optionally install. |
| **Filter** | `poe2_filter_restyle` | `poe2 filter restyle <source_path_or_neversink_strictness> [--theme <preset>] [--look "<prompt>"] [--sound-style <style>] [--pob <code>] [--slot <slot>] [--out <path>] [--sound-dir <dir>] [--preview-html <html_path>] [--install] --json` | Auto-heal stale `0.5.5` `BaseType`s/`HasExplicitMod` spacing on local or MIT NeverSink `.filter` files, inject live `0.5.5` economy + PoB2 overrides, and restyle look & sounds to `0` errors. |
| **Filter** | `poe2_filter_validate` | `poe2 filter validate <filter_file_or_text> [--sound-dir <dir>] --json` | Validate any `.filter` file or text against all 7 PoE2 `0.5.5` client-crash traps (`ItemClasses` ID vs Name, unknown/removed bases, `HasExplicitMod` spacing, `Continue`-last, `SetFontSize 18..45`, sound paths). |

## Mandatory Anti-Hallucination Guardrails (Zero Tolerance)

1. **Never apply PoE1 or stale Patch `0.1` rules.** Before stating any mechanic or formula, call `poe2_explain_mechanic` or read [mechanics-0.5.5.md](references/mechanics-0.5.5.md):
   - **Conversion scales post-conversion types ONLY** (2-step `Skill -> Gear/Tree`, zero PoE1 double-dipping; DoTs cannot convert).
   - **Weapon/Armour Quality is `1% MORE` (multiplicative)**, not additive with local `% increased`. Attributes grant **0 per level** (`Str = +2 Life`, `Dex = +6 Accuracy`, `Int = +2 Mana`).
   - **Support Gems (`0.3+`)**: Standard supports are Tiered (`I`/`II`/`III`) and **can be equipped on multiple skills** (max 1 per `SupportGemCategory` per skill). Only **Lineage Supports** (and persistent global Spirit supports) are capped at `1 per character`.
   - **Defenses (`0.5.5`)**: Base passive Block cap is **`50%`** (NOT 75%). **Deflection** mitigates `40%` of hit/ailment damage independently of Evasion (`95%` cap). **Chaos deals `2x` damage to ES** (does NOT bypass ES), whereas **Bleeding AND Poison both bypass ES** directly to Life. **Grim Feast** is a Minion Revival skill (no longer doubles ES).
2. **Exalted Orb (`1.0`) is the unit currency**, not Chaos (`1 Divine ≈ 450–465 Exalted`, `1 Chaos ≈ 36 Exalted` in `Forbidden Rites`). Never treat missing/unpriced crafting currencies as `0` cost.
3. **Never hallucinate removed bases or crafting levers**:
   - PoE1 bases (`Vaal Regalia`, `Hubris Circlet`, `Astral Plate`, `Stygian Vise`) and removed `0.1` `Expert ` prefixes (`Expert Hexer's Robe` -> `Hexer's Robe`) fail in `0.5.5` — validate every base with `poe2_inspect_base`.
   - In-game PoE2 tooltips since `0.2.1` use **`Tier 1` (`display_tier = 1`) = BEST tier** (internal `.datc64` `internal_rank_asc = 1` is worst).
   - Zero Scouring, Alteration, Fusing, Chromatic, Fossils, or Crafting Bench. **`Omen of Homogenising Exaltation` / `Coronation` (removed in `0.4`)** and **Recombinators (`Omen of Recombination`, phased out in `0.5`)** do NOT drop in `0.5.5`.
4. **Never scrape dead/protected endpoints, emit unvalidated `.filter` syntax, or violate GGG ToS**:
   - `poe.ninja`'s PoE2 `/stash/` endpoint returns `HTTP 404`, `/api/protected/poe2/builds/...` is Cloudflare-gated (`HTTP 403`), and `poe2scout.com/api` returns HTML — always use `poe2brain`'s verified clients (`api.poe2scout.com`, `poe.ninja/poe2/api/economy/exchange/current/overview`, `poe.ninja/poe2/pob/raw/overview/code`, and `pathofexile.com/api/trade2`).
   - Never hand-emit raw PoE2 `.filter` rules without `poe2_filter_validate` (raw SQLite `item_class` IDs like `Body Armour`/`Warstaff`, stale `BaseType`s like `"Fist of Kalguur"`, or `HasExplicitMod >= 2` spaces crash PoE2 filter load!).
   - Strictly read-only public APIs and clipboard text: never read `POESESSID`, inject game inputs, automate trade clicks, or send automated whispers.

## Core Decision Playbooks

### Playbook A: Meta/Ascendancy Selection, Build Audit & Gear Optimization
Read [pob2-workflows.md](references/pob2-workflows.md) (and [mechanics-0.5.5.md](references/mechanics-0.5.5.md) when mechanics interact).
1. **Verify Live Meta & Ascendancy Notables**: For *"best build / best ascendancy / budget allocation"* prompts, call `poe2_meta_ladder` (SC/HC/SSF/HCSSF shares + chase demand) and `poe2_inspect_ascendancy` (all 23 PoE2 `0.5.5` Ascendancies; blocks PoE1/unreleased `Shadow`, `Duelist`, `Necromancer`, `Scion`, `Slayer` traps) + `poe2_currency_price` on key Uniques/Lineage Supports before Theorycrafting in headless PoB2.
2. **Load & Dewater FIRST**: Load the user's `eN...` code, XML, or `.build` file into `main` and clone to `ref`. Run `poe2_pob_audit` before making any recommendation so inflated PoB-warrior Config flags (unsustained charges, manual 50% Shock, Onslaught-on-kill, `customMods`) are stripped into a true `sustainable_pinnacle_stats` baseline.
3. **Check Hard Survival Gates**: Verify Elemental Resistances (`>= 75%`), `spirit_unreserved >= 0` across both Weapon Sets, attribute requirements, ES bypass vulnerability (Bleed/Poison on non-CI ES builds), and passive Block (`<= 50%` unless investing in `+max block`).
4. **Optimize Tree, Gems & Gear Upgrades**: Run `poe2_pob_suggest_nodes` for passive reallocations, check support gem tiers (`I -> III`) and unused 1-of Lineage Support upgrades via `poe2_inspect_gem`, and run `poe2_pob_stat_weights` -> `poe2_find_upgrades` under the user's Exalted budget (`{"max": max_exalted}` Exalted-Equivalent includes Divine-priced items).

### Playbook B: Clipboard Item Price-Checking & Budget Upgrade Hunting
Read [trade-economy.md](references/trade-economy.md).
1. **Parse & Classify (`Ctrl+C` / `Ctrl+Alt+C`)**: Run `poe2_price_check` on the raw item text. Stackables, Omens, Essences, Uncut/Lineage Gems, and uncorrupted base Uniques resolve in sub-ms via Tier-1 (`poe2scout` + `poe.ninja`); Rares, Waystones, Precursor Tablets, Cut Gems, and roll-sensitive/corrupted Uniques query live `trade2`.
2. **Separate Instant Buyout (`securable`) from Whisper Fixers**: Always report **both** `securable_min_exalted` / `securable_median_exalted` (async Gold `fee` buyout — zero seller whisper needed, works offline) and `whisper` prices. Flag `fixer_warnings` whenever `min_exalted` is undercut by single-account whisper spam.
3. **Hunt Budget Upgrades**: When shopping for a slot upgrade, pass `--max-exalted <budget>` (and `--securable-only` when the user wants instant Gold purchase) to `poe2_find_upgrades` so every candidate is evaluated inside headless PoB2 (`GetMiscCalculator()`) rather than judged by eye.

### Playbook C: Profitable Craft Discovery & Expected-Cost Crafting vs Buying
Read [crafting-playbook.md](references/crafting-playbook.md).
1. **Scan Profitable Crafts First**: When asked *"what craft is most profitable?"*, run `poe2_scan_craft_profit` (`--budget all|low|mid|high`) to rank deterministic & positive-EV crafts with sanitized live input costs, `p50`/`p80` drawdowns, live `trade2` `securable` `p10`/`median` exit prices, listing depth, and step-by-step recipes while flagging negative-EV traps.
2. **Anchor Market Buy Price & Mod Pool**: For a specific item craft, establish `buy_price_ex` via `poe2_price_check` / `poe2_find_upgrades` and call `poe2_inspect_base` to confirm `0.5.5` base name, `T1` (`display_tier=1`) `required_level` breakpoints, `Omen of Whittling` ceilings, and elemental tag-blocking.
3. **Simulate Optimal `0.5.5` Policy**: Run `poe2_craft_plan --base "<base>" --ilvl <ilvl> --target ... --buy-exalted <buy_price_ex> --sims 3000`.
4. **Decide on Mean AND Tail Risk**: Recommend **Buy** when `buy_price_ex <= expected_cost_exalted` or when bankroll cannot absorb `p80_cost_exalted`; recommend **Craft** when `expected_cost_exalted` materially beats market and bankroll covers `p80`.

### Playbook D: Currency Market Arbitrage, Liquidity & Farming Strategy
Read [trade-economy.md](references/trade-economy.md) and [mechanics-0.5.5.md](references/mechanics-0.5.5.md).
1. **Run `poe2_scan_arbitrage` for Executable Plays**: Never treat raw `poe2scout` vs `poe.ninja` Exalted spreads as arbitrage (VWAP `Div/Ex` numeraire lag creates illusory `13–18%` spreads). Use `poe2_scan_arbitrage` to rank deterministic hideout conversions (`2-Socket Masterwork Rune + Orb of Extraction -> Perfect Runes`, `Origin Spark + Cradle -> Origin Core`, `300x Simulacrum Splinter -> Simulacrum`, `DistilledEmotion` 3-to-1, drop-weighted `Refined Catalyst` EV) and Alva asynchronous limit-order spreads by **both ROI % and `profit_ex_per_10k_gold`**.
2. **Enforce Gold-Tax & Liquidity Discipline**: Quote bulk flips in `Divine` or `Chaos` (never `450+` `Exalted` stacks that burn `~45k Gold/cycle`), never cross `/api/trade2/exchange` shark bids, and require `volumePrimaryValue >= 2–5 Div` and `>= 8–10` traded units on both legs.
3. **Align Atlas Setup to Build**: Match Waystone mod count to revive tolerance (`6 mods = 0 revives`), slot Precursor Tablets directly into the **Map Device** (not Towers), and leverage Patch `0.5` *Masters of the Atlas* (*Jado, Hilda, Doryani*) and Verisium Runeforging recipes (`poe2_inspect_augment`).

### Playbook E: `poe.ninja` Build Discovery -> Headless PoB2 Audit -> Build-Tailored Loot Filter + Custom Look & Sound Pack
Read [filters-and-ninja.md](references/filters-and-ninja.md) and [pob2-workflows.md](references/pob2-workflows.md).
1. **Discover & Filter Real `Forbidden Rites` Builds (`poe2_ninja_search`)**: Filter across `--ascendancy`, `--skill`, `--unique`, `--keystone`, `--weapon`, and `--min-dps`/`--min-ehp` to compare top skills, Lineage Supports, uniques, keystones, and `median`/`p90` DPS & EHP distributions (use `poe2_ninja_snapshots` for live SC/HC/SSF/HCSSF character totals and time-machine labels).
2. **Import Live Character into Persistent PoB2 (`poe2_ninja_character --load-pob`)**: Pull any `poe.ninja` character URL, `(account, character)` pair (`Account-1234`), `?timemachine=` snapshot, or `poe.ninja`/`poe2db`/`maxroll`/`pobb.in` share URL directly into `--slot main`, then run `poe2_pob_audit` (`--audit`) to verify sustainable Pinnacle Boss DPS/EHP and uncapped resistances.
3. **Compile Build-Tailored Filter, Custom Look & Sound Pack (`poe2_filter_generate` / `poe2_filter_restyle`)**:
   - Pass `--slot main` (or `--pob <code>`), desired `--strictness` (`soft`..`uber_strict`), a preset `--theme` (`forbidden_abyss`, `cyberpunk_neon`, `darkmode_obsidian`, `colorblind_safe`, etc.) and/or natural-language `--look "<prompt>"`, plus `--sound-style` (`celestial_chime`, `ritual_gong`, `cyber_synth`, `arcade_retro`, `zen_minimal`, or spoken `voice_callout`) and `--preview-html <path.html>`.
   - If the user prefers NeverSink's base rules, run `poe2_filter_restyle <path_or_strictness>` to auto-heal stale `0.10.4a` `BaseType`s (`"Fist of Kalguur"`, `"Fragments of the Past"`), inject live `0.5.5` economy + PoB2 overrides, and apply the custom look & soundpack.
4. **Validate & Preview Before Installing**: Confirm `validation.valid == true` and `error_count == 0` (`poe2_filter_validate`), share the interactive HTML preview (`--preview-html`), and pass `--install` (`install=True`) only when the user asks to install into their local PoE2 filter directory.
