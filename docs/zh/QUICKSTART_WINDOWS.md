# Windows 零编译 3 分钟保姆级安装指南

> 适用环境：Windows 10 / Windows 11 (64-bit) · **无需安装 Visual Studio、MinGW 或任何 C 编译器**。

`poe2brain` 专门针对 Windows 玩家做了零编译适配：初始化无头 Path of Building 2 引擎时，会自动检测你电脑里已安装的桌面版 PoB2，或者自动拉取合规的 MIT 开源 x64 `lua51.dll` 运行时，全程纯命令行 3 分钟跑通。

> **懒人最快方式（不懂命令行也能用）：** 如果你已经装了 **Cursor / Claude Code / Codex / Antigravity**，直接把下面这句话发给它，让 AI 全自动帮你执行下面所有步骤：
>
> > *「帮我把 `https://github.com/lifan-builds/poe2brain` 克隆到 `C:\dev\poe2brain`，按 `docs/zh/QUICKSTART_WINDOWS.md` 装好 `uv`、执行 `uv sync` 和 `uv run poe2 bootstrap pob2`，最后跑 `uv run poe2 version --json` 确认成功并帮我配好 MCP。」*

---

## 1. 安装 Git 与 `uv`

1. 如果还没有安装 Git，从 [git-scm.com](https://git-scm.com/download/win) 下载并默认下一步安装即可。
2. 打开 **PowerShell**（按 `Win + X` → 选择「终端 / PowerShell」），安装 `uv`：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装完成后**重新打开一个新的 PowerShell 窗口**，验证 `uv --version` 有输出即可。

---

## 2. 克隆仓库并安装依赖

选择一个**不含空格或特殊符号**的目录（推荐 `C:\dev\` 或 `D:\poe2\`）：

```powershell
mkdir C:\dev -Force
cd C:\dev
git clone https://github.com/lifan-builds/poe2brain.git
cd poe2brain
uv sync
```

> **提示：** `uv sync` 会自动在项目内下载 Python 3.12+ 独立虚拟环境，不会污染你系统里原有的 Python。

---

## 3. 一键初始化无头 PoB2 引擎（零编译器）

```powershell
uv run poe2 bootstrap pob2
```

这一步会自动完成两件事（缓存目录位于 `%USERPROFILE%\.cache\poe2brain\pob2`）：
1. 拉取 MIT 开源 `PathOfBuildingCommunity/PathOfBuilding-PoE2` 计算源码（`TreeData/0_5`）。
2. 自动配置 x64 LuaJIT 运行时：
   - 如果你已经安装了桌面版 **Path of Building (PoE2)**，会优先直接复用它的原生库；
   - 否则自动下载轻量 MIT x64 `lua51.dll` 运行时（无需任何编译步骤）。

验证就绪状态：

```powershell
uv run poe2 version --json
```

看到 `"pob2_runtime_ready": true` 和 `"gamedata_db_ready": true` 即表示全部安装成功（也可以直接运行 `uv run poe2 status` 查看人类可读摘要）！

---

## 4. 三种方式导入你自己的角色（零 `POESESSID`）

### 方式 A（最简单，开荒第 1 天立即可用）：同步本地桌面版 PoB2 存档

只要你在电脑版 **Path of Building (PoE2)** 里保存过 BD（默认保存在 `文档\Path of Building (PoE2)\Builds\`），直接运行：

```powershell
# 列出本地所有已保存的 .xml 存档（按最近修改时间排序）
uv run poe2 pob import-local --list

# 直接把最新修改的那个 BD 导入到 main 槽位
uv run poe2 pob import-local --slot main

# 或者按文件名关键词导入
uv run poe2 pob import-local "冰射追猎" --slot main
```

### 方式 B：官方 GGG OAuth 2.1 登录（与 PoB2 相同的官方安全授权）

```powershell
uv run poe2 auth login
uv run poe2 pob import-character --list
uv run poe2 pob import-character "你的角色名" --slot main
```

### 方式 C：导入 `poe.ninja` 链接或 PoB2 分享码

```powershell
uv run poe2 ninja character "https://poe.ninja/poe2/builds/..." --load-pob --slot main
uv run poe2 pob load "eN..." --slot main
```

---

## 5. 常见问题排查（FAQ）

### Q0：卡在第三步报错 `Failed to spawn: poe2` 怎么办？
99% 是下面三个原因之一，按顺序检查即可解决：

1. **当前 PowerShell 目录不在 `poe2brain` 文件夹里**（比如新开了一个终端窗口，或者下载 ZIP 解压后多套了一层 `poe2brain-main\poe2brain-main`）：
   先确认当前目录里能看到 `pyproject.toml` 文件：
   ```powershell
   cd C:\dev\poe2brain
   dir pyproject.toml
   ```
   如果提示找不到 `pyproject.toml`，说明还在外层目录，再 `cd` 进有 `pyproject.toml` 的那一层。
2. **上一步 `uv sync` 没有完整跑完**（例如国内网络超时中断，导致 `.venv\Scripts\poe2.exe` 没生成）：
   先配好代理（见下方 Q2），再重新执行：
   ```powershell
   uv sync
   uv run poe2 bootstrap pob2
   ```
3. **不想切目录，直接显式指定路径运行**：
   ```powershell
   uv --directory C:\dev\poe2brain run poe2 bootstrap pob2
   ```

### Q1：PowerShell 输出中文乱码或报编码错怎么办？
`poe2brain` 已内置 UTF-8 自动重配置；如果旧版 Windows 终端仍显示乱码，先执行：

```powershell
chcp 65001
$env:PYTHONUTF8 = "1"
```

### Q2：国内网络下载 GitHub 慢或超时怎么办？
如果你本地开了代理工具（例如端口 `7890`），在当前 PowerShell 窗口设置代理后再执行 `git clone` / `poe2 bootstrap pob2`：

```powershell
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
```

### Q3：我的 PoB2 存档放在自定义盘符，`import-local` 找不到怎么办？
通过 `--dir` 指定目录即可：

```powershell
uv run poe2 pob import-local --dir "D:\Games\PoB2\Builds" --list
```

### Q4：会被 GGG 封号吗？
**不会。** `poe2brain` 严格只做局外计算、剪贴板文本分析和官方公开 API 查询：绝不读 `POESESSID`、绝不读游戏内存、绝不自动点交易或自动发密语。

---

下一步：把 `poe2brain` 接到你的 AI 助手（Claude Code / Cursor / Cherry Studio / Antigravity），见 **[AI 助手接入与中文提示词指南](AGENT_SETUP.md)**。
