$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = (& uv python find 3.15).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Не удалось найти Python 3.15 через uv.'
}
$pyprojectPath = Join-Path $projectRoot 'pyproject.toml'
$expectedVersion = (
    & $python -I -c "import sys, tomllib; print(tomllib.load(open(sys.argv[1], 'rb'))['project']['version'])" $pyprojectPath
).Trim()
if ($LASTEXITCODE -ne 0 -or -not $expectedVersion) {
    throw 'Не удалось прочитать каноническую версию пакета из pyproject.toml.'
}
$expectedVersionOutput = "language-review $expectedVersion"
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('language-review-smoke-' + [guid]::NewGuid().ToString('N'))
$tempRoot = [IO.Path]::GetFullPath($tempRoot)
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
if (-not $tempRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Временный путь smoke-проверки вышел за системный каталог временных файлов.'
}
$previousAppData = $env:APPDATA
$previousPythonIOEncoding = $env:PYTHONIOENCODING
$previousOutputEncoding = [Console]::OutputEncoding
New-Item -ItemType Directory -Path $tempRoot | Out-Null
$env:APPDATA = Join-Path $tempRoot 'appdata'
New-Item -ItemType Directory -Path $env:APPDATA | Out-Null
try {
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
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
    $sampleFile = Join-Path $sample 'Пример.rs'
    Set-Content -LiteralPath $sampleFile `
        -Value 'fn main() { println!("Пример для установленного пакета"); }' -Encoding utf8NoBOM
    $sampleHash = (Get-FileHash -LiteralPath $sampleFile -Algorithm SHA256).Hash
    $artifacts = @(
        Get-ChildItem -LiteralPath $distributionDirectory -File |
            Where-Object { $_.Name -like "language_review-$expectedVersion*" } |
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
        if ($LASTEXITCODE -ne 0 -or $version.Trim() -ne $expectedVersionOutput) {
            throw "Установленная команда $kind --version не прошла smoke-проверку."
        }
        $moduleVersion = (& $environmentPython -m language_review --version) -join ''
        if ($LASTEXITCODE -ne 0 -or $moduleVersion.Trim() -ne $expectedVersionOutput) {
            throw "Установленный модуль $kind не прошёл smoke-проверку."
        }
        $installedVersion = (
            & $environmentPython -I -c "import importlib.metadata as m; print(m.version('language-review'))"
        ).Trim()
        if ($LASTEXITCODE -ne 0 -or $installedVersion -ne $expectedVersion) {
            throw "Метаданные установленного $kind не совпадают с версией pyproject.toml."
        }
        $jsonOutput = (& $launcher scan $sample --format json) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0) { throw "Установленная команда $kind scan завершилась ошибкой." }
        if ((Get-FileHash -LiteralPath $sampleFile -Algorithm SHA256).Hash -ne $sampleHash) {
            throw "Установленный пакет $kind scan изменил исходный файл."
        }
        $report = $jsonOutput | ConvertFrom-Json
        if ($report.schema_version -ne '1.0' -or $report.summary.selected -ne 1) {
            throw "Установленная команда $kind вернула неожиданный отчёт."
        }

        $extractJsonOutput = (& $launcher extract $sample --format json) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0) {
            throw "Установленная команда $kind extract завершилась ошибкой."
        }
        if ((Get-FileHash -LiteralPath $sampleFile -Algorithm SHA256).Hash -ne $sampleHash) {
            throw "Установленный пакет $kind extract изменил исходный файл."
        }
        $extractReport = $extractJsonOutput | ConvertFrom-Json
        $extractedText = @(
            $extractReport.documents |
                ForEach-Object { $_.fragments } |
                ForEach-Object { $_.text }
        )
        if (
            $extractReport.schema -ne 'language-review.extract' -or
            $extractReport.summary.extracted -ne 1 -or
            $extractedText -notcontains 'Пример для установленного пакета'
        ) {
            throw "Установленная команда $kind extract вернула неожиданный отчёт."
        }

        $moduleExtractOutput = (
            & $environmentPython -m language_review extract $sample --format json
        ) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0) {
            throw "Установленный модуль $kind extract завершился ошибкой."
        }
        if ((Get-FileHash -LiteralPath $sampleFile -Algorithm SHA256).Hash -ne $sampleHash) {
            throw "Установленный модуль $kind extract изменил исходный файл."
        }
        $moduleExtractReport = $moduleExtractOutput | ConvertFrom-Json
        if (
            $moduleExtractReport.schema -ne 'language-review.extract' -or
            $moduleExtractReport.summary.fragments -lt 1
        ) {
            throw "Установленный модуль $kind extract вернул неожиданный отчёт."
        }

        # check без сервисов: Vale проверяется встроенными правилами из пакета, если программа
        # установлена; иначе ожидается явный статус unavailable и код 5, а не успех.
        $valeAvailable = $null -ne (Get-Command vale -ErrorAction SilentlyContinue)
        $checkJsonOutput = (
            & $launcher check $sampleFile --engine vale --format json
        ) -join [Environment]::NewLine
        $checkExitCode = $LASTEXITCODE
        if ((Get-FileHash -LiteralPath $sampleFile -Algorithm SHA256).Hash -ne $sampleHash) {
            throw "Установленный пакет $kind check изменил исходный файл."
        }
        $checkReport = $checkJsonOutput | ConvertFrom-Json
        if ($checkReport.schema -ne 'language-review.check' -or $checkReport.suggestions_applied) {
            throw "Установленная команда $kind check вернула неожиданный отчёт."
        }
        if ($valeAvailable) {
            if ($checkExitCode -ne 0 -or $checkReport.status -ne 'clean' -or
                $checkReport.engines[0].details.configuration -ne 'bundled' -or
                $checkReport.engines[0].status -ne 'ok') {
                throw "Установленная команда $kind check с Vale вернула код $checkExitCode."
            }
        }
        elseif ($checkExitCode -ne 5 -or $checkReport.status -ne 'unavailable') {
            throw "Установленная команда $kind check без Vale вернула код $checkExitCode."
        }
        Write-Output "${kind}: установка, entry point, python -m, scan, extract и check прошли."
    }
}
finally {
    $env:APPDATA = $previousAppData
    $env:PYTHONIOENCODING = $previousPythonIOEncoding
    [Console]::OutputEncoding = $previousOutputEncoding
    $finalTemp = [IO.Path]::GetFullPath($tempRoot)
    if ($finalTemp.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $finalTemp -Recurse -Force
    }
}
