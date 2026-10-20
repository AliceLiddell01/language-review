param(
    [string]$LiveLlmModel,
    [string]$EmbeddingModel,
    [string]$EmbeddingRevision,
    [int]$EmbeddingDimensions = 768
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('language-review-memory-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $scratch | Out-Null
$project = 'lr-memory-acceptance-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
if ($project -notmatch '^lr-memory-acceptance-[0-9a-f]{12}$') { throw 'Unsafe test project identity' }
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()
$names = @('LANGUAGE_REVIEW_DB_PASSWORD', 'LANGUAGE_REVIEW_DB_PORT',
    'LANGUAGE_REVIEW_MEMORY_DSN', 'LANGUAGE_REVIEW_MEMORY_TEST_DSN',
    'LANGUAGE_REVIEW_MEMORY_LIVE_LLM', 'LANGUAGE_REVIEW_MEMORY_EMBEDDING_MODEL',
    'LANGUAGE_REVIEW_MEMORY_EMBEDDING_REVISION', 'LANGUAGE_REVIEW_MEMORY_EMBEDDING_DIMENSIONS',
    'APPDATA', 'XDG_CONFIG_HOME')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
function Invoke-Checked([string]$Executable, [string[]]$CommandArguments) {
    & $Executable @CommandArguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed: $LASTEXITCODE" }
}
$compose = @('compose', '-f', (Join-Path $repo 'compose.memory.yml'), '-p', $project)
$summary = @{status='FAIL'; environment='Windows/PowerShell/Python'; artifact_directory=$scratch}
Push-Location $repo
try {
    $env:LANGUAGE_REVIEW_DB_PASSWORD = [guid]::NewGuid().ToString('N')
    $env:LANGUAGE_REVIEW_DB_PORT = [string]$port
    $env:LANGUAGE_REVIEW_MEMORY_DSN = "postgresql://language_review:$($env:LANGUAGE_REVIEW_DB_PASSWORD)@127.0.0.1:$port/language_review"
    $env:LANGUAGE_REVIEW_MEMORY_TEST_DSN = $env:LANGUAGE_REVIEW_MEMORY_DSN
    $env:LANGUAGE_REVIEW_MEMORY_LIVE_LLM = $LiveLlmModel
    $env:LANGUAGE_REVIEW_MEMORY_EMBEDDING_MODEL = $EmbeddingModel
    $env:LANGUAGE_REVIEW_MEMORY_EMBEDDING_REVISION = $EmbeddingRevision
    $env:LANGUAGE_REVIEW_MEMORY_EMBEDDING_DIMENSIONS = [string]$EmbeddingDimensions
    $env:APPDATA = Join-Path $scratch 'appdata'
    $env:XDG_CONFIG_HOME = Join-Path $scratch 'xdg-config'
    New-Item -ItemType Directory -Force -Path $env:APPDATA, $env:XDG_CONFIG_HOME | Out-Null
    Invoke-Checked docker ($compose + @('up', '-d', '--wait'))
    Invoke-Checked uv @('run','--locked','--extra','memory','language-review','memory','migrate')
    Invoke-Checked uv @('run','--locked','--extra','memory','language-review','memory','health')
    Invoke-Checked uv @('run','--locked','--extra','memory','pytest','-n','8',
        'tests/test_memory.py','tests/test_memory_integration.py','tests/test_memory_worker.py')
    $before = Join-Path $scratch 'before.json'
    Invoke-Checked uv @('run','--locked','--extra','memory','python','scripts/memory-snapshot.py','--output',$before)
    Invoke-Checked docker ($compose + @('restart','postgres'))
    Invoke-Checked docker ($compose + @('up','-d','--wait'))
    Invoke-Checked uv @('run','--locked','--extra','memory','python','scripts/memory-snapshot.py',
        '--output',(Join-Path $scratch 'restarted.json'),'--expect',$before)
    Invoke-Checked docker ($compose + @('up','-d','--force-recreate','--wait'))
    Invoke-Checked uv @('run','--locked','--extra','memory','python','scripts/memory-snapshot.py',
        '--output',(Join-Path $scratch 'recreated.json'),'--expect',$before)
    $backup = Join-Path $scratch 'memory.dump'
    & (Join-Path $PSScriptRoot 'memory-db.ps1') -Action Backup -Project $project -BackupPath $backup
    # Только сгенерированный выше acceptance project; пользовательские volumes не выбираются.
    Invoke-Checked docker ($compose + @('down','--volumes'))
    Invoke-Checked docker ($compose + @('up','-d','--wait'))
    & (Join-Path $PSScriptRoot 'memory-db.ps1') -Action Restore -Project $project -BackupPath $backup
    Invoke-Checked uv @('run','--locked','--extra','memory','language-review','memory','health')
    Invoke-Checked uv @('run','--locked','--extra','memory','python','scripts/memory-snapshot.py',
        '--output',(Join-Path $scratch 'restored.json'),'--expect',$before)
    # Реальный отказ PostgreSQL: код 6, пустой stdout; --no-memory сохраняет прежний анализ.
    Invoke-Checked docker ($compose + @('stop','postgres'))
    $source = Join-Path $scratch 'input.md'
    [IO.File]::WriteAllText($source, 'Корректный русский текст.')
    $unavailable = & uv run --locked --extra memory language-review check $source --engine vale `
        --memory --memory-project ([guid]::NewGuid()) --format json 2> (Join-Path $scratch 'db-failure.stderr.log')
    if ($LASTEXITCODE -ne 6 -or $unavailable) { throw 'Database failure did not fail closed' }
    & uv run --locked language-review check $source --engine vale --no-memory --format json `
        1> (Join-Path $scratch 'without-memory.json') 2> (Join-Path $scratch 'without-memory.stderr.log')
    if ($LASTEXITCODE -notin @(0,1,5)) { throw 'Disabled memory changed baseline behavior' }
    $summary.status = 'PASS'
    $summary.database_loss_restore = 'PASS'
    $summary.restart_recreate = 'PASS'
    $summary.database_failure = 'PASS'
    $summary.live_llm = $(if ($LiveLlmModel) { 'PASS' } else { 'NOT_RUN' })
    $summary.live_embeddings = $(if ($EmbeddingModel) { 'PASS' } else { 'NOT_RUN' })
} finally {
    $summary | ConvertTo-Json | Set-Content (Join-Path $scratch 'acceptance.json') -Encoding utf8NoBOM
    try { Invoke-Checked docker ($compose + @('down','--volumes')) } finally {
        foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
        Pop-Location
    }
    Write-Output "Acceptance artifacts: $scratch"
}
