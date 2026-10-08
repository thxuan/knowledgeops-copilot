param(
    [Parameter(Mandatory = $true)][double]$InputPricePerMillion,
    [Parameter(Mandatory = $true)][double]$OutputPricePerMillion,
    [Parameter(Mandatory = $true)][double]$EmbeddingPricePerMillion
)
# Use in the same already-configured real RAG shell. Prices must share one currency.
if (-not $env:KNOWLEDGEOPS_GENERATION_API_KEY -or -not $env:KNOWLEDGEOPS_GENERATION_BASE_URL) {
    throw 'Configure the real RAG key and endpoint in this PowerShell session first.'
}
if ($InputPricePerMillion -lt 0 -or $OutputPricePerMillion -lt 0 -or $EmbeddingPricePerMillion -lt 0) {
    throw 'Prices must be non-negative and reflect the actual billing configuration.'
}
$env:KNOWLEDGEOPS_AGENT_PROVIDER = 'compatible'
$env:KNOWLEDGEOPS_AGENT_MODEL_ID = $env:KNOWLEDGEOPS_GENERATION_MODEL_ID
$env:KNOWLEDGEOPS_AGENT_BASE_URL = $env:KNOWLEDGEOPS_GENERATION_BASE_URL
$env:KNOWLEDGEOPS_AGENT_API_KEY = $env:KNOWLEDGEOPS_GENERATION_API_KEY
$env:KNOWLEDGEOPS_AGENT_INPUT_PRICE = $InputPricePerMillion.ToString([Globalization.CultureInfo]::InvariantCulture)
$env:KNOWLEDGEOPS_AGENT_OUTPUT_PRICE = $OutputPricePerMillion.ToString([Globalization.CultureInfo]::InvariantCulture)
$env:KNOWLEDGEOPS_EMBEDDING_COST_PER_MILLION_TOKENS = $EmbeddingPricePerMillion.ToString([Globalization.CultureInfo]::InvariantCulture)
$env:KNOWLEDGEOPS_AGENT_DATABASE_URL = $env:KNOWLEDGEOPS_DATABASE_URL
if (-not $env:KNOWLEDGEOPS_AGENT_BUSINESS_DATABASE_URL) {
    $env:KNOWLEDGEOPS_AGENT_BUSINESS_DATABASE_URL = 'postgresql://knowledgeops_agent_reader:local-agent-read-only@127.0.0.1:55432/knowledgeops'
}
Write-Host 'Agent uses the current generation endpoint/key/model. No key saved and no API call made.'
Write-Host 'Default max task cost is 0.1 in the same currency as these prices; adjust KNOWLEDGEOPS_AGENT_MAX_COST if needed.'
