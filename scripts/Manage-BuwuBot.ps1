[CmdletBinding()]
param(
    [ValidateSet('Install','Start','Run','Stop','Status','EnableAutostart','DisableAutostart')]
    [string]$Action = 'Start',
    [string]$DataDir,
    [string]$PythonPath,
    [ValidateRange(1,65535)][int]$Port = 8765,
    [switch]$Force,
    [switch]$Development,
    [switch]$LocalEmbedding,
    [switch]$SkipDependencies
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $DataDir) { $DataDir = Join-Path $root 'data' }
$DataDir = [IO.Path]::GetFullPath($DataDir)
$stopFile = Join-Path $DataDir 'stop.request'
$python = if ($PythonPath) { [IO.Path]::GetFullPath($PythonPath) } else { Join-Path $root '.venv\Scripts\python.exe' }
$env:PYTHONPATH = Join-Path $root 'src'
Set-Location -LiteralPath $root

function Assert-Runtime {
    if (-not (Test-Path -LiteralPath $python)) { throw 'Runtime missing. Run -Action Install first.' }
}
function Start-Dependencies {
    # External gateways and QQ are configured independently; never guess local paths.
    return
}
function Write-LauncherEvent($Event, $ChildExitCode = $null, $ErrorType = $null) {
    try {
        $logs = Join-Path $DataDir 'logs'
        [IO.Directory]::CreateDirectory($logs) | Out-Null
        [ordered]@{
            timestamp = [DateTimeOffset]::Now.ToString('o')
            event = $Event
            launcher_pid = $PID
            child_exit_code = $ChildExitCode
            stop_requested = [bool](Test-Path -LiteralPath $stopFile)
            error_type = $ErrorType
        } | ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $logs 'launcher.jsonl') -Encoding UTF8
    } catch { Write-Warning 'Could not append launcher diagnostics.' }
}

switch ($Action) {
    'Install' {
        if ($PythonPath) { throw 'Install manages this project environment; omit -PythonPath.' }
        $bootstrapPython = Join-Path $root '.bootstrap\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $bootstrapPython)) {
            if (Get-Command py.exe -ErrorAction SilentlyContinue) {
                & py.exe -3 -m venv (Join-Path $root '.bootstrap')
            } else {
                & python.exe -m venv (Join-Path $root '.bootstrap')
            }
            if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated installer environment.' }
        }
        $uv = Join-Path $root '.bootstrap\Scripts\uv.exe'
        if (-not (Test-Path -LiteralPath $uv)) {
            & $bootstrapPython -m pip install 'uv==0.13.4'
            if ($LASTEXITCODE -ne 0) { throw 'Could not install the pinned uv bootstrap tool.' }
        }
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $root '.tools\python'
        $env:UV_CACHE_DIR = Join-Path $root '.tools\uv-cache'
        $arguments = @('sync','--locked','--python','3.12')
        if ($Development) { $arguments += @('--extra','dev') }
        if ($LocalEmbedding) { $arguments += @('--extra','local-embedding') }
        & $uv @arguments
        if ($LASTEXITCODE -ne 0) { throw 'Runtime installation failed; existing data was preserved.' }
        Write-Output 'Installed. Use -Action Start, then open http://127.0.0.1:8765.'
    }
    'Start' {
        Assert-Runtime
        if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
        $snapshot = & $python -m buwubot.runtime --data-dir $DataDir --status
        if ($LASTEXITCODE -eq 0) { Write-Output $snapshot; return }
        $self = Join-Path $PSScriptRoot 'Manage-BuwuBot.ps1'
        $arguments = @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
            ('"{0}"' -f $self),'-Action','Run','-DataDir',('"{0}"' -f $DataDir),'-Port',"$Port")
        if ($SkipDependencies) { $arguments += '-SkipDependencies' }
        if ($PythonPath) { $arguments += @('-PythonPath', ('"{0}"' -f $python)) }
        Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -WindowStyle Hidden
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            Start-Sleep -Milliseconds 250
            $snapshot = & $python -m buwubot.runtime --data-dir $DataDir --status
            if ($LASTEXITCODE -eq 0) { Write-Output $snapshot; return }
        }
        throw ('Service did not start. Inspect {0}\logs\service.log.' -f $DataDir)
    }
    'Run' {
        try {
            Assert-Runtime
            if (Test-Path -LiteralPath $stopFile) {
                Remove-Item -LiteralPath $stopFile
                Write-LauncherEvent 'stale_stop_cleared'
            }
            # RestartOnFailure did not restart child failures on the acceptance machine.
            # Keep the scheduled launcher alive; an intentional graceful stop exits zero.
            do {
                if (Test-Path -LiteralPath $stopFile) {
                    Write-LauncherEvent 'stop_requested'
                    exit 0
                }
                $snapshot = & $python -m buwubot.runtime --data-dir $DataDir --status
                if ($LASTEXITCODE -eq 0) {
                    Write-LauncherEvent 'already_running'
                    exit 0
                }
                try { Start-Dependencies }
                catch { Write-Warning 'A dependency could not start; the local management service will still start.' }
                Write-LauncherEvent 'child_start'
                & $python -m buwubot.app --data-dir $DataDir --port $Port
                $result = $LASTEXITCODE
                Write-LauncherEvent 'child_exit' -ChildExitCode $result
                if ($result -eq 0) { exit 0 }
                Start-Sleep -Seconds 30
            } while ($true)
        } catch {
            Write-LauncherEvent 'launcher_exception' -ErrorType $_.Exception.GetType().FullName
            throw
        }
    }
    'Stop' {
        Assert-Runtime
        $arguments = @('-m','buwubot.runtime','--data-dir',$DataDir,'--stop')
        if ($Force) { $arguments += '--force' }
        & $python @arguments
        if ($LASTEXITCODE -ne 0) { throw 'Stop failed. Verify the service log; -Force is opt-in.' }
    }
    'Status' {
        Assert-Runtime
        & $python -m buwubot.runtime --data-dir $DataDir --status
        exit $LASTEXITCODE
    }
    { $_ -in 'EnableAutostart','DisableAutostart' } {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($DataDir.ToLowerInvariant()))).Replace('-','').Substring(0,8) }
        finally { $sha.Dispose() }
        $taskName = "BuwuBot-$hash"
        if ($Action -eq 'DisableAutostart') {
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
            Write-Output 'Autostart removed; data and the running service were preserved.'
            return
        }
        Assert-Runtime
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $script = Join-Path $PSScriptRoot 'Manage-BuwuBot.ps1'
        $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Action Run -DataDir "{1}" -Port {2}' -f $script,$DataDir,$Port
        if ($SkipDependencies) { $arguments += ' -SkipDependencies' }
        if ($PythonPath) { $arguments += ' -PythonPath "{0}"' -f $python }
        $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger `
            -Principal $principal -Settings $settings -Description 'Local QQ companion; configuration via localhost only.' -Force | Out-Null
        Write-Output "Autostart enabled for the current user: $taskName. Windows login settings were not changed."
    }
}
