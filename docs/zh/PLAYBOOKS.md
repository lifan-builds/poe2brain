# 流放之路 2 (`Patch 0.5.5` 禁忌仪式) 六大实战工作流手册

本手册整理了 `poe2brain` 最常用的实战命令与 **PoE1 → PoE2 `0.5.5` 防踩坑铁律**，既可以直接在终端运行，也可以作为你给 AI 助手下达复杂任务时的参考蓝图。

---

## 铁律速查：PoE2 `0.5.5` 与流放 1 最大的 8 个区别

1. **伤害转化只吃转化后加成：** 两步转化（技能 → 装备/天赋），**没有流放 1 的双重加成**；持续伤害（DoT）不可转化。
2. **装备品质是独立乘区 (`MORE`)：** 武器和护甲的 `20%` 品质不再和局部 `% increased` 相加，而是独立放大。
3. **格挡基础上限是 `50%`（不是 75%）：** 偏斜（Deflection）独立于闪避，命中后固定减免 `40%` 击中与异常伤害。
4. **能量护盾（ES）机制巨变：** 混沌伤害对 ES 造成 **2 倍伤害**（但不直接穿盾）；**流血和中毒都会直接穿透 ES 扣生命**（低血非 CI 护盾号极易被流血/中毒秒杀）。
5. **辅助宝石机制 (`0.3+`)：** 普通辅助分 `I / II / III` 阶，**多个技能可以重复装同名普通辅助**；只有**血脉辅助（Lineage Support）**与全局光环精魂辅助全角色限装 1 颗。
6. **通货本位是崇高石 (`Exalted = 1.0`)：** 工艺台、改造石、重铸石、链接石、化石在 PoE2 中均不存在；`0.4` 移除的**同质化预兆（Homogenising Omens）**和 `0.5` 退场的**重铸台/重组预兆**在 `0.5.5` 中已绝版。
7. **词缀阶级 `T1` 是最好 (`display_tier = 1`)：** 自 `0.2.1` 起游戏内展示的 `Tier 1` 即最高阶词缀（内部数据 `internal_rank_asc = 1` 才是最低阶），`poe2brain` 统一展示游戏内口径 `display_tier`。
8. **引路石复活次数：** `0–6` 词条分别对应 `5–0` 次复活（6 词满词条 = `0` 次复活，暴毙即炸图）。

---

## 工作流 1：挖掘冷门高伤高速黑马 BD → 找过渡快照

```bash
# 1. 一键搜索冷门、高真实去水 DPS、高移速黑马 BD（自动排除天价永恒时刻）
uv run poe2 ninja search --preset sleeper --limit 5

# 2. 自定义反向排除（逗号分隔，前缀 ! 表示排除）
uv run poe2 ninja search \
  --ascendancy "!Gemling Legionnaire,!Deadeye" \
  --unique "!Temporalis,!Mageblood" \
  --sort mobility --limit 10

# 3. 探测目标大佬的时间机器快照，自动定位最省钱的过渡周（stepping_stone_label）
uv run poe2 ninja character "<poe.ninja角色链接>" --probe-timeline

# 4. 把过渡周装进 early、终局装进 main，精准对比装备/天赋差异与金币洗点成本
uv run poe2 ninja character "<poe.ninja角色链接>" --timemachine week-2 --load-pob --slot early
uv run poe2 ninja character "<poe.ninja角色链接>" --load-pob --slot main
uv run poe2 pob compare --slot-a early --slot-b main --dewater
```

---

## 工作流 2：PoB2 去水体检 → 词缀溯源 → 辅助与范围珠宝优化

```bash
# 1. 剥离虚假战神配置，暴露真实巅峰 Boss DPS、移速、CI 状态与前 5 大瓶颈
uv run poe2 pob audit --slot main

# 2. 硬核 / 巅峰 Boss 生存审计（查一击线、流血中毒穿盾、护符与防御洗点）
uv run poe2 pob hc-audit --slot main --top-nodes 5

# 3. 拆解任意属性（FullDPS / EnergyShield / ManaCost / CritChance）的精确来源
uv run poe2 pob calc-breakdown FullDPS --slot main

# 4. 全量扫描可用辅助宝石（控制未切割阶数 & 过滤血脉冲突）
uv run poe2 pob supports --slot main --max-tier 3 --exclude-conflicts

# 5. 遍历全天赋盘珠宝孔 × 环径，寻找「可控蜕变」或「抗衡黑暗」最佳插孔
uv run poe2 pob radius-jewels --jewel "Controlled Metamorphosis" --slot main

# 6. 无损假设沙盒：借大佬某件装备或改词缀测收益（不加 --apply 自动回滚）
uv run poe2 pob what-if --slot main --borrow-from ref --borrow-slot "Weapon 1" --scenario-matrix
```

---

## 工作流 3：预算内市集按 PoB2 权重找提升 & 四倍仓一键分拣

```bash
# 1. 自动按 PoB2 真实边际权重搜市集，自动给未腐化装备补满 20% 品质 + 孔位 + 高级符文
#    再按真实总成本与 DPS/EHP 性价比排序
uv run poe2 trade upgrades \
  --equip-slot "Weapon 1" \
  --max-exalted 50 \
  --securable-only \
  --slot main

# 2. 单件装备 Ctrl+C 实时估价（结合全服行情并标记压价异常号）
uv run poe2 trade check --file item.txt

# 3. 四倍仓批量分拣（把多件 Ctrl+C 粘进 dump.txt）
uv run poe2 trade triage --file dump.txt --min-exalted 5
```

---

## 工作流 4：精确做装决策（期望成本 + 脸黑 p80 + 赚钱做装榜）

```bash
# 1. 查看低/中/高本金最赚钱的 0.5.5 做装项目（自带竞争度 competition_level 与细分评分）
uv run poe2 craft profit --budget mid

# 2. 半成品抢救：给已有高阶词缀的装备算下一步概率（默认开启 --protect-existing 防砸毁）
uv run poe2 craft odds --base "Attuned Wand" --ilvl 82 --item-text "$(cat wand.txt)"

# 3. 规划完整做装策略 + 3000 次蒙特卡洛成本分布，直接对比市集买价给 Buy/Craft 结论
uv run poe2 craft plan \
  --base "Attuned Wand" --ilvl 82 \
  --target "T1 Spell Damage" --target "+4 to Level of all Lightning Spell Skills" \
  --buy-exalted 1200 --sims 3000
```

---

## 工作流 5：藏身处金币复利套利 & 50 字符防翻车引路石正则

```bash
# 1. 统一 --budget（支持崇高 500 或神圣 5d）与 --gold 扫描当前套利机会
uv run poe2 econ arbitrage --budget 5d --gold 50000

# 2. 本金滚雪球复利阶梯（例如 500 崇高 + 8 万金币冲击 40 神圣）
uv run poe2 econ arbitrage --budget 500 --gold 80000 --ladder-target-divine 40

# 3. 按当前 BD 弱点自动编译 <=50 字符异界引路石 & 碑牌搜索正则
uv run poe2 wiki regex --slot main
```

---

## 工作流 6：一句大白话生成 0.5.5 语音过滤器 & 修复 NeverSink / 老过滤器

```bash
# 1. 按当前 PoB2 构筑 + 0.5.5 实时物价生成严格过滤器（自动抓 82 物等/超凡底材 + 中文大货语音播报 + 一键安装）
uv run poe2 filter generate \
  --slot main \
  --strictness strict \
  --look "暗黑金色 中文语音播报" \
  --out my_build.filter \
  --preview-html preview.html \
  --install

# 2. 双人开黑共享过滤器（自动合并两人的武器、防具基底与未满抗性）
uv run poe2 filter generate --slots p1,p2 --strictness semi_strict --out duo.filter

# 3. 兼容并魔改 NeverSink / 修复 PoE1 或 0.1 报错老过滤器（清除失效词条与前缀，校验至 0 报错）
uv run poe2 filter restyle strict --slot main --look "赛博霓虹 电子音效" --out ns_custom.filter
uv run poe2 filter heal old_broken.filter --out healed_055.filter
```
