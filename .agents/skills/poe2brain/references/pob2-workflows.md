# Headless Path of Building 2 (`PoB2`) Workflows

`poe2brain` embeds a persistent macOS ARM64 `LuaJIT` + `PathOfBuilding-PoE2` (`tree 0_5`, `v0.23.1`) JSON-RPC bridge (`~/.cache/poe2brain/pob2`, seeded in `<2s` by `scripts/bootstrap_pob2.sh`). All DPS, EHP, Spirit, resistance, passive node, and item swap numbers come from real PoB2 `CalcOffence.lua` / `CalcDefence.lua` execution — never estimate DPS by hand.

## 1. Runtime & Multi-Slot Session Hygiene

1. **Check Readiness**: If `poe2_status` (`poe2 version --json`) reports `pob2_runtime_ready: false`, run:
   ```bash
   /Users/lfan/Project/poe2/poe2brain/scripts/bootstrap_pob2.sh
   ```
2. **Supported Build Inputs (`poe2_pob_load`)**:
   - **`eN...` Share Codes** (PoB2 export strings, decoded via ZLib `wbits=47` / encoded `wbits=15`).
   - **`https://pobb.in/<id>` URLs** (auto-fetched via `/raw`).
   - **Raw PoB2 `.xml` files**.
   - **Official GGG `.build` JSON planner files** (`BuildExportPoE2.lua`).
3. **Named Build Slots & Undo/Redo**:
   - `main`: User's current in-game character state.
   - `ref`: Frozen baseline snapshot (clone `main -> ref` before mutating passives/gems, or load a build-guide reference PoB here to diff against `main`).
   - `candidate`: Scratch slot for testing multi-piece swaps, keystone pivots (`CI`, `MoM`, `Giant's Blood`), or weapon-set split trees without polluting `main` (each slot also maintains a 20-step XML `undo`/`redo` stack).
4. **Passive Notables & Amulet Anoints (`poe2_pob_suggest_nodes`)**:
   - `--mode perPoint`: Highest `+FullDPS` / `+TotalEHP` per passive point within `max_path_dist=8` (auto-falls back to `EffectiveDPS` / `CombinedDPS` when `poe.ninja` imports leave `includeInFullDPS="false"`).
   - `--mode total`: Highest raw cluster payoff within reach.
   - `--mode anoint`: Scans the **entire `0_5` passive tree** (ignoring pathing distance) to find the #1 Distilled Liquid Emotion Amulet anoint, returning exact `0.5.5` 3-Emotion recipes (`anoint_emotions`) and SSF farmability tier (`ssf_anoint_tier`).
5. **Progression Diff & Gold Respec Calculator (`poe2_pob_compare`)**:
   - Load early-league vs endgame snapshots into two slots (`early` & `main`) and run `poe2 pob compare --slot-a early --slot-b main --dewater --json` (`BuildSlotComparison`) to diff DPS/EHP, added/removed Uniques, skill/support links, keystones, and exact PoE2 `0.5.5` Gold respec cost (`GoldRespecPrices.dat`: `1x` per passive refund, `5x` per Ascendancy refund, `0 Gold` to allocate unspent points — Orbs of Regret do NOT exist).
6. **Hardcore Pinnacle Boss One-Shot & ES-Bypass Audit (`poe2_pob_hc_audit`)**:
   - Run `poe2 pob hc-audit [<code_url_or_file>] --slot main --top-nodes 5 --json` (`HcRipAuditReport`) to inspect sustainable Pinnacle Phys/Ele/Chaos Max Hits, catch lethal **non-CI Low-Life ES Bleed/Poison bypass** (`bleed_poison_es_bypass`) and **Chaos `2x` ES damage** (`chaos_2x_es_vulnerability`), check Belt Charms, and rank top reachable defensive EHP passive respec clusters with exact Gold cost.
7. **Calculation Provenance, Support Gem Sweeps, Radius Jewels & Transactional What-Ifs**:
   - **Formula & Modifier Provenance (`poe2_pob_calc_breakdown`)**: Run `poe2 pob calc-breakdown "FullDPS" --slot main --json` (or `"EnergyShield"`, `"ManaCost"`, `"CritChance"`, `"ChaosResist"`) to inspect exact PoB2 `Calcs.lua` formula lines and every `BASE`/`INC`/`MORE`/`FLAG` modifier grouped by `Item`, `Tree`, `Skill`, and `Config` source — automatically warning when `calcsInput.skill_number` was pointing at a secondary socket group instead of `mainSocketGroup`.
   - **Support Gem Optimizer (`poe2_pob_supports`)**: Run `poe2 pob supports --slot main [--group <idx>] [--replace-gem "<name>"] [--max-tier 3] --json` to sweep every compatible PoE2 `0.5.5` Support Gem (Tier `I`–`III` + Lineage) via `GetMiscCalculator()`, reporting DPS/EHP deltas alongside `mana_sustainable_after`, `spirit_overflow`, `attributes_met_after`, and cross-group `lineage_conflict_skill` (`max_copies_per_char=1`).
   - **Radius Jewel Socket × Ring Solver (`poe2_pob_radius_jewels`)**: Run `poe2 pob radius-jewels "Controlled Metamorphosis" --slot main --json` (or `"Against the Darkness"`) to rank every Passive Tree Jewel Socket × radius ring (`Very Small`..`Massive`) by per-point gain & travel points saved.
   - **Transactional Hypothesis Sandbox (`poe2_pob_what_if`)**: Run `poe2 pob what-if --slot main [--edit-slot "Weapon 1" --add-mod "+100 to maximum Life" --quality 20] [--alloc-node <id>] [--borrow-from ref --borrow-slot "Ring 1"] [--scenario-matrix] --json` to test combined gear/tree/config changes or attribute why a reference `poe.ninja` build out-scales `main` per borrowed piece — automatically rolling `main` back unless `--apply` is passed.

---

## 2. Mandatory Step 1: The "Dewatering" (PoB-Warrior) Audit

Public build guides and user-exported PoBs routinely inflate `FullDPS` by **2x–8x** using unsustainable Config tab overrides. Always run `poe2_pob_audit` (`poe2 pob audit --slot main --json`) **before** diagnosing DPS or survivability.

### What `poe2_pob_audit` Strips & Measures (`DewateredAudit`)
1. **Enemy Preset**: Normalizes `enemyIsBoss` to **Pinnacle Boss** (fixing `0` enemy resist/armour "white mob" configs).
2. **Unsustained Charges**: Strips `usePowerCharges`, `useFrenzyCharges`, and `useEnduranceCharges` when the build lacks a boss-sustainable generator (PoE2 charges require corpse/freeze/cull/voll's loops and **do NOT grant inherent stats** — they are consumed by skills unless Charge Infusion/notables are allocated!).
3. **On-Kill & Conditional Buffs**: Disables `buffOnslaught`, `KilledRecently`, `HitRecently` (if conditional), `FullLife` (on non-CI builds without instant recovery), and `LowLife` (unless reserved/Coward's Legacy).
4. **Unbacked Debuffs & Overrides**: Resets manual `overrideShock` (`50%` Shock ticked without magnitude scaling), max `Withered` stacks, `ArmourFullyBroken`, `Intimidated`, `Unnerved`, and `Exposure` when no active/triggered skill applies them.
5. **Custom Mod Cheats (`customMods`)**: Flags and strips hand-typed lines in PoB's *Custom Modifiers* box (`+500% more damage`, fake flask/shrine mods, resistance padding).

### How to Report Audit Output
- Always show **both** `raw_stats.full_dps` / `raw_stats.total_ehp` and `sustainable_pinnacle_stats.full_dps` / `sustainable_pinnacle_stats.total_ehp` (`dps_inflation_pct`, `ehp_inflation_pct`).
- List the top culprits from `stripped_flags` (`flag`, `raw_value`, `dps_inflation_pct`) and use `sustainable_pinnacle_stats` as the baseline for all downstream upgrade comparisons.

---

## 3. Hard Build Gate Checklist (`BuildStats`)

After dewatering, verify these six PoE2 failure modes before chasing raw DPS:
1. **Spirit Reservation (`spirit_unreserved >= 0`)**: Check `spirit_reserved` vs `spirit_total` on **both Weapon Sets**. Upgrading a Sceptre, Body Armour, Shield, Focus, or Amulet that drops `+Spirit` will silently disable Heralds, Cast-on-X Meta-Gems, or Minions!
2. **Elemental & Chaos Resistances**: `fire_resist`, `cold_resist`, `lightning_resist >= 75%` (accounting for campaign Act penalties). On non-CI builds, low `chaos_resist` exposes ES to **`2x` Chaos hit damage** AND Life to **Poison bypass**.
3. **Bleed & Poison vs Energy Shield**: If `energy_shield > life` and `CI` is **not** allocated, verify Anti-Bleed / Anti-Poison Belt Charms (`Charm 1..3`) or regeneration on Life.
4. **Attribute & Level Requirements**: Especially when running **`Giant's Blood`** (`3x` martial weapon Str/Dex/Int requirements) or high-level Skill/Support gems (`Level 20` uncut skill gems require ~`157–205` primary attribute).
5. **Passive Block Cap (`50%` Base)**: Flag wasted passive tree points if raw passive block investment overcaps `50%` without `+% to maximum Block chance`.
6. **Support Gem Legality**: Confirm at most **1 copy per character** of each **Lineage Support Gem** (`max_copies_per_char = 1`), while leveraging Tier `III` standard supports across secondary damage/utility skills.

---

## 4. Non-Destructive Item Sweeps (`GetMiscCalculator()`)

`poe2_pob_eval_item` wraps PoB2's native `build.calcsTab:GetMiscCalculator()` (`<10 ms` per candidate in batch mode) so candidate items never mutate the loaded slot.

### Canonical PoB2 Equipment Slot Names (`--equip-slot`)
- Weapons / Offhands: `"Weapon 1"`, `"Weapon 2"`, `"Weapon 1 Swap"`, `"Weapon 2 Swap"` (handles 2H + Offhand Sceptre/Focus/Shield under `Giant's Blood` / `Lord of the Wilds` / `Instruments of Power`, plus Bow + `"Weapon 2"` Quiver).
- Armour: `"Helmet"`, `"Body Armour"`, `"Gloves"`, `"Boots"`.
- Jewellery: `"Amulet"`, `"Ring 1"`, `"Ring 2"`, `"Belt"`.
- Flasks & Charms: `"Flask 1"` (Life), `"Flask 2"` (Mana), `"Charm 1"`, `"Charm 2"`, `"Charm 3"`.

### Candidate Evaluation Rules (`ItemUpgradeDiff`)
- **Pipe `trade2` Listings Directly**: `TradeAdvisor` (`trade2_fetch_to_pob2_text`) converts live `/api/trade2/fetch` JSON into exact PoB2 `Item.lua` syntax (stripping `[Target|Label]` wiki markup, preserving `{rune}`, `{fractured}`, `{desecrated}`, and avoiding double-counting Catalyst quality already baked into `trade2` jewellery stat numbers).
- **Guard Against "Paper DPS" Bricks**: Filter out any `ItemUpgradeDiff` where:
  - `spirit_unreserved + spirit_unreserved_delta < 0`, OR
  - any elemental resistance drops below `75%` without an open Rune socket (`Sockets: S S` -> Iron/Elemental Rune overwrite) to patch the gap!
- **Rank by ROI**: Compare both absolute gain (`full_dps_delta`, `total_ehp_delta`) and budget efficiency (`dps_per_exalted = full_dps_delta / price_exalted`).

---

## 5. The Closed-Loop Stat-Weight -> `trade2` Upgrade Pipeline

Never hand-guess `trade2` stat weights when a build is loaded in PoB2:
1. **Compute Exact Marginal Weights**: Run `poe2_pob_stat_weights --equip-slot "<slot>" --objective <dps|ehp|balanced>` (`TradeQueryGenerator`-backed). It perturbation-tests every eligible mod + open `rune_sockets` on that slot to compute exact `StatWeightEntry` weights and compiles `SlotStatWeights.trade2_weighted_filter` into an unauthenticated-safe `type: "count"` query (`min: 2..3` across top highest-impact mods, avoiding unauthenticated `/api/trade2/search` `HTTP 400` complexity rejections on `type: "weight"`).
2. **Search + Fetch + Simulate (`poe2_find_upgrades`)**: Pass the slot, `--max-exalted <budget>`, required `--min-spirit` (if replacing a Spirit slot), and optional `--securable-only` (Instant Buyout Gold listings).
3. **Verify Non-Linear Interactions & Craft Top-Off Normalization**: Linear stat weights and `count` pre-filters are approximations (crit chance × crit bonus, accuracy caps, archmage mana × cast speed are non-linear). `poe2_find_upgrades` defaults to `normalize_craft_topoff=True`, evaluating **both** the raw listing (`upgrade_diff`) and its bench-finished state (`topoff_upgrade_diff`: `20%` Quality + Artificer sockets + Greater Iron/Body Runes on non-corrupted martial/caster/armour items, leaving corrupted listings untouched) with exact currency cost (`topoff_cost_exalted`) and all-in `all_in_price_exalted` ROI.
