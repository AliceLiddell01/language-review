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
$previousLocalAppData = $env:LOCALAPPDATA
$previousUvCacheDir = $env:UV_CACHE_DIR
$previousPythonIOEncoding = $env:PYTHONIOENCODING
$previousOutputEncoding = [Console]::OutputEncoding
New-Item -ItemType Directory -Path $tempRoot | Out-Null
try {
    if ([string]::IsNullOrWhiteSpace($env:UV_CACHE_DIR)) {
        $uvCacheDirectory = (& uv cache dir).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $uvCacheDirectory) {
            throw 'Не удалось определить каталог кэша uv.'
        }
        $env:UV_CACHE_DIR = $uvCacheDirectory
    }
    $env:APPDATA = Join-Path $tempRoot 'appdata'
    $env:LOCALAPPDATA = Join-Path $tempRoot 'localappdata'
    New-Item -ItemType Directory -Path $env:APPDATA | Out-Null
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
        & $environmentPython -I -c 'from importlib.resources import files; import json; p=files("language_review.memory"); assert "CREATE EXTENSION" in p.joinpath("migrations/001.sql").read_text(); assert "lr_jobs_finding" in p.joinpath("migrations/002.sql").read_text(); assert json.loads(p.joinpath("decision.schema.json").read_text())["type"] == "object"'
        if ($LASTEXITCODE -ne 0) { throw "В $kind отсутствуют ресурсы памяти." }
        $memoryHelp = (& $launcher memory --help) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0 -or $memoryHelp -notmatch 'migrate') {
            throw "В $kind недоступен CLI памяти без optional extra."
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
        $reviewPath = Join-Path $tempRoot ('review-' + $kind + '.json')
        & $launcher check $sampleFile --engine vale --view review --format json --output $reviewPath
        if ($LASTEXITCODE -ne $checkExitCode) {
            throw "Review $kind изменил код завершения анализа."
        }
        $reviewReport = Get-Content -LiteralPath $reviewPath -Raw | ConvertFrom-Json
        if ($reviewReport.schema -ne 'language-review.review' -or
            $reviewReport.schema_version -ne '1.0' -or $reviewReport.suggestions_applied -or
            $reviewReport.analysis.schema -ne 'language-review.check') {
            throw "Review $kind вернул неожиданный контракт."
        }
        $reviewHuman = (& $launcher show-review $reviewPath --all) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0 -or $reviewHuman -notmatch 'Языковое ревью:') {
            throw "Show-review $kind не прочитал сохранённый результат."
        }
        $verificationOutput = (
            & $environmentPython -m language_review verify-review $reviewPath `
                --root ('root-0001=' + $sampleFile) --format json
        ) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0 -or ($verificationOutput | ConvertFrom-Json).status -ne 'current') {
            throw "Verify-review $kind не подтвердил исходные байты."
        }
        if ((Get-FileHash -LiteralPath $sampleFile -Algorithm SHA256).Hash -ne $sampleHash) {
            throw "Установленный пакет $kind review изменил исходный файл."
        }
        # Оценщик импортирует сохранённую синтетическую выдачу offline: серверы не нужны.
        $evalLauncher = Join-Path $environment 'Scripts/language-review-eval.exe'
        $evalFixture = Join-Path $projectRoot 'tests/fixtures/evaluation'
        $evalCorpus = Join-Path $tempRoot ("corpus-$kind.json")
        $evalReport = Join-Path $tempRoot ("saved-report-$kind.json")
        Copy-Item -LiteralPath (Join-Path $evalFixture 'corpus.json') -Destination $evalCorpus
        Copy-Item -LiteralPath (Join-Path $evalFixture 'report.json') -Destination $evalReport
        $evalCorpusHash = (Get-FileHash -LiteralPath $evalCorpus -Algorithm SHA256).Hash
        $evalReportHash = (Get-FileHash -LiteralPath $evalReport -Algorithm SHA256).Hash
        $evalValidation = (& $evalLauncher validate $evalCorpus) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0 -or ($evalValidation | ConvertFrom-Json).status -ne 'PASS') {
            throw "Оценщик $kind не прочитал упакованную схему корпуса."
        }
        $evalResult = Join-Path $tempRoot ("evaluation-$kind.json")
        & $evalLauncher score $evalCorpus $evalReport --split dev --bind 'root-0001=demo' `
            --format json --output $evalResult
        $evalExit = $LASTEXITCODE
        $scored = Get-Content -LiteralPath $evalResult -Raw | ConvertFrom-Json
        if ($evalExit -ne 4 -or $scored.status -ne 'PARTIAL' -or
            $scored.report_kind -ne 'synthetic_contract_check' -or
            $scored.summary.pipeline_detection.tp -ne 2 -or
            $scored.summary.pipeline_detection.fn -ne 9) {
            throw "Оценщик $kind вернул неверные контрольные числа или скрыл неполноту."
        }
        $expectations = Join-Path $evalFixture 'expectations.json'
        $regression = (
            & $environmentPython -m language_review.evaluation regress $evalResult $expectations
        ) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0 -or ($regression | ConvertFrom-Json).status -ne 'PASS') {
            throw "Оценщик $kind нарушил детерминированные ожидания."
        }
        $comparison = (
            & $evalLauncher compare $evalResult $evalResult --format json
        ) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0 -or ($comparison | ConvertFrom-Json).new_fp.Count -ne 0) {
            throw "Оценщик $kind не сравнил сохранённые результаты."
        }
        $annotationPacket = Join-Path $tempRoot ("annotation-$kind")
        & $evalLauncher annotation-export $evalCorpus --split dev --output $annotationPacket
        if ($LASTEXITCODE -ne 0) {
            throw "Оценщик $kind не экспортировал слепой пакет."
        }
        $roundtripCorpus = Join-Path $tempRoot ("annotation-roundtrip-$kind.json")
        & $environmentPython -m language_review.evaluation annotation-import `
            $evalCorpus $annotationPacket --output $roundtripCorpus
        if ($LASTEXITCODE -ne 0) {
            throw "Оценщик $kind не импортировал непроверенный слепой пакет."
        }
        & $environmentPython -c 'import json,sys; from pathlib import Path; assert json.loads(Path(sys.argv[1]).read_bytes()) == json.loads(Path(sys.argv[2]).read_bytes())' `
            $roundtripCorpus $evalCorpus
        if ($LASTEXITCODE -ne 0) {
            throw "Оценщик $kind изменил разметку непроверенного слепого пакета."
        }
        $catalogOutput = (& $evalLauncher import-source --help) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0 -or $catalogOutput -notmatch 'rust-book-ru' -or
            $catalogOutput -notmatch 'sage-github-ru') {
            throw "Оценщик $kind не загрузил упакованный pinned каталог."
        }
        if ((Get-FileHash -LiteralPath $evalCorpus -Algorithm SHA256).Hash -ne $evalCorpusHash -or
            (Get-FileHash -LiteralPath $evalReport -Algorithm SHA256).Hash -ne $evalReportHash -or
            (Get-FileHash -LiteralPath $sampleFile -Algorithm SHA256).Hash -ne $sampleHash) {
            throw "Оценщик $kind изменил входные данные."
        }
        # Ожидаемый код 5 проверен выше; он не должен стать кодом завершения всего скрипта.
        $global:LASTEXITCODE = 0
        $cloudJson = (& $launcher check $sample --engine llm --llm-provider chatgpt `
            --analysis-mode deep --view review --format json) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 5) { throw "$kind не отклонил облако без согласия." }
        $cloudReport = $cloudJson | ConvertFrom-Json
        if ($cloudReport.schema_version -ne '1.1' -or
            $cloudReport.analysis.engines[0].diagnostics -notcontains 'cloud_consent_required') {
            throw "$kind потерял контракт облачного отказа."
        }
        if ((Get-FileHash -LiteralPath $sampleFile -Algorithm SHA256).Hash -ne $sampleHash) {
            throw "$kind cloud check изменил исходник."
        }
        & $environmentPython -I -c 'import importlib.util; assert importlib.util.find_spec("jwt") is None'
        if ($LASTEXITCODE -ne 0) { throw "Базовый $kind неожиданно требует JWT extra." }
        & uv pip install --python $environmentPython ($artifact.FullName + '[chatgpt]')
        if ($LASTEXITCODE -ne 0) { throw "Не установлено chatgpt extra для $kind." }
        & $environmentPython -I -c 'import jwt; from language_review.chatgpt_store import dpapi; assert dpapi(dpapi(b"synthetic-test"), decrypt=True) == b"synthetic-test"'
        if ($LASTEXITCODE -ne 0) { throw "Не работают JWT/DPAPI в установленном $kind." }
        $authStatus = (& $launcher chatgpt status) -join [Environment]::NewLine | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0 -or $authStatus.active -or $authStatus.accounts.Count -ne 0) {
            throw "$kind не изолировал OAuth storage smoke-проверки."
        }
        & uv pip install --python $environmentPython ($artifact.FullName + '[memory]')
        if ($LASTEXITCODE -ne 0) { throw "Не установлено memory extra для $kind." }
        & $environmentPython -I -c 'import psycopg; from language_review.memory.store import Store; from language_review.memory.settings import MemorySettings; assert Store(MemorySettings()).settings.enabled is False'
        if ($LASTEXITCODE -ne 0) { throw "Не работает optional memory extra для $kind." }
        if ($env:LANGUAGE_REVIEW_MEMORY_TEST_DSN) {
            $memoryConfig = Join-Path $tempRoot ("memory-$kind.toml")
            [IO.File]::WriteAllText($memoryConfig, "[memory]`ndsn_env=`"LANGUAGE_REVIEW_MEMORY_TEST_DSN`"`n")
            & $launcher --config $memoryConfig memory health
            if ($LASTEXITCODE -ne 0) { throw "Установленный $kind не подключился к тестовой БД." }
        }
        Write-Output "${kind}: установка, CLI, python -m, scan, extract, check, review и eval прошли."
    }
}
finally {
    $env:APPDATA = $previousAppData
    $env:LOCALAPPDATA = $previousLocalAppData
    $env:UV_CACHE_DIR = $previousUvCacheDir
    $env:PYTHONIOENCODING = $previousPythonIOEncoding
    [Console]::OutputEncoding = $previousOutputEncoding
    $finalTemp = [IO.Path]::GetFullPath($tempRoot)
    if ($finalTemp.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $finalTemp -Recurse -Force
    }
}
