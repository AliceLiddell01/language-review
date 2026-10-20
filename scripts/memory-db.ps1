param(
    [Parameter(Mandatory)][ValidateSet('Up', 'Status', 'Stop', 'Backup', 'Restore')][string]$Action,
    [string]$Project = 'language-review-memory',
    [string]$BackupPath
)
$ErrorActionPreference = 'Stop'
$composeFile = Join-Path $PSScriptRoot '..\compose.memory.yml'
$composeArgs = @('compose', '-f', $composeFile, '-p', $Project)
function Invoke-Docker([string[]]$DockerArguments) {
    & docker @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw "docker failed: $LASTEXITCODE" }
}
switch ($Action) {
    'Up' { Invoke-Docker ($composeArgs + @('up', '-d', '--wait', '--wait-timeout', '120')) }
    'Status' { Invoke-Docker ($composeArgs + @('ps')) }
    'Stop' { Invoke-Docker ($composeArgs + @('stop')) }
    'Backup' {
        if (-not $BackupPath) { throw 'BackupPath is required' }
        $target = [IO.Path]::GetFullPath($BackupPath)
        if (Test-Path -LiteralPath $target) { throw 'Backup destination already exists' }
        $container = & docker @composeArgs ps -q postgres
        if ($LASTEXITCODE -ne 0 -or -not $container) { throw 'Database container unavailable' }
        $remoteFile = '/tmp/lr-backup-' + [guid]::NewGuid().ToString('N') + '.dump'
        try {
            Invoke-Docker ($composeArgs + @('exec', '-T', 'postgres', 'pg_dump', '-U',
                'language_review', '-d', 'language_review', '-Fc', '-f', $remoteFile))
            Invoke-Docker @('cp', "${container}:$remoteFile", $target)
            Invoke-Docker ($composeArgs + @('exec', '-T', 'postgres', 'pg_restore', '--list', $remoteFile))
            Get-FileHash -LiteralPath $target -Algorithm SHA256
        } finally {
            Invoke-Docker ($composeArgs + @('exec', '-T', 'postgres', 'rm', '-f', $remoteFile))
        }
    }
    'Restore' {
        if (-not $BackupPath -or -not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
            throw 'Existing BackupPath is required'
        }
        $tables = & docker @composeArgs exec -T postgres psql -U language_review -d language_review -Atc `
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        if ($LASTEXITCODE -ne 0 -or $tables.Trim() -ne '0') { throw 'Restore requires an empty database' }
        $container = & docker @composeArgs ps -q postgres
        if ($LASTEXITCODE -ne 0 -or -not $container) { throw 'Database container unavailable' }
        $remoteFile = '/tmp/lr-restore-' + [guid]::NewGuid().ToString('N') + '.dump'
        try {
            Invoke-Docker @('cp', ([IO.Path]::GetFullPath($BackupPath)), "${container}:$remoteFile")
            Invoke-Docker ($composeArgs + @('exec', '-T', 'postgres', 'pg_restore', '-U',
                'language_review', '-d', 'language_review', '--exit-on-error', '--single-transaction',
                '--no-owner', $remoteFile))
        } finally {
            Invoke-Docker ($composeArgs + @('exec', '-T', 'postgres', 'rm', '-f', $remoteFile))
        }
    }
}
