$ErrorActionPreference = 'Stop'
$bd = Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root |
      Where-Object { $_.Subject -match 'Bitdefender' }
$lines = New-Object System.Collections.Generic.List[string]
foreach ($c in $bd) {
  $lines.Add('-----BEGIN CERTIFICATE-----')
  $lines.Add([Convert]::ToBase64String($c.RawData, 'InsertLineBreaks'))
  $lines.Add('-----END CERTIFICATE-----')
}
$lines | Set-Content -Encoding ascii 'C:\Workspace\SoriaProblem\certs\bitdefender.pem'
Write-Output ('exported ' + $bd.Count + ' cert(s)')
