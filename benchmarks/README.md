# PoE2Bench-0.5.5 — Accuracy Benchmark (Without vs. With `poe2brain`)

`PoE2Bench-0.5.5` is a **22-question, 75-check**, offline-deterministic A/B benchmark measuring how accurately a frontier LLM answers real **Path of Exile 2 (`Patch 0.5.5` / `Forbidden Rites`)** questions **without** vs. **with** `poe2brain`.

## Headline Results (`gpt-6.2-astra`)

Evaluated on **2026-11-26** with the exact same model (`gpt-6.2-astra`), zero web search, and identical local plain-text fixture files (decoded `.xml` PoB2 build, `.filter` file, Chinese Tencent clipboard item, and multi-item triage dump) supplied to both arms. Raw responses and per-check scores are archived in [`results/codex_0_5_5.json`](results/codex_0_5_5.json).

| Metric | Without `poe2brain` | With `poe2brain` | Delta |
| :--- | :---: | :---: | :---: |
| **Exact Task Pass Rate** | 6/22 (`27.3%`) | **22/22 (`100.0%`)** | **+72.7 pp (`3.7×`)** |
| **Sub-Check Factual Accuracy** | 38/75 (`50.7%`) | **75/75 (`100.0%`)** | **+49.3 pp** |
| **PoE1 / Stale-Patch Hallucinations** | 4/22 (`18.2%`) | **0/22 (`0.0%`)** | **-18.2 pp (zero)** |

### Accuracy by Pillar

| Pillar | Questions | Without `poe2brain` | With `poe2brain` |
| :--- | :---: | :---: | :---: |
| **Mechanics & Defences** | 4 | 2/4 (`50.0%`) | **4/4 (`100.0%`)** |
| **Roster & Bases (EN + `zh-CN`)** | 3 | 2/3 (`66.7%`) | **3/3 (`100.0%`)** |
| **Crafting (Rules & Exact Mod-Pool Math)** | 4 | 0/4 (`0.0%`) | **4/4 (`100.0%`)** |
| **Atlas & Waystone Regex** | 4 | 1/4 (`25.0%`) | **4/4 (`100.0%`)** |
| **Headless PoB2 (Audit, Swaps, HC, Runes, What-If)** | 6 | 0/6 (`0.0%`) | **6/6 (`100.0%`)** |
| **Loot Filters** | 1 | 1/1 (`100.0%`) | **1/1 (`100.0%`)** |

---

## Sample Questions & Observed Failures vs. `poe2brain`

| ID | Question | Without `poe2brain` (`gpt-6.2-astra`) | With `poe2brain` (`gpt-6.2-astra`) |
| :--- | :--- | :--- | :--- |
| **Q02** | Base passive Block cap, `Glancing Blows` keystone effect, and `Deflection` mitigation % / cap in `0.5.5` | **FAIL [Hallucination]:** Hallucinated PoE1 `75%` block cap / PoE1 rules | **PASS (3/3):** Exact `50%` block cap, `Evade Unlucky / Deflect Lucky`, `40%` mitigation / `95%` cap |
| **Q09** | `ilvl 82` Rare `Warmonger Bow`: compare `Greater` vs. `Perfect Exalted Orb` odds for T1 `% Physical Damage` (weight 25) | **FAIL (0/4):** Cannot compute `0.5.5` affix pool weights; symbolic formula only | **PASS (4/4):** Greater = `40,482` weight (`0.0618%`, `1,619.28` tries) vs. Perfect = `24,782` (`0.1009%`, `991.28` tries, `1.634×`) |
| **Q10** | `0/2/4/6`-mod Waystone revives, Tablet placement, 3 Patch `0.5` Atlas Masters, and `0.5.5` Sacred Blooms | **FAIL [Hallucination]:** Wrong `6 revives` on 0-mod; couldn't name Masters or Sacred Blooms | **PASS (4/4):** `5/4/2/0` revives, Map Device Tablets, `Jado / Hilda / Doryani` (1 active), Viridian Wildwood / Azmeri Wisps |
| **Q12** | Compile a `<=50`-char PoE2 negated stash regex hiding all 6 CI/ES (`es_ci`) brick Waystone hazards | **FAIL [Hallucination]:** Put `!` inside quotes (`"!m player r\|..."`, invalid PoE2 syntax) | **PASS (1/1):** Compiled **`!"buil\|ever\|of l\|pene\|weak\|player r"`** (**36 chars**, 6/6 hazards, `0` FP/FN) |
| **Q13** | Dewater real `0.5.5` Amazon Ice Shot PoB2 XML: raw vs. sustainable Pinnacle DPS, `%` inflation, #1 culprit, Spirit / Chaos / Phys Max Hit | **FAIL (2/5):** Read stale XML header (`237,936`), could not compute sustainable Pinnacle DPS or culprit delta | **PASS (5/5):** Exact `237,993 → 121,674` (**+95.6%** inflation), #1 culprit `multiplierRage = 30` (`+48.36%`), `-96` Spirit, `21%` Chaos, `3,340` Phys |
| **Q17** | Legal 6-link dewatered support swap (`--best-swap --dewater`) on 5/5-filled Ice Shot + verify `Elemental Focus` Int req | **FAIL (0/5):** Could not find weakest support, legal swap DPS, or recalculated Int requirement | **PASS (5/5):** Auto-replaced weakest #4 **`Stoicism II`** with **`Hit and Run`** (`121,674 → 170,344`, **`+40.0%`**); #2 `Elemental Focus` (`+23.98%`, `attributes_met_after = true`) |
| **Q18** | HC Pinnacle audit: dewatered Max Hits, reconciled Spirit, and top 2 reachable dewatered defensive passives | **FAIL (2/4):** Could not compute reachable dewatered EHP notables | **PASS (4/4):** Max Hits `3,340 / 11,296 / 3,877`; Spirit `139 / 235 / -96`; **`Shimmering` (`+21.0%`)** & **`Careful Consideration` (`+31.6%`)** |
| **Q19** | Rune & Soul Core optimization (`--offline-prices`) with Socket-bound lock & true net PoB2 swap delta | **FAIL (2/5):** Used stale `+7%` Tacati value (wasting 7 cores instead of 3); could not compute net DPS/EHP swap delta | **PASS (5/5):** Locked `Kolr's Hunt` (`11/12` overwriteable), **3 `Soul Core of Tacati`** (`21% → 60%` Chaos), true net swap **`-43,108 DPS / +2,017 EHP`**, offline cost `null` |
| **Q22** | Exact `what-if` (`multiplierRage=0` only) DPS drop + offline quad-tab triage (`--no-live-trade`) | **FAIL (1/4):** Mental math divided by `1.30` (`183,020`, `-23.1%` — off by ~9.5 pp due to Berserk/Rage effect scaling!) | **PASS (4/4):** Exact **`237,993 → 160,418 (-32.6%)`**; offline triage (`heuristic_offline`): Normal `ilvl 82` wand = `3P/3S` open, ring = `salvage_or_vendor` |

---

## Full 22-Question Breakdown

| ID | Pillar | Task | Without `poe2brain` | With `poe2brain` |
| :--- | :--- | :--- | :---: | :---: |
| Q01 | Mechanics & Defences | Energy Shield, Chaos 2x Loss, Bleed/Poison Bypass & CI Immunity | FAIL (2/3) | **PASS** (3/3) |
| Q02 | Mechanics & Defences | Passive Block Cap (50%), Glancing Blows & Deflection Layer | FAIL (2/3) [Hallucination] | **PASS** (3/3) |
| Q03 | Mechanics & Defences | Damage Conversion Scaling, Multiplicative Quality & Base Crit Bonus | PASS (3/3) | **PASS** (3/3) |
| Q04 | Mechanics & Defences | Support Gem Duplication (0.3+), Lineage Supports & Grim Feast Rework | PASS (3/3) | **PASS** (3/3) |
| Q05 | Roster & Bases | Patch 0.5.5 Playable Classes, Ascendancy Count & Abyssal Lich | FAIL (2/3) | **PASS** (3/3) |
| Q06 | Roster & Bases | Stale 'Expert ' Base Trap, Augment Overwriting & Talisman Weapon Class | PASS (3/3) | **PASS** (3/3) |
| Q07 | Crafting | Modifier Tier Direction (T1=Best), Tiered Orb Floors & Magic Alchemy | FAIL (2/3) [Hallucination] | **PASS** (3/3) |
| Q08 | Crafting | Removed 0.4/0.5 Levers, Perfect Essences, Fracturing & Whittling | FAIL (2/3) | **PASS** (3/3) |
| Q09 | Crafting | Exact Conditional Mod-Pool Math: ilvl 82 Warmonger Bow Exalt Slam | FAIL (0/4) | **PASS** (4/4) |
| Q10 | Atlas & Regex | Waystone Revives, Map Device Tablets, Atlas Masters & Sacred Blooms | FAIL (1/4) [Hallucination] | **PASS** (4/4) |
| Q11 | Atlas & Regex | Item Rarity (IIR) Currency Scaling, Negative-IIR & Sekhemas Breakpoint | FAIL (1/3) | **PASS** (3/3) |
| Q12 | Atlas & Regex | Compiled <=50-Char PoE2 Stash Safe-Waystone Regex (es_ci) | FAIL (0/1) [Hallucination] | **PASS** (1/1) |
| Q13 | Headless PoB2 | Headless PoB2 Dewatering Audit (Amazon Ice Shot 0.5.5) | FAIL (2/5) | **PASS** (5/5) |
| Q14 | Headless PoB2 | Headless PoB2 Exact Support Gem Sweep (Amazon Ice Shot 0.5.5) | FAIL (0/3) | **PASS** (3/3) |
| Q15 | Loot Filters | Patch 0.5.5 Loot Filter 7-Trap Crash Validation | PASS (5/5) | **PASS** (5/5) |
| Q16 | Crafting | Patch 0.4/0.5 Crafting Additions: Verisium Alloys, Architect's Orb & Liquid Emotions | FAIL (0/3) | **PASS** (3/3) |
| Q17 | Headless PoB2 | Headless PoB2 Legal 6-Link Dewatered Support Swap (--best-swap --dewater) | FAIL (0/5) | **PASS** (5/5) |
| Q18 | Headless PoB2 | Headless PoB2 HC Pinnacle Audit, Spirit Reconciliation & Dewatered Defensive Passives | FAIL (2/4) | **PASS** (4/4) |
| Q19 | Headless PoB2 | Headless PoB2 Rune Resist Tetris with Socket-Bound Augment Lock | FAIL (2/5) | **PASS** (5/5) |
| Q20 | Roster & Bases | Bilingual Chinese (zh-CN) Lookup & Tencent Spaced Clipboard Parsing | PASS (3/3) | **PASS** (3/3) |
| Q21 | Atlas & Regex | Compiled <=50-Char Waystone Juicing Regex (120%/20%) & Max-Res Math | PASS (2/2) | **PASS** (2/2) |
| Q22 | Headless PoB2 | Headless PoB2 Exact What-If Delta & Offline Quad-Tab Triage | FAIL (1/4) | **PASS** (4/4) |

---

## Methodology & Fairness Guarantees

1. **Same Model, Zero Web Search:** Both arms run the exact same model (`gpt-6.2-astra`) offline with zero web search.
2. **No Artificial Encoding Handicap:** For PoB2 (`Q13`–`Q14`, `Q17`–`Q19`, `Q22`), Loot Filter (`Q15`), Chinese clipboard (`Q20`), and Triage (`Q22`) tasks, the **Baseline** arm receives the full decoded plain-text files ([`amazon_ice_shot_0_5_5.xml`](fixtures/amazon_ice_shot_0_5_5.xml), [`broken_legacy.filter`](fixtures/broken_legacy.filter), [`zh_bow_0_5_5.txt`](fixtures/zh_bow_0_5_5.txt), [`triage_dump_0_5_5.txt`](fixtures/triage_dump_0_5_5.txt)) in an isolated temp directory — never raw compressed `eN...` base64 blobs.
3. **100% Offline-Deterministic Ground Truth:** Every check in [`poe2_bench_0_5_5.json`](poe2_bench_0_5_5.json) is verified against offline Patch `0.5.5` SQLite gamedata, headless LuaJIT PoB2, `ModPoolEngine`, `WaystoneRegexEngine`, `TradeAdvisor`, and `FilterValidator` (zero drifting live currency prices).
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
