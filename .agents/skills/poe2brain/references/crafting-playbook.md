# PoE2 Patch `0.5.5` Crafting Decision Tree & Playbook

All probabilities, expected Exalted costs, and profit rankings must be computed via `poe2_craft_odds`, `poe2_craft_plan`, and `poe2_scan_craft_profit` (`ModPoolEngine` + `CraftingPlanner` + `CraftProfitScanner` over `data/poe2_0.5.5.sqlite` + sanitized live `EconomyService` & `trade2` quotes). Never eyeball mod weights or apply PoE1 crafting steps.

## 1. Non-Negotiable `0.5.5` Crafting Invariants

1. **Zero PoE1 Reset Currencies**: There is **NO** Orb of Scouring, Orb of Alteration, Orb of Fusing, Chromatic Orb, Harvest Reforge, Fossil/Resonator, Eldritch Ichor/Ember, or Crafting Bench in Path of Exile 2. Once a Normal base becomes Magic/Rare, it stays Magic/Rare (failed items are salvaged, recomb-less vendored, or abandoned for a fresh base).
2. **Removed Levers — NEVER Recommend in `Forbidden Rites`**:
   - **`Omen of Homogenising Exaltation` & `Omen of Homogenising Coronation`**: **Removed from active drop pools in Patch `0.4`** (`is_active_drop_in_patch = 0`; legacy copies exist only on permanent `Standard`).
   - **Expedition Recombinators / `Omen of Recombination`**: **Phased out in Patch `0.5`** (`is_active_drop_in_patch = 0`).
3. **Tier Numbering Direction (`0.2.1+`)**:
   - **`display_tier = 1` (`Tier 1`) is ALWAYS the BEST / highest tier** in-game and in `poe2brain` user outputs.
   - Internal `.datc64` / RePoE `internal_rank_asc` counts upward from `1` (lowest ilvl) — never confuse internal rank `1` with `T1`!
4. **Server-Side Weights & Dynamic Tag-Blocking**:
   - GGG removed numerical spawn weights from `.datc64` client tables; `poe2_0.5.5.sqlite` joins community-verified `poe2db` `ModsView` weights (`1000`, `500`, `250`, `50`, etc.) onto `repoe-fork` affix families.
   - **Dynamic Elemental Tag-Blocking (Wands/Staves/Sceptres)**: Certain affixes inject tags (`adds_tags_json`, e.g., rolling a `fire_Mod` or `+Level of Fire Spell Skills` dynamically blocks or re-weights competing `cold`/`lightning` `+Spell Skills` mods via `spawn_weights_json`). Always let `ModPoolEngine` compute conditional pool denominators after each rolled mod!
5. **Never Price Missing Currencies at `0`**: `EconomyService.get_bulk_crafting_prices` returns `None` (unavailable) for unquoted ultra-rare currencies/essences rather than `0.0`, preventing the MDP solver from hallucinating "free" Mirror/Corrupted Essence steps.

---

## 2. Complete Patch `0.5.5` Crafting Lever Reference

### A. Tiered Basic Orbs (`min_mod_level` Floors)
Higher-tier basic orbs truncate every affix tier with `required_level < floor` out of the eligible weight denominator — dramatically boosting `T1`–`T2` hit rates on ilvl `80–82` bases:

| Currency Family | Action | Basic Floor | **Greater** Floor | **Perfect** Floor |
| :--- | :--- | :---: | :---: | :---: |
| **Orb of Transmutation** | `Normal -> Magic` (`+1` mod) | `1` | **`55`** | **`70`** |
| **Orb of Augmentation** | `Magic (1 mod) -> Magic (2 mods)` | `1` | **`55`** | **`70`** |
| **Regal Orb** | `Magic -> Rare` (`+1` mod) | `1` | **`35`** | **`50`** |
| **Exalted Orb** | `Rare -> Rare` (`+1` mod, up to `3P/3S`) | `1` | **`35`** | **`50`** |
| **Chaos Orb** | `Rare -> Rare` (`-1` random mod, `+1` random mod) | `1` | **`35`** | **`50`** |

*(Orb of Alchemy upgrades `Normal -> 4-mod Rare`; Orb of Annulment removes `1` random non-fractured mod on Magic/Rare.)*

### B. Essences & Patch `0.5` Verisium Alloys (Critical Distinction!)
Do **NOT** confuse PoE1 Essences (reroll whole Rare) with PoE2's two distinct Essence classes:
1. **Lesser / Normal / Greater Essences** = **`Magic -> Rare` Upgrade** (acts like a **targeted Regal Orb**: keeps existing 1–2 Magic mods and adds **1 guaranteed mod** from the Essence's category/tier). Cannot be used on Rare items!
2. **Perfect Essences (`19`), Corrupted Essences (`6`: *Delirium, Horror, Hysteria, Insanity, Breach, Essence of the Abyss*), and Patch `0.5` Verisium Alloys (`13`)** = **`Rare -> Rare` Targeted Swap** (**removes `1` random existing explicit mod and adds `1` guaranteed high-tier/exclusive mod**!).
   - **Power Combo (`Sinistral / Dextral Crystallisation`)**: Activate **`Omen of Sinistral Crystallisation`** (forces removal of a **Prefix**) or **`Omen of Dextral Crystallisation`** (forces removal of a **Suffix**) before applying a Perfect Essence, Corrupted Essence, or Verisium Alloy! Example: if Suffixes are `3/3` finished (`+5 Lightning Skills`, `T1 Cast Speed`, `T1 Crit`), a Prefix-adding Perfect Essence + **`Sinistral Crystallisation`** removes a trash Prefix and adds a guaranteed `T1` Prefix with **`0%` risk to your finished Suffixes**!

### C. Fracturing Orb (`1/4` Rule)
- Permanently locks **`1 / k`** random explicit modifier (turning it golden/immutable across Annuls, Chaos, Whittling, Crystallisation, and Desecration) on any Rare item with **`k >= 4` explicit modifiers**.
- **Optimal Rule**: Always fracture at **exactly `4` mods (`25.0%` hit probability)** — never slam to 5 (`20%`) or 6 (`16.7%`) mods before fracturing!

### D. Ritual Crafting Omens (`0.5.5` Active Pool)
- **Prefix (`Sinistral`) / Suffix (`Dextral`) Targeting**:
  - `Omen of Sinistral / Dextral Exaltation`: Next Exalt adds Prefix / Suffix.
  - `Omen of Sinistral / Dextral Coronation`: Next Regal adds Prefix / Suffix.
  - `Omen of Sinistral / Dextral Annulment`: Next Annul removes Prefix / Suffix.
  - `Omen of Sinistral / Dextral Erasure`: Next Chaos **removes only** a Prefix / Suffix (note: the newly added mod can still roll either side unless the opposite side is already `3/3` full!).
  - `Omen of Sinistral / Dextral Alchemy`: Next Alchemy guarantees max Prefixes (`3`) / max Suffixes (`3`).
  - `Omen of Sinistral / Dextral Crystallisation`: Next Perfect/Corrupted Essence or Verisium Alloy **removes** a Prefix / Suffix.
  - `Omen of Sinistral / Dextral Necromancy`: Next Abyssal Desecration adds a Desecrated Prefix / Suffix.
- **Multi-Roll & Catalyst Omens**:
  - `Omen of Greater Exaltation` (`+2` mods on next Exalt) & `Omen of Greater Annulment` (`-2` mods on next Annul).
  - `Omen of Catalysing Exaltation`: Consumes Jewellery/item Catalyst quality to weight the next Exalted Orb heavily toward the Catalyst's mod tag!
- **`Omen of Whittling` (Deterministic Low-ilvl Cleanup) & Mid-Craft Collateral Protection**:
  - Next **Chaos Orb** (including Greater/Perfect Chaos!) **removes the non-fractured explicit modifier with the LOWEST `required_level`** instead of choosing randomly (fractured mods are ignored even if their `required_level` is lowest on the item).
  - **Whittling & Collateral Safety Check (`poe2_craft_odds --protect-existing`)**: Inspect `ParsedMod.required_level` on **every** desired mod before using Whittling (if tied for lowest `required_level`, removal splits across tied mods!). When `--item-text` is supplied, `poe2_craft_odds` defaults to `protect_existing_targets=True`, sorting `0.0%`-collateral actions (`Whittling`, side-locked `Crystallisation`, `Omen of Light`) ahead of raw Chaos/swap actions that risk bricking finished T1 affixes.
- **Terminal Lock Finishers (Apply LAST Only)**:
  - **`Omen of Sanctification` + Divine Orb**: Re-rolls & multiplies all explicit modifier values independently by **`[0.78, 1.22]`** (can push `+4` or `T1` rolls above normal caps!) and **Sanctifies (locks) the item** against all further modification (including Runes!).
  - **`Omen of Corruption` + Vaal Orb**: Removes the "no change" Vaal outcome (forces implicit/socket/mod/brick outcome) and corrupts the item.

### E. Abyssal Desecration (Well of Souls)
- **Bones by Base Class × Tier**:
  - Class: **Jawbones** (Weapons & Quivers) | **Ribs** (Armour & Shields/Foci) | **Collarbones** (Amulets, Rings, Belts).
  - Tier: **Gnawed** (`ilvl <= 64` items ONLY) | **Preserved** (any ilvl, `floor = 1`) | **Ancient** (any ilvl, **`min_mod_level = 40`** — always use Ancient on endgame crafts!).
- **Mechanics & Unveiling**:
  - Maximum **`1` Desecrated mod** per item. Unveiling at the **Well of Souls** draws **3 distinct mod families without replacement** (hypergeometric 3-draw) and lets you pick the best.
  - **`Omen of Abyssal Echoes`**: Grants **1 full reroll of all 3 unveil choices** at the Well of Souls (nearly doubling unveil hit odds!).
  - **Abyssal Boss Omens (`Liege` / `Blackblooded` / `Sovereign`)**: Restrict or guarantee signature **Amanamu**, **Kurgal**, or **Ulaman** Desecrated modifier pools.
  - **`Essence of the Abyss`** (`Rare -> Rare` remove-1 + add-1, combinable with `Sinistral / Dextral Crystallisation`): Places *Mark of the Abyssal Lord* on a 6-mod Rare item, which the next Desecration Bone **100% deterministically replaces** — acting as a targeted 6-mod replacement anchor without risking the other 5 affixes!
  - **`Omen of Light` + Orb of Annulment**: **100% deterministically removes the Desecrated modifier** while leaving all 5 regular/fractured mods untouched — making the 6th-mod Desecration loop completely safe to repeat!

---

## 3. Standard `0.5.5` Endgame Crafting Decision Tree

When planning or explaining a craft (`poe2_craft_plan`), evaluate these six stages in order:

```text
[Stage 0: Base & Trade Check]
  ├─ Check finished item `securable` price on `trade2` (`buy_price_ex`)
  ├─ Check `trade2` for pre-Fractured 1-mod Rare or 2-T1 Magic bases (often cheaper than self-fracturing!)
  └─ Verify exact ilvl breakpoint via `poe2_inspect_base` (e.g. ilvl 81/82 for `+5 Spell Skills`, avoiding unneeded ilvl 82+ trash dilution if T1 unlocks at ilvl 78!)
        │
        ▼
[Stage 1: Magic 2-Mod Opener (Cheap Normal Bases)]
  └─ `Perfect Orb of Transmutation` (floor=70) + `Perfect Orb of Augmentation` (floor=70)
     └─ Miss? Discard base and buy fresh Normal base (costs pennies vs Annul/Chaos!)
        │
        ▼
[Stage 2: Rare 3-Mod Transition]
  ├─ Target mod has a `Greater Essence`? -> Apply `Greater Essence` (100% guaranteed 3rd mod!)
  └─ Otherwise -> `Perfect Regal Orb` (floor=50) [+ `Sinistral/Dextral Coronation` if side matters]
        │
        ▼
[Stage 3: 4-Mod Fracture Checkpoint (High-Budget / Rare T1 Target)]
  └─ Slam 4th mod -> `Fracturing Orb` (`25%` lock on your hardest mod, e.g. `+5 Skills` or `T1 % Phys`)
        │
        ▼
[Stage 4: Finish 3 Prefixes OR 3 Suffixes + Clean Other Side]
  ├─ Force guaranteed T1/exclusive mods via `Perfect Essence` / `Corrupted Essence` / `Verisium Alloy`
  │  paired with `Sinistral` or `Dextral Crystallisation` (protects completed side 100%!)
  ├─ Slam with `Greater/Perfect Exalted Orb` (floor 35/50) + `Sinistral/Dextral/Catalysing Exaltation`
  └─ Strip low-`required_level` bricks deterministically with `Omen of Whittling` + `Perfect Chaos Orb` (floor=50)
        │
        ▼
[Stage 5: Deterministic 6th-Mod Abyssal Desecration]
  ├─ Apply `Ancient Jawbone / Rib / Collarbone` (floor=40) + `Sinistral/Dextral Necromancy` [+ Boss Omen]
  ├─ Unveil at Well of Souls with `Omen of Abyssal Echoes` (best of 3, rerollable once)
  └─ Miss unveil? -> `Omen of Light` + `Orb of Annulment` (100% safe removal) -> repeat Stage 5!
        │
        ▼
[Stage 6: Socketables, Quality & Optional Lock Finisher]
  ├─ Apply 20% Quality / Catalysts + socket Runes / Soul Cores / Idols (overwritable BEFORE lock!)
  └─ Optional high-variance gamble ONLY at very end: `Omen of Sanctification` + Divine OR `Vaal Infuser` / `Omen of Corruption` + Vaal
```

---

## 4. Profitable Craft Discovery & Sunk-Cost Discipline (`poe2_scan_craft_profit`)

When recommending crafts for profit (`poe2 craft profit --budget all|low|mid|high --json`):

1. **Sunk-Cost Restart Rule**: Never burn `Chaos Orb` or `Omen of Sinistral/Dextral Erasure` on 0-hit Magic or unfractured partial Rare bases when buying a fresh Normal base + `Perfect Transmute / Greater Aug` is cheaper (`CraftingPlanner` enforces this automatically).
2. **Deterministic Cross-Side `Crystallisation` Combos**: The highest risk-adjusted ROI crafts in `0.5.5` pad disposable prefixes/suffixes and force guaranteed `Perfect/Corrupted Essence` (*Horror, Hysteria, Breach, Sorcery*) or `Verisium Alloy` (*Swift, Transcendent, Sovereign, Celestial*) mods via `Omen of Sinistral / Dextral Crystallisation`, followed by guaranteed or 3-draw Abyssal Bone (`Jawbone` / `Rib` / `Collarbone` + `Liege` / `Blackblooded` + `Omen of Light`) finishers.
3. **Verify `securable` Exit Depth & Sanitize Thin Quotes**: Always price exits against live `trade2` Instant Buyout (`securable` `p10` / `median`) and active listing count (`commoditized` generic 2-mod rares often sell below orb cost!), and reject `<0.08 Div`-volume exchange dust/RMT outliers (`build_sanitized_crafting_prices`).
