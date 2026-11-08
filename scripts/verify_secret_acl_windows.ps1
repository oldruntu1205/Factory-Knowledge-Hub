#requires -RunAsAdministrator
<#
.SYNOPSIS
  用临时普通账户实证：实例 secrets/ 的密钥文件不可读、目录不可列举。

.DESCRIPTION
  Windows 的 chmod 0600 不改变 DACL；本脚本创建一个临时本地账户（默认加入 Users，
  非管理员），以该账户启动 PowerShell 去读 jwt.secret / db-admin.password /
  db-app.password，并列举 secrets 目录。预期全部 DENIED，同时能读取一个公开的
  控制文件（证明探测进程本身工作正常）。脚本结束删除临时账户与临时目录。

  必须从**提升的** PowerShell 运行（创建/删除本地账户需要管理员权限）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\verify_secret_acl_windows.ps1 `
    -SecretDir .acceptance\sec-a\secrets -ControlFile README.md -OutJson .acceptance\acl-probe.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SecretDir,
    [string]$ControlFile = (Join-Path $PSScriptRoot '..\README.md'),
    [string]$OutJson = "",
    [switch]$KeepUser
)

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "需要提升的 PowerShell（管理员）才能创建临时普通账户。当前令牌不是管理员。"
}

$SecretDir = (Resolve-Path -LiteralPath $SecretDir).Path
$ControlFile = (Resolve-Path -LiteralPath $ControlFile).Path
if (-not (Test-Path -LiteralPath (Join-Path $SecretDir 'jwt.secret'))) {
    throw "SecretDir 下没有 jwt.secret：$SecretDir"
}

$tag = [guid]::NewGuid().ToString('N').Substring(0, 8)
$user = "fkh_acl_$tag"
$password = "Fkh!" + [guid]::NewGuid().ToString('N') + "aA9"
$probeDir = Join-Path $env:PUBLIC "fkh-acl-probe-$tag"
New-Item -ItemType Directory -Path $probeDir -Force | Out-Null
# 普通账户需要能读取探测脚本；密钥目录本身仍只允许所有者。
& icacls $probeDir /grant "*S-1-5-32-545:(OI)(CI)(RX)" | Out-Null

$probeScript = Join-Path $probeDir 'probe.ps1'
$wrapperScript = Join-Path $probeDir 'wrapper.ps1'
$outFile = Join-Path $probeDir 'probe.out'
$errFile = Join-Path $probeDir 'probe.err'

$secretFiles = @(
    (Join-Path $SecretDir 'jwt.secret'),
    (Join-Path $SecretDir 'db-admin.password'),
    (Join-Path $SecretDir 'db-app.password')
)
$fileLiterals = ($secretFiles | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ','
$probeLines = @(
    "param([string]`$SecretDir,[string]`$ControlFile)",
    "`$ErrorActionPreference='Continue'",
    "`$files=@($fileLiterals)",
    "`$r=[ordered]@{who=(whoami); files=@{}; dir='?'; control='?'; admin_group=`$false}",
    "foreach(`$f in `$files){",
    "  try { `$null=[IO.File]::ReadAllText(`$f); `$r.files[`$f]='READABLE' }",
    "  catch { `$r.files[`$f]='DENIED:'+`$_.Exception.InnerException.GetType().Name }",
    "}",
    "try { `$null=[IO.Directory]::GetFiles(`$SecretDir); `$r.dir='LISTABLE' }",
    "catch { `$r.dir='DENIED:'+`$_.Exception.InnerException.GetType().Name }",
    "try { `$null=[IO.File]::ReadAllText(`$ControlFile); `$r.control='READABLE' }",
    "catch { `$r.control='DENIED' }",
    "`$groups = whoami /groups /fo csv /nh",
    "`$r.admin_group = [bool](`$groups | Select-String -SimpleMatch 'S-1-5-32-544')",
    "`$r | ConvertTo-Json -Compress"
)
Set-Content -LiteralPath $probeScript -Value $probeLines -Encoding UTF8

$wrapperLines = @(
    "param([string]`$UserName,[string]`$Password,[string]`$Probe,[string]`$Out,[string]`$Err,[string]`$SecretDir,[string]`$ControlFile)",
    "`$sec=ConvertTo-SecureString `$Password -AsPlainText -Force",
    "`$cred=New-Object System.Management.Automation.PSCredential(`"`$env:COMPUTERNAME\`$UserName`",`$sec)",
    "`$ps=`"`$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe`"",
    "`$p=Start-Process -FilePath `$ps -Credential `$cred -Wait -PassThru -ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',`$Probe,'-SecretDir',`$SecretDir,'-ControlFile',`$ControlFile) -RedirectStandardOutput `$Out -RedirectStandardError `$Err -WindowStyle Hidden",
    "exit `$p.ExitCode"
)
Set-Content -LiteralPath $wrapperScript -Value $wrapperLines -Encoding UTF8

$result = $null
try {
    $created = & net user $user $password /add /y 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "创建临时普通账户失败：$created"
    }

    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $wrapperScript `
        -UserName $user -Password $password -Probe $probeScript `
        -Out $outFile -Err $errFile -SecretDir $SecretDir -ControlFile $ControlFile | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $detail = if (Test-Path -LiteralPath $errFile) { Get-Content -LiteralPath $errFile -Raw } else { "" }
        throw "普通账户探测进程失败：$detail"
    }

    $probe = Get-Content -LiteralPath $outFile -Raw | ConvertFrom-Json
    $result = [ordered]@{
        user = $user
        secret_dir = $SecretDir
        control_file = $ControlFile
        probe = $probe
    }
    $json = $result | ConvertTo-Json -Depth 6 -Compress
    if ($OutJson) {
        Set-Content -LiteralPath $OutJson -Value $json -Encoding UTF8
    }
    Write-Output $json
}
finally {
    if (-not $KeepUser) {
        & net user $user /delete 2>$null | Out-Null
        Remove-Item -LiteralPath (Join-Path $env:SystemDrive "Users\$user") -Recurse -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $probeDir -Recurse -Force -ErrorAction SilentlyContinue
}
