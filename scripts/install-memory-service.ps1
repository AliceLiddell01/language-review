param(
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
    throw 'Requires Windows and PowerShell 7.'
}
$source = Split-Path -Parent $PSScriptRoot
$runtime = [IO.Path]::GetFullPath($RuntimeDirectory)
if ($runtime.Contains('"') -or $runtime.TrimEnd('\') -eq [IO.Path]::GetPathRoot($runtime).TrimEnd('\') -or
    $runtime -eq $env:USERPROFILE -or $runtime -eq $source -or
    $runtime.StartsWith($source + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'RuntimeDirectory must be outside the checkout and contain no quotes.'
}
if (Test-Path -LiteralPath $runtime) {
    if ((Get-Item -LiteralPath $runtime -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'RuntimeDirectory must not be a link.'
    }
    if (@(Get-ChildItem -LiteralPath $runtime -Force).Count -and
        -not (Test-Path -LiteralPath (Join-Path $runtime 'deployment.json')) -and
        -not (Test-Path -LiteralPath (Join-Path $runtime 'database.credential.xml'))) {
        throw 'Refusing to change permissions of a nonempty directory not owned by this deployment.'
    }
}
$docker = (Get-Command docker -ErrorAction Stop).Source
$uv = (Get-Command uv -ErrorAction Stop).Source
$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
$taskName = 'LanguageReview-Memory-' + $Project
$manifestPath = Join-Path $runtime 'deployment.json'
$credentialPath = Join-Path $runtime 'database.credential.xml'
$old = if (Test-Path -LiteralPath $manifestPath) {
    Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
} else { $null }
if ($old -and ($old.project -ne $Project -or $old.port -ne $Port)) {
    throw 'Existing deployment has different project/port; use a separate RuntimeDirectory.'
}
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask -and $existingTask.Description -ne "Language Review memory: $runtime") {
    throw 'Task name already belongs to a different deployment.'
}
& $docker info --format '{{.ServerVersion}}'
if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop with Linux containers first.' }
$tags = (Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 10).models
$llm = @($tags | Where-Object name -EQ $LlmModel)
$embedding = @($tags | Where-Object name -EQ $EmbeddingModel)
if ($llm.Count -ne 1) { throw "Install the configured local model: ollama pull $LlmModel" }
if ($embedding.Count -ne 1 -or $embedding[0].digest -ne $EmbeddingRevision) {
    throw "Install/verify $EmbeddingModel and supply its exact -EmbeddingRevision; tag content must match."
}
$probe = Invoke-RestMethod 'http://127.0.0.1:11434/api/embed' -Method Post -ContentType 'application/json' `
    -Body (@{model = $EmbeddingModel; input = @('Проверка русского технического текста CLI')} | ConvertTo-Json) -TimeoutSec 120
if ($probe.embeddings.Count -ne 1 -or $probe.embeddings[0].Count -ne $EmbeddingDimensions) {
    throw 'Embedding dimensions do not match the installed model.'
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
    # Refuse to guess/reset a password for an existing volume, even if detached.
    $volume = & $docker volume ls --filter "name=^${Project}_memory-data$" --format '{{.Name}}'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect deployment volume.' }
    if ($volume) { throw 'Existing volume requires its original database.credential.xml.' }
    $password = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
    [pscredential]::new('language_review', (ConvertTo-SecureString $password -AsPlainText -Force)) |
        Export-Clixml -LiteralPath $credentialPath
    Remove-Variable password
}
# Build from this checkout, then install only uv.lock dependencies and the built wheel.
# The scheduled process never imports this checkout or depends on its virtualenv.
$release = Join-Path $runtime ('releases\' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $release | Out-Null
Push-Location $source
try {
    & $uv export --locked --no-dev --extra memory --no-emit-project --output-file (Join-Path $release 'requirements.txt') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency export failed.' }
    & $uv build --wheel --out-dir $release
    if ($LASTEXITCODE -ne 0) { throw 'Wheel build failed.' }
    & $uv venv --python 3.15 (Join-Path $release 'venv')
    if ($LASTEXITCODE -ne 0) { throw 'Runtime environment creation failed.' }
    $python = Join-Path $release 'venv\Scripts\python.exe'
    & $uv pip install --python $python --require-hashes -r (Join-Path $release 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency install failed.' }
    $wheel = @(Get-ChildItem -LiteralPath $release -Filter '*.whl')
    if ($wheel.Count -ne 1) { throw 'Expected one built wheel.' }
    & $uv pip install --python $python --no-deps $wheel[0].FullName
    if ($LASTEXITCODE -ne 0) { throw 'Wheel install failed.' }
    # venv launchers are redirectors: Scheduler must own the actual pythonw process.
    $pythonw = (& $python -I -c "import pathlib,sys; print(pathlib.Path(sys._base_executable).with_name('pythonw.exe'))").Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
        throw 'Cannot locate the base windowless Python interpreter.'
    }
    $sourceSha = (& git rev-parse HEAD).Trim()
    $sourceDirty = [bool](& git status --porcelain=v1 --untracked-files=normal)
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
$deployment = [ordered]@{
    project = $Project; port = $Port; task = $taskName; python = $python; docker = $docker; pwsh = $pwsh
    config = $workerConfig; release = $release; worker_limit = $workerLimit; source_sha = $sourceSha; source_dirty = $sourceDirty
    wheel_sha256 = (Get-FileHash -LiteralPath $wheel[0].FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $source 'uv.lock') -Algorithm SHA256).Hash.ToLowerInvariant()
    llm_model = $LlmModel; llm_revision = $llm[0].digest
    embedding_model = $EmbeddingModel; embedding_revision = $EmbeddingRevision
}
$taskPaused = $false
$databaseEnvironment = @(
    'LANGUAGE_REVIEW_DB_PASSWORD',
    'LANGUAGE_REVIEW_DB_PORT',
    'LANGUAGE_REVIEW_MEMORY_DSN'
)
$previousDatabaseEnvironment = @{}
foreach ($name in $databaseEnvironment) {
    $previousDatabaseEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    if ($existingTask) {
        $taskPaused = $true
        Stop-ScheduledTask -TaskName $taskName
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
            if ([DateTime]::UtcNow -gt $deadline) { throw 'Existing task did not stop.' }
            Start-Sleep -Milliseconds 250
        }
        Disable-ScheduledTask -TaskName $taskName | Out-Null
    }

    try {
        $credential = Import-Clixml -LiteralPath $credentialPath
        $password = $credential.GetNetworkCredential().Password
        $env:LANGUAGE_REVIEW_DB_PASSWORD = $password
        $env:LANGUAGE_REVIEW_DB_PORT = [string]$Port
        $env:LANGUAGE_REVIEW_MEMORY_DSN = "postgresql://language_review:$password@127.0.0.1:$Port/language_review"

        & $docker compose -f (Join-Path $source 'compose.memory.yml') -p $Project up -d --wait --wait-timeout 120
        if ($LASTEXITCODE -ne 0) { throw 'Database startup failed; credentials and volume preserved.' }
        & $python -I -m language_review --config $candidateWorkerConfig memory migrate
        if ($LASTEXITCODE -ne 0) { throw 'Database migration failed; active release was preserved.' }
        & $python -I -m language_review --config $candidateWorkerConfig memory health
        if ($LASTEXITCODE -ne 0) { throw 'Database health failed; active release was preserved.' }
    } finally {
        foreach ($name in $databaseEnvironment) {
            [Environment]::SetEnvironmentVariable($name, $previousDatabaseEnvironment[$name], 'Process')
        }
        Remove-Variable credential, password -ErrorAction SilentlyContinue
    }

    Copy-Item -LiteralPath (Join-Path $source 'compose.memory.yml') -Destination $runtime -Force
    foreach ($file in @('connect-memory.ps1', 'run-memory-service.ps1', 'run-memory-service.pyw')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $runtime -Force
    }
    $workerConfigTemp = Join-Path $runtime ('worker.toml.' + [guid]::NewGuid().ToString('N') + '.tmp')
    Copy-Item -LiteralPath $candidateWorkerConfig -Destination $workerConfigTemp
    [IO.File]::Move($workerConfigTemp, $workerConfig, $true)
    $manifestTemp = Join-Path $runtime ('deployment.json.' + [guid]::NewGuid().ToString('N') + '.tmp')
    $deployment | ConvertTo-Json | Set-Content -LiteralPath $manifestTemp -Encoding utf8NoBOM
    [IO.File]::Move($manifestTemp, $manifestPath, $true)

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
        -Settings $settings -Description "Language Review memory: $runtime" -Force | Out-Null
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    $taskPaused = $false
    Start-ScheduledTask -TaskName $taskName
    $deadline = [DateTime]::UtcNow.AddSeconds($workerLimit * $llmTimeoutSeconds + 180)
    $stateFile = Join-Path $runtime 'logs\service-state.json'
    $started = [DateTime]::UtcNow.AddSeconds(-2)
    do {
        Start-Sleep -Milliseconds 500
        $ready = if (Test-Path -LiteralPath $stateFile) {
            Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
        } else { $null }
        if ($ready -and $ready.release -eq $release -and [DateTime]$ready.at -gt $started -and
            $ready.status -in @('idle', 'processed', 'partial')) { break }
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $ready -or $ready.release -ne $release -or [DateTime]$ready.at -lt $started -or
        $ready.status -notin @('idle', 'processed', 'partial')) {
        throw "Task did not report readiness; inspect $stateFile and logs."
    }
    Write-Output "Installed: $taskName ($($ready.status)); state: $stateFile"
} finally {
    if ($taskPaused) {
        Enable-ScheduledTask -TaskName $taskName | Out-Null
        Start-ScheduledTask -TaskName $taskName
    }
}
