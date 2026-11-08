# 起一套单实例（RAG 知识基座 · 私域部署）
#
# 用法：
#   .\scripts\instance-up.ps1 -Name factory -PgData .instance-factory `
#       -DbPort 55441 -ApiPort 8101 -UiPort 8111
#
# 说明：
# - 每套实例各自拥有独立 PG 数据目录与端口；互不共享数据、索引、凭据；
# - API 与 UI 由 demo_app 同源托管（UI 通过 /api/* 代理到同一实例）；
# - 同一提交可起任意多套，差异只在配置与导入的资料。

param(
  [string]$Name = "factory",
  [string]$PgData = ".instance-factory",
  [int]$DbPort = 55441,
  [int]$ApiPort = 8101,
  [int]$UiPort = 8111,
  [string]$Provider = "ollama",
  [string]$Model = "qwen3:14b",
  [string]$Corpus = "_corpus/catalog_2023.md",
  [string]$Secret = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if (-not $Secret) {
  # 每套实例必须有独立密钥：A 的令牌不能用于 B
  $Secret = "instance-$Name-" + ([guid]::NewGuid().ToString("N").Substring(0, 24))
}

if (-not (Test-Path $PgData)) {
  New-Item -ItemType Directory -Path $PgData | Out-Null
}

Write-Host "[instance:$Name] repo=$repo"
Write-Host "[instance:$Name] pgdata=$PgData db=$DbPort api=$ApiPort ui=$UiPort"

$log = Join-Path $PgData "run.log"
$err = Join-Path $PgData "run.err"

Start-Process -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList @(
    "-u", "-m", "fkh.tools.demo_app",
    "--pgdata", $PgData,
    "--db-port", "$DbPort",
    "--api-port", "$ApiPort",
    "--ui-port", "$UiPort",
    "--provider", $Provider,
    "--model", $Model,
    "--corpus", $Corpus,
    "--secret", $Secret
  ) `
  -RedirectStandardOutput $log `
  -RedirectStandardError $err `
  -WindowStyle Hidden | Out-Null

$deadline = (Get-Date).AddSeconds(240)
$ready = $false
while (-not $ready -and (Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 5
  try {
    $null = Invoke-RestMethod "http://127.0.0.1:$UiPort/api/status" -TimeoutSec 3
    $ready = $true
  } catch {
    $ready = $false
  }
}

if (-not $ready) {
  Write-Host "[instance:$Name] 启动失败，最近日志："
  if (Test-Path $err) { Get-Content $err -Tail 20 }
  if (Test-Path $log) { Get-Content $log -Tail 20 }
  exit 1
}

$status = Invoke-RestMethod "http://127.0.0.1:$UiPort/api/status" -TimeoutSec 10
Write-Host "[instance:$Name] 就绪：http://127.0.0.1:$UiPort/  subject=$($status.subject) tenant=$($status.tenant_label)"
Write-Host "[instance:$Name] 日志：$log"
