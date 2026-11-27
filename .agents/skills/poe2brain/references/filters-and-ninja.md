# `poe.ninja` Build Intelligence & Agentic Loot Filter Studio (`0.5.5`)

`poe2brain` provides end-to-end Path of Exile 2 (`Patch 0.5.5` / `Forbidden Rites`) build discovery from `poe.ninja` (`poe2brain.ninja`) and a self-validating `.filter` compiler, WCAG visual theme composer, custom `.wav`/`.mp3` soundpack studio, and `NeverSink` auto-healer (`poe2brain.filters`).

---

## 1. `poe.ninja` PoE2 Builds & Multi-Site Character Import (`poe2 ninja ...`)

### Clean Endpoint Architecture (Zero `403` Scraping)
- **Live League & Snapshot Telemetry (`poe2_ninja_snapshots`)**:
  - Queries `GET https://poe.ninja/poe2/api/data/build-index-state` (`30m` TTL) + `GET https://poe.ninja/poe2/api/data/snapshot-versions?league=<slug>` (`15m` TTL) across all 4 modes: `Forbidden Rites`, `HC Forbidden Rites`, `SSF Forbidden Rites`, and `HC SSF Forbidden Rites` (note: `snapshotVersions` moved out of `index-state` in Nov 2026).
- **Multi-Facet Leaderboard Search (`poe2_ninja_search`)**:
  - Combines live mode totals/class playrates with instant multi-facet filtering (`--class`, `--ascendancy`, `--skill`, `--support`, `--unique`, `--keystone`, `--weapon`, `--min-dps`, `--min-ehp`, `--sort dps|ehp|level|es|life|sleeper|mobility`) and `min`/`median`/`p90`/`max` stat distributions over the runtime SQLite `BuildStore` (`~/.cache/poe2brain/builds.sqlite`), synced via ETag from the daily GitHub Actions rolling `build-index` release (`poe2 ninja sync`) plus write-through caching on live imports.
  - Pass `--ssf-viable` (`ssf_viable_first=True`), `--exclude-unique "Temporalis,Mageblood"`, and `--max-uniques <N>` to isolate SSF-achievable builds and surface T0 chase co-dependency warnings; pass `--refresh-live <N>` (`refresh_live_top`) when you want the top `N` matched characters live-refreshed via `poe.ninja`'s raw PoB endpoint.
- **Live Character, Time-Machine Timeline & Share-Link Import (`poe2_ninja_character` & `poe2_pob_load`)**:
  - Fetches live `HTTP 200` `<PathOfBuilding2>` exports from `GET https://poe.ninja/poe2/pob/raw/overview/code?overview={snapshotName}&type=exp&account={acct}&name={char}[&timemachine={label}]` (`30m` disk cache, `1.0s` polite pacing on cache miss, nearest-available time-machine fallback when a character was created mid-league, and archived corpus fallback on `HTTP 404`).
  - Pass `--probe-timeline` (`probe_timeline=True`) to binary-search chronological weekly milestones in `<= 7` probes (`CharacterTimelineReport`, pinpointing `earliest_available_label` and `latest_available_label`).
  - Normalizes `Account#1234` -> `Account-1234` automatically and preserves UTF-8 CJK/Korean character names.
  - Also resolves public PoB2 share links across **`poe.ninja/poe2/pob/<id>`**, **`poe2db.tw/pob/<id>`**, **`maxroll.gg/poe2/pob/<id>`**, and **`pobb.in/<id>`**.
  - **Strict Boundary**: `NinjaBuildsService` refuses any `/api/protected/...` URL (`403` Cloudflare-gated internal endpoint).
- **Slot Persistence & Performance**:
  - Passing `--load-pob --slot main` (`load_into_pob=True`) loads the character into `Pob2Bridge` in `~1s` **and** saves `~/.cache/poe2brain/pob_slots/main.xml`, so subsequent `poe2 pob audit --slot main`, `poe2 trade upgrades --slot main`, and `poe2 filter generate --slot main` commands immediately see the imported build.
  - Keep `audit=False` during bulk inspection (`audit_build()` runs multi-pass `OnFrame` sweeps taking `~15–35s` on 12-skill builds); pass `--audit` (`audit=True`) when evaluating real Pinnacle Boss DPS/EHP.

---

## 2. PoE2 `0.5.5` `.filter` Grammar & 7 Fatal Client-Crash Traps (`poe2_filter_validate`)

Every `.filter` generated or restyled by `poe2brain` is verified by `FilterValidator` against `data/poe2_0.5.5.sqlite` + `data/filter/item_classes_0.5.5.json` (`0.5.5.10` `.datc64` `ItemClasses` & `BaseItemTypes`). Never hand-write raw `.filter` blocks without running `poe2_filter_validate`:

1. **Trap #1 — Internal `.datc64` `item_class` IDs vs `.filter` `Class` Names**:
   - SQLite `base_items.item_class` stores internal IDs (`Body Armour`, `Warstaff`, `SoulCore`, `TowerAugmentation`, `UtilityFlask`, `Map`). Emitting those raw strings crashes PoE2 filter load (`Invalid Class`).
   - Always use `.filter` `ItemClasses.Name` (`"Body Armours"`, `"Quarterstaves"`, `"Augment"`, `"Tablet"`, `"Charms"`, `"Waystones"`), and never emit `Class` for empty-name `Meta Skill Gem` (`Cast on Shock` etc. must match by `BaseType ==`).
2. **Trap #2 — Stale / Unknown `BaseType` or `Class` Aborts Entire Filter**:
   - Any `BaseType` or `Class` matching `0` live `0.5.5` items aborts loading the entire `.filter` file. Notably, `NeverSink-Filter-for-PoE2` `0.10.4a` contains removed bases (`"Fist of Kalguur"`, `"Fragments of the Past"`) and misses `18` `0.5.5` keys/fragments (`"Zarokh's Reliquary Key: Temporalis"`, `"Origin Core"`, `"Origin Cradle"`, Calamity/Crisis Fragments). `NeverSinkAdapter` (`poe2_filter_restyle`) heals these automatically.
3. **Trap #3 — `HasExplicitMod` Operator Whitespace**:
   - PoE2 rejects spaces between operator and count: `HasExplicitMod >= 2 "Merciless"` **crashes**; always glue operator and count: `HasExplicitMod >=2 "Merciless"`.
4. **Trap #4 — Statements After `Continue`**:
   - `Continue` must be the final statement inside a `Show`/`Hide` block.
5. **Trap #5 — Missing Non-Optional `CustomAlertSound` File**:
   - Non-optional `CustomAlertSound "pack/s_plus.wav"` fails filter load if the audio file is missing from the PoE2 filter root, and absolute paths (`/Users/...`, `C:\...`) are rejected. `poe2brain` emits `PlayAlertSound` fallback + `CustomAlertSoundOptional "poe2brain_<pack>/<tier>.wav"` with relative forward-slash paths.
6. **Trap #6 — PoE1-Only Conditions & Removed Currencies**:
   - Block `SocketGroup`, `LinkedSockets`, `MapTier` (use `WaystoneTier 1..16`), `ShaperItem`, `ElderItem`, `SynthesisedItem`, `Replica`, and dormant/removed items (`Orb of Alteration`, `Orb of Scouring`, `* Fossil`, `* Scarab`, `Omen of Homogenising *`, `Recombinator`).
   - Active PoE2 conditions include `WaystoneTier`, `UnidentifiedItemTier` (`1..5`), `Sockets` (`0..6` Rune/Augment socket count, e.g. `Sockets >= 2`), `TwiceCorrupted`, `IsVaalUnique`, `HasVaalUniqueMod`, and `SetFontSize 18..45`.
7. **Trap #7 — Multi-Base Chase Unique Ambiguity**:
   - Ground filters cannot distinguish `Temporalis` from a 1-Exalted unique on `Silk Robe` prior to identification (whereas `"Zarokh's Reliquary Key: Temporalis"` is an exact `BaseType`). Multi-unique bases (`Silk Robe`, `Stellar Amulet`, `Heavy Belt`, `Time-Lost Diamond`) are tiered by `max(price_ex)` across sharing uniques and **never hidden** even on `uber_strict`.

---

## 3. Visual Themes (`--theme` / `--look`) & Custom Soundpack Studio (`--sound-style`)

1. **6 Strictness Tiers (`--strictness`)**:
   - `soft` -> `regular` -> `semi_strict` (default endgame starter) -> `strict` -> `very_strict` -> `uber_strict`.
2. **8 WCAG-Verified Visual Presets (`--theme`) + Natural-Language Composer (`--look`)**:
   - Built-in presets: `forbidden_abyss` (signature `0.5.5` emerald/gold on obsidian-violet), `solaris_gold`, `vaal_blood`, `cyberpunk_neon`, `darkmode_obsidian` (zero-glare matte charcoal, no white backgrounds), `cobalt_arcane`, `zen_minimal`, and `colorblind_safe` (Okabe-Ito luminance + distinct minimap icon shapes per tier).
  - Pass `--look "<prompt>"` and/or `--cvd-safe deuteranopia|protanopia|tritanopia` (`cvd_safe`) to compose a custom palette; `compose_theme` enforces monotonic `S+ >= S >= A >= B >= C >= D` font sizes (`18..45`), Viénot/Machado dichromacy CIELAB $\Delta E^*_{ab}$ separation with **10 unique minimap icon shapes** (zero `Red`/`Green` beam/icon conflicts), and `WCAG >= 4.5:1` (`>= 6.0:1` on `S+`/`S`).
  - Pass `--slots p1,p2` (`slots` on MCP) to union multiple persisted PoB2 builds into one shared co-op party filter (`merge_build_filter_contexts`).
3. **6 Custom Soundpack Styles (`--sound-style`), 2 AM Tinnitus-Safe Mode (`--warm-night`) & Custom Callouts (`--callout`)**:
   - Pure-Python zero-dependency `44.1 kHz` `16-bit` stereo `.wav` polyphonic synthesizers (`--musical-key C..B`, `--scale lydian|major|minor|pentatonic|phrygian|dorian`): `celestial_chime`, `ritual_gong`, `cyber_synth`, `arcade_retro`, `zen_minimal`.
   - Pass `--warm-night` (`warm_night_mode=True`) or `--lowpass-hz <hz>` to apply a 4th-order Butterworth IIR low-pass cascade (`-24 dB/oct`, `<1.5%` energy `>2.5 kHz`) while keeping distinct per-tier rhythmic pulse counts.
   - Hybrid **`voice_callout`** (or per-tier `--callout tier="phrase"` / `custom_callouts` on MCP): mixes a `140ms` FM chime attack with auto-ducked spoken callouts (`127.0.0.1:18455/v1/audio/speech` -> macOS `say`+`afconvert` -> pure-Python formant fallback) + `.mp3` twin export when `ffmpeg` is installed.
4. **Legacy `.filter` Doctor (`poe2_filter_heal`)**:
   - Run `poe2 filter heal <broken.filter> [--out <healed.filter>] --json` (`heal_filter_text`) to auto-repair broken PoE1 / Patch `0.1` filters (`MapTier -> WaystoneTier`, `SocketGroup`/`LinkedSockets`/`Scarab` pruning, singular `Class == "Body Armour" -> "Body Armours"`, spaced/glued `Expert ` / `Advanced ` prefixes, `Dualstring Bow -> Gemini Bow`, `HasExplicitMod >= 2 -> >=2`, `SetFontSize` clamping, and `Continue` hoisting without truncating trailing actions) to `0` errors.
5. **Preview & Local Installation Hygiene**:
   - Always pass `--preview-html <path.html>` when designing a custom look/soundpack so the user gets a single-file Wraeclast ground-drop simulator with inline click-to-play audio.
   - Local PoE2 filter root (`detect_poe2_filter_dir()`): `~/Library/Application Support/Path of Exile 2/` (macOS), `%USERPROFILE%\Documents\My Games\Path of Exile 2\` (Windows), or Proton `compatdata/2694490/.../Path of Exile 2/` (Linux). Never write into `onlinefilters/`, and only pass `--install` (`install=True`) when the user explicitly asks to install into their local PoE2 folder.
