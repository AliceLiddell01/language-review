<# Bootstrap: доверие GitHub HTTPS/API издателя; SHA-256 подтверждает integrity, не отдельную подпись. #>
param(
    [string]$Tag,
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'LanguageReview/application'),
    [ValidateSet('core', 'chatgpt', 'memory', 'all')][string]$Extras,
    [switch]$Codex,
    [switch]$Claude,
    [switch]$NonInteractive,
    [switch]$NoPath,
    # Локальный комплект только для явно инициированной приёмки с независимо переданным digest.
    [string]$BundleDirectory,
    [ValidatePattern('^[a-f0-9]{64}$')][string]$TrustedManifestSha256
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $PSVersionTable.PSVersion.Major -lt 7) {
    throw 'Поддерживаются Windows x64 и PowerShell 7. Установите PowerShell 7 вручную (winget install Microsoft.PowerShell), затем повторите команду в pwsh.'
}
if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
    throw 'Этот установщик поддерживает Windows x64; ARM64 не проверен.'
}
$repository = 'AliceLiddell01/language-review'
$hostsAllowed = @('api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')
function Get-VerifiedDownload([string]$Url, [string]$Destination, [string]$Hash, [long]$Limit = 100000000) {
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.DefaultRequestHeaders.UserAgent.ParseAdd('language-review-installer')
    $client.Timeout = [TimeSpan]::FromSeconds(90)
    $partial = $Destination + '.partial'
    try {
        $uri = [uri]$Url
        for ($redirect = 0; $redirect -lt 6; $redirect++) {
            if ($uri.Scheme -ne 'https' -or $uri.Host -notin $hostsAllowed -or $uri.Port -ne 443 -or $uri.UserInfo) {
                throw 'Отказ загрузки: неожиданный узел HTTPS или учётные данные в URL.'
            }
            $response = $client.GetAsync($uri, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
            if ([int]$response.StatusCode -in @(301, 302, 303, 307, 308)) {
                $uri = [uri]::new($uri, $response.Headers.Location)
                $response.Dispose()
                continue
            }
            $response.EnsureSuccessStatusCode() | Out-Null
            $stream = $response.Content.ReadAsStream()
            $file = [IO.File]::Open($partial, 'CreateNew', 'Write', 'None')
            try {
                $buffer = [byte[]]::new(65536)
                $total = 0L
                while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $total += $read
                    if ($total -gt $Limit) { throw 'Превышен допустимый размер загрузки.' }
                    $file.Write($buffer, 0, $read)
                }
            } finally { $file.Dispose(); $stream.Dispose(); $response.Dispose() }
            if ($Hash -and (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Hash) {
                throw 'SHA-256 загрузки не совпадает; файл не выполнен.'
            }
            [IO.File]::Move($partial, $Destination)
            return
        }
        throw 'Слишком много перенаправлений.'
    } catch { throw 'Загрузка GitHub не завершена или asset не прошёл проверку; установка не переключена.' }
    finally {
        $client.Dispose()
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial }
    }
}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('language-review-bootstrap-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
    if (-not $uv -and (Test-Path -LiteralPath (Join-Path $Root 'installation.json'))) {
        $installed = Get-Content -Raw -LiteralPath (Join-Path $Root 'installation.json') | ConvertFrom-Json
        if ($installed.owner -eq 'language-review.installer/1' -and $installed.active -match '^[a-f0-9]{32}$') {
            $runtime = Join-Path $Root ('releases/' + $installed.active)
            $privateUv = Join-Path $runtime 'tools/uv.exe'
            $indexPath = Join-Path $runtime 'owned-files.json'
            if ((Test-Path -LiteralPath $privateUv) -and (Test-Path -LiteralPath $indexPath)) {
                $index = Get-Content -Raw -LiteralPath $indexPath | ConvertFrom-Json
                if ($index.owner -eq 'language-review.installer/1' -and
                    (Get-FileHash -LiteralPath $privateUv).Hash.ToLowerInvariant() -eq $index.files.'tools\uv.exe') {
                    $uv = [pscustomobject]@{Source = $privateUv}
                }
            }
        }
    }
    if ($uv) {
        $uvPath = $uv.Source
        $uvVersion = (& $uvPath --version).Split(' ')[1]
        if ($LASTEXITCODE -ne 0 -or [version]$uvVersion -lt [version]'0.13.1') { throw 'Обновите uv до 0.13.1 или новее.' }
    } else {
        $metadata = Join-Path $temporary 'uv-release.json'
        Get-VerifiedDownload 'https://api.github.com/repos/astral-sh/uv/releases/tags/0.13.1' $metadata '' 1000000
        $uvRelease = Get-Content -Raw -LiteralPath $metadata | ConvertFrom-Json
        $asset = @($uvRelease.assets | Where-Object name -EQ 'uv-x86_64-pc-windows-msvc.zip')
        if ($asset.Count -ne 1 -or $asset[0].digest -notmatch '^sha256:[a-f0-9]{64}$' -or
            $asset[0].browser_download_url -ne 'https://github.com/astral-sh/uv/releases/download/0.13.1/uv-x86_64-pc-windows-msvc.zip') {
            throw 'Нет проверяемой поставки uv; установите uv по официальной инструкции.'
        }
        $zipPath = Join-Path $temporary 'uv.zip'
        Get-VerifiedDownload $asset[0].browser_download_url $zipPath $asset[0].digest.Substring(7)
        $zip = [IO.Compression.ZipFile]::OpenRead($zipPath)
        try {
            $entries = @($zip.Entries | Where-Object FullName -EQ 'uv.exe')
            if ($entries.Count -ne 1 -or $entries[0].Length -gt 100000000) { throw 'Неожиданный архив uv.' }
            $uvPath = Join-Path $temporary 'uv.exe'
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entries[0], $uvPath)
        } finally { $zip.Dispose() }
    }
    $bundle = Join-Path $temporary 'bundle'
    New-Item -ItemType Directory -Path $bundle | Out-Null
    if ($BundleDirectory) {
        if (-not $TrustedManifestSha256 -or
            (Get-FileHash -LiteralPath (Join-Path $BundleDirectory 'release.json')).Hash.ToLowerInvariant() -ne $TrustedManifestSha256) {
            throw 'Приёмка требует явно доверенный SHA-256 локального manifest.'
        }
        Copy-Item -Path (Join-Path $BundleDirectory '*') -Destination $bundle
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $bundle 'release.json') | ConvertFrom-Json
    } else {
        if ($Tag -and $Tag -notmatch '^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-(alpha|beta|rc)\.[1-9]\d*)?$') { throw 'Неверный tag.' }
        $metadata = Join-Path $temporary 'release-api.json'
        $suffix = if ($Tag) { 'tags/' + $Tag } else { 'latest' }
        Get-VerifiedDownload "https://api.github.com/repos/$repository/releases/$suffix" $metadata '' 8000000
        $release = Get-Content -Raw -LiteralPath $metadata | ConvertFrom-Json
        if ($release.draft -or -not $release.published_at -or (-not $Tag -and $release.prerelease)) { throw 'Нужен опубликованный стабильный выпуск или явно указанный prerelease.' }
        $prefix = "https://github.com/$repository/releases/download/$($release.tag_name)/"
        $asset = @($release.assets | Where-Object name -EQ 'release.json')
        if ($asset.Count -ne 1 -or $asset[0].digest -notmatch '^sha256:[a-f0-9]{64}$' -or $asset[0].browser_download_url -ne $prefix + 'release.json') { throw 'Не найден проверяемый release manifest.' }
        Get-VerifiedDownload $asset[0].browser_download_url (Join-Path $bundle 'release.json') $asset[0].digest.Substring(7) 1000000
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $bundle 'release.json') | ConvertFrom-Json
        if ($manifest.repository -ne $repository -or $manifest.tag -ne $release.tag_name -or
            $manifest.commit -notmatch '^[a-f0-9]{40}$' -or
            [bool]$release.prerelease -ne ($manifest.channel -eq 'prerelease')) {
            throw 'Manifest не соответствует GitHub release.'
        }
        $commitPath = Join-Path $temporary 'commit.json'
        Get-VerifiedDownload "https://api.github.com/repos/$repository/git/ref/tags/$($manifest.tag)" $commitPath '' 1000000
        $tagObject = (Get-Content -Raw -LiteralPath $commitPath | ConvertFrom-Json).object
        for ($depth = 0; $depth -lt 6 -and $tagObject.type -eq 'tag'; $depth++) {
            if ($tagObject.sha -notmatch '^[a-f0-9]{40}$') { throw 'Некорректный annotated tag.' }
            $annotated = Join-Path $temporary ('tag-' + $depth + '.json')
            Get-VerifiedDownload "https://api.github.com/repos/$repository/git/tags/$($tagObject.sha)" $annotated '' 1000000
            $tagObject = (Get-Content -Raw -LiteralPath $annotated | ConvertFrom-Json).object
        }
        if ($tagObject.type -ne 'commit' -or $tagObject.sha -ne $manifest.commit) { throw 'SHA тега не совпадает с manifest.' }
        foreach ($entry in $manifest.assets.PSObject.Properties) {
            $name = $entry.Name
            if ($name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$' -or $name.Contains('..')) { throw 'Недопустимое имя asset.' }
            $asset = @($release.assets | Where-Object name -EQ $name)
            if ($asset.Count -ne 1 -or $asset[0].digest -ne 'sha256:' + $entry.Value.sha256 -or $asset[0].browser_download_url -ne $prefix + $name -or $asset[0].size -ne $entry.Value.size) { throw 'Asset не соответствует manifest.' }
            Get-VerifiedDownload ($prefix + $name) (Join-Path $bundle $name) $entry.Value.sha256 $entry.Value.size
        }
    }
    # До установки/выполнения wheel проверяется каждый локальный файл, в том числе тестовый комплект.
    foreach ($entry in $manifest.assets.PSObject.Properties) {
        if ($entry.Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$' -or $entry.Name.Contains('..')) { throw 'Недопустимое имя asset.' }
        $path = Join-Path $bundle $entry.Name
        if ((Get-Item -LiteralPath $path).Length -ne $entry.Value.size -or (Get-FileHash -LiteralPath $path).Hash.ToLowerInvariant() -ne $entry.Value.sha256) { throw 'Asset повреждён.' }
    }
    $wheel = @(Get-ChildItem -LiteralPath $bundle -Filter '*.whl')
    if ($wheel.Count -ne 1) { throw 'Ожидался один wheel.' }
    $seed = Join-Path $temporary 'seed'
    & $uvPath venv --no-config --python 3.15 $seed
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить Python 3.15; проверьте сеть и uv.' }
    $python = Join-Path $seed 'Scripts/python.exe'
    & $uvPath pip install --no-config --python $python --no-deps $wheel[0].FullName
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить проверенный seed wheel.' }
    # Установщик копирует собственный uv внутрь кандидата и учитывает его принадлежность.
    $uvInstalled = $uvPath
    $installArgs = @('-I', '-m', 'language_review.distribution', '--bundle', $bundle, '--root', $Root, '--uv', $uvInstalled)
    if ($Extras) { $installArgs += @('--extras', $Extras) }
    if ($Codex) { $installArgs += @('--agent', 'codex') }
    if ($Claude) { $installArgs += @('--agent', 'claude') }
    if ($NoPath) { $installArgs += '--no-path' }
    & $python @installArgs
    if ($LASTEXITCODE -ne 0) { throw 'Установка ядра не завершена; прежняя версия сохранена.' }
    $active = Get-Content -Raw -LiteralPath (Join-Path $Root 'installation.json') | ConvertFrom-Json
    $installedPython = Join-Path $Root ('releases/' + $active.active + '/venv/Scripts/python.exe')
    $setupArgs = @('-I', '-m', 'language_review', 'setup')
    if ($NonInteractive) { $setupArgs += '--non-interactive' }
    & $installedPython @setupArgs
    if ($LASTEXITCODE -ne 0) { throw 'Ядро установлено, setup не завершён; повторите language-review setup.' }
} finally {
    $resolved = [IO.Path]::GetFullPath($temporary)
    if ($resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()), [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolved).StartsWith('language-review-bootstrap-')) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
