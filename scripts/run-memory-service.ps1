# Installed copy, run in the foreground by Task Scheduler. No checkout/PATH dependency.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'connect-memory.ps1')
$deployment = $languageReviewDeployment
$logs = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$compose = @('compose', '-f', (Join-Path $PSScriptRoot 'compose.memory.yml'), '-p', $deployment.project)
$log = Join-Path $logs 'service.jsonl'
$state = Join-Path $logs 'service-state.json'
$lock = $null
try {
    # Also excludes direct manual duplicates, beyond Scheduler's IgnoreNew policy.
    $lock = [IO.File]::Open((Join-Path $logs 'service.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
} catch [IO.IOException] { exit 0 }
function Write-ServiceState($record) {
    $record['at'] = [DateTime]::UtcNow.ToString('o')
    $record['supervisor_pid'] = $PID
    $record['release'] = $deployment.release
    $json = $record | ConvertTo-Json -Compress -Depth 30
    $temp = $state + '.tmp'
    Set-Content -LiteralPath $temp -Value $json -Encoding utf8NoBOM
    Move-Item -LiteralPath $temp -Destination $state -Force
    if ($record.status -ne 'idle') {
        if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 5MB) {
            Move-Item -LiteralPath $log -Destination ($log + '.1') -Force
        }
        Add-Content -LiteralPath $log -Value $json -Encoding utf8NoBOM
    }
}
function Invoke-ServiceProcess([string]$Executable, [string[]]$ProcessArguments) {
    $info = [Diagnostics.ProcessStartInfo]::new($Executable)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    foreach ($argument in $ProcessArguments) { $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        return @{code = $process.ExitCode; output = $stdout.GetAwaiter().GetResult(); error = $stderr.GetAwaiter().GetResult()}
    } finally { $process.Dispose() }
}
try {
    while ($true) {
        try {
            $models = (Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5).models
            if (-not ($models | Where-Object { $_.name -eq $deployment.llm_model -and $_.digest -eq $deployment.llm_revision }) -or
                -not ($models | Where-Object { $_.name -eq $deployment.embedding_model -and $_.digest -eq $deployment.embedding_revision })) {
                Write-ServiceState @{status = 'waiting'; reason = 'configured_model_unavailable_or_changed'}
                Start-Sleep -Seconds 5
                continue
            }
            # Docker Desktop and Ollama have their own user logon startup. Wait for readiness;
            # never restart the daemon or enumerate/mutate unrelated containers.
            $invocation = Invoke-ServiceProcess $deployment.python @('-I', '-m', 'language_review', '--config', $deployment.config,
                'memory', 'work', '--all-projects', '--limit', '8')
            Set-Content -LiteralPath (Join-Path $logs 'worker.stderr.log') -Value $invocation.error -Encoding utf8NoBOM
            $code = $invocation.code
            if ($code -in @(0, 4)) {
                $result = $invocation.output | ConvertFrom-Json
                $status = if (@($result.jobs).Count -or $result.index.indexed -or $code -eq 4) {
                    $result.status
                } else { 'idle' }
                Write-ServiceState @{status = $status; exit_code = $code; result = $result}
            } else {
                Write-ServiceState @{status = 'waiting'; exit_code = $code}
                # Recover only this Compose project after Docker becomes available again.
                $database = Invoke-ServiceProcess $deployment.docker ($compose + @('up', '-d', '--wait', '--wait-timeout', '30'))
                Set-Content -LiteralPath (Join-Path $logs 'database.log') -Value ($database.output + $database.error) -Encoding utf8NoBOM
            }
        } catch {
            # Exception text can contain process environment or source text: keep only type.
            Write-ServiceState @{status = 'waiting'; error_type = $_.Exception.GetType().Name}
        }
        Start-Sleep -Seconds 5
    }
} finally {
    if ($lock) { $lock.Dispose() }
}
