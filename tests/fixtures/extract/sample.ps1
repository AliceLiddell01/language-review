<#
.SYNOPSIS
Описание команды для пользователя.
#>
Write-Error "Не удалось открыть $Path"
Write-Error "Не удалось записать $(Get-Path $Path)"
Write-Error @"
Не удалось повторить чтение для $Path
"@
Write-Host 'Готово'
$machineStatus = 'FAILED'
$here = @"
Не найдено: $name
"@
