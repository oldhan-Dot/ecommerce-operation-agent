# 本机开发环境启动脚本：使用项目独立虚拟环境，不弹出额外窗口。

param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'process-tools.ps1')
# 所有路径以项目位置为基准，避免用户从不同目录启动时找错文件。
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskBackend = Join-Path $taskRoot 'backend'
$taskFrontend = Join-Path $taskRoot 'frontend'
$taskRuntime = Join-Path $taskRoot '.local'
$taskPython = Join-Path $taskBackend '.venv\Scripts\python.exe'
$taskVite = Join-Path $taskFrontend 'node_modules\vite\bin\vite.js'
$taskNode = (Get-Command node -ErrorAction Stop).Source
$taskQuotedVite = '"' + $taskVite + '"'
if (-not (Test-Path -LiteralPath $taskPython) -or -not (Test-Path -LiteralPath $taskVite)) {
    throw 'Run backend uv sync --frozen and frontend npm ci before starting.'
}
# 不主动终止占用端口的进程，避免影响用户的其他项目。
foreach ($taskPort in @(8000,5173)) {
    if (Get-NetTCPConnection -LocalPort $taskPort -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $taskPort is already in use; no existing process was stopped."
    }
}
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskStatePath = Join-Path $taskRuntime 'dev-processes.json'
if (Test-Path -LiteralPath $taskStatePath) {
    # 显式按 UTF-8 读取中文路径，并兼容 Windows PowerShell 5 的数组行为。
    $taskPrevious = ConvertFrom-Json -InputObject (Get-Content -LiteralPath $taskStatePath -Raw -Encoding utf8)
    foreach ($taskEntry in $taskPrevious) {
        $taskProcess = Get-Process -Id $taskEntry.id -ErrorAction SilentlyContinue
        if ($taskProcess -and $taskProcess.StartTime.ToUniversalTime().Ticks.ToString() -eq $taskEntry.start_ticks) {
            throw 'An environment process is still running. Run scripts/stop-dev.ps1 first.'
        }
    }
}
$taskStarted = @()
# 后台运行并重定向日志；记录启动时间和路径，供停止脚本核验身份。
function Start-TrackedService {
    param([string]$ServiceName,[string]$Executable,[string[]]$ServiceArguments,[string]$Directory)
    $taskProcess = Start-Process -FilePath $Executable -ArgumentList $ServiceArguments -WorkingDirectory $Directory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRuntime "$ServiceName.stdout.log") -RedirectStandardError (Join-Path $taskRuntime "$ServiceName.stderr.log")
    Start-Sleep -Milliseconds 350
    $taskProcess.Refresh()
    if ($taskProcess.HasExited) { throw "$ServiceName exited; inspect .local/$ServiceName.stderr.log" }
    return [pscustomobject]@{name=$ServiceName;id=$taskProcess.Id;executable=$Executable;start_ticks=$taskProcess.StartTime.ToUniversalTime().Ticks.ToString()}
}
try {
    $taskStarted += Start-TrackedService 'api' $taskPython @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000') $taskBackend
    $taskStarted += Start-TrackedService 'worker' $taskPython @('-m','app.worker') $taskBackend
    $taskStarted += Start-TrackedService 'frontend' $taskNode @($taskQuotedVite,'--host','127.0.0.1','--port','5173','--strictPort') $taskFrontend
    ConvertTo-Json -InputObject @($taskStarted) | Set-Content -LiteralPath $taskStatePath -Encoding utf8
    Write-Output 'Frontend: http://127.0.0.1:5173'
    Write-Output 'API docs: http://127.0.0.1:8000/api/docs'
    Write-Output 'Worker: environment probe only; business task processing is not implemented.'
    Write-Output 'Logs: .local/*.log; after editing backend/.env, stop and restart these services.'
} catch {
    foreach ($taskEntry in $taskStarted) {
        Stop-TrackedProcessTree $taskEntry
    }
    throw
}
