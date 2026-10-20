# Установленная копия; Task Scheduler запускает её на переднем плане. Checkout и PATH не нужны.
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
    # Исключает и ручные дубликаты, помимо политики IgnoreNew планировщика.
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
function Get-DatabaseServiceState {
    # Только чтение состояния собственного Compose project; чужие контейнеры не перечисляются.
    $probe = Invoke-ServiceProcess $deployment.docker ($compose + @('ps', '--all', '--format', 'json', 'postgres'))
    if ($probe.code -ne 0) { return 'docker_unavailable' }
    $containers = @(foreach ($line in ($probe.output -split "`r?`n")) {
        if ($line.Trim()) { $line | ConvertFrom-Json }
    })
    if (-not $containers.Count) { return 'absent' }
    $running = @($containers | Where-Object { $_.State -eq 'running' -and [string]$_.Health -in @('', 'healthy') })
    if ($running.Count) { return 'running' }
    return 'stopped'
}
$retryDelay = 5
try {
    Write-ServiceState @{status = 'starting'}
    while ($true) {
        $delay = 5
        try {
            $models = (Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5).models
            if (-not ($models | Where-Object { $_.name -eq $deployment.llm_model -and $_.digest -eq $deployment.llm_revision }) -or
                -not ($models | Where-Object { $_.name -eq $deployment.embedding_model -and $_.digest -eq $deployment.embedding_revision })) {
                Write-ServiceState @{status = 'waiting'; reason = 'configured_model_unavailable_or_changed'}
                Start-Sleep -Seconds 5
                continue
            }
            # Docker Desktop и Ollama запускаются при входе пользователя сами. Ждём их готовности;
            # демон не перезапускается, чужие контейнеры не перечисляются и не изменяются.
            $invocation = Invoke-ServiceProcess $deployment.python @('-I', '-m', 'language_review', '--config', $deployment.config,
                'memory', 'work', '--all-projects', '--limit', [string]$deployment.worker_limit)
            Set-Content -LiteralPath (Join-Path $logs 'worker.stderr.log') -Value $invocation.error -Encoding utf8NoBOM
            $code = $invocation.code
            if ($code -in @(0, 4)) {
                $retryDelay = 5
                $result = $invocation.output | ConvertFrom-Json
                $status = if (@($result.jobs).Count -or $result.index.indexed -or $code -eq 4) {
                    $result.status
                } else { 'idle' }
                Write-ServiceState @{status = $status; exit_code = $code; result = $result}
            } else {
                # Код 6 сопровождается в stderr только диагностическим кодом memory_*;
                # текст ошибки, DSN и пути в состояние не попадают.
                $reason = if ($code -eq 6 -and $invocation.error -match '\b(memory_[a-z0-9_]+)\b') {
                    $Matches[1]
                } else { 'worker_failed' }
                $database = if ($reason -eq 'memory_database_failed') { Get-DatabaseServiceState } else { $null }
                if ($database -in @('stopped', 'absent')) {
                    # Восстанавливаем только собственный Compose project, и только когда сам Docker
                    # подтверждает, что его PostgreSQL не работает.
                    $retryDelay = 5
                    Write-ServiceState @{status = 'waiting'; exit_code = $code; reason = 'database_unavailable'; database = $database; recovery = 'compose_up'}
                    $recovery = Invoke-ServiceProcess $deployment.docker ($compose + @('up', '-d', '--wait', '--wait-timeout', '30'))
                    Set-Content -LiteralPath (Join-Path $logs 'database.log') -Value ($recovery.output + $recovery.error) -Encoding utf8NoBOM
                } elseif ($database -eq 'docker_unavailable') {
                    $retryDelay = 5
                    Write-ServiceState @{status = 'waiting'; exit_code = $code; reason = 'docker_unavailable'}
                } else {
                    # Конфигурация, схема, зависимости или отказ при работающей БД: Docker не трогаем,
                    # повторяем с растущей паузой до устранения причины оператором.
                    $delay = $retryDelay
                    $retryDelay = [Math]::Min(300, $retryDelay * 2)
                    $record = @{status = 'error'; exit_code = $code; reason = $reason; retry_in_seconds = $delay}
                    if ($database) { $record['database'] = $database }
                    Write-ServiceState $record
                }
            }
        } catch {
            # Текст исключения может содержать окружение процесса или исходный текст: сохраняем только тип.
            Write-ServiceState @{status = 'waiting'; error_type = $_.Exception.GetType().Name}
        }
        Start-Sleep -Seconds $delay
    }
} finally {
    if ($lock) { $lock.Dispose() }
}
