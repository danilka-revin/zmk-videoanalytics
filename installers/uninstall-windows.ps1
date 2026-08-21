#Requires -Version 5.1
param([switch]$Purge)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker is not installed" }
docker compose --profile telegram --profile max --profile training --profile production down --remove-orphans
if ($LASTEXITCODE -ne 0) { throw "Failed to stop ZMK Vision services" }
if ($Purge) {
  $answer = Read-Host "Delete persistent database and volumes? Type DELETE"
  if ($answer -eq "DELETE") {
    docker compose --profile telegram --profile max --profile training --profile production down -v --remove-orphans
    if (Test-Path data) { Remove-Item data -Recurse -Force }
  }
}
Write-Host "ZMK Vision services stopped. Data preserved unless -Purge was confirmed."
