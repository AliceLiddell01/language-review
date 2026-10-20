param(
    [Parameter(Mandatory)][string]$Config,
    [Parameter(Mandatory)][guid]$Project,
    [Parameter(Mandatory)][string]$LogDirectory
)
$ErrorActionPreference = 'Stop'
$configFile = (Resolve-Path -LiteralPath $Config).Path
$logs = [IO.Path]::GetFullPath($LogDirectory)
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$executable = (Get-Command language-review -ErrorAction Stop).Source
# Start-Process получает пути в кавычках; значения GUID не интерпретируются shell.
$arguments = @('--config', ('"' + $configFile + '"'), 'memory', 'work', '--project', $Project, '--watch')
$process = Start-Process -FilePath $executable -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logs 'worker.stdout.log') `
    -RedirectStandardError (Join-Path $logs 'worker.stderr.log')
$process.Id
