<#
.SYNOPSIS
    把「超星文件下载器」打包成单文件 exe，并用出货的 exe 自身跑冻结自检。

.DESCRIPTION
    产物：<脚本目录>\dist\超星文件下载器.exe

    打包参数固化在 cx-download.spec 里，本脚本只负责：校验输入 → 生成图标（缺了才生成）
    → 调 PyInstaller → 用 exe 自己跑 --selftest（验依赖收集 / HTTPS / 写盘）→ 打印报告。

    自检跑在**主 exe 内部**，而不是另编一个自检 exe —— 后者有自己的打包参数，
    验不了主 exe 的依赖收集情况。

.PARAMETER Python
    用于打包的 python 解释器，默认 python。

.PARAMETER SkipSelftest
    只打包，不跑自检。

.PARAMETER NoNetwork
    自检只验打包完整性，不联网（离线环境用）。

.PARAMETER SelftestOids
    自检用的 objectid。默认用 selftest.py 里的公开样例；样例失效时从这里覆盖。

.PARAMETER OutputDir
    产物输出目录，默认 <脚本目录>\dist。

.EXAMPLE
    pwsh -NoProfile -File build.ps1

.EXAMPLE
    pwsh -NoProfile -File build.ps1 -SelftestOids e934d91c35851d5bcbf3338d41648aea,39a58c0915664f14110611625ef5d07b

.EXAMPLE
    pwsh -NoProfile -File build.ps1 -SkipSelftest
#>
param(
    [string]$Python = 'python',
    [switch]$SkipSelftest,
    [switch]$NoNetwork,
    [string[]]$SelftestOids = @(),
    [string]$OutputDir = ''
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($OutputDir -eq '') { $OutputDir = Join-Path $here 'dist' }
$workDir = Join-Path $here 'build'
$spec = Join-Path $here 'cx-download.spec'
$icon = Join-Path $here 'app.ico'
$exeName = '超星文件下载器.exe'
$report = Join-Path $env:TEMP 'cx_download_selftest.txt'

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red; exit 1 }

# ---------- 1. 校验输入 ----------
Step '1/5 校验输入'
foreach ($f in @('cx_download_gui.py', 'cx_download.py', 'selftest.py')) {
    $p = Join-Path $here $f
    if (-not (Test-Path -LiteralPath $p)) { Fail "缺少源文件 $f" }
    Write-Host "  ok  $f"
}
if (-not (Test-Path -LiteralPath $spec)) { Fail "缺少 spec: $spec" }
Write-Host "  ok  cx-download.spec"

# ---------- 2. 图标 ----------
Step '2/5 图标'
if (-not (Test-Path -LiteralPath $icon)) {
    Write-Host '  app.ico 不存在，用 make_icon.py 生成'
    & $Python (Join-Path $here 'make_icon.py') $icon
    if ($LASTEXITCODE -ne 0) { Fail 'make_icon.py 失败' }
} else {
    Write-Host '  ok  app.ico 已存在'
}

# ---------- 3. 打包 ----------
Step '3/5 PyInstaller 打包'
Remove-Item -Recurse -Force $OutputDir, $workDir -ErrorAction SilentlyContinue
& $Python -m PyInstaller --noconfirm --clean `
    --distpath $OutputDir --workpath $workDir $spec
if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller 打包失败' }

$exe = Join-Path $OutputDir $exeName
if (-not (Test-Path -LiteralPath $exe)) { Fail "没有产出 $exe" }
$mb = [math]::Round((Get-Item -LiteralPath $exe).Length / 1MB, 1)
Write-Host "  ok  $exe  ($mb MB)"

# ---------- 4. 冻结自检 ----------
if ($SkipSelftest) {
    Step '4/5 冻结自检（已跳过）'
} else {
    Step '4/5 冻结自检（跑在出货的 exe 内部）'
    Remove-Item -Force $report -ErrorAction SilentlyContinue

    $selfArgs = @('--selftest')
    if ($NoNetwork) { $selfArgs += '--no-network' }
    if ($SelftestOids.Count -gt 0) { $selfArgs += $SelftestOids }

    $proc = Start-Process -FilePath $exe -ArgumentList $selfArgs -PassThru
    $finished = $proc.WaitForExit(300000)   # 5 分钟上限
    if (-not $finished) {
        $proc.Kill()
        Fail '自检超时（300s）'
    }
    $code = $proc.ExitCode

    if (Test-Path -LiteralPath $report) {
        Get-Content -LiteralPath $report | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Warning "没找到自检报告 $report（windowed exe 无 stdout，报告是唯一线索）"
    }
    if ($code -ne 0) { Fail "自检未通过（exit=$code），详见 $report" }
    Write-Host '  SELFTEST PASS' -ForegroundColor Green
}

# ---------- 5. 完成 ----------
Step '5/5 完成'
Write-Host "产物: $exe"
Write-Host "大小: $mb MB"
Write-Host @"
提醒:
  - onefile 每次启动要解压到 %TEMP%\_MEIxxxx，首次慢几秒
  - onefile 被杀软误报很常见；介意就改 --onedir 发压缩包
  - 界面观感复核请用 tools\gui-shot\gui_shot.ps1 截图，别只凭"进程起来了"
  - 启动期报错排查：把 spec 里的 console=False 临时改 True 重打
"@
