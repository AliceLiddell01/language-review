param([Parameter(Mandatory)][string]$Output)
$ErrorActionPreference = 'Stop'
$name = 'lr-hindsight-experiment-' + [guid]::NewGuid().ToString('N')
$image = 'ghcr.io/vectorize-io/hindsight:1.0.0@sha256:a88678c038b0e4340822b02a5efc566c56d8176b5bffa731bc433fd37f978e4c'
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()
if (Test-Path -LiteralPath $Output) { throw 'Output already exists' }
try {
    & docker run -d --name $name -p "127.0.0.1:${port}:8888" `
        -e HINDSIGHT_API_LLM_PROVIDER=ollama -e HINDSIGHT_API_LLM_MODEL=qwen3.5:4b `
        -e HINDSIGHT_API_LLM_BASE_URL=http://host.docker.internal:11434/v1 `
        -e HINDSIGHT_API_LLM_OLLAMA_NUM_CTX=16384 -e HINDSIGHT_API_LLM_STRICT_SCHEMA=true `
        -e HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai `
        -e HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL=http://host.docker.internal:11434/v1 `
        -e HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL=embeddinggemma:300m `
        -e HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY=local `
        -e HINDSIGHT_API_RERANKER_PROVIDER=rrf $image
    if ($LASTEXITCODE -ne 0) { throw 'Cannot start isolated Hindsight experiment' }
    $api = $null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $api = Invoke-RestMethod "http://127.0.0.1:$port/openapi.json" -TimeoutSec 2
            break
        } catch { Start-Sleep -Seconds 2 }
    }
    if ($null -eq $api) { throw 'Hindsight did not become ready' }
    if ($api.info.version -ne '1.0.0') { throw "Unexpected API version: $($api.info.version)" }
    $projectRoot = Split-Path -Parent $PSScriptRoot
    & uv run --project $projectRoot --locked --extra memory python (Join-Path $PSScriptRoot 'benchmark-memory.py') `
        --hindsight "http://127.0.0.1:$port" --output $Output
    if ($LASTEXITCODE -ne 0) { throw 'Hindsight benchmark failed' }
} finally {
    # Только созданный этим скриптом контейнер без mounts пользовательского хранилища.
    & docker rm -f $name
}
