# 进程生命周期辅助函数：防止遗留子进程或误结束其他项目。

function Stop-TrackedProcessTree {
    param($Entry)
    $taskParent = Get-Process -Id $Entry.id -ErrorAction SilentlyContinue
    if (-not $taskParent) { return }
    # PID 可能被系统复用，必须同时校验启动时间和程序路径。
    if ($taskParent.StartTime.ToUniversalTime().Ticks.ToString() -ne $Entry.start_ticks -or $taskParent.Path -ne $Entry.executable) {
        Write-Warning "Skipped reused or unexpected PID for $($Entry.name)"
        return
    }
    # Windows 虚拟环境启动器可能派生真正的解释器子进程。
    # 先验证根进程身份，再结合创建时间收集其后代，避免只停启动器。
    $taskSnapshot = @(Get-CimInstance Win32_Process)
    $taskDescendants = [System.Collections.Generic.List[object]]::new()
    function Find-TaskChildren {
        param([int]$ParentId)
        foreach ($taskChild in @($taskSnapshot | Where-Object ParentProcessId -eq $ParentId)) {
            if ($taskChild.CreationDate.ToUniversalTime().Ticks -lt [long]$Entry.start_ticks) { continue }
            Find-TaskChildren ([int]$taskChild.ProcessId)
            $taskDescendants.Add($taskChild)
        }
    }
    Find-TaskChildren ([int]$Entry.id)
    # 先停止后代再处理根进程；停止前再次检查创建时间。
    foreach ($taskChild in $taskDescendants) {
        $taskCurrent = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskChild.ProcessId)"
        if ($taskCurrent -and $taskCurrent.CreationDate -eq $taskChild.CreationDate) {
            Stop-Process -Id $taskChild.ProcessId -ErrorAction SilentlyContinue
        }
    }
    $taskParent = Get-Process -Id $Entry.id -ErrorAction SilentlyContinue
    if ($taskParent -and $taskParent.StartTime.ToUniversalTime().Ticks.ToString() -eq $Entry.start_ticks) {
        Stop-Process -Id $Entry.id -ErrorAction SilentlyContinue
    }
}
