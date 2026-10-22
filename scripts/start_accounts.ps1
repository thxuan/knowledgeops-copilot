param([string]$Username='admin')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$pythonPath=Join-Path (Split-Path -Parent $projectRoot) '.venv-knowledgeops-langchain\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $pythonPath)){$pythonPath=Join-Path (Split-Path -Parent $projectRoot) '.venv-knowledgeops\Scripts\python.exe'}
if(-not(Test-Path -LiteralPath $pythonPath)){$pythonPath=(Get-Command python -ErrorAction Stop).Source}
# Auth storage is pinned explicitly so selecting an existing corpus does not move accounts.
if(-not $env:KNOWLEDGEOPS_AUTH_DATABASE_URL){
 if($env:KNOWLEDGEOPS_DATABASE_URL){$env:KNOWLEDGEOPS_AUTH_DATABASE_URL=$env:KNOWLEDGEOPS_DATABASE_URL}
 else{$env:KNOWLEDGEOPS_AUTH_DATABASE_URL='postgresql://knowledgeops:local-day5-only@127.0.0.1:55432/knowledgeops'}
}
Write-Host 'Create the first administrator if needed (hidden password prompt). Existing accounts are preserved.'
& $pythonPath -B (Join-Path $PSScriptRoot 'manage_accounts.py') --ensure-admin --username $Username --database-url $env:KNOWLEDGEOPS_AUTH_DATABASE_URL
if($LASTEXITCODE -ne 0){throw 'Account initialization did not complete. If it already exists, start with the account-mode environment variables documented in docs/accounts-and-operations.md.'}
$env:KNOWLEDGEOPS_AUTH_MODE='accounts'
$env:KNOWLEDGEOPS_PUBLIC_ORIGIN='http://127.0.0.1:8000'
$env:KNOWLEDGEOPS_AUTH_COOKIE_SECURE='false'
Push-Location -LiteralPath $projectRoot
try{& $pythonPath -B (Join-Path $PSScriptRoot 'workbench.py')}
finally{Pop-Location}
