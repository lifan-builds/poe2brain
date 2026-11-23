# Path of Exile 2 Patch `0.5.5` Mechanics & Anti-Hallucination Reference

Verified against client `4.5.4.3`, Path of Building 2 `v0.23.1` (`CalcOffence.lua`, `CalcDefence.lua`, tree `0_5`), and `repoe-fork/poe2` `keywords.min.json` (769 official GGG `KeywordPopups`). Query `poe2_explain_mechanic` (`poe2 wiki mechanic "<topic>" --json`) whenever an unlisted edge case arises.

## 1. PoE1 / Stale-`0.1` Traps vs Patch `0.5.5` Ground Truth

| Topic | PoE1 / Stale `0.1` Hallucination (WRONG) | PoE2 Patch `0.5.5` Ground Truth (RIGHT) |
| :--- | :--- | :--- |
| **Damage Conversion** | Multi-step chain (`Phys -> Light -> Cold -> Fire`) double-dipping `% increased` at each stage | **2-step max (`Skill -> Gear/Tree`)**; scales **ONLY post-conversion damage types**; DoTs never convert |
| **Weapon / Armour Quality** | Additive with local `% increased Physical / Defences` | **`1% MORE` (multiplicative)** base damage / defence per `1%` Quality (up to `30%` via Vaal Infusers) |
| **Base Crit Damage Bonus** | `+50%` (`150%` total) | **`+100%`** (`200%` total multiplier; tooltips display bonus portion) |
| **Attributes & Leveling** | Life/Mana/Accuracy per level; Str=melee%, Dex=evasion%, Int=ES% | **`0` inherent stats per level**; `Str = +2 Life`, `Dex = +6 Accuracy`, `Int = +2 Mana` ONLY |
| **Support Gem Limit** | `0.1` rule: max 1 copy of ANY Support Gem per character | **`0.3+` Tiered Supports (`I`/`II`/`III`) usable across multiple skills**; `1-per-character` applies **ONLY to Lineage Supports** & persistent global Spirit supports |
| **Buff Reservation** | Mana reservation + `Enlighten` | **Spirit** reserves Buffs, Heralds, Minions, and Meta-Gems (Mana is strictly for active casting/MoM) |
| **Passive Block Cap** | `75%` base maximum Block | **`50%` base maximum Passive Block** (`90%` hard cap with `+max block`); active `Raise Shield` blocks `100%` in `210°` front arc vs Heavy Stun buildup |
| **Chaos vs Energy Shield** | Chaos damage bypasses Energy Shield | **Chaos does NOT bypass ES — it removes `2x` ES** (`Chaos Inoculation` sets `Life = 1` + immune to **Chaos AND Bleeding**) |
| **Bleed & Poison vs ES** | ES absorbs Poison/Bleed before Life (or only Poison bypasses) | **BOTH Bleeding AND Poison bypass Energy Shield** directly to Life |
| **Grim Feast** | Overcaps/doubles maximum Energy Shield via remnants | **Reworked into a Minion Revival remnant skill** — provides **zero Energy Shield** |
| **Spell Suppression** | `50%` damage prevented (`Spell Suppression`) | Replaced in `0.5` by **Deflection**: mitigates **`40%`** of hit & ailment damage (works on attacks & spells, `95%` cap) |
| **Utility Flasks** | 5 Flask slots (Quicksilver, Granite, Silver, etc.) | **1 Life Flask + 1 Mana Flask + up to 3 Belt Charm slots** (zero utility flasks; PoE2 `0.5` `Mageblood` grants 4× permanent `Mage's Legacies` instead of flasks) |
| **Mod Tier Numbers** | `Tier 1` = worst (`.datc64` internal rank) or inconsistent | Since `0.2.1`, in-game **`Tier 1` (`display_tier = 1`) = BEST tier** (`internal_rank_asc = 1` = lowest ilvl) |
| **Waystone Portals** | Always 6 portals per map | **Revives scale inversely with Waystone mod count**: `0 mods = 5`, `2 mods = 4`, `4 mods = 2`, **`6 mods = 0 revives`** |
| **Precursor Tablets** | Socketed inside Precursor Towers on the Atlas | **Placed directly in the Map Device** alongside your Waystone (Towers reveal fog & drop Tablets) |
| **Augment Sockets** | Socketed Runes are permanent and unremovable | Standard Runes, Soul Cores, and Idols **can be overwritten** in `0.5.5` (except `Socket-bound` items like Atziri's Soul Cores) |

---

## 2. Offence, Scaling & Conversion Math

### Two-Step Conversion & "Gain as Extra"
1. **Step 1 (Skill Gem Conversion)**: Innate skill conversion executes first (e.g., `80% of Physical converted to Lightning`). Skill conversion ignores gear/tree conversion modifiers on the converted portion.
2. **Step 2 (Non-Skill Conversion & Gain as Extra)**: All gear, passive tree, and support conversion/gain modifiers execute simultaneously on the remaining pool. If non-skill conversion exceeds `100%`, ratios normalize proportionally. Conversion **never chains a second time** on non-skill sources, and **Damage over Time cannot be converted**.
3. **Post-Conversion Scaling Invariant**: Modifiers (`% increased`, `% more`, penetration, crit) apply **strictly to the final damage type(s)** after conversion/gain. Global `% increased Physical Damage` grants **`0%` benefit** to Physical damage converted to Lightning (note: *local* `% increased Physical Damage` on a martial weapon scales the weapon's base Physical roll before skill conversion reads it).

### Quality, Crit & Inherent Attributes
- **Martial Weapons & Armour Quality**: `FinalBase = RawBaseWithLocalInc * (1 + Quality / 100)`. Standard cap is `20%`; **Vaal Infusers** / corruption can push Quality to `30%`.
- **Jewellery Catalyst Quality**: Scales magnitudes of explicit/implicit modifiers matching the Catalyst tag (`Attack`, `Caster`, `Life`, `Mana`, `Defences`, `Fire`, `Cold`, `Lightning`, `Chaos`, `Physical`, `Speed`, `Attribute`) up to `+20%`.
- **Critical Hits**: Base Crit Damage Bonus is **`+100%`** (`2.0x` total hit multiplier). Modifiers stating `+X% to Critical Damage Bonus` add to `+100%`, whereas `% increased Critical Damage Bonus` multiplies your total bonus multiplicatively against base crit bonus!
- **Attributes**: Characters gain **0 Life, 0 Mana, 0 Accuracy per character level** unless specified on base stats; inherent attribute bonuses are strictly:
  - `1 Strength = +2 Maximum Life`
  - `1 Dexterity = +6 Accuracy Rating`
  - `1 Intelligence = +2 Maximum Mana`

---

## 3. Gems, Spirit, Meta-Gems & Dual Weapon Sets

- **Classes, 23 Ascendancies & Ladder Meta (`TreeData/0_5`)**: Query `poe2_inspect_ascendancy` (`poe2 wiki ascendancy`) and `poe2_meta_ladder` (`poe2 wiki meta`):
  - **8 playable classes / 23 Ascendancies**: **Witch** (*Infernalist, Blood Mage, Lich, Abyssal Lich*), **Ranger** (*Deadeye, Pathfinder*), **Warrior** (*Titan, Warbringer, Smith of Kitava*), **Sorceress** (*Stormweaver, Chronomancer, Disciple of Varashta*), **Huntress** (*Amazon, Ritualist, Spirit Walker*), **Mercenary** (*Witchhunter, Gemling Legionnaire, Tactician*), **Monk** (*Invoker, Acolyte of Chayula, Martial Artist*), and **Druid** (*Oracle, Shaman* — including **Talismans** & shapeshifting forms).
  - **PoE1 / Unreleased `1.0` Traps**: *Shadow, Duelist, Templar, Marauder, Scion, Necromancer, Elementalist, Occultist, Assassin, Trickster, Slayer, Juggernaut, Inquisitor, Hierophant* do **NOT** exist in `0.5.5` (`poe2_inspect_ascendancy` returns structured `LookupError` guidance). Note that `Abyssal Lich` inherits `Lich`'s base tree while replacing 4 notables (*Steward of Kulemak, Umbral Well, Unwilling Offering, Rupture the Flesh*), and notables like `Implanted Gems`, `Brew Concoction`, `Path Seeker`, and `Lucid Dreaming` expand into multiple-choice sub-notables.
- **Gem Sockets**: Sockets live on Skill/Spirit/Meta Gems, NOT gear:
  - Base = `2` support sockets -> **Lesser Jeweller's Orb** (`3`) -> **Greater Jeweller's Orb** (`4`) -> **Perfect Jeweller's Orb** (`5` support sockets = "6-link"; Vaal corruption can add +1 on eligible gems).
- **Support Gem Rules (`0.3+` vs Lineage)**:
  - **Standard Support Gems** drop in **Tiers `I` / `II` / `III`** (higher tiers upgrade values or add mechanics). You **CAN** use multiple copies of the same standard Support Gem across different skills on one character; within a *single* skill, max `1` support per `SupportGemCategory` family.
  - **Lineage Support Gems** (75 endgame drop-only named supports) enforce **`max_copies_per_char = 1`**. Check `GemSummary.max_copies_per_char` via `poe2_inspect_gem`.
- **Spirit & Weapon Set Passives**:
  - **Spirit** reserves persistent Buffs, Heralds, Minions, Blasphemy curses, and Meta-Gems (`Cast on Shock`, `Cast on Crit`, `Cast on Minion Death`, etc., which build **Energy** per trigger condition rather than PoE1 cooldowns).
  - Campaign Spirit bosses grant permanent base Spirit (`+100` total across campaign); extra Spirit comes ONLY from **Sceptres** (implicit `100` base Spirit + `% increased Spirit` local prefixes), **Solar Amulets** / explicit `# to Spirit` on Amulets, **Body Armour**, **Shields**, **Foci**, and specific Uniques/Ascendancies.
  - **Weapon Set I & II (`0 ms` swap)**: Each Weapon Set has independent **Spirit reservation toggles** AND up to **24+ Weapon Set Passive Points** allowing two distinct passive branches (e.g., boss single-target curse/exposure setup on Set II vs clear setup on Set I).

---

## 4. Defenses & Keystones (`CalcDefence.lua` Verified Math)

### Armour (`Before` Resistances!)
$$\text{DR}_{\text{armour}} = \min\!\left(0.90,\; \frac{A}{A + 10 \cdot H}\right)$$
- **Critical Order of Operations**: When a build has *"Armour also applies to Elemental Damage from Hits"*, **Armour mitigation is calculated BEFORE Elemental Resistances** against the raw pre-resistance hit `H` (`CalcDefence.lua:L363`), making Armour drastically weaker against large elemental slams than PoE1 players expect!
- **Negative Armour**: **Armour Break** below `0` causes enemies/players to take amplified Physical hit damage.

### Evasion & Deflection (`0.5` Rework)
- **Evasion** evades **melee, projectile, AND spell hits** by default (`can_evade_spells = 1`), excluding ground/AoE slams unless **Acrobatics** is allocated (`Can Evade all Hits`, **`75% less Evasion Rating`**):
  $$\text{HitChance}_{\%} = \text{clamp}\!\left(\text{round}\!\left(100 \cdot \left(1 - \frac{0.95 \cdot E}{E + 4 \cdot \text{Acc}}\right)\right),\; 5,\; 100\right)$$
- **Deflection** rolls **independently** on un-evaded hits (including AoE hits!) and prevents **`40%` of hit and damaging ailment damage** (`95%` chance cap):
  $$\text{DeflectChance}_{\%} = \text{clamp}\!\left(100 - \text{round}\!\left(\frac{150 \cdot \text{Acc}}{\text{Acc} + 0.12 \cdot D} - 50\right),\; 0,\; 95\right)$$

### Passive & Active Block
- **Passive Block**: Base maximum Block chance is **`50%`** (`90%` hard cap with `+% to maximum Block chance`). Passive Block works against strikes and projectiles (not AoE slams unless **Svalinn** / specific notables).
- **Active Block (`Raise Shield` / `Parry`)**: Blocks `100%` of blockable hits in a `210°` frontal arc (cannot block red-flash Boss Unblockable skills), accumulating your **Heavy Stun** bar; hitting `100%` Heavy Stun triggers a **`3.0s` incapacitated stun** where you cannot act or block.

### Energy Shield, Chaos, Bleed, Poison & Keystones
- **Chaos Damage**: Removes **`2x` Energy Shield** (`200%` damage to ES) — it does **NOT** bypass ES!
- **Bleeding & Poison**: **BOTH bypass Energy Shield** and drain Life directly! Pure high-ES / low-Life builds (without CI) die instantly to Poison or Bleed unless mitigated via Chaos Res / Staunching & Antidote Charms.
- **ES Recharge**: Base `12.5%` of max ES per second after a `4.0s` delay without taking damage to ES.
- **Grim Feast**: Reworked into an active **Minion Revival** remnant skill; **never** recommend Grim Feast for spellcaster/attack ES sustain.
- **Core Keystones (`0.5.5`)**:
  - **Chaos Inoculation (CI)**: `Maximum Life becomes 1; Immune to Chaos Damage AND Bleeding` (solves both Poison AND Bleed ES-bypass completely!).
  - **Eldritch Battery (EB)**: `Converts 100% of Energy Shield to Mana; Doubles Mana Cost of Skills` (flat base ES converts before `% increased ES`, so `% increased ES` no longer scales converted Mana unless explicitly stated!).
  - **Mind Over Matter (MoM)**: `100% of Damage is taken from Mana before Life; 50% less Mana Recovery Rate`.
  - **Glancing Blows**: `Evade chance is Unlucky; Deflect chance is Lucky` (affects Evasion/Deflection, **NOT Block**!).
  - **Avatar of Fire**: `75% of Damage converted to Fire; Deal no Non-Fire Damage`.
  - **Giant's Blood**: Wield Two-Handed Axes/Maces/Swords in one hand; **triples (`3x`) attribute requirements** of Martial Weapons AND **halves inherent Life granted by Strength** (`+1` Life per Strength instead of `+2`).

---

## 5. Ailments, Flasks/Charms & Endgame Atlas (`0.5.5`)

- **Ailments & Thresholds**:
  - **Ignite**: Inflicted via **Flammability** buildup proportional to Fire hit vs enemy Ailment Threshold (`20%` of hit's base Fire damage/s for `4s` once Flammability triggers).
  - **Shock**: Base **`20%` increased damage taken** (`8s` base duration).
  - **Freeze / Electrocute / Pin**: Accumulate separate buildup bars against enemy Ailment Threshold (`0.5 * MonsterMaxLife` base on Pinnacle bosses with diminishing returns after repeated CC).
  - **Culling Strike**: Kills Normal (`<= 35%` Life), Magic (`<= 20%`), Rare (`<= 10%`), and **Unique Bosses at `<= 5%` Life**.
- **Flasks & Belt Charms**: Every character has **1 Life Flask + 1 Mana Flask** (zero utility flasks). Belts carry **1 to 3 Charm slots** (`implicit` + explicit `+1-2 Charm Slots`), auto-triggering on Freeze/Stun/Bleed/Poison/Elemental hits by consuming flask-style Charm charges.
- **Endgame Atlas (`0.5.5`)**:
  - **Waystone Revives**: Inverse to explicit modifier count — `0 mods = 5 revives`, `1–2 mods = 4`, `3–4 mods = 2–3` (`4 mods = 2`), `5 mods = 1`, **`6 mods (full Rare/Vaaled) = 0 revives` (1 death bricks map & XP!)**.
  - **Map-Device Precursor Tablets**: Tablets (`Breach`, `Ritual`, `Delirium`, `Expedition`, `Irradiated`, `Overseer`, `Abyss`) have limited `# uses remaining` and go **directly into the Map Device** alongside your Waystone. Completing **Precursor Towers** reveals Atlas fog-of-war and drops Tablets.
  - **Masters of the Atlas (`0.5`)**: **Jado**, **Hilda**, and **Doryani** provide specialized Atlas mission encounters and endgame progression rewards.
  - **Runes of Aldur & Socketables**: **Verisium Anvil** and **Kalguuran Remnant** sequences (`poe2_inspect_augment`) craft specialized runes/augments; Shaman ascendancy unlocks **`Bonded:`** bonus lines on socketed Runes/Idols. Standard socketables can be freely overwritten in `0.5.5` unless flagged `Socket-bound`.
