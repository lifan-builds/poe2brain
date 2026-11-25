# poe2brain — 流放之路 2（PoE2）AI 智能引擎

> English documentation: [README.md](README.md)

**`poe2brain`** 是专为 **流放之路 2（Path of Exile 2，Patch `0.5.5` / `Forbidden Rites`）** 打造的 AI Agent 引擎（**`AGPL-3.0-only` 开源协议**，附 [ATTRIBUTION.md](ATTRIBUTION.md) 第 7 条署名条款），原生支持 **Windows、macOS、Linux**。同一套 Python 3.12+ 引擎同时提供 **JSON CLI (`poe2`)** 与 **MCP Server (`poe2brain-mcp`，42 个 1:1 工具)**，可直接接入 Claude Code、Cursor、Antigravity、Codex、Devin 等 AI 编程/游戏助手。

---

## 核心能力（六大模块，拒绝 AI 幻觉）

1. **PoE2 `0.5.5` 真值知识库 & 23 升华 Meta（支持中英文检索）**
   - 内置离线 SQLite + FTS5 数据库，覆盖官方关键词弹窗、49 条防 PoE1 幻觉规则（转化机制、品质独立乘区、50% 格挡上限、偏斜、混沌对护盾双倍、流血/中毒穿盾）、1,711 技能宝石、435 传奇、3,219 基底与 23 升华天赋。
   - 原生支持中文查询（例如 `poe2 wiki ascendancy "风暴编织者"`、`poe2 wiki mechanic "护盾"`、`poe2 wiki gem "拉基亚塔的流光"`）与中文客户端 `Ctrl+C` 装备解析。
2. **无头 Path of Building 2（PoB2）深度计算引擎**
   - 直接运行 MIT `PathOfBuilding-PoE2` 内核：**无需安装 C 编译器**，Windows 自动复用本地 PoB2 或内置 `lua51.dll`！
   - **去水审计 (`pob audit`)**：剥离 PoB 战神虚假勾选，计算真实巅峰 Boss 持续 DPS 与生存。
   - **词缀溯源 (`pob calc-breakdown`)**：精确拆解 `FullDPS` / `EnergyShield` / `ManaCost` 的 `BASE / INC / MORE` 来源（装备/天赋/宝石/配置）。
   - **辅助宝石全量扫榜 (`pob supports`)**：按未切割辅助宝石阶数 (`1-5`) 与血脉辅助 (`max_copies=1`) 自动排查魔力续航、精魂溢出与冲突。
   - **范围珠宝选孔 (`pob radius-jewels`)**：自动遍历全部珠宝孔 × 环径，计算 **可控蜕变 (`Controlled Metamorphosis`)** 与 **抗衡黑暗 (`Against the Darkness`)** 每点天赋收益与省点数。
   - **无损假设沙盒 (`pob what-if`)**：模拟改词缀、换装备、从榜一大佬槽位借单件装备 (`--borrow-from`) 归因测试、三场景矩阵对比，默认自动回滚。
3. **实时市集捡漏、装备估价与仓库一键分拣（零 `POESESSID`）**
   - 无需抓包 Cookie，遵守 GGG 限流协议：自动区分 **金币一口价 (`securable`，离线秒买)** 与 **密语低价钓鱼 (`whisper`)**，去重压价号。
   - **市集自动找提升 (`trade upgrades`)**：用 PoB2 真实边际权重生成查询，自动给未腐化装备补满 `20%` 品质与高级符文后在 PoB2 中跑分，按「真实全部成本性价比」排序。
   - **四倍仓一键分拣 (`trade triage`)**：批量粘贴多件 `Ctrl+C` 装备，自动分成「立即秒卖 / 做装半成品 / 赌鉴定揭秘 / 分解卖店」。
4. **PoE2 `0.5.5` 精确做装概率、最优策略与赚钱做装榜**
   - 严格遵循 `0.5.5` 条件词池、阶梯通货等级下限、动态元素标签屏蔽、4 词破裂检查点、完美精华结晶置换与深渊揭秘（自动拦截 `0.4` 移除的同质化预兆与 `0.5` 移除的重铸）。
   - `craft profit` 与 `econ arbitrage` 自动结合实时物价、流动性与 **每 1 万金币收益** 给出低/中/高本金复利路线。
5. **poe.ninja 抄作业、时光机对比 & 开荒角色导入**
   - 内置 353 套全 23 升华排行榜数据 + 实时 `poe.ninja` 时光机探测 (`--probe-timeline`)，一键对比开荒第 1 周 vs 终局配装与金币洗点花费。
6. **防崩客户端过滤器生成器 + 视觉主题 + 护耳音效工坊**
   - 内置全部 7 类 PoE2 过滤器崩溃规则校验，支持色盲友好 (`--cvd-safe`)、双人组队合并 (`--slots p1,p2`)、凌晨 2 点防耳鸣低通滤波音效 (`--warm-night`)，以及一键修复 PoE1 / 0.1 老过滤器 (`filter heal`)。

---

## Windows / macOS 一键安装（3 分钟上手）

### 1. 安装 `uv`（Python 包管理器）
- **Windows (PowerShell):**
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **macOS / Linux:**
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### 2. 下载并初始化 `poe2brain`
```bash
git clone https://github.com/lifan-builds/poe2brain.git
cd poe2brain
uv sync

# 初始化无头 PoB2 引擎（Windows 自动复用本地 PoB2 或拉取 MIT x64 DLL，无需装编译器！）
uv run poe2 bootstrap pob2

# 检查引擎就绪状态
uv run poe2 version --json
```

---

## 导入你的角色（支持开荒第 1 天，无需 `POESESSID`）

### 方式 A：零登录同步本地 PoB2（推荐，立即可用）
只要你在桌面版 Path of Building (PoE2) 导入并保存过 BD，直接一键加载最新存档：
```bash
uv run poe2 pob import-local --list
uv run poe2 pob import-local --slot main
```

### 方式 B：官方 GGG OAuth 2.1 登录（与 PoB2 相同的官方 PKCE 协议）
```bash
uv run poe2 auth login
uv run poe2 pob import-character --list
uv run poe2 pob import-character "你的角色名" --slot main
```

### 方式 C：`poe.ninja` 链接或 PoB2 分享码
```bash
uv run poe2 ninja character "https://poe.ninja/poe2/builds/..." --load-pob --slot main
uv run poe2 pob load "eN..." --slot main
```

---

## 常用玩法速查

```bash
# 中文查升华/机制/宝石
uv run poe2 wiki ascendancy "风暴编织者"
uv run poe2 wiki mechanic "能量护盾"
uv run poe2 wiki gem "拉基亚塔的流光"

# 去水审计 + 词缀溯源 + 找最强辅助宝石 + 找最佳可控蜕变孔位
uv run poe2 pob audit --slot main
uv run poe2 pob calc-breakdown EnergyShield --slot main
uv run poe2 pob supports --slot main --max-tier 2 --no-lineage
uv run poe2 pob radius-jewels --jewel "Controlled Metamorphosis" --slot main

# 市集自动找 50E 以内一口价最大提升（自动模拟补满 20% 品质与符文）
uv run poe2 trade upgrades --equip-slot "Weapon 1" --max-exalted 50 --securable-only --slot main

# 生成 <=50 字符防翻车异界引路石正则
uv run poe2 wiki regex --slot main

# 为当前 BD 生成护耳、暗黑、防崩客户端的严格过滤器
uv run poe2 filter generate --slot main --strictness strict --theme darkmode_obsidian --warm-night --out my_build.filter
```

---

## 接入 AI 助手（Claude Code / Cursor / Antigravity / Codex）

仓库已内置各主流 Agent 配置文件：
- **Claude Code / Cursor**：[.mcp.json](.mcp.json)
- **Antigravity**：[plugin.json](plugin.json) + [.agents/skills/poe2brain/SKILL.md](.agents/skills/poe2brain/SKILL.md)
- **手动启动 MCP Server**：`uv run poe2brain-mcp`

---

## 合规与开源声明

- 严格遵守 GGG ToS：**绝不读取 `POESESSID`**，绝不读游戏内存，绝不自动点击/自动密语交易，所有市集与 API 请求均经过动态限流器。
- **开源协议 (`AGPL-3.0-only` + Section 7 署名条款)**：本项目代码依据 [LICENSE](LICENSE) (`AGPL-3.0-only`) 与 [ATTRIBUTION.md](ATTRIBUTION.md) 开源；任何复制、修改或网络服务分发版本均须保持 `AGPL-3.0-only` 协议、公开完整源码，并在 README 及 `--version` / MCP 输出中保留署名：
  > Based on poe2brain by lifan-builds (https://github.com/lifan-builds/poe2brain)
- 欢迎依据 [CONTRIBUTING.md](CONTRIBUTING.md) 提交 PR（首次 PR 签署一次性 [CLA](CLA.md)）；商业/非 AGPL 授权请提 Issue 联系 `lifan-builds`。
- 上游数据致谢：`data/` 内置查表数据与无头计算内核均源自合规 MIT 开源上游项目（`PathOfBuilding-PoE2`、`Exiled-Exchange-2`、`ackness/pobr`、`repoe-fork/poe2`、`NeverSink-Filter-for-PoE2`），原 MIT 许可声明已完整保留。
