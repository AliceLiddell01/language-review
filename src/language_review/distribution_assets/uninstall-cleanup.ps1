param([Parameter(Mandatory)][string]$Root, [int]$WaitForPid, [int]$WaitForParentPid, [int]$WaitForLauncherPid)
$ErrorActionPreference = 'Stop'
$waitForPids = @($WaitForPid, $WaitForParentPid, $WaitForLauncherPid) | Where-Object { $_ -gt 0 } | Select-Object -Unique
if ($waitForPids.Count) { Wait-Process -Id $waitForPids -Timeout 60 -ErrorAction SilentlyContinue }
$rootPath = [IO.Path]::GetFullPath($Root)
$lockPath = Join-Path $rootPath '.install-lock'
try { $lock = [IO.File]::Open($lockPath, 'CreateNew', 'Write', 'None') }
catch {
    try {
        @{status = 'cleanup_deferred'; reason = 'install_lock_busy'; data = 'Runtime не очищен; повторите очистку после завершения операции'} |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rootPath 'uninstall-result.json') -Encoding utf8NoBOM
    } catch { }
    throw 'Очистка не начата: установка занята. Повторите внешнюю очистку после завершения операции.'
}
try {
$cleanupStage = 'validate_ownership'
$statePath = Join-Path $rootPath 'installation.json'
$state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
if ($state.owner -ne 'language-review.installer/1' -or -not $state.uninstalled -or $state.root -ne $rootPath) {
    throw 'Внешняя очистка требует ownership и завершённый uninstall.'
}
$checkedPaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
function Assert-NoLink([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if ($checkedPaths.Contains($current)) { return }
        if (([IO.File]::Exists($current) -or [IO.Directory]::Exists($current)) -and
            ([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Очистка ссылки запрещена.'
        }
        $null = $checkedPaths.Add($current)
        $parent = [IO.Directory]::GetParent($current)
        $current = if ($parent) { $parent.FullName } else { $null }
    }
}
$cleanupStage = 'validate_paths'
Assert-NoLink $rootPath
$preserved = [Collections.Generic.List[string]]::new()
$releasesPath = Join-Path $rootPath 'releases'
foreach ($release in Get-ChildItem -LiteralPath $releasesPath -Directory) {
    $cleanupStage = 'inspect_release'
    Assert-NoLink $release.FullName
    $leaseDirectory = Join-Path $release.FullName '.run-leases'
    $liveLease = $false
    if (Test-Path -LiteralPath $leaseDirectory -PathType Container) {
        foreach ($leaseFile in Get-ChildItem -LiteralPath $leaseDirectory -Filter '*.lock' -File) {
            try {
                $leaseProbe = [IO.File]::Open($leaseFile.FullName, 'Open', 'ReadWrite', 'None')
                $leaseProbe.Dispose()
            } catch {
                $liveLease = $true
                break
            }
        }
    }
    if ($liveLease) {
        $preserved.Add('Runtime занят работающим CLI: ' + $release.Name)
        continue
    }
    $indexPath = Join-Path $release.FullName 'owned-files.json'
    if (-not (Test-Path -LiteralPath $indexPath)) { continue }
    $index = Get-Content -Raw -LiteralPath $indexPath | ConvertFrom-Json
    if ($index.owner -ne 'language-review.installer/1') { continue }
    foreach ($entry in $index.files.PSObject.Properties) {
        $cleanupStage = 'remove_owned_file'
        $path = [IO.Path]::GetFullPath((Join-Path $release.FullName $entry.Name))
        if (-not $path.StartsWith($release.FullName + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Путь inventory выходит за release.' }
        Assert-NoLink $path
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $removed = $false
            for ($attempt = 0; $attempt -lt 5; $attempt++) {
                if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $removed = $true; break }
                Assert-NoLink $path
                try {
                    if ((Get-FileHash -LiteralPath $path).Hash.ToLowerInvariant() -ne $entry.Value) { break }
                    Remove-Item -LiteralPath $path -ErrorAction Stop
                } catch { }
                if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $removed = $true; break }
                if ($attempt -lt 4) { Start-Sleep -Milliseconds 200 }
            }
            if (-not $removed) { $preserved.Add($entry.Name) }
        }
    }
    if (Test-Path -LiteralPath $leaseDirectory -PathType Container) {
        $cleanupStage = 'remove_stale_leases'
        foreach ($staleLease in Get-ChildItem -LiteralPath $leaseDirectory -Filter '*.lock' -File) {
            Remove-Item -LiteralPath $staleLease.FullName -ErrorAction SilentlyContinue
        }
    }
    # Удаляются только пустые каталоги. Неизвестные файлы и изменённые байты остаются.
    $cleanupStage = 'remove_empty_directories'
    foreach ($directory in Get-ChildItem -LiteralPath $release.FullName -Directory -Recurse | Sort-Object { $_.FullName.Length } -Descending) {
        Assert-NoLink $directory.FullName
        if (-not @(Get-ChildItem -LiteralPath $directory.FullName -Force).Count) { Remove-Item -LiteralPath $directory.FullName }
    }
    if (@(Get-ChildItem -LiteralPath $release.FullName -Force).Count -eq 1) {
        $cleanupStage = 'remove_empty_release'
        Remove-Item -LiteralPath $indexPath
        Remove-Item -LiteralPath $release.FullName
    }
}
$cleanupStage = 'write_result'
@{status = 'uninstalled'; preserved = @($preserved); data = 'Пользовательские данные, OAuth и memory deployment сохранены'} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rootPath 'uninstall-result.json') -Encoding utf8NoBOM
Remove-Item -LiteralPath $PSCommandPath
} catch {
    @{status = 'cleanup_failed'; reason = $_.Exception.GetType().Name; stage = $cleanupStage; error_id = $_.FullyQualifiedErrorId; script_line = $_.InvocationInfo.ScriptLineNumber; data = 'Пользовательские данные сохранены; проверьте остатки runtime'} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rootPath 'uninstall-result.json') -Encoding utf8NoBOM
    throw
} finally { $lock.Dispose(); Remove-Item -LiteralPath $lockPath }
