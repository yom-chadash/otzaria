Set-StrictMode -Version Latest

function Get-PerfBindingIdentity {
  param([ValidateSet('integration')][string]$BindingMode)
  return @{
    bindingMode = 'integration'; bindingClass = 'IntegrationTestWidgetsFlutterBinding'
    framePolicy = 'fullyLive'; platformDispatcher = 'test'
    probeTarget = 'integration_test/perf_resource_probe_test.dart'
  }
}

function Assert-PerfBindingIdentity {
  param($Value, [ValidateSet('integration')][string]$BindingMode)
  $expected = Get-PerfBindingIdentity $BindingMode
  foreach ($key in $expected.Keys) {
    if ($null -eq $Value -or $Value.$key -isnot [string] -or $Value.$key -cne $expected[$key]) {
      throw "Missing or unexpected $BindingMode binding identity: $key"
    }
  }
}

Export-ModuleMember -Function Get-PerfBindingIdentity, Assert-PerfBindingIdentity
