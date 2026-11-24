#Requires -Version 7.4
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'probe_binding.psm1') -Force
$count = 0

function Check([scriptblock]$Action, [bool]$ShouldThrow = $false) {
  $thrown = $false
  try { & $Action } catch { $thrown = $true }
  if ($thrown -ne $ShouldThrow) { throw "Unexpected contract result: $Action" }
  $script:count++
}

function Copy-Value($Value) {
  $Value | ConvertTo-Json -Depth 20 | ConvertFrom-Json
}

$identity = Get-PerfBindingIdentity 'integration'
Check { Assert-PerfBindingIdentity $identity 'integration' }
foreach ($key in $identity.Keys) {
  $missing = Copy-Value $identity
  $missing.PSObject.Properties.Remove($key)
  Check { Assert-PerfBindingIdentity $missing 'integration' } $true
  $changed = Copy-Value $identity
  $changed.$key = 'unexpected'
  Check { Assert-PerfBindingIdentity $changed 'integration' } $true
}
Check { Get-PerfBindingIdentity 'framework' } $true
Check { Get-PerfBindingIdentity 'benchmarkLive' } $true
Check { Assert-PerfBindingIdentity $identity 'framework' } $true

# Obsolete PDF options fail during parameter binding, before any process or data changes.
foreach ($script in @('run_perf_probe.ps1', 'runner.ps1')) {
  $path = Join-Path $PSScriptRoot $script
  foreach ($scenario in @('pdf-reader', 'pdf-book-view', 'pdf-multi-window')) {
    Check { & $path -ScenarioSet $scenario } $true
  }
  Check { & $path -BindingMode 'framework' } $true
  Check { & $path -PdfCase 'removed-case.json' } $true
}
Write-Output "$count general integration binding contracts passed."
