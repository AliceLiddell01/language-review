param(
    [Parameter(Mandatory = $true)][string]$Destination,
    [string]$Config,
    [ValidateSet('vale', 'languagetool', 'llm')][string[]]$Engine = @('vale'),
    [string]$CacheRoot,
    [ValidateRange(1, 10000)][int]$PairLimit = 24,
    [ValidateRange(1, 10000)][int]$BookLimit = 250,
    [ValidateRange(1, 10000)][int]$CodeLimit = 24
)
$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$target = [IO.Path]::GetFullPath($Destination)
$parent = (Resolve-Path -LiteralPath (Split-Path -Parent $target) -ErrorAction Stop).Path
$target = Join-Path $parent (Split-Path -Leaf $target)
if ($target.Equals($repository, [StringComparison]::OrdinalIgnoreCase) -or
    $target.StartsWith($repository + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Данные и результаты должны находиться вне checkout.'
}
if (Test-Path -LiteralPath $target) { throw 'Нужен новый каталог артефактов.' }
if ($Config) { $Config = (Resolve-Path -LiteralPath $Config).Path }
if ($CacheRoot) { $CacheRoot = (Resolve-Path -LiteralPath $CacheRoot).Path }
function Invoke-Evaluation([string[]]$Arguments, [int[]]$Allowed = @(0)) {
    & uv run --locked language-review-eval @Arguments
    if ($LASTEXITCODE -notin $Allowed) { throw "Evaluation завершился с кодом $LASTEXITCODE" }
}
Push-Location -LiteralPath $repository
try {
    $sha = (git rev-parse HEAD).Trim()
    if (git status --porcelain=v1) { throw 'Нужен чистый committed checkout для привязки capture к SHA.' }
    & uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить locked runtime.' }
    New-Item -ItemType Directory -Path $target | Out-Null
    $metadata = [ordered]@{
        product_commit = $sha
        python = (& uv run --locked python --version | Out-String).Trim()
        uv = (& uv --version | Out-String).Trim()
        powershell = $PSVersionTable.PSVersion.ToString()
        engines = $Engine
        config_sha256 = if ($Config) { (Get-FileHash -LiteralPath $Config -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
        protocol = 'Published correction agreement; draft annotation; no independent baseline claim'
    }
    $metadata | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $target 'environment.json') -Encoding utf8
    $sources = @(
        @('rust-book-ru', 'dev', $BookLimit),
        @('sage-github-ru', 'test', $PairLimit),
        @('cubic-spline-ru', 'validation', $CodeLimit),
        @('xnet-ru', 'validation', $CodeLimit),
        @('far-ru', 'validation', $CodeLimit),
        @('tglock-cli-ru', 'validation', $CodeLimit)
    )
    $corpora = @()
    foreach ($source in $sources) {
        $name, $split, $limit = $source
        $bundle = Join-Path $target $name
        $arguments = @('import-source', $name, '--limit', "$limit", '--output', $bundle)
        if ($CacheRoot) { $arguments += @('--cache', (Join-Path $CacheRoot $name)) }
        Invoke-Evaluation $arguments > (Join-Path $target "$name-import.json")
        $corpus = Join-Path $bundle 'corpus.json'
        $corpora += (Get-Content -Raw -LiteralPath $corpus | ConvertFrom-Json)
        Invoke-Evaluation @('validate', $corpus) > (Join-Path $target "$name-validation.json")
        Invoke-Evaluation @('annotation-export', $corpus, '--split', $split, '--output', (Join-Path $target "$name-blind"))
        Invoke-Evaluation @('annotation-import', $corpus, (Join-Path $target "$name-blind"), '--output', (Join-Path $target "$name-untouched.json"))
        if ((Get-Content -Raw -LiteralPath $corpus | ConvertFrom-Json | ConvertTo-Json -Depth 100 -Compress) -cne
            (Get-Content -Raw -LiteralPath (Join-Path $target "$name-untouched.json") | ConvertFrom-Json | ConvertTo-Json -Depth 100 -Compress)) {
            throw 'Untouched пакет неожиданно изменил разметку.'
        }
        if ($split -eq 'test') {
            $frozen = Join-Path $target "$name-frozen.json"
            Invoke-Evaluation @('freeze-test', $corpus, '--output', $frozen)
            $corpus = $frozen # Draft snapshot, not an independently verified release.
        }
    }
    $combined = $corpora[0]
    $combined.id = 'real-reference-pilot'
    $combined.version = $sha
    $combined.documents = @($corpora | ForEach-Object { $_.documents })
    $combined.cases = @($corpora | ForEach-Object { $_.cases })
    $combined.split_policy.strategy = 'Distinct named repository families; normalized duplicate/near-duplicate guard across all splits; SAGE original repository identities unknown'
    $combinedPath = Join-Path $target 'combined-corpus.json'
    $combined | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $combinedPath -Encoding utf8
    Invoke-Evaluation @('validate', $combinedPath) > (Join-Path $target 'cross-split-validation.json')
    foreach ($source in $sources) {
        $name, $split, $limit = $source
        $bundle = Join-Path $target $name
        $corpus = if ($split -eq 'test') { Join-Path $target "$name-frozen.json" } else { Join-Path $bundle 'corpus.json' }
        $run = Join-Path $target "$name-run"
        $arguments = @('run', $corpus, '--split', $split, '--product-commit', $sha, '--output', $run, '--timeout', '900')
        foreach ($selected in $Engine) { $arguments += @('--engine', $selected) }
        if ($Config) { $arguments += @('--config', $Config) }
        Invoke-Evaluation $arguments @(4) > (Join-Path $target "$name-capture.json")
        $report = Join-Path $run 'report-1.json'
        $manifest = Join-Path $run 'run-1.json'
        if (-not (Test-Path -LiteralPath $report)) { throw "Нет действительного live report: $name" }
        $rescored = Join-Path $target "$name-rescored.json"
        Invoke-Evaluation @('score', $corpus, $report, '--split', $split, '--run-manifest', $manifest, '--format', 'json', '--output', $rescored) @(4)
        Invoke-Evaluation @('compare', (Join-Path $run 'evaluation-1.json'), $rescored, '--format', 'json', '--output', (Join-Path $target "$name-compare.json"))
        if ((Get-FileHash -LiteralPath (Join-Path $run 'evaluation-1.json')).Hash -ne (Get-FileHash -LiteralPath $rescored).Hash) {
            throw 'Повторный offline scoring не совпал побайтово.'
        }
        if ($name -eq 'sage-github-ru') {
            foreach ($replay in 1..2) {
                Invoke-Evaluation @('reference-score', $corpus, $report, '--source-bundle', $bundle, '--run-manifest', $manifest, '--split', 'test', '--output', (Join-Path $target "reference-$replay.json")) @(0, 4)
            }
            if ((Get-FileHash -LiteralPath (Join-Path $target 'reference-1.json')).Hash -ne (Get-FileHash -LiteralPath (Join-Path $target 'reference-2.json')).Hash) {
                throw 'Повторный reference assessment не совпал побайтово.'
            }
        }
    }
    if ((git rev-parse HEAD).Trim() -ne $sha -or (git status --porcelain=v1)) { throw 'Source checkout изменился при capture.' }
}
finally { Pop-Location }
Write-Output "Артефакты: $target. Независимая разметка и judgments не выполнялись."
