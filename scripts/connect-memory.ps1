# Dot-source to load this installation's credentials into the current process only.
$languageReviewDeployment = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'deployment.json') | ConvertFrom-Json
$languageReviewCredential = Import-Clixml -LiteralPath (Join-Path $PSScriptRoot 'database.credential.xml')
$env:LANGUAGE_REVIEW_DB_PASSWORD = $languageReviewCredential.GetNetworkCredential().Password
$env:LANGUAGE_REVIEW_DB_PORT = [string]$languageReviewDeployment.port
$env:LANGUAGE_REVIEW_MEMORY_DSN = "postgresql://language_review:$($env:LANGUAGE_REVIEW_DB_PASSWORD)@127.0.0.1:$($languageReviewDeployment.port)/language_review"
Remove-Variable languageReviewCredential
