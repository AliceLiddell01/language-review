param([Parameter(Mandatory)][string]$Root, [int]$WaitForPid)
$ErrorActionPreference = 'Stop'
if ($WaitForPid) { Wait-Process -Id $WaitForPid -ErrorAction SilentlyContinue }
$rootPath = [IO.Path]::GetFullPath($Root)
$lockPath = Join-Path $rootPath '.install-lock'
try { $lock = [IO.File]::Open($lockPath, 'CreateNew', 'Write', 'None') }
catch { throw 'Очистка не начата: установка занята. Повторите внешнюю очистку после завершения операции.' }
try {
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
Assert-NoLink $rootPath
$preserved = [Collections.Generic.List[string]]::new()
$releasesPath = Join-Path $rootPath 'releases'
foreach ($release in Get-ChildItem -LiteralPath $releasesPath -Directory) {
    Assert-NoLink $release.FullName
    $indexPath = Join-Path $release.FullName 'owned-files.json'
    if (-not (Test-Path -LiteralPath $indexPath)) { continue }
    $index = Get-Content -Raw -LiteralPath $indexPath | ConvertFrom-Json
    if ($index.owner -ne 'language-review.installer/1') { continue }
    foreach ($entry in $index.files.PSObject.Properties) {
        $path = [IO.Path]::GetFullPath((Join-Path $release.FullName $entry.Name))
        if (-not $path.StartsWith($release.FullName + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Путь inventory выходит за release.' }
        Assert-NoLink $path
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            if ((Get-FileHash -LiteralPath $path).Hash.ToLowerInvariant() -eq $entry.Value) {
                try { Remove-Item -LiteralPath $path } catch { $preserved.Add($entry.Name) }
            } else { $preserved.Add($entry.Name) }
        }
    }
    # Удаляются только пустые каталоги. Неизвестные файлы и изменённые байты остаются.
    foreach ($directory in Get-ChildItem -LiteralPath $release.FullName -Directory -Recurse | Sort-Object { $_.FullName.Length } -Descending) {
        Assert-NoLink $directory.FullName
        if (-not @(Get-ChildItem -LiteralPath $directory.FullName -Force).Count) { Remove-Item -LiteralPath $directory.FullName }
    }
    if (@(Get-ChildItem -LiteralPath $release.FullName -Force).Count -eq 1) {
        Remove-Item -LiteralPath $indexPath
        Remove-Item -LiteralPath $release.FullName
    }
}
@{status = 'uninstalled'; preserved = @($preserved); data = 'Пользовательские данные, OAuth и memory deployment сохранены'} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rootPath 'uninstall-result.json') -Encoding utf8NoBOM
Remove-Item -LiteralPath $PSCommandPath
} catch {
    @{status = 'cleanup_failed'; reason = $_.Exception.GetType().Name; data = 'Пользовательские данные сохранены; проверьте остатки runtime'} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rootPath 'uninstall-result.json') -Encoding utf8NoBOM
    throw
} finally { $lock.Dispose(); Remove-Item -LiteralPath $lockPath }
