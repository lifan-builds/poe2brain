# 国内零卡壳安装 & 国产免费客户端（Cherry Studio / Trae + DeepSeek）零 JSON 接入指南

专门针对**国内网络环境**和**不想折腾命令行 / 付费 Claude 账号**的玩家整理：全程走国内高校/大厂开源镜像，自动生成配置，不用手改 JSON。

---

## 一、Windows 一行命令免代理安装（自动走阿里 / 南京大学镜像）

打开 Windows **PowerShell**，复制下面任意一种方式运行：

### 方式 1：已下载仓库到本地后运行（最稳）
```powershell
cd C:\dev\poe2brain
powershell -ExecutionPolicy ByPass -File .\scripts\install_cn.ps1
```

### 方式 2：全自动拉取 + 安装（走国内镜像站直连，无需 Git 也不卡 GitHub）
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://cn.poe2brain.clifan.com/install_cn.ps1 | iex"
```
*(国内代码与文档自动同步镜像：**[GitCode poe2brain](https://gitcode.com/gcw_rYL140He/poe2brain)**；只想免安装先体验的话，直接浏览器打开国内直连轻量网页版 **[cn.poe2brain.clifan.com](https://cn.poe2brain.clifan.com/)**。)*

脚本会自动完成以下 5 件事：
1. 写入国内镜像配置（**南京大学** `python-build-standalone` 镜像 + **阿里云** PyPI 镜像，失败自动切清华源）。
2. 自动安装 `uv`（优先通过国内 PyPI / 镜像下载，不死磕 `astral.sh`）。
3. 克隆或 ZIP 下载 `poe2brain` 到 `C:\dev\poe2brain`。
4. 执行 `uv sync` 并运行 `uv run poe2 bootstrap pob2 --cn`（自动优先复用本机已装的桌面版 PoB2，未装时自动走国内镜像拉取 MIT 运行时）。
5. 自动运行 `uv run poe2 doctor` 确认一切就绪。

> **排错自检：** 如果安装或使用中遇到网络或路径问题，运行：
> ```powershell
> uv run poe2 doctor --network
> ```
> 会直接定位问题并给出可复制的修复命令。

---

## 二、国产免费客户端「零 JSON 接入」（两条零成本路线）

国产模型或免费额度（**DeepSeek-V3/R1、豆包、通义千问 Qwen、硅基流动免费额度**）上下文窗口比旗舰模型紧，建议接入时加上 **`--profile core`** —— 只暴露最常用的 **17 个核心工具**（覆盖机制查询、PoB2 去水审计/换辅助/配符文、引路石正则、市集查价/找提升/四倍仓分拣、做装算账、黑马 BD、过滤器生成与校验），减少约 62% 工具 Schema 占用，调用又准又省钱。

### 路线 A：Cherry Studio + DeepSeek / 硅基流动免费额度（最像微信/豆包聊天）

1. 下载安装免费开源客户端 **[Cherry Studio](https://www.cherry-ai.com/)**，在模型设置里填入你的 DeepSeek / 硅基流动 / 阿里云百炼 / 火山方舟 API Key。
2. 在 `poe2brain` 目录运行一行命令（自动解析 `uv.exe` 绝对路径并复制到系统剪贴板，彻底避免 Windows 找不到 `uv` 报错）：
   ```powershell
   uv run poe2 setup-mcp --client cherry --profile core --copy
   ```
3. 打开 **Cherry Studio → 设置 → MCP 服务器 → 添加服务器 → 从 JSON 导入**，按 `Ctrl+V` 粘贴并保存打开开关即可开问！

### 路线 B：Trae 国内版（免费自带国产大模型，国内直连）

1. 安装 **Trae 国内版**（国内直连下载，自带免费国产大模型）。
2. 在 `poe2brain` 目录运行一行命令自动写入 Trae MCP 配置：
   ```powershell
   uv run poe2 setup-mcp --client trae --profile core
   ```
3. 重启 Trae 即可直接用中文问它配装、算做装和调过滤器。

### 其他客户端一键写入（Cursor / Claude Code / Codex）

```powershell
# Cursor（推荐旗舰模型用全量 45 工具，省略 --profile 即可）
uv run poe2 setup-mcp --client cursor

# Claude Code / Codex
uv run poe2 setup-mcp --client claude
uv run poe2 setup-mcp --client codex

# 一次性全部配置（可加 --dry-run 先预览不写文件）
uv run poe2 setup-mcp --client all --profile core --dry-run
```

---

## 三、1.0 正式版一键更新 & 干净卸载

### 一行命令平滑升级（1.0 上线后用这句）
```powershell
uv run poe2 update --cn --force-pob2
```
自动完成 `git pull` + 国内源 `uv sync` + 刷新 PoB2 运行时，不用重装。

### 彻底卸载（纯绿色，不写 Windows 注册表）
```powershell
uv run poe2 uninstall
```
自动清理各 AI 客户端里的 `poe2brain` MCP 配置以及 `%USERPROFILE%\.cache\poe2brain` 缓存目录，最后按提示删掉 `C:\dev\poe2brain` 文件夹即 100% 清理干净。
