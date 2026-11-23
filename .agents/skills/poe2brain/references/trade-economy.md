# PoE2 Live Trade & Currency Economy Reference (`Forbidden Rites` / `0.5.5`)

## 1. Currency Baseline & Active Leagues

- **Exalted Orb (`exalted = 1.0`) is the PoE2 unit currency**, NOT Chaos Orb!
  - In `Forbidden Rites` (SC primary challenge league), `1 Divine ≈ 453–462 Exalted` and `1 Chaos ≈ 36.5 Exalted` (`1 Divine ≈ 12.7 Chaos`) — **always pull live rates via `poe2_currency_price` (`EconomyService.get_reference_rates`)**, never hardcode ratios.
  - **Tiered Basic Currencies** trade independently at distinct premiums: `exalted` vs `greater-exalted-orb` vs `perfect-exalted-orb`, `chaos` vs `greater-chaos-orb` vs `perfect-chaos-orb`, `regal` vs `greater-regal-orb` vs `perfect-regal-orb`, `greater-orb-of-transmutation` / `perfect-orb-of-transmutation`, `greater-orb-of-augmentation` / `perfect-orb-of-augmentation`.
- **Active League IDs** (identical across `trade2`, `poe2scout`, and `poe.ninja`):
  - `Forbidden Rites` (default SC challenge league)
  - `HC Forbidden Rites`
  - `Runes of Aldur` / `HC Runes of Aldur` (active secondary/event leagues)
  - `Standard` / `Hardcore`

---

## 2. The 3-Tier Pricing Router & Endpoint Traps

`poe2brain` routes every pricing request to the fastest authoritative source with disk TTL caching (`~/.cache/poe2brain/http`):

### Tier 1: Bulk Currency, Omen, Essence, Gem & Base-Unique Snapshot (`15m` TTL)
Combines two zero-CAPTCHA public APIs:
1. **`https://api.poe2scout.com`** (**NEVER** `https://poe2scout.com/api`, which returns HTML):
   - `/poe2/Leagues/{league}/ReferenceCurrencies` and `/poe2/Leagues/{league}/Items` (1,288+ priced Currencies & Uniques in Exalted).
   - `/Currencies/ByCategory` across 17 categories: `currency`, `ritual` (50 Omens), `essences` (82), `ultimatum` (Soul Cores), `runes` (143), `abyss` (Gnawed/Preserved/Ancient Bones & Desecrated mats), `uncutgems`, `lineagesupportgems` (75), `fragments`, `breach`, `delirium`, `expedition`, `verisium` (Alloys), `incursion`, `idol`, `vaultkeys`, `vaal` — with 7-day `PriceLogs`.
   - `/Uniques/ByCategory` across 7 categories (`accessory`, `armour`, `flask`, `jewel`, `map`, `weapon`, `sanctum`) with `CurrentPrice`, `CurrentQuantity`, and **`IsChanceable`**.
2. **`https://poe.ninja/poe2/api/economy/exchange/current/overview?league={league}&type={type}`**:
   - Adds **real in-game Currency Exchange liquidity** (`volumePrimaryValue` in Divines, `maxVolumeCurrency`) and 7-day sparklines across 13 exchange types (`Currency`, `Fragments`, `Runes`, `Essences`, `Ritual`, `SoulCores`, `Delirium`, `Breach`, `Expedition`, `Verisium`, `Abyss`, `UncutGems`, `LineageSupportGems`).
   - **CRITICAL TRAP**: `poe.ninja`'s PoE2 Stash/Unique endpoint (`/poe2/api/economy/stash/...`) returns **`HTTP 404`** (`"indexed": false`). Always use `poe2scout` for base Unique prices and Official `trade2` for roll-sensitive or corrupted Uniques!

### Tier 2: Live Official `trade2` Search & Fetch (`https://www.pathofexile.com/api/trade2/...`)
- **Zero Auth Required**: `100%` unauthenticated (`no POESESSID`), requiring only `poe2brain`'s descriptive `USER_AGENT` and dynamic `GGGRateLimiter` (`X-Rate-Limit-Ip` + `X-Rate-Limit-Ip-State` headers with `+1.0s` safety buffer).
- Static metadata (`/data/{leagues,stats,items,static,filters}`) cached `4h`.
- Used for all **Rare equipment, Magic/Fractured crafting bases, Waystones, Precursor Tablets, Jewels, Cut/21-20/6-socket Skill Gems, Sanctified items, and roll-specific/corrupted Uniques**.

### Tier 3: Clipboard (`Ctrl+C` / `Ctrl+Alt+C`) Price-Check & PoB2 Upgrade Loop
- `TradeAdvisor.price_check_item` parses `Ctrl+C` text -> auto-routes stackables/uncorrupted base Uniques to Tier 1 and affix-bearing items to Tier 2 (`roll_tolerance = 0.9` default on top affixes) -> returns `PriceCheckResult` + clickable `https://www.pathofexile.com/trade2/search/poe2/<League>/<query_id>`.

---

## 3. Instant Buyout (`securable`) vs In-Person (`whisper`) & Price-Fixer Defense

Path of Exile 2 supports **asynchronous Gold-fee Instant Buyout** alongside legacy whisper trade:

| Listing Field / Mode | Instant Buyout (`SaleType.SECURABLE`) | In-Person Whisper (`SaleType.WHISPER`) |
| :--- | :--- | :--- |
| **`trade2` `status.option`** | `"securable"` (or subset of `"available"`) | Included in `"available"` / `"online"` |
| **JSON Marker in `/fetch`** | `listing.fee` is an integer **Gold cost** | `listing.fee` is `null`; `listing.whisper` present |
| **Seller Online Required?** | **No** — instant purchase even when seller is offline | **Yes** — seller must invite & trade manually |
| **Price-Fixer Risk** | **Near-zero** (underpriced items get bought instantly by bots/players) | **High** (AFK/DND accounts list at 1–5 Exalted and never reply) |

### Mandatory Pricing Rules for Agents
1. **Default search `status.option` to `"available"`**, then inspect **both** `distribution.securable_min_exalted` / `securable_median_exalted` and overall `median_exalted`.
2. **Never quote raw `min_exalted` on `"available"` as market value** when `securable_min_exalted >> min_exalted` or `fixer_warnings` is non-empty (`Trade2Client` deduplicates repeated listings from the same `account_name`).
3. **Factor Gold `fee`**: High-ilvl equipment and bulk maps carry non-trivial Gold buyout fees (`listing.fee`); surface the Gold fee alongside Exalted price when recommending `securable` Instant Buyouts.
4. **When feeding `CraftingPlanner` (`buy_price_ex`)**: Anchor `buy_price_ex` to `securable_min_exalted` / `securable_median_exalted` (or deduplicated `p25_exalted` if only whisper listings exist) so fake 1-Exalt whisper fixers don't trick the planner into saying *"Buy instead of Craft"*.

---

## 4. `Ctrl+C` / `Ctrl+Alt+C` Parsing & Stat-Matching Nuances

`poe2brain.trade` (`item_parser.py` + `stat_matcher.py`, backed by `Exiled-Exchange-2` MIT data + official `trade2/data/stats`) handles eight PoE2-specific traps automatically — keep these in mind when constructing manual filters:

1. **Wiki Markup in `trade2/fetch`**: Official fetch descriptions contain `[MaximumLife|maximum Life]` or `[CriticalHitChance|Critical Hit Chance]` tags -> stripped cleanly via `strip_wiki_markup`.
2. **Hidden Desecrated Mods in `explicitMods`**: `trade2/fetch` can return Abyssal Desecrated mods inside `item.explicitMods` with `mod.domain == "desecrated"` or `flags.desecrated == true` rather than `item.desecratedMods`. Always inspect `ParsedMod.domain`.
3. **Jewellery Catalyst Double-Counting**: In `trade2/fetch`, Catalyst `%` quality (`Quality (Caster Modifiers): +20%`) is **already multiplied into the displayed explicit stat numbers**. `trade2_fetch_to_pob2_text` omits Jewellery Catalyst headers when emitting PoB2 text so PoB2 does not multiply them a second time (whereas Weapon/Armour `Quality: +20%` IS emitted because PoB2 computes base weapon/armour stats from base item tables).
4. **`Spirit` Filter Split**: On **Sceptres, Body Armour, Shields, and Foci**, total Spirit (implicit + local `% increased`/`+#` Spirit) is queried via **`equipment_filters.filters.spirit`**. On **Amulets** (e.g. Solar Amulet or explicit `# to Spirit`), query the stat filter (`pseudo` / `explicit` Spirit) because Amulets have no equipment Spirit property block!
5. **Local vs Global Stat Collisions**: Lines like `#% increased Attack Speed` or `#% increased Energy Shield` map to distinct `local` (Weapons/Armour) vs `global` (Jewellery/Gloves) `trade2` stat IDs. Whenever ambiguous, `stat_matcher` emits `{ "type": "count", "value": {"min": 1}, "filters": [...] }` so neither variant is missed.
6. **Reduced -> Negated Increased**: PoE2 merges many `"#% reduced X"` lines into negative values of `"#% increased X"` (`negate: true`), requiring sign and min/max bound inversion (`max` instead of `min`).
7. **Sockets & Shaman `Bonded:` Runes**: Equipment rune sockets format as `Sockets: S S` (`equipment_filters.filters.rune_sockets`), whereas Skill/Meta Gem support sockets format as `Sockets: G G G G G` (`misc_filters.filters.gem_sockets`). Socketed Runes/Idols appear under `rune` (`"Augment"`) stat group, including Shaman `Bonded: ...` lines.
8. **Meta Gems Omit `Item Class:`**: Clipboard text for Meta Gems (`Cast on Shock`, `Cast on Crit`) starts directly at `Rarity: Gem` with no `Item Class:` header.
