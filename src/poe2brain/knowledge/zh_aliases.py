# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Bilingual zh-CN / zh-TW <-> English PoE2 0.5.5 terminology & clipboard normalizer.

Zero external dependencies; zero overhead on ASCII-only English queries.
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")

# Longest-match-first bilingual terms covering PoE2 0.5.5 classes, ascendancies,
# mechanics, popular skills, chase uniques, currencies, crafting levers, and slang.
ZH_TO_EN_ALIASES: dict[str, str] = {
    # --- 8 Playable Classes & 23 Ascendancies (zh-CN + zh-TW + community slang) ---
    "风暴编织者": "Stormweaver",
    "風暴編織者": "Stormweaver",
    "时空术士": "Chronomancer",
    "時空術士": "Chronomancer",
    "瓦拉什塔信徒": "Disciple of Varashta",
    "瓦拉什塔門徒": "Disciple of Varashta",
    "瓦拉什塔门徒": "Disciple of Varashta",
    "宝石军团": "Gemling Legionnaire",
    "寶石軍團": "Gemling Legionnaire",
    "宝石兵团": "Gemling Legionnaire",
    "宝石军团兵": "Gemling Legionnaire",
    "猎巫人": "Witchhunter",
    "獵巫人": "Witchhunter",
    "战术家": "Tactician",
    "戰術家": "Tactician",
    "武术家": "Martial Artist",
    "武術家": "Martial Artist",
    "祈求者": "Invoker",
    "祈求師": "Invoker",
    "夏乌拉侍僧": "Acolyte of Chayula",
    "夏烏拉侍僧": "Acolyte of Chayula",
    "夏乌拉门徒": "Acolyte of Chayula",
    "狱火师": "Infernalist",
    "獄火師": "Infernalist",
    "炼狱师": "Infernalist",
    "血法师": "Blood Mage",
    "血法師": "Blood Mage",
    "深渊巫妖": "Abyssal Lich",
    "深淵巫妖": "Abyssal Lich",
    "巫妖": "Lich",
    "锐眼": "Deadeye",
    "銳眼": "Deadeye",
    "追猎者": "Pathfinder",
    "追獵者": "Pathfinder",
    "亚马逊": "Amazon",
    "亞馬遜": "Amazon",
    "灵行者": "Spirit Walker",
    "靈行者": "Spirit Walker",
    "仪式师": "Ritualist",
    "儀式師": "Ritualist",
    "泰坦": "Titan",
    "战争使者": "Warbringer",
    "戰爭使者": "Warbringer",
    "奇塔弗铁匠": "Smith of Kitava",
    "奇塔弗鐵匠": "Smith of Kitava",
    "先知": "Oracle",
    "神谕": "Oracle",
    "神諭": "Oracle",
    "萨满": "Shaman",
    "薩滿": "Shaman",
    # Base classes
    "女巫": "Witch",
    "魔法师": "Sorceress",
    "魔法師": "Sorceress",
    "术士": "Sorceress",
    "游侠": "Ranger",
    "遊俠": "Ranger",
    "女猎手": "Huntress",
    "女獵手": "Huntress",
    "战士": "Warrior",
    "戰士": "Warrior",
    "雇佣兵": "Mercenary",
    "傭兵": "Mercenary",
    "武僧": "Monk",
    "德鲁伊": "Druid",
    "德魯伊": "Druid",
    # PoE1 / Unreleased 1.0 trap classes (keep mapped so trap guardrails fire!)
    "圣堂武僧": "Templar",
    "聖堂武僧": "Templar",
    "野蛮人": "Marauder",
    "野蠻人": "Marauder",
    "暗影刺客": "Shadow",
    "决斗者": "Duelist",
    "決鬥者": "Duelist",
    "贵族": "Scion",
    "貴族": "Scion",
    # --- Core PoE2 Mechanics, Defences, Keystones & Ailments ---
    "混沌免疫": "Chaos Inoculation",
    "异能魔力": "Eldritch Battery",
    "異能魔力": "Eldritch Battery",
    "心灵升华": "Mind Over Matter",
    "心靈昇華": "Mind Over Matter",
    "苦痛灵曲": "Pain Attunement",
    "苦痛靈曲": "Pain Attunement",
    "巨人之血": "Giant's Blood",
    "能量护盾": "Energy Shield",
    "能量護盾": "Energy Shield",
    "护盾": "Energy Shield",
    "護盾": "Energy Shield",
    "精魂": "Spirit",
    "偏斜": "Deflection",
    "格挡": "Block",
    "格擋": "Block",
    "护甲": "Armour",
    "護甲": "Armour",
    "闪避": "Evasion",
    "閃避": "Evasion",
    "感电": "Shock",
    "感電": "Shock",
    "冰缓": "Chill",
    "冰緩": "Chill",
    "冰冻": "Freeze",
    "冰凍": "Freeze",
    "点燃": "Ignite",
    "點燃": "Ignite",
    "流血": "Bleed",
    "中毒": "Poison",
    "暴击": "Critical",
    "暴擊": "Critical",
    "穿透": "Penetration",
    "抗性": "Resistance",
    # --- Popular Skills, Supports & Chase Uniques ---
    "拉基亚塔的流光": "Rakiata's Flow",
    "拉基亞塔的流光": "Rakiata's Flow",
    "可控蜕变": "Controlled Metamorphosis",
    "可控蛻變": "Controlled Metamorphosis",
    "抗衡黑暗": "Against the Darkness",
    "深渊之暗": "Against the Darkness",
    "无中生有": "From Nothing",
    "無中生有": "From Nothing",
    "卡兰德之触": "Kalandra's Touch",
    "卡蘭德之觸": "Kalandra's Touch",
    "莫里奥": "Morior Invictus",
    "莫里奧": "Morior Invictus",
    "猎首": "Headhunter",
    "獵首": "Headhunter",
    "魔血": "Mageblood",
    "暴击施放": "Cast on Critical",
    "暴擊時施放": "Cast on Critical",
    "大法师": "Archmage",
    "大法師": "Archmage",
    "魔力残片": "Mana Remnants",
    "魔力殘片": "Mana Remnants",
    "闪电箭": "Lightning Arrow",
    "閃電箭": "Lightning Arrow",
    "电球": "Spark",
    "電球": "Spark",
    "火花": "Spark",
    "彗星": "Comet",
    "电弧": "Arc",
    "電弧": "Arc",
    "冰霜炸弹": "Frost Bomb",
    "冰霜炸彈": "Frost Bomb",
    "幽灵舞步": "Ghost Dance",
    "鬼舞": "Ghost Dance",
    # --- Currencies, Crafting Levers & Atlas ---
    "神圣石": "Divine Orb",
    "神聖石": "Divine Orb",
    "崇高石": "Exalted Orb",
    "混沌石": "Chaos Orb",
    "机会石": "Orb of Chance",
    "機會石": "Orb of Chance",
    "富豪石": "Regal Orb",
    "瓦尔宝珠": "Vaal Orb",
    "瓦爾寶珠": "Vaal Orb",
    "破裂石": "Fracturing Orb",
    "分裂石": "Fracturing Orb",
    "剥离石": "Orb of Annulment",
    "剝離石": "Orb of Annulment",
    "完美精华": "Perfect Essence",
    "完美精華": "Perfect Essence",
    "预兆": "Omen",
    "預兆": "Omen",
    "同质化": "Homogenising",
    "同質化": "Homogenising",
    "重铸": "Recombinator",
    "重鑄": "Recombinator",
    "亵渎": "Desecration",
    "褻瀆": "Desecration",
    "引路石": "Waystone",
    "先驱石板": "Precursor Tablet",
    "先驅石板": "Precursor Tablet",
    "符文": "Rune",
    "灵魂核心": "Soul Core",
    "靈魂核心": "Soul Core",
    "法术技能等级": "Spell Skills",
    "法術技能等級": "Spell Skills",
    "闪电法术技能": "Lightning Spell Skills",
    "閃電法術技能": "Lightning Spell Skills",
    "施法速度": "Cast Speed",
    "法术伤害": "Spell Damage",
    "法術傷害": "Spell Damage",
}

_SORTED_ZH_TERMS: tuple[tuple[str, str], ...] = tuple(
    sorted(ZH_TO_EN_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True)
)

_ZH_ITEM_CLASS_MAP: dict[str, str] = {
    "法杖": "Wands",
    "法器": "Foci",
    "聚能器": "Foci",
    "弓": "Bows",
    "弩": "Crossbows",
    "細杖": "Quarterstaves",
    "细杖": "Quarterstaves",
    "長杖": "Staves",
    "长杖": "Staves",
    "權杖": "Sceptres",
    "权杖": "Sceptres",
    "胸甲": "Body Armours",
    "護甲": "Body Armours",
    "頭部": "Helmets",
    "头盔": "Helmets",
    "手套": "Gloves",
    "鞋子": "Boots",
    "靴子": "Boots",
    "腰帶": "Belts",
    "腰带": "Belts",
    "戒指": "Rings",
    "護身符": "Amulets",
    "项链": "Amulets",
    "項鍊": "Amulets",
    "盾牌": "Shields",
    "箭袋": "Quivers",
    "珠寶": "Jewels",
    "珠宝": "Jewels",
    "引路石": "Waystones",
    "通貨": "Stackable Currency",
    "通货": "Stackable Currency",
    "技能寶石": "Skill Gems",
    "技能宝石": "Skill Gems",
    "輔助寶石": "Support Gems",
    "辅助宝石": "Support Gems",
}

_ZH_RARITY_MAP: dict[str, str] = {
    "普通": "Normal",
    "魔法": "Magic",
    "稀有": "Rare",
    "傳奇": "Unique",
    "传奇": "Unique",
    "暗金": "Unique",
    "寶石": "Gem",
    "宝石": "Gem",
    "通貨": "Currency",
    "通货": "Currency",
}


def contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def translate_zh_query(text: str) -> str:
    """Translate Chinese PoE2 terms in a user query into canonical English terms."""
    if not text or not contains_cjk(text):
        return text
    out = text
    for zh, en in _SORTED_ZH_TERMS:
        if zh in out:
            out = out.replace(zh, f" {en} ")
    return " ".join(out.split())


def normalize_zh_item_clipboard(raw_text: str) -> str:
    """Normalize Traditional/Simplified Chinese PoE2 Ctrl+C item headers to English."""
    if not raw_text or not contains_cjk(raw_text):
        return raw_text

    out_lines: list[str] = []
    for line in raw_text.splitlines():
        s = line.strip()
        if s.startswith(("物品類別:", "物品类别:", "物品種類:", "物品种类:")):
            val = s.split(":", 1)[1].strip()
            out_lines.append(f"Item Class: {_ZH_ITEM_CLASS_MAP.get(val, val)}")
            continue
        if s.startswith(("稀有度:", "稀有度：")):
            val = s.replace("：", ":", 1).split(":", 1)[1].strip()
            out_lines.append(f"Rarity: {_ZH_RARITY_MAP.get(val, val)}")
            continue
        if s.startswith(("物品等級:", "物品等级:", "物品等級：", "物品等级：")):
            val = s.replace("：", ":", 1).split(":", 1)[1].strip()
            out_lines.append(f"Item Level: {val}")
            continue
        if s.startswith(("品質:", "品质:", "品質：", "品质：")):
            val = s.replace("：", ":", 1).split(":", 1)[1].strip()
            val = val.replace("(已強化)", "(augmented)").replace("(已强化)", "(augmented)")
            out_lines.append(f"Quality: {val}")
            continue
        if s.startswith(("插槽:", "插槽：")):
            val = s.replace("：", ":", 1).split(":", 1)[1].strip()
            out_lines.append(f"Sockets: {val}")
            continue
        if s.startswith(("需求:", "需求：")):
            val = s.replace("：", ":", 1).split(":", 1)[1].strip()
            val = (
                val.replace("等級", "Level")
                .replace("等级", "Level")
                .replace("力量", "Str")
                .replace("敏捷", "Dex")
                .replace("智慧", "Int")
                .replace("智力", "Int")
            )
            out_lines.append(f"Requires: {val}")
            continue
        if s in ("已汙染", "已污染", "已腐化"):
            out_lines.append("Corrupted")
            continue
        if s in ("未鑑定", "未鉴定"):
            out_lines.append("Unidentified")
            continue

        # Normalize mod tags such as (破裂) / (褻瀆) / (符文) / (基底)
        norm_line = (
            line.replace("(破裂)", "(fractured)")
            .replace("（破裂）", "(fractured)")
            .replace("(褻瀆)", "(desecrated)")
            .replace("(亵渎)", "(desecrated)")
            .replace("（亵渎）", "(desecrated)")
            .replace("(符文)", "(rune)")
            .replace("（符文）", "(rune)")
            .replace("(固定詞綴)", "(implicit)")
            .replace("(基底)", "(implicit)")
            .replace("(附魔)", "(enchant)")
        )
        out_lines.append(norm_line)

    return "\n".join(out_lines)
