# 本机开发环境停止脚本：只处理启动脚本记录的服务。

param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'process-tools.ps1')
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskStatePath = Join-Path $taskRoot '.local\dev-processes.json'
if (-not (Test-Path -LiteralPath $taskStatePath)) { Write-Output 'No tracked environment processes.'; exit 0 }
# 明确使用 UTF-8，避免中文项目路径在旧版 PowerShell 中乱码。
$taskState = ConvertFrom-Json -InputObject (Get-Content -LiteralPath $taskStatePath -Raw -Encoding utf8)
# 仅停止记录中经过身份核验的本项目进程，不按端口批量结束进程。
foreach ($taskEntry in $taskState) {
    Stop-TrackedProcessTree $taskEntry
    Write-Output "Stopped or already exited: $($taskEntry.name)"
}
Remove-Item -LiteralPath $taskStatePath
