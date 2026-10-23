$ErrorActionPreference = 'Stop'
$chromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw 'Google Chrome not found.' }
if (Get-NetTCPConnection -State Listen -LocalPort 9222 -ErrorAction SilentlyContinue) {
    throw 'Port 9222 already in use. Check the existing process; do not attach blindly.'
}
$avitoProfile = Join-Path $env:LOCALAPPDATA 'Avito-CDP-Research'
# Deliberately no personal profile, headless, proxy or fingerprint flags.
Start-Process -FilePath $chrome -ArgumentList @(
    "--user-data-dir=`"$avitoProfile`"",
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=9222',
    'about:blank'
)
Start-Sleep -Seconds 3
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 9222 -ErrorAction SilentlyContinue)
if (-not $listeners.Count) { throw 'CDP not listening. Inspect Chrome before continuing.' }
if ($listeners | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }) {
    throw 'UNSAFE CDP bind: close this research Chrome immediately; do not open the tunnel.'
}
$listeners | Select-Object LocalAddress, LocalPort, OwningProcess
Write-Host "Dedicated profile: $avitoProfile"
Write-Host 'Verify Profile Path in chrome://version, then perform the manual Avito control.'
