# AI 助手接入指南 & 开箱即用中文提示词

完成 [Windows / macOS 安装](QUICKSTART_WINDOWS.md) 后，把 `poe2brain` 接入你常用的 AI 助手，就能直接用大白话让 AI 调真 PoB2、查实时市集、算做装成本和生成过滤器。

---

## 1. 各大 AI 客户端一键接入

> **零手写 JSON（推荐）：** 在 `poe2brain` 目录直接运行一键配置命令，自动填入 `uv` 绝对路径（根治 Windows GUI 找不到 `uv` 报错），省去手改 JSON：
> ```bash
> # Cherry Studio + DeepSeek/豆包/Qwen（精简 17 核心工具并自动复制到剪贴板，去软件里「从 JSON 导入」粘贴即可）
> uv run poe2 setup-mcp --client cherry --profile core --copy
> # Trae / Cursor / Claude Code / Codex 自动写入配置文件
> uv run poe2 setup-mcp --client trae --profile core
> uv run poe2 setup-mcp --client cursor
> ```
> 详细国内免费方案（Trae 国内版 / Cherry Studio + 硅基流动/DeepSeek）见 **[国内免代理一键装 & 零 JSON 接入指南](CN_FREE_SETUP.md)**。

> 手动配置注意：把下面路径里的 `/ABSOLUTE/PATH/TO/poe2brain`（或 Windows 的 `C:/dev/poe2brain`，**推荐用正斜杠 `/`**）换成你电脑上的实际目录。

### Claude Code（最推荐）
直接在 `poe2brain` 目录启动 `claude` 会自动加载仓库内的 `.mcp.json`；如果想在任意目录都能用，运行一行命令全局注册：

```bash
claude mcp add poe2brain -- uv --directory /ABSOLUTE/PATH/TO/poe2brain run poe2brain-mcp
```

### Cursor
1. 打开 **Settings → Tools & MCP → Add new global MCP server**（或编辑 `~/.cursor/mcp.json`）。
2. 粘贴以下配置并保存，刷新工具列表看到 **45 个 `poe2_*` 工具**（使用 `--profile core` 时为 17 个核心工具） 即成功：

```json
{
  "mcpServers": {
    "poe2brain": {
      "command": "uv",
      "args": ["--directory", "C:/dev/poe2brain", "run", "poe2brain-mcp"]
    }
  }
}
```

### Antigravity
仓库自带完整的 `plugin.json`、`.agents/mcp_config.json` 与 `.agents/skills/poe2brain/SKILL.md`，直接在 Antigravity 打开 `poe2brain` 工作区或安装为本地插件即可自动加载全部 45 个工具与防幻觉规则。

### Cherry Studio / Cline / 通用 MCP 客户端
- **类型 (Type):** `STDIO`
- **命令 (Command):** `uv`
- **参数 (Args):** `--directory`、`C:/dev/poe2brain`、`run`、`poe2brain-mcp`

### Codex CLI / 命令行 Agent
在支持执行终端命令的 Agent 中，直接告知它调用：
```bash
uv --directory C:/dev/poe2brain run poe2 <子命令> --json
```

---

## 2. 12 套开箱即用的中文对话提示词（直接复制发给 AI）

### 挖 BD 与抄作业
1. **挖冷门高速黑马 BD：**
   > 帮我用 `poe2brain` 搜一下当前赛季冷门、不要永恒时刻（Temporalis）、去水真实伤害高且刷图移速快的 Top 3 黑马 BD，并解释每套的核心机制和生存短板。
2. **找低预算开荒/过渡版抄作业：**
   > 查一下这个 poe.ninja 角色链接的时间机器快照，帮我找出它还没买魔血/猎首之前最省钱的过渡周版本，对比过渡期和成型期的装备、天赋差距以及金币洗点花费。
3. **SSF / 独狼可行性排查：**
   > 我在 SSF 掉了「风暴合唱（Choir of the Storm）」，帮我搜不依赖永恒时刻等 T0 传奇就能玩的配置。

### PoB2 深度体检与优化
4. **给自己的 BD 做去水体检：**
   > 把我本地 PoB2 最新保存的 BD 导进 `main` 槽位，跑一次去水审计，告诉我面板水份有多少、当前最急需补的 3 个瓶颈是什么。
5. **硬核 (HC) / 巅峰 Boss 防猝死检查：**
   > 帮我给 `main` 槽位跑一次 HC 巅峰 Boss 猝死审计，检查物理/元素/混沌一击线、流血中毒穿盾风险，再推荐 5 个性价比最高的防御天赋洗点方案和金币花费。
6. **辅助宝石全量扫榜：**
   > 帮我用 PoB2 真算扫一遍我主技能所有能装的辅助宝石（只要 1-2 阶未切割辅助、不要血脉辅助），按真实 DPS 提升排序，标出蓝耗撑不住或者精魂溢出的。
7. **可控蜕变 / 抗衡黑暗选孔：**
   > 帮我算一下「可控蜕变（Controlled Metamorphosis）」插在我天赋盘哪个珠宝孔、选哪个环径每点天赋收益最高、能省多少过路点。
8. **从榜一大佬身上「借装备」归因测试：**
   > 把我自己的号放 `main`，把这个榜一大佬的链接放 `ref`，用 `what-if` 分别单独借他的武器、护身符、技能连线测一遍，告诉我差距主要来自哪一件。

### 市集捡漏、四倍仓分拣与做装
9. **预算内按 PoB2 权重市集找提升：**
   > 我手里有 50 崇高，帮我按 `main` 槽位 PoB2 真实权重去市集搜武器提升，自动模拟补满 20% 品质和高级符文后告诉我买哪把性价比最高。
10. **四倍仓垃圾一键分拣：**
    > 这是我从游戏里批量 `Ctrl+C` 复制的装备文本，按 5 崇高门槛帮我分拣成：立即秒卖 / 值得继续做 / 赌鉴定揭秘 / 直接分解，给出每件的下一步操作。
11. **做装前算账（自己做 vs 直接买）：**
    > 我想拿 82 级「调谐法杖（Attuned Wand）」做 T1 法伤 + 闪电法术技能等级，市集成品卖 1200 崇高，帮我算 PoE2 0.5.5 精确期望成本和 p80 脸黑成本，告诉我该自己做还是直接买。

### 刷图正则与防崩过滤器
12. **一条命令搞定引路石正则 + 护耳暗黑过滤器：**
    > 按我 `main` 槽位生成一条不超过 50 字符的异界引路石防翻车正则；再帮我生成一个严格档位、暗黑金色、红绿色盲友好、凌晨 2 点不刺耳的过滤器并校验 0 报错。
