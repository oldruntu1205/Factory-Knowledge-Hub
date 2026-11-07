# 本地私域演示环境一键启动（Windows PowerShell）
#
#   powershell -ExecutionPolicy Bypass -File scripts\demo-up.ps1
#
# 做三件事：
#   1. 预检：推理服务（Ollama 11434）在不在；不在就先拉起，并给你明确提示；
#   2. 起演示：内嵌 PG + API + 问答页 + 冲突待办页（全部只绑 127.0.0.1）；
#   3. 告诉你地址与强刷提示。
#
# 演示当天最容易翻车的是"模型没起来"（实测两次）：接口会 503/500，看起来像系统坏了。
# 所以这里把它变成启动时的一句话提示，而不是演示中途才暴露。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Test-Port([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Say($msg, $color = "Gray") { Write-Host "[demo] $msg" -ForegroundColor $color }

Say "工作目录：$root"

# 1) 推理服务预检
if (Test-Port 11434) {
    Say "推理服务 Ollama 已在运行（11434）" "Green"
} else {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe",
        "C:\Program Files\Ollama\ollama.exe"
    ) | Where-Object { Test-Path $_ }
    if ($candidates.Count -eq 0) {
        Say "未找到 Ollama，描述类问答会返回 503（货号/报价不受影响）" "Yellow"
    } else {
        Say "Ollama 未运行，正在拉起：$($candidates[0])" "Yellow"
        Start-Process -FilePath $candidates[0] -WindowStyle Hidden | Out-Null
        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-Port 11434) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
        if (Test-Port 11434) { Say "Ollama 已就绪" "Green" }
        else { Say "Ollama 30 秒内未就绪，描述类问答会 503" "Yellow" }
    }
}

# 2) 端口占用检查（避免"页面没变"的假象：旧进程还占着）
foreach ($port in 8010, 8000) {
    if (Test-Port $port) {
        $pid = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1).OwningProcess
        Say "端口 $port 已被 pid $pid 占用；先停掉它再启动（否则你看到的仍是旧页面）" "Red"
        exit 1
    }
}

# 3) 起演示
Say "启动中（首次会初始化数据库并入库目录，约需 20–60 秒）…"
python -m fkh.tools.demo_app --pgdata .demo-pgdata --db-port 55432 --provider ollama --model qwen3:14b
