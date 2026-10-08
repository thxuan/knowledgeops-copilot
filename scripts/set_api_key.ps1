# Run in the same PowerShell window used to start the application.
# The key is stored only in this process environment, never in this file.
$taskSecureKey = Read-Host 'Paste the API key for both embedding and generation' -AsSecureString
try {
    if ($taskSecureKey.Length -eq 0) { throw 'API key cannot be empty.' }
    $taskKeyCredential = [System.Management.Automation.PSCredential]::new('knowledgeops', $taskSecureKey)
    $env:KNOWLEDGEOPS_EMBEDDING_API_KEY = $taskKeyCredential.GetNetworkCredential().Password
    $env:KNOWLEDGEOPS_GENERATION_API_KEY = $env:KNOWLEDGEOPS_EMBEDDING_API_KEY
    Write-Host 'API key configured for this PowerShell session. No key saved to disk.'
    Write-Host 'Provider, base URL and model settings must be configured separately.'
}
finally {
    $taskSecureKey.Dispose()
    Remove-Variable taskSecureKey, taskKeyCredential -ErrorAction SilentlyContinue
}
