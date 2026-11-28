# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
#
# Windows 国内免代理一键安装脚本（自动配置南京大学 Python 镜像 + 阿里云 PyPI 镜像 + GitHub 镜像兜底）
# 用法示例：
#   powershell -ExecutionPolicy ByPass -File .\scripts\install_cn.ps1
#   powershell -ExecutionPolicy ByPass -c "irm https://cn.poe2brain.clifan.com/install_cn.ps1 | iex"

param(
    [string]$InstallDir = "C:\dev\poe2brain",
    [string]$CnGitRepoUrl = "https://gitcode.com/lifan-builds/poe2brain.git",
    [string]$GitRepoUrl = "https://github.com/lifan-builds/poe2brain.git",
    [string]$GitHubMirror = "https://ghfast.top/",
    [string]$PyPiMirror = "https://mirrors.aliyun.com/pypi/simple/",
    [string]$PythonMirror = "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/",
    [switch]$NoWriteUvToml
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Step($msg) {
    Write-Host "`n==> $msg" -ForegroundColor Cyan
}

function Add-PathIfPresent($dir) {
    if ($dir -and (Test-Path $dir) -and ($env:Path -notlike "*$dir*")) {
        $env:Path = "$dir;$env:Path"
    }
}

Write-Host "============================================================" -ForegroundColor Yellow
Write-Host "  poe2brain Windows 国内零卡壳一键安装器 (Patch 0.5.5)" -ForegroundColor Yellow
Write-Host "  阿里云 PyPI + 南京大学 Python 镜像 + GitHub 镜像兜底" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Yellow

# 1. 配置 uv 国内镜像环境变量与 uv.toml
Write-Step "1/5 配置国内镜像源（阿里云 PyPI + 南京大学 Python 运行时）"
$env:UV_INDEX_URL = $PyPiMirror
$env:UV_PYTHON_INSTALL_MIRROR = $PythonMirror
$env:POE2BRAIN_GITHUB_MIRROR = $GitHubMirror

if (-not $NoWriteUvToml) {
    $uvConfigDir = Join-Path $env:APPDATA "uv"
    $uvTomlPath = Join-Path $uvConfigDir "uv.toml"
    if (-not (Test-Path $uvTomlPath)) {
        New-Item -ItemType Directory -Force -Path $uvConfigDir | Out-Null
        @"
python-install-mirror = "$PythonMirror"

[[index]]
url = "$PyPiMirror"
default = true
"@ | Set-Content -Path $uvTomlPath -Encoding UTF8
        Write-Host "已写入国内镜像配置：$uvTomlPath" -ForegroundColor Green
    } else {
        Write-Host "检测到已有 $uvTomlPath，本次仅通过环境变量启用国内镜像。" -ForegroundColor DarkGray
    }
}

# 2. 检测或安装 uv（不强依赖 astral.sh 直连）
Write-Step "2/5 检查并安装 uv"
Add-PathIfPresent (Join-Path $env:USERPROFILE ".local\bin")
Add-PathIfPresent (Join-Path $env:USERPROFILE ".cargo\bin")

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $installed = $false

    # 优先尝试已有 Python + 阿里云 PyPI 安装 uv（国内最稳）
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pyCmd) { $pyCmd = Get-Command py -ErrorAction SilentlyContinue }
    if ($pyCmd) {
        Write-Host "检测到本机 Python，尝试通过阿里云镜像安装 uv..." -ForegroundColor Gray
        try {
            & $pyCmd.Source -m pip install --user -i $PyPiMirror uv
            $scriptsDir = & $pyCmd.Source -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"
            Add-PathIfPresent $scriptsDir
            if (Get-Command uv -ErrorAction SilentlyContinue) { $installed = $true }
        } catch {
            Write-Host "pip 安装 uv 未成功，切换下载预编译 uv.exe..." -ForegroundColor Yellow
        }
    }

    # 兜底：通过 GitHub 镜像直接下载 uv Windows x64 zip
    if (-not $installed) {
        $uvBinDir = Join-Path $env:USERPROFILE ".local\bin"
        New-Item -ItemType Directory -Force -Path $uvBinDir | Out-Null
        $uvZip = Join-Path $env:TEMP "uv-x86_64-pc-windows-msvc.zip"
        $uvRawUrl = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
        $uvCandidates = @(
            "$($GitHubMirror.TrimEnd('/'))/$uvRawUrl",
            "https://gh-proxy.com/$uvRawUrl",
            $uvRawUrl
        )
        foreach ($u in $uvCandidates) {
            try {
                Write-Host "正在下载 uv：$u" -ForegroundColor Gray
                Invoke-WebRequest -Uri $u -OutFile $uvZip -UseBasicParsing -TimeoutSec 60
                Expand-Archive -Path $uvZip -DestinationPath $uvBinDir -Force
                Remove-Item -Force $uvZip -ErrorAction SilentlyContinue
                Add-PathIfPresent $uvBinDir
                if (Get-Command uv -ErrorAction SilentlyContinue) {
                    $installed = $true
                    break
                }
            } catch {
                Write-Host "该源下载失败，尝试下一个镜像..." -ForegroundColor Yellow
            }
        }
    }

    if (-not $installed) {
        Write-Host "尝试官方脚本安装 uv..." -ForegroundColor Yellow
        Invoke-RestMethod "https://astral.sh/uv/install.ps1" | Invoke-Expression
        Add-PathIfPresent (Join-Path $env:USERPROFILE ".local\bin")
    }
}

$uvExe = (Get-Command uv -ErrorAction Stop).Source
Write-Host "uv 就绪：$uvExe" -ForegroundColor Green

# 3. 获取 poe2brain 仓库（已在仓库内则直接复用；无 Git 自动下 ZIP）
Write-Step "3/5 获取 poe2brain 代码"
if (Test-Path (Join-Path (Get-Location) "pyproject.toml")) {
    $InstallDir = (Get-Location).Path
    Write-Host "检测到当前目录已是 poe2brain 仓库：$InstallDir" -ForegroundColor Green
} elseif (Test-Path (Join-Path $InstallDir "pyproject.toml")) {
    Write-Host "检测到已有目录：$InstallDir" -ForegroundColor Green
} else {
    $parent = Split-Path -Parent $InstallDir
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }

    $fetched = $false
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $mirrorGit = "$($GitHubMirror.TrimEnd('/'))/$GitRepoUrl"
        foreach ($repo in @($CnGitRepoUrl, $mirrorGit, $GitRepoUrl)) {
            Write-Host "尝试 git clone：$repo" -ForegroundColor Gray
            & git clone --depth 1 $repo $InstallDir
            if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $InstallDir "pyproject.toml"))) {
                $fetched = $true
                break
            }
            if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue }
        }
    }

    if (-not $fetched) {
        Write-Host "未检测到 Git 或 clone 失败，改用镜像下载源码 ZIP..." -ForegroundColor Yellow
        $zipRaw = "https://github.com/lifan-builds/poe2brain/archive/refs/heads/main.zip"
        $zipFile = Join-Path $env:TEMP "poe2brain-main.zip"
        $extractDir = Join-Path $env:TEMP "poe2brain-extract"
        foreach ($zUrl in @("$($GitHubMirror.TrimEnd('/'))/$zipRaw", "https://gh-proxy.com/$zipRaw", $zipRaw)) {
            try {
                Invoke-WebRequest -Uri $zUrl -OutFile $zipFile -UseBasicParsing -TimeoutSec 120
                if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
                Expand-Archive -Path $zipFile -DestinationPath $extractDir -Force
                $inner = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1
                if ($inner -and (Test-Path (Join-Path $inner.FullName "pyproject.toml"))) {
                    Move-Item -Path $inner.FullName -Destination $InstallDir
                    $fetched = $true
                    break
                }
            } catch {
                Write-Host "ZIP 源失败，尝试下一个镜像..." -ForegroundColor Yellow
            }
        }
        Remove-Item -Force $zipFile -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $extractDir -ErrorAction SilentlyContinue
    }

    if (-not $fetched) {
        throw "未能下载 poe2brain 仓库，请检查网络或手动从 GitCode/GitHub 下载后重跑本脚本。"
    }
}

Set-Location $InstallDir

# 4. 安装 Python 3.12 依赖并初始化 PoB2 引擎
Write-Step "4/5 执行 uv sync 与无头 PoB2 引擎初始化（国内镜像模式）"
& $uvExe sync
if ($LASTEXITCODE -ne 0) {
    Write-Host "阿里云源同步失败，自动切换清华源重试..." -ForegroundColor Yellow
    $env:UV_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
    & $uvExe sync
    if ($LASTEXITCODE -ne 0) { throw "uv sync 失败，请检查网络连接。" }
}

Write-Host "首次交互初始化时请选择游戏区服：cn（国服）或 international（国际服）。可用 uv run poe2 init 切换。"
& $uvExe run poe2 bootstrap pob2 --cn
if ($LASTEXITCODE -ne 0) { throw "poe2 bootstrap pob2 失败，可运行 uv run poe2 doctor --network 排查。" }

# 5. 自检与下一步指引
Write-Step "5/5 运行环境自检"
& $uvExe run poe2 doctor

Write-Host "`n============================================================" -ForegroundColor Green
Write-Host "  安装完成！下一步：一行命令零 JSON 接入你的 AI 客户端：" -ForegroundColor Green
Write-Host "  - 国产免费客户端 (Cherry Studio + DeepSeek/豆包/Qwen)：" -ForegroundColor White
Write-Host "      uv run poe2 setup-mcp --client cherry --profile core --copy" -ForegroundColor Yellow
Write-Host "  - Trae / Cursor / Claude / Codex 一键写入：" -ForegroundColor White
Write-Host "      uv run poe2 setup-mcp --client trae --profile core" -ForegroundColor Yellow
Write-Host "      uv run poe2 setup-mcp --client cursor" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Green
