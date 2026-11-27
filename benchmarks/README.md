# PoE2Bench-0.5.5 — Accuracy Benchmark (Without vs. With `poe2brain`)

`PoE2Bench-0.5.5` is a 16-question, offline-deterministic A/B benchmark measuring how accurately a frontier LLM answers real **Path of Exile 2 (`Patch 0.5.5` / `Forbidden Rites`)** questions **without** vs. **with** `poe2brain`.

## Headline Results (`gpt-6.2-astra`)

Evaluated on **2026-11-26** with the exact same model (`gpt-6.2-astra`), zero web search, and identical local plain-text fixture files (decoded `.xml` PoB2 build + `.filter` file) supplied to both arms. Raw responses and per-check scores are archived in [`results/codex_0_5_5.json`](results/codex_0_5_5.json).

| Metric | Without `poe2brain` | With `poe2brain` | Delta |
| :--- | :---: | :---: | :---: |
| **Exact Task Pass Rate** | 5/16 (31.2%) | **16/16 (100.0%)** | **+68.8 pp (3.2×)** |
| **Sub-Check Factual Accuracy** | 28/52 (53.8%) | **52/52 (100.0%)** | **+46.2 pp** |
| **PoE1 / Stale-Patch Hallucinations** | 3/16 (18.8%) | **0/16 (0.0%)** | **-18.8 pp** |

### Accuracy by Pillar

| Pillar | Questions | Without `poe2brain` | With `poe2brain` |
| :--- | :---: | :---: | :---: |
| **Mechanics & Defences** | 4 | 2/4 (50.0%) | **4/4 (100.0%)** |
| **Roster & Bases** | 2 | 1/2 (50.0%) | **2/2 (100.0%)** |
| **Crafting (Rules & Exact Mod-Pool Math)** | 4 | 1/4 (25.0%) | **4/4 (100.0%)** |
| **Atlas & Waystone Regex** | 3 | 0/3 (0.0%) | **3/3 (100.0%)** |
| **Headless PoB2 (Dewatering & Supports)** | 2 | 0/2 (0.0%) | **2/2 (100.0%)** |
| **Loot Filters** | 1 | 1/1 (100.0%) | **1/1 (100.0%)** |

---

## Sample Questions & Observed Failures vs. `poe2brain`

| ID | Question | Without `poe2brain` (`gpt-6.2-astra`) | With `poe2brain` (`gpt-6.2-astra`) |
| :--- | :--- | :--- | :--- |
| **Q02** | Base passive Block cap, Glancing Blows keystone effect, and Deflection mitigation % / chance cap in `0.5.5` | **FAIL [Hallucination]:** Claimed PoE1's `75%` block cap, PoE1 Glancing Blows (`doubles Block, take 65%`), and wrong `75%` Deflect cap | **PASS (3/3):** Exact `50%` base block cap, Glancing Blows = `Evade Unlucky / Deflect Lucky`, Deflection = `40%` mitigation / `95%` cap |
| **Q05** | Total playable classes & Ascendancies in `0.5.5`, Druid/Shadow availability, and `Abyssal Lich` lineage | **FAIL (2/3):** Miscounted `20 Ascendancies` instead of `23` | **PASS (3/3):** Exact `8` playable classes, `23` Ascendancies, Druid playable / Shadow unavailable, `Abyssal Lich` = Witch replacing Lich |
| **Q07** | Tooltip tier direction, Greater/Perfect orb floors, and whether Orb of Alchemy works on Magic items | **FAIL [Hallucination]:** Claimed `No — Orb of Alchemy requires a Normal item` (PoE1 / pre-0.5 rule) | **PASS (3/3):** T1 = best, floors `55/70` & `35/50`, and `Yes` — Alchemy wipes & rerolls Magic items as Rare since `0.5` |
| **Q09** | `ilvl 82` Rare `Warmonger Bow`: compare Greater vs. Perfect Exalted Orb odds for T1 `Increased Physical Damage` (weight 25) | **FAIL (0/4):** Cannot compute `0.5.5` affix pool weights; returned symbolic formulas only | **PASS (4/4):** Greater = `40,482` weight (`0.0618%`, `1,619.28` attempts) vs. Perfect = `24,782` weight (`0.1009%`, `991.28` attempts, `1.634×`) |
| **Q10** | `0/2/4/6`-mod Waystone revives, Tablet placement, 3 Patch `0.5` Atlas Masters, and `0.5.5` Sacred Blooms | **FAIL [Hallucination]:** Said `6 revives` on 0-mod (wrong — `5`) and couldn't name Atlas Masters or Sacred Blooms | **PASS (4/4):** `5/4/2/0` revives, Tablets in Map Device, `Jado / Hilda / Doryani` (1 active), Sacred Blooms spawn Viridian Wildwood / Azmeri Wisps |
| **Q12** | Compile a verified `<=50`-char PoE2 negated stash regex for CI/ES (`es_ci`) brick Waystones | **FAIL (0/1):** Emitted `!maximum.player.res\|penetrat\|weakness\|cannot.take` (49 chars, missing PoE2 `!"..."` syntax) | **PASS (1/1):** Compiled `!"ever\|pene\|weak\|player r"` (**26 chars**, verified `0` false negatives & `0` false positives) |
| **Q13** | Dewater real `0.5.5` Amazon Ice Shot PoB2 XML: raw vs. sustainable Pinnacle DPS, % inflation, #1 culprit, Spirit/Chaos/Phys Max Hit | **FAIL (2/5):** Read stale XML header (`237,936`), could not compute sustainable Pinnacle DPS, `%` inflation, or culprit delta | **PASS (5/5):** Exact `237,993 -> 121,674` (**+95.6%** inflation), #1 culprit `multiplierRage = 30` (`+48.36%`), `pinnacle_ready = false`, `-96` Spirit, `21%` Chaos Res, `3,340` Phys Max Hit |
| **Q14** | Rank all compatible `0.5.5` Support Gems for the Amazon Ice Shot PoB2 build by DPS gain & Spirit overflow | **FAIL (1/3):** Could not simulate the 254 compatible supports; reported `0%` | **PASS (3/3):** **`Hit and Run`** ranked #1 (`237,993 -> 332,692`, **+39.8%** DPS), `spirit_overflow = false` |
| **Q16** | Patch `0.5` Verisium Alloys, Vaal Temple `Architect's Orb`, and Liquid Emotion Rare Jewel crafting | **FAIL (0/3):** Admitted zero recall for all three Patch `0.4`/`0.5` systems | **PASS (3/3):** Verisium Anvil rune sequences (`-1 random / +1 guaranteed Alloy mod`), `Architect's Orb` double-corrupts corrupted gear/jewels, Liquid Emotions swap `-1/+1` on Rare Basic/Time-Lost Jewels |

---

## Full 16-Question Breakdown

| ID | Pillar | Task | Without `poe2brain` | With `poe2brain` |
| :--- | :--- | :--- | :---: | :---: |
| Q01 | Mechanics & Defences | Energy Shield, Chaos 2x Loss, Bleed/Poison Bypass & CI Immunity | FAIL (2/3) | **PASS** (3/3) |
| Q02 | Mechanics & Defences | Passive Block Cap (50%), Glancing Blows & Deflection Layer | FAIL (0/3) [Hallucination] | **PASS** (3/3) |
| Q03 | Mechanics & Defences | Damage Conversion Scaling, Multiplicative Quality & Base Crit Bonus | PASS (3/3) | **PASS** (3/3) |
| Q04 | Mechanics & Defences | Support Gem Duplication (0.3+), Lineage Supports & Grim Feast Rework | PASS (3/3) | **PASS** (3/3) |
| Q05 | Roster & Bases | Patch 0.5.5 Playable Classes, Ascendancy Count & Abyssal Lich | FAIL (2/3) | **PASS** (3/3) |
| Q06 | Roster & Bases | Stale 'Expert ' Base Trap, Augment Overwriting & Talisman Weapon Class | PASS (3/3) | **PASS** (3/3) |
| Q07 | Crafting | Modifier Tier Direction (T1=Best), Tiered Orb Floors & Magic Alchemy | FAIL (2/3) [Hallucination] | **PASS** (3/3) |
| Q08 | Crafting | Removed 0.4/0.5 Levers, Perfect Essences, Fracturing & Whittling | PASS (3/3) | **PASS** (3/3) |
| Q09 | Crafting | Exact Conditional Mod-Pool Math: ilvl 82 Warmonger Bow Exalt Slam | FAIL (0/4) | **PASS** (4/4) |
| Q10 | Atlas & Regex | Waystone Revives, Map Device Tablets, Atlas Masters & Sacred Blooms | FAIL (1/4) [Hallucination] | **PASS** (4/4) |
| Q11 | Atlas & Regex | Item Rarity (IIR) Currency Scaling, Negative-IIR & Sekhemas Breakpoint | FAIL (1/3) | **PASS** (3/3) |
| Q12 | Atlas & Regex | Compiled <=50-Char PoE2 Stash Safe-Waystone Regex (es_ci) | FAIL (0/1) | **PASS** (1/1) |
| Q13 | Headless PoB2 | Headless PoB2 Dewatering Audit (Amazon Ice Shot 0.5.5) | FAIL (2/5) | **PASS** (5/5) |
| Q14 | Headless PoB2 | Headless PoB2 Exact Support Gem Sweep (Amazon Ice Shot 0.5.5) | FAIL (1/3) | **PASS** (3/3) |
| Q15 | Loot Filters | Patch 0.5.5 Loot Filter 7-Trap Crash Validation | PASS (5/5) | **PASS** (5/5) |
| Q16 | Crafting | Patch 0.4/0.5 Crafting Additions: Verisium Alloys, Architect's Orb & Liquid Emotions | FAIL (0/3) | **PASS** (3/3) |

---

## Methodology & Fairness Guarantees

1. **Same Model, Zero Web Search:** Both arms run the exact same model (`gpt-6.2-astra`) offline with zero web search.
2. **No Artificial Encoding Handicap:** For PoB2 (`Q13`–`Q14`) and Loot Filter (`Q15`) tasks, the **Baseline** arm receives the full decoded plain-text [`fixtures/amazon_ice_shot_0_5_5.xml`](fixtures/amazon_ice_shot_0_5_5.xml) and [`fixtures/broken_legacy.filter`](fixtures/broken_legacy.filter) in an isolated temp directory — never raw compressed `eN...` base64 blobs.
3. **100% Offline-Deterministic Ground Truth:** Every check in [`poe2_bench_0_5_5.json`](poe2_bench_0_5_5.json) is verified against offline Patch `0.5.5` SQLite gamedata, headless LuaJIT PoB2, `ModPoolEngine`, `WaystoneRegexEngine`, and `FilterValidator` (no drifting live currency prices).
4. **Negation-Aware Hallucination Detection:** [`scorer.py`](scorer.py) normalizes markdown/Unicode formatting and checks the local clause around every trap pattern so explicitly negated references (e.g., *"50%, not 75% like PoE1"*) are never falsely flagged.

## Reproduce or Run on Your Own Model

```bash
# Re-score the archived gpt-6.2-astra A/B predictions deterministically (0 API calls)
uv run python benchmarks/run_benchmark.py score benchmarks/results/codex_0_5_5.json

# Run a fresh A/B evaluation via Codex CLI
uv run python benchmarks/run_benchmark.py run --runner codex-cli --mode both \
  --out benchmarks/results/my_run.json

# Run against any OpenAI-compatible API endpoint
OPENAI_API_KEY=... uv run python benchmarks/run_benchmark.py run \
  --runner openai --model <model-id> --mode both \
  --out benchmarks/results/openai_run.json
```
