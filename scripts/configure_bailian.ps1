param([Parameter(Mandatory = $true)][string]$BaseUrl)
# Run in the PowerShell window where set_api_key.ps1 was executed.
# No API calls; no key is saved to disk.
$taskEndpoint = $null
if (-not [Uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$taskEndpoint) -or
    $taskEndpoint.Scheme -ne 'https' -or
    -not $taskEndpoint.AbsolutePath.TrimEnd('/').EndsWith('/compatible-mode/v1') -or
    $taskEndpoint.Query -or $taskEndpoint.Fragment -or $taskEndpoint.UserInfo -or
    $BaseUrl.Contains('{') -or $BaseUrl.Contains('}')) {
    throw 'Provide the complete HTTPS compatible-mode/v1 Base URL from your Bailian console.'
}
if (-not $env:KNOWLEDGEOPS_EMBEDDING_API_KEY -or -not $env:KNOWLEDGEOPS_GENERATION_API_KEY) {
    throw 'Run .\scripts\set_api_key.ps1 in this PowerShell window first.'
}
$env:KNOWLEDGEOPS_EMBEDDING_PROVIDER = 'compatible'
$env:KNOWLEDGEOPS_EMBEDDING_MODEL_ID = 'text-embedding-v4'
$env:KNOWLEDGEOPS_EMBEDDING_DIMENSIONS = '1024'
$env:KNOWLEDGEOPS_EMBEDDING_BATCH_SIZE = '10'
$env:KNOWLEDGEOPS_EMBEDDING_BASE_URL = $BaseUrl.TrimEnd('/')
$env:KNOWLEDGEOPS_EMBEDDING_COLLECTION = 'bailian-v4-1024-validation-v1'
$env:KNOWLEDGEOPS_GENERATION_PROVIDER = 'compatible'
$env:KNOWLEDGEOPS_GENERATION_MODEL_ID = 'qwen-plus'
$env:KNOWLEDGEOPS_GENERATION_BASE_URL = $BaseUrl.TrimEnd('/')
if (-not $env:KNOWLEDGEOPS_DATABASE_URL) {
    $env:KNOWLEDGEOPS_DATABASE_URL = 'postgresql://knowledgeops:local-day5-only@127.0.0.1:55432/knowledgeops'
}
Write-Host 'Real model configuration set for this session. No API calls made.'
Write-Host 'Next: run scripts/verify_real_models.py with the project Python interpreter.'

Remove-Item Env:KNOWLEDGEOPS_INGESTION_DB_PATH -ErrorAction SilentlyContinue
