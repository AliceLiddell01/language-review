param(
    [string]$LlmModel = 'qwen3.5:4b',
    [string]$EmbeddingModel = 'embeddinggemma:300m',
    [string]$EmbeddingRevision = '85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1',
    [int]$EmbeddingDimensions = 768
)
# Настоящий установленный CLI + live Ollama + собственные БД и задача. Запускайте из чистого клона.
$ErrorActionPreference = 'Stop'
$scratch = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ('lr-service-' + [guid]::NewGuid().ToString('N'))))
$runtime = Join-Path $scratch 'runtime with spaces'
New-Item -ItemType Directory -Path $scratch | Out-Null
$project = 'lr-service-test-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$task = 'LanguageReview-Memory-' + $project
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()
$envNames = @('APPDATA', 'XDG_CONFIG_HOME', 'LANGUAGE_REVIEW_MEMORY_DSN', 'LANGUAGE_REVIEW_DB_PASSWORD',
    'LANGUAGE_REVIEW_DB_PORT', 'LANGUAGE_REVIEW_TEST_INSTALL_FAULT')
$previous = @{}
foreach ($name in $envNames) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
$summary = [ordered]@{status = 'FAIL'; project = $project; port = $port; reboot = 'NOT_RUN'; checks = @()}
$operationFailure = $null
$cleanupErrors = [Collections.Generic.List[string]]::new()
$compose = @('compose', '-f', (Join-Path $runtime 'compose.memory.yml'), '-p', $project)
$stateFile = Join-Path $runtime 'logs\service-state.json'
$manifestFile = Join-Path $runtime 'deployment.json'
function Assert-Check([string]$Name, [bool]$Condition) {
    if (-not $Condition) { throw "FAIL: $Name" }
    $script:summary.checks += $Name
}
function Invoke-Cli([string[]]$CliArguments, [int[]]$Codes = @(0)) {
    $output = & $script:deployment.python -I -m language_review --config $script:deployment.config `
        @CliArguments 2> (Join-Path $scratch 'cli.stderr.log')
    if ($LASTEXITCODE -notin $Codes) { throw "CLI завершился с кодом $LASTEXITCODE; см. cli.stderr.log" }
    return ($output -join "`n") | ConvertFrom-Json
}
function Install-Service([string]$Fault) {
    $env:LANGUAGE_REVIEW_TEST_INSTALL_FAULT = $Fault
    try {
        & (Join-Path $PSScriptRoot 'install-memory-service.ps1') -RuntimeDirectory $runtime -Project $project -Port $port `
            -LlmModel $LlmModel -EmbeddingModel $EmbeddingModel -EmbeddingRevision $EmbeddingRevision `
            -EmbeddingDimensions $EmbeddingDimensions | Out-Host
    } finally { $env:LANGUAGE_REVIEW_TEST_INSTALL_FAULT = $null }
}
function Install-WithFault([string]$Fault) {
    try { Install-Service $Fault } catch { return $_.Exception.Message }
    throw "Установка с контролируемым сбоем $Fault неожиданно завершилась успешно."
}
function Read-State {
    try { Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json } catch { $null }
}
function Wait-State([string]$Release, [string[]]$Statuses, [DateTime]$After, [int]$Seconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        Start-Sleep -Seconds 1
        $current = Read-State
        if ($current -and $current.release -eq $Release -and $current.status -in $Statuses -and
            [DateTime]$current.at -gt $After) { return $current }
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}
function Wait-Processed([string]$ProjectId) {
    $deadline = [DateTime]::UtcNow.AddMinutes(5)
    do {
        $status = Invoke-Cli @('memory', 'status', '--project', $ProjectId)
        if (@($status.jobs).Count -gt 0 -and @($status.jobs | Where-Object state -NE 'done').Count -eq 0 -and
            $status.index.rules -gt 0 -and $status.index.indexed -eq $status.index.rules) {
            return $status
        }
        if (@($status.jobs | Where-Object state -EQ 'failed').Count) {
            throw 'Worker исчерпал попытки; см. сохранённые ошибки заданий.'
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Истекло время автоматической обработки worker.'
}
try {
    foreach ($name in @('LANGUAGE_REVIEW_MEMORY_DSN', 'LANGUAGE_REVIEW_DB_PASSWORD', 'LANGUAGE_REVIEW_DB_PORT')) {
        [Environment]::SetEnvironmentVariable($name, $null)
    }
    # Сбой первой установки не должен оставлять задачу или manifest, выдающие runtime за установленный.
    $firstFailure = Install-WithFault 'start'
    Assert-Check 'failed_first_install_leaves_no_task_or_manifest' (
        $firstFailure.Contains('Установка не выполнена') -and
        -not (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) -and
        -not (Test-Path -LiteralPath $manifestFile) -and
        (Test-Path -LiteralPath (Join-Path $runtime 'database.credential.xml')))
    Install-Service ''
    $script:deployment = Get-Content -Raw -LiteralPath $manifestFile | ConvertFrom-Json
    Assert-Check 'installed_release_independent_of_checkout' (
        $deployment.python.StartsWith($runtime + '\') -and -not $deployment.source_dirty -and $deployment.status -eq 'installed')
    . (Join-Path $runtime 'connect-memory.ps1')
    $env:APPDATA = Join-Path $scratch 'empty-appdata'
    $env:XDG_CONFIG_HOME = $env:APPDATA
    $definition = Get-ScheduledTask -TaskName $task
    Assert-Check 'current_user_logon_trigger_and_restart_policy' (
        $definition.Triggers[0].CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' -and
        $definition.Principal.LogonType -eq 'Interactive' -and $definition.Settings.MultipleInstances -eq 'IgnoreNew' -and
        $definition.Settings.ExecutionTimeLimit -eq 'PT0S' -and $definition.Settings.RestartCount -eq 999 -and
        $definition.Triggers[1].Repetition.Interval -eq 'PT1M')
    $container = (docker inspect "${project}-postgres-1" | ConvertFrom-Json)[0]
    Assert-Check 'own_volume_loopback_port_and_restart_policy' (
        $container.HostConfig.RestartPolicy.Name -eq 'unless-stopped' -and
        $container.Mounts[0].Name -eq "${project}_memory-data" -and
        $container.HostConfig.PortBindings.'5432/tcp'[0].HostIp -eq '127.0.0.1' -and
        $container.HostConfig.PortBindings.'5432/tcp'[0].HostPort -eq [string]$port)
    $sample = Join-Path $scratch 'собственные данные.md'
    Set-Content -LiteralPath $sample -Encoding utf8NoBOM -Value 'Нужно праверить параметр конфигурации. Этот параметр возврощает результат проверки.'
    $sourceHash = (Get-FileHash -LiteralPath $sample).Hash
    $projectId = [guid]::NewGuid().ToString()
    $report = Invoke-Cli @('check', $sample, '--memory-project', $projectId, '--memory-dataset', 'synthetic', '--view', 'review', '--format', 'json') @(0, 1, 4)
    Assert-Check 'real_check_automatically_enqueues_observations' ($report.schema -eq 'language-review.review' -and @($report.items).Count -gt 0)
    $status = Wait-Processed $projectId
    $status | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath (Join-Path $scratch 'processed.json') -Encoding utf8NoBOM
    Assert-Check 'scheduled_worker_automatically_consolidates_and_indexes' ($status.latest_consolidation.model -eq $LlmModel -and $status.index.indexed -gt 0)
    Assert-Check 'hypotheses_do_not_become_human_decisions' (@($status.findings | Where-Object state -NE 'unverified').Count -eq 0)
    Assert-Check 'source_bytes_unchanged' ((Get-FileHash -LiteralPath $sample).Hash -eq $sourceHash)
    $before = Read-State
    # Завершаем только записанный supervisor собственной задачи; планировщик перезапустит его.
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($before.supervisor_pid)"
    Assert-Check 'owned_supervisor_identity' ($process.CommandLine.Contains((Join-Path $runtime 'run-memory-service.ps1')))
    Stop-Process -Id $before.supervisor_pid -Force
    $deadline = [DateTime]::UtcNow.AddSeconds(100)
    do {
        Start-Sleep -Seconds 2
        $after = Read-State
        if ($after.supervisor_pid -ne $before.supervisor_pid -and
            $after.status -in @('idle', 'processed', 'partial')) { break }
    } while ([DateTime]::UtcNow -lt $deadline)
    Assert-Check 'scheduler_restarts_crashed_supervisor' (
        $after.supervisor_pid -ne $before.supervisor_pid -and $after.status -in @('idle', 'processed', 'partial'))
    $secondProject = [guid]::NewGuid().ToString()
    $null = Invoke-Cli @('check', $sample, '--memory-project', $secondProject, '--memory-dataset', 'synthetic', '--view', 'review', '--format', 'json') @(0, 1, 4)
    $second = Wait-Processed $secondProject
    Assert-Check 'new_project_processed_after_restart_without_manual_work' ($second.index.indexed -gt 0)

    # Сбои обновления на этапах регистрации, запуска и readiness восстанавливают прежнюю установку.
    $manifestBytes = [IO.File]::ReadAllBytes($manifestFile)
    $configBytes = [IO.File]::ReadAllBytes($deployment.config)
    foreach ($fault in @('register', 'start', 'readiness')) {
        $failedAt = [DateTime]::UtcNow
        $message = Install-WithFault $fault
        $restoredTask = Get-ScheduledTask -TaskName $task
        $resumed = Wait-State $deployment.release @('idle', 'processed', 'partial') $failedAt 120
        Assert-Check "failed_update_${fault}_restores_previous_release" (
            $message.Contains('Обновление не выполнено') -and $message.Contains('восстановлены') -and
            -not $message.Contains('Откат выполнен не полностью') -and
            [Linq.Enumerable]::SequenceEqual([byte[]][IO.File]::ReadAllBytes($manifestFile), [byte[]]$manifestBytes) -and
            [Linq.Enumerable]::SequenceEqual([byte[]][IO.File]::ReadAllBytes($deployment.config), [byte[]]$configBytes) -and
            $restoredTask.State -ne 'Disabled' -and $restoredTask.Description -eq "Language Review memory: $runtime" -and
            $null -ne $resumed)
    }
    $update = Invoke-Cli @('check', $sample, '--memory-project', $projectId, '--memory-dataset', 'synthetic', '--view', 'review', '--format', 'json') @(0, 1, 4)
    Assert-Check 'previous_worker_keeps_processing_after_rollback' ($update.schema -eq 'language-review.review')
    $beforeDatabaseStop = Wait-Processed $projectId
    $releases = @($deployment.release)
    foreach ($attempt in 1..2) {
        Install-Service ''
        $script:deployment = Get-Content -Raw -LiteralPath $manifestFile | ConvertFrom-Json
        Assert-Check "successful_update_${attempt}_publishes_ready_release" (
            $deployment.status -eq 'installed' -and $deployment.release -notin $releases -and
            (Read-State).release -eq $deployment.release)
        $releases += $deployment.release
    }

    # Ошибка конфигурации worker не является недоступностью БД: Docker recovery не вызывается.
    $configBackup = Join-Path $scratch 'worker.toml.backup'
    Copy-Item -LiteralPath $deployment.config -Destination $configBackup
    $broken = [DateTime]::UtcNow
    Add-Content -LiteralPath $deployment.config -Value 'invalid = = toml' -Encoding utf8NoBOM
    $failed = Wait-State $deployment.release @('error') $broken 90
    Assert-Check 'worker_configuration_error_is_visible_and_not_database_recovery' (
        $failed -and $failed.reason -eq 'worker_failed' -and $failed.exit_code -eq 2)
    & docker @compose stop postgres
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось остановить собственную тестовую БД.' }
    $stopped = [DateTime]::UtcNow
    Start-Sleep -Seconds 30
    $idle = (docker inspect "${project}-postgres-1" | ConvertFrom-Json)[0]
    $recoveries = @(Get-Content -LiteralPath (Join-Path $runtime 'logs\service.jsonl') | ForEach-Object { $_ | ConvertFrom-Json } |
        Where-Object { $_.recovery -and [DateTime]$_.at -gt $broken })
    Assert-Check 'non_database_error_never_starts_docker_recovery' (
        -not $idle.State.Running -and -not $recoveries.Count -and (Read-State).status -eq 'error')
    Copy-Item -LiteralPath $configBackup -Destination $deployment.config -Force
    $deadline = [DateTime]::UtcNow.AddSeconds(240)
    do {
        Start-Sleep -Seconds 2
        $recovered = (docker inspect "${project}-postgres-1" | ConvertFrom-Json)[0]
        if ($recovered.State.Health.Status -eq 'healthy' -and $recovered.State.Running) { break }
    } while ([DateTime]::UtcNow -lt $deadline)
    Assert-Check 'supervisor_recovers_own_database_without_data_loss' ($recovered.State.Health.Status -eq 'healthy' -and $recovered.State.Running)
    $recoveryRecord = @(Get-Content -LiteralPath (Join-Path $runtime 'logs\service.jsonl') | ForEach-Object { $_ | ConvertFrom-Json } |
        Where-Object { $_.recovery -eq 'compose_up' -and [DateTime]$_.at -gt $stopped })
    Assert-Check 'database_recovery_is_reported_as_database_unavailable' (
        $recoveryRecord.Count -and $recoveryRecord[0].reason -eq 'database_unavailable')
    $persisted = Wait-Processed $projectId
    Assert-Check 'original_events_persist_across_database_restart' ($persisted.durable_events -eq $beforeDatabaseStop.durable_events)
    $owned = Read-State
    Stop-ScheduledTask -TaskName $task
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        $remaining = Get-Process -Id $owned.supervisor_pid -ErrorAction SilentlyContinue
    } while ($remaining -and [DateTime]::UtcNow -lt $deadline)
    Assert-Check 'stopping_task_terminates_supervisor_tree' (-not $remaining)
    Start-ScheduledTask -TaskName $task
    $summary.status = 'PASS'
    $summary.source_sha = $deployment.source_sha
    $summary.wheel_sha256 = $deployment.wheel_sha256
    $summary.llm_revision = $deployment.llm_revision
    $summary.embedding_revision = $deployment.embedding_revision
} catch {
    $operationFailure = $_.Exception
} finally {
    $canRemoveRuntime = $true
    try {
        $ownedTask = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
        if ($ownedTask -and $ownedTask.Description -eq "Language Review memory: $runtime") {
            Stop-ScheduledTask -TaskName $task
            Unregister-ScheduledTask -TaskName $task -Confirm:$false
            Start-Sleep -Seconds 2
        }
    } catch {
        $cleanupErrors.Add("Не удалось удалить задачу: $($_.Exception.GetType().Name)")
        $canRemoveRuntime = $false
    }

    # Сбой первой установки удаляет compose-файл runtime; volume проекта удаляется по файлу checkout.
    $cleanupCompose = if (Test-Path -LiteralPath (Join-Path $runtime 'compose.memory.yml')) { $compose } else {
        @('compose', '-f', (Join-Path (Split-Path -Parent $PSScriptRoot) 'compose.memory.yml'), '-p', $project)
    }
    if ($project -notmatch '^lr-service-test-[0-9a-f]{12}$') {
        $cleanupErrors.Add('Небезопасный project для очистки.')
        $canRemoveRuntime = $false
    } elseif ($canRemoveRuntime) {
        try {
            $env:LANGUAGE_REVIEW_DB_PASSWORD = 'cleanup-only'
            $env:LANGUAGE_REVIEW_DB_PORT = [string]$port
            & docker @cleanupCompose down --volumes
            if ($LASTEXITCODE -ne 0) {
                $cleanupErrors.Add("Не удалось очистить тестовый Compose ($LASTEXITCODE).")
                $canRemoveRuntime = $false
            }
        } catch {
            $cleanupErrors.Add("Не удалось очистить тестовый Compose: $($_.Exception.GetType().Name)")
            $canRemoveRuntime = $false
        }
    }

    $expectedRuntime = Join-Path ([IO.Path]::GetFullPath($scratch)) 'runtime with spaces'
    if ([IO.Path]::GetFullPath($runtime) -ne $expectedRuntime -or
        -not $runtime.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()), [StringComparison]::OrdinalIgnoreCase)) {
        $cleanupErrors.Add('Небезопасный путь runtime для очистки.')
        $canRemoveRuntime = $false
    } elseif ($canRemoveRuntime -and (Test-Path -LiteralPath $runtime)) {
        try {
            Remove-Item -LiteralPath $runtime -Recurse -Force
        } catch {
            $cleanupErrors.Add("Не удалось удалить runtime: $($_.Exception.GetType().Name)")
        }
    }

    foreach ($name in $envNames) {
        try {
            [Environment]::SetEnvironmentVariable($name, $previous[$name])
        } catch {
            $cleanupErrors.Add("Не удалось восстановить переменную окружения $name.")
        }
    }
    if ($cleanupErrors.Count) { $summary.status = 'FAIL' }
    $summary.cleanup_status = if ($cleanupErrors.Count) { 'FAIL' } else { 'PASS' }
    $summary.cleanup_errors = $cleanupErrors.ToArray()
    if ($operationFailure) { $summary.failure = $operationFailure.Message }
    try {
        $summary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $scratch 'acceptance.json') -Encoding utf8NoBOM
    } catch {
        $cleanupErrors.Add("Не удалось записать итог приёмки: $($_.Exception.GetType().Name)")
    }
    Write-Output "Артефакты приёмки службы: $scratch"
}
if ($operationFailure) { throw $operationFailure }
if ($cleanupErrors.Count) { throw 'Очистка приёмки не выполнена; см. acceptance.json и сохранённый runtime.' }
