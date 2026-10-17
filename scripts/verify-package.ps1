$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = (& uv python find 3.15).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Не удалось найти Python 3.15 через uv.'
}
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('language-review-smoke-' + [guid]::NewGuid().ToString('N'))
$tempRoot = [IO.Path]::GetFullPath($tempRoot)
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
if (-not $tempRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Временный путь smoke-проверки вышел за системный каталог временных файлов.'
}
$previousAppData = $env:APPDATA
New-Item -ItemType Directory -Path $tempRoot | Out-Null
$env:APPDATA = Join-Path $tempRoot 'appdata'
New-Item -ItemType Directory -Path $env:APPDATA | Out-Null
try {
    $distributionDirectory = Join-Path $tempRoot 'dist'
    New-Item -ItemType Directory -Path $distributionDirectory | Out-Null
    Push-Location $projectRoot
    try {
        & uv build --wheel --sdist --out-dir $distributionDirectory
        if ($LASTEXITCODE -ne 0) { throw 'Сборка wheel/sdist завершилась ошибкой.' }
    }
    finally {
        Pop-Location
    }

    $sample = Join-Path $tempRoot 'пример'
    New-Item -ItemType Directory -Path $sample | Out-Null
    Set-Content -LiteralPath (Join-Path $sample 'Пример.rs') -Value 'fn main() {}' -Encoding utf8NoBOM
    $artifacts = @(
        Get-ChildItem -LiteralPath $distributionDirectory -File |
            Where-Object { $_.Name -like 'language_review-0.1.0*' } |
            Sort-Object Name
    )
    if ($artifacts.Count -ne 2) {
        throw "Ожидались wheel и sdist, найдено: $($artifacts.Count)."
    }

    foreach ($artifact in $artifacts) {
        $kind = if ($artifact.Extension -eq '.whl') { 'wheel' } else { 'sdist' }
        $environment = Join-Path $tempRoot ('venv-' + $kind)
        & uv venv --python $python $environment
        if ($LASTEXITCODE -ne 0) { throw "Не удалось создать окружение $kind." }
        $environmentPython = Join-Path $environment 'Scripts\python.exe'
        & uv pip install --python $environmentPython $artifact.FullName
        if ($LASTEXITCODE -ne 0) { throw "Не удалось установить $kind." }

        $launcher = Join-Path $environment 'Scripts\language-review.exe'
        $version = (& $launcher --version) -join ''
        if ($LASTEXITCODE -ne 0 -or $version.Trim() -ne 'language-review 0.1.0') {
            throw "Установленная команда $kind --version не прошла smoke-проверку."
        }
        $moduleVersion = (& $environmentPython -m language_review --version) -join ''
        if ($LASTEXITCODE -ne 0 -or $moduleVersion.Trim() -ne 'language-review 0.1.0') {
            throw "Установленный модуль $kind не прошёл smoke-проверку."
        }
        $jsonOutput = (& $launcher scan $sample --format json) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0) { throw "Установленная команда $kind scan завершилась ошибкой." }
        $report = $jsonOutput | ConvertFrom-Json
        if ($report.schema_version -ne '1.0' -or $report.summary.selected -ne 1) {
            throw "Установленная команда $kind вернула неожиданный отчёт."
        }
        Write-Output "${kind}: установка, entry point, python -m и scan прошли."
    }
}
finally {
    $env:APPDATA = $previousAppData
    $finalTemp = [IO.Path]::GetFullPath($tempRoot)
    if ($finalTemp.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $finalTemp -Recurse -Force
    }
}
