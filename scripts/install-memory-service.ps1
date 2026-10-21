param(
    [string]$ReleaseDirectory,
    [string]$RuntimeDirectory = (Join-Path $env:USERPROFILE '.language-review\memory'),
    [ValidatePattern('^[a-z][a-z0-9-]{2,60}$')][string]$Project = 'language-review-memory',
    [ValidateRange(1024, 65535)][int]$Port = 54329,
    [ValidatePattern('^[a-zA-Z0-9._:/-]+$')][string]$LlmModel = 'qwen3.5:4b',
    [ValidatePattern('^(embeddinggemma|qwen3-embedding)[a-zA-Z0-9._:/-]*$')]
    [string]$EmbeddingModel = 'embeddinggemma:300m',
    [ValidatePattern('^[a-f0-9]{64}$')]
    [string]$EmbeddingRevision = '85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1',
    [ValidateRange(1, 2000)][int]$EmbeddingDimensions = 768
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $PSVersionTable.PSVersion.Major -lt 7) {
    throw 'Требуются Windows и PowerShell 7.'
}
$source = if ($ReleaseDirectory) { [IO.Path]::GetFullPath($ReleaseDirectory) } else { Split-Path -Parent $PSScriptRoot }
$releaseManifest = $null
if ($ReleaseDirectory) {
    $releaseManifest = Get-Content -Raw -LiteralPath (Join-Path $source 'release.json') | ConvertFrom-Json
    if ($releaseManifest.schema -ne 'language-review.release/1' -or $releaseManifest.commit -notmatch '^[a-f0-9]{40}$') {
        throw 'Некорректный релизный комплект memory-worker.'
    }
    foreach ($entry in $releaseManifest.assets.PSObject.Properties) {
        if ($entry.Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$' -or $entry.Name.Contains('..') -or
            (Get-FileHash -LiteralPath (Join-Path $source $entry.Name)).Hash.ToLowerInvariant() -ne $entry.Value.sha256) {
            throw 'Релизный asset memory-worker повреждён.'
        }
    }
}
$runtime = [IO.Path]::GetFullPath($RuntimeDirectory)
if ($runtime.Contains('"') -or $runtime.TrimEnd('\') -eq [IO.Path]::GetPathRoot($runtime).TrimEnd('\') -or
    $runtime -eq $env:USERPROFILE -or $runtime -eq $source -or
    $runtime.StartsWith($source + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'RuntimeDirectory должен находиться вне checkout и не содержать кавычек.'
}
if (Test-Path -LiteralPath $runtime) {
    if ((Get-Item -LiteralPath $runtime -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'RuntimeDirectory не должен быть ссылкой.'
    }
    if (@(Get-ChildItem -LiteralPath $runtime -Force).Count -and
        -not (Test-Path -LiteralPath (Join-Path $runtime 'deployment.json')) -and
        -not (Test-Path -LiteralPath (Join-Path $runtime 'database.credential.xml'))) {
        throw 'Отказ менять права непустого каталога, не принадлежащего этой установке.'
    }
}
$docker = (Get-Command docker -ErrorAction Stop).Source
$uv = (Get-Command uv -ErrorAction Stop).Source
$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
$taskName = 'LanguageReview-Memory-' + $Project
$taskDescription = "Language Review memory: $runtime"
$manifestPath = Join-Path $runtime 'deployment.json'
$credentialPath = Join-Path $runtime 'database.credential.xml'
$old = if (Test-Path -LiteralPath $manifestPath) {
    Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
} else { $null }
if ($old -and ($old.project -ne $Project -or $old.port -ne $Port)) {
    throw 'Существующая установка использует другие project/port; задайте отдельный RuntimeDirectory.'
}
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask -and $existingTask.Description -ne $taskDescription) {
    throw 'Задача с этим именем принадлежит другой установке.'
}
& $docker info --format '{{.ServerVersion}}'
if ($LASTEXITCODE -ne 0) { throw 'Сначала запустите Docker Desktop с Linux-контейнерами.' }
$tags = (Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 10).models
$llm = @($tags | Where-Object name -EQ $LlmModel)
$embedding = @($tags | Where-Object name -EQ $EmbeddingModel)
if ($llm.Count -ne 1) { throw "Установите настроенную локальную модель: ollama pull $LlmModel" }
if ($embedding.Count -ne 1 -or $embedding[0].digest -ne $EmbeddingRevision) {
    throw "Установите/проверьте $EmbeddingModel и передайте точный -EmbeddingRevision; содержимое тега должно совпадать."
}
$probe = Invoke-RestMethod 'http://127.0.0.1:11434/api/embed' -Method Post -ContentType 'application/json' `
    -Body (@{model = $EmbeddingModel; input = @('Проверка русского технического текста CLI')} | ConvertTo-Json) -TimeoutSec 120
if ($probe.embeddings.Count -ne 1 -or $probe.embeddings[0].Count -ne $EmbeddingDimensions) {
    throw 'Размерность embedding не совпадает с установленной моделью.'
}
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$directoryInfo = [IO.DirectoryInfo]::new($runtime)
$acl = [IO.FileSystemAclExtensions]::GetAccessControl($directoryInfo, [Security.AccessControl.AccessControlSections]::Access)
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
    $acl.RemoveAccessRuleAll($rule)
}
foreach ($sid in @($identity.User, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
[IO.FileSystemAclExtensions]::SetAccessControl($directoryInfo, $acl)
if (-not (Test-Path -LiteralPath $credentialPath)) {
    # Пароль существующего volume не угадывается и не сбрасывается, даже если volume не подключён.
    $volume = & $docker volume ls --filter "name=^${Project}_memory-data$" --format '{{.Name}}'
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось проверить volume установки.' }
    if ($volume) { throw 'Для существующего volume нужен его исходный database.credential.xml.' }
    $password = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
    [pscredential]::new('language_review', (ConvertTo-SecureString $password -AsPlainText -Force)) |
        Export-Clixml -LiteralPath $credentialPath
    Remove-Variable password
}
# Сборка из этого checkout; затем устанавливаются только зависимости uv.lock и собранный wheel.
# Процесс задачи не импортирует checkout и не зависит от его virtualenv.
$release = Join-Path $runtime ('releases\' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $release | Out-Null
Push-Location $source
try {
    if ($ReleaseDirectory) {
        Copy-Item -LiteralPath (Join-Path $source 'requirements-memory.txt') -Destination (Join-Path $release 'requirements.txt')
        $releasedWheels = @(Get-ChildItem -LiteralPath $source -Filter '*.whl')
        if ($releasedWheels.Count -ne 1) { throw 'Ожидался один релизный wheel.' }
        Copy-Item -LiteralPath $releasedWheels[0].FullName -Destination $release
    } else {
        & $uv export --locked --no-dev --extra memory --no-emit-project --output-file (Join-Path $release 'requirements.txt') | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось экспортировать закреплённые зависимости.' }
        & $uv build --wheel --out-dir $release
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось собрать wheel.' }
    }
    & $uv venv --python 3.15 (Join-Path $release 'venv')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение runtime.' }
    $python = Join-Path $release 'venv\Scripts\python.exe'
    & $uv pip install --python $python --require-hashes -r (Join-Path $release 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить закреплённые зависимости.' }
    $wheel = @(Get-ChildItem -LiteralPath $release -Filter '*.whl')
    if ($wheel.Count -ne 1) { throw 'Ожидался ровно один собранный wheel.' }
    & $uv pip install --python $python --no-deps $wheel[0].FullName
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить wheel.' }
    # Launcher venv — перенаправитель: планировщик должен владеть настоящим процессом pythonw.
    $pythonw = (& $python -I -c "import pathlib,sys; print(pathlib.Path(sys._base_executable).with_name('pythonw.exe'))").Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
        throw 'Не найден базовый безоконный интерпретатор Python.'
    }
    if ($ReleaseDirectory) {
        $sourceSha = $releaseManifest.commit
        $sourceDirty = $false
    } else {
        $sourceSha = (& git rev-parse HEAD).Trim()
        $sourceDirty = [bool](& git status --porcelain=v1 --untracked-files=normal)
    }
} finally { Pop-Location }
# Первый прогон worker пишет состояние только после завершения: до worker_limit заданий по timeout.
$workerLimit = 8
$llmTimeoutSeconds = 120
$workerConfig = Join-Path $runtime 'worker.toml'
$candidateWorkerConfig = Join-Path $release 'worker.toml'
$configText = @"
[check]
engines = ["llm"]
[check.llm]
provider = "ollama"
timeout_seconds = $llmTimeoutSeconds
[check.llm.ollama]
model = "$LlmModel"
[memory]
enabled = true
dsn_env = "LANGUAGE_REVIEW_MEMORY_DSN"
recall_mode = "hybrid"
embedding_model = "$EmbeddingModel"
embedding_revision = "$EmbeddingRevision"
embedding_dimensions = $EmbeddingDimensions
"@
Set-Content -LiteralPath $candidateWorkerConfig -Value $configText -Encoding utf8NoBOM
# До подтверждённой готовности manifest помечает release как кандидата.
$deployment = [ordered]@{
    status = 'candidate'
    project = $Project; port = $Port; task = $taskName; python = $python; docker = $docker; pwsh = $pwsh
    config = $workerConfig; release = $release; worker_limit = $workerLimit; source_sha = $sourceSha; source_dirty = $sourceDirty
    wheel_sha256 = (Get-FileHash -LiteralPath $wheel[0].FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    lock_sha256 = if ($ReleaseDirectory) { $releaseManifest.lock_sha256 } else { (Get-FileHash -LiteralPath (Join-Path $source 'uv.lock') -Algorithm SHA256).Hash.ToLowerInvariant() }
    llm_model = $LlmModel; llm_revision = $llm[0].digest
    embedding_model = $EmbeddingModel; embedding_revision = $EmbeddingRevision
}
$databaseEnvironment = @(
    'LANGUAGE_REVIEW_DB_PASSWORD',
    'LANGUAGE_REVIEW_DB_PORT',
    'LANGUAGE_REVIEW_MEMORY_DSN'
)
# Файлы корня runtime, которые вместе с задачей образуют опубликованную установку.
$publishedFiles = @('compose.memory.yml', 'connect-memory.ps1', 'run-memory-service.ps1',
    'run-memory-service.pyw', 'worker.toml', 'deployment.json')
$previousFiles = Join-Path $release 'previous'
$previousTask = if ($existingTask) {
    @{xml = Export-ScheduledTask -TaskName $taskName; enabled = $existingTask.State -ne 'Disabled'}
} else { $null }
$stateFile = Join-Path $runtime 'logs\service-state.json'

function Invoke-WithDatabaseEnvironment([scriptblock]$Action) {
    $saved = @{}
    foreach ($name in $databaseEnvironment) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    try {
        $credential = Import-Clixml -LiteralPath $credentialPath
        $password = $credential.GetNetworkCredential().Password
        $env:LANGUAGE_REVIEW_DB_PASSWORD = $password
        $env:LANGUAGE_REVIEW_DB_PORT = [string]$Port
        $env:LANGUAGE_REVIEW_MEMORY_DSN = "postgresql://language_review:$password@127.0.0.1:$Port/language_review"
        Remove-Variable credential, password
        & $Action
    } finally {
        foreach ($name in $databaseEnvironment) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
        }
    }
}
function Write-RuntimeFile([string]$Name, [string]$From) {
    # Каждый файл заменяется атомарно: временная копия в том же каталоге и rename.
    $temp = Join-Path $runtime ($Name + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    Copy-Item -LiteralPath $From -Destination $temp
    [IO.File]::Move($temp, (Join-Path $runtime $Name), $true)
}
function Write-Manifest([string]$Status) {
    $deployment.status = $Status
    $temp = Join-Path $release 'deployment.json'
    $deployment | ConvertTo-Json | Set-Content -LiteralPath $temp -Encoding utf8NoBOM
    Write-RuntimeFile 'deployment.json' $temp
}
function Get-OwnSupervisors {
    $script = Join-Path $runtime 'run-memory-service.ps1'
    @(Get-CimInstance Win32_Process -Filter "Name = 'pwsh.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($script) })
}
function Stop-OwnTask {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
            if ([DateTime]::UtcNow -gt $deadline) { throw 'Задача не остановилась за 30 секунд.' }
            Start-Sleep -Milliseconds 250
        }
    }
    # PowerShell из MSIX-пакета не входит в Job Object: остановка задачи гарантированно
    # завершает только wrapper. Supervisor этой установки должен выйти сам; release без
    # watchdog завершается вместе с потомками по командной строке собственного runtime.
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while ((Get-OwnSupervisors).Count -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
    foreach ($supervisor in Get-OwnSupervisors) {
        try { [Diagnostics.Process]::GetProcessById([int]$supervisor.ProcessId).Kill($true) } catch [ArgumentException] {}
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while ((Get-OwnSupervisors).Count) {
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Supervisor этой установки не завершился.' }
        Start-Sleep -Milliseconds 250
    }
}
function Invoke-TestFault([string]$Stage) {
    # Управляемые сбои только для временных установок verify-memory-service.ps1.
    return $Project -match '^lr-service-test-[0-9a-f]{12}$' -and $env:LANGUAGE_REVIEW_TEST_INSTALL_FAULT -eq $Stage
}
function Restore-PreviousDeployment {
    # Компенсирующее восстановление; каждая ошибка шага сохраняется, а не скрывается.
    $errors = [Collections.Generic.List[string]]::new()
    try { Stop-OwnTask } catch { $errors.Add("остановка новой задачи: $($_.Exception.Message)") }
    if ($script:filesSnapshotted) {
        foreach ($name in $publishedFiles) {
            try {
                $saved = Join-Path $previousFiles $name
                if (Test-Path -LiteralPath $saved) {
                    Write-RuntimeFile $name $saved
                } elseif (Test-Path -LiteralPath (Join-Path $runtime $name)) {
                    Remove-Item -LiteralPath (Join-Path $runtime $name) -Force
                }
            } catch { $errors.Add("файл ${name}: $($_.Exception.Message)") }
        }
    }
    try {
        if ($previousTask) {
            Register-ScheduledTask -TaskName $taskName -Xml $previousTask.xml -Force | Out-Null
            if ($previousTask.enabled) {
                Enable-ScheduledTask -TaskName $taskName | Out-Null
                Start-ScheduledTask -TaskName $taskName
            } else {
                Disable-ScheduledTask -TaskName $taskName | Out-Null
            }
        } else {
            $created = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($created -and $created.Description -eq $taskDescription) {
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            }
        }
    } catch { $errors.Add("задача ${taskName}: $($_.Exception.Message)") }
    if ($script:migrated -and $old -and $errors.Count -eq 0) {
        # Миграции не откатываются: проверяем, принимает ли прежний release текущую схему.
        try {
            Invoke-WithDatabaseEnvironment {
                & $old.python -I -m language_review --config $old.config memory health *> $null
                if ($LASTEXITCODE -ne 0) {
                    $errors.Add('прежний release не принимает текущую схему БД (memory health, код ' +
                        "$LASTEXITCODE); применённые кандидатом миграции автоматически не откатываются, " +
                        'повторите обновление после устранения причины или восстановите backup')
                }
            }
        } catch { $errors.Add("проверка прежнего release: $($_.Exception.Message)") }
    }
    return $errors
}

$script:filesSnapshotted = $false
$script:migrated = $false
$committed = $false
try {
    if ($existingTask) {
        Stop-OwnTask
        Disable-ScheduledTask -TaskName $taskName | Out-Null
    }
    Invoke-WithDatabaseEnvironment {
        & $docker compose -f (Join-Path $source 'compose.memory.yml') -p $Project up -d --wait --wait-timeout 120
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось запустить БД; учётные данные и volume сохранены.' }
        $script:migrated = $true
        & $python -I -m language_review --config $candidateWorkerConfig memory migrate
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось применить миграции БД.' }
        & $python -I -m language_review --config $candidateWorkerConfig memory health
        if ($LASTEXITCODE -ne 0) { throw 'Проверка БД (memory health) не пройдена.' }
    }

    New-Item -ItemType Directory -Path $previousFiles | Out-Null
    foreach ($name in $publishedFiles) {
        $current = Join-Path $runtime $name
        if (Test-Path -LiteralPath $current) { Copy-Item -LiteralPath $current -Destination $previousFiles }
    }
    $script:filesSnapshotted = $true
    Write-RuntimeFile 'compose.memory.yml' (Join-Path $source 'compose.memory.yml')
    foreach ($file in @('connect-memory.ps1', 'run-memory-service.ps1', 'run-memory-service.pyw')) {
        Write-RuntimeFile $file (Join-Path $PSScriptRoot $file)
    }
    if (Invoke-TestFault 'readiness') {
        # Настоящая ошибка конфигурации кандидата; её должен обнаружить штатный readiness.
        $faulty = Join-Path $release 'worker.fault.toml'
        Set-Content -LiteralPath $faulty -Value ($configText + "`ninvalid = = toml") -Encoding utf8NoBOM
        Write-RuntimeFile 'worker.toml' $faulty
    } else {
        Write-RuntimeFile 'worker.toml' $candidateWorkerConfig
    }
    Write-Manifest 'candidate'

    if (Invoke-TestFault 'register') { throw 'Контролируемый сбой на этапе регистрации задачи.' }
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument (
        '-I "' + (Join-Path $runtime 'run-memory-service.pyw') + '"')
    $trigger = @(
        (New-ScheduledTaskTrigger -AtLogOn -User $identity.Name),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
    )
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description $taskDescription -Force | Out-Null
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    if (Invoke-TestFault 'start') { throw 'Контролируемый сбой на этапе запуска задачи.' }
    Start-ScheduledTask -TaskName $taskName
    $deadline = [DateTime]::UtcNow.AddSeconds($workerLimit * $llmTimeoutSeconds + 180)
    $started = [DateTime]::UtcNow.AddSeconds(-2)
    $ready = $null
    do {
        Start-Sleep -Milliseconds 500
        $ready = if (Test-Path -LiteralPath $stateFile) {
            try { Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json } catch { $null }
        } else { $null }
        $own = $ready -and $ready.release -eq $release -and [DateTime]$ready.at -gt $started
        if ($own -and $ready.status -in @('idle', 'processed', 'partial')) { break }
        if ($own -and $ready.status -eq 'error') {
            throw "Новый release сообщил об ошибке worker ($($ready.reason)); см. $stateFile и журналы."
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $own -or $ready.status -notin @('idle', 'processed', 'partial')) {
        throw "Задача не подтвердила готовность; см. $stateFile и журналы."
    }
    # Точка фиксации: только после подтверждённой готовности release объявляется установленным.
    Write-Manifest 'installed'
    $committed = $true
    Write-Output "Установлено: $taskName ($($ready.status)); состояние: $stateFile"
} catch {
    $failure = $_.Exception.Message
    if ($committed) { throw }
    $rollbackErrors = @(Restore-PreviousDeployment)
    $outcome = if ($rollbackErrors.Count) {
        'Откат выполнен не полностью: ' + ($rollbackErrors -join '; ') +
            ". Проверьте задачу $taskName и каталог $runtime."
    } elseif ($previousTask) {
        "Прежние release, конфигурация и задача восстановлены: $($old.release)."
    } elseif ($old) {
        'Прежние файлы runtime восстановлены; задачи до обновления не было, новая задача удалена.'
    } else {
        'Задача и опубликованные файлы runtime удалены; учётные данные, volume и данные БД сохранены.'
    }
    $prefix = if ($previousTask -or $old) { 'Обновление не выполнено' } else { 'Установка не выполнена' }
    throw "${prefix}: $failure $outcome Кандидат оставлен для диагностики: $release."
}
