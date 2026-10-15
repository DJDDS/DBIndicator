<#
  Dhan candle downloader for the DBIndicator 90-session backtest (09:25 model x 09:45 hierarchy desk).
  Reads DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN from dhan.env (same folder; copy dhan.env.example to dhan.env and fill it in - git ignores dhan.env). Read-only: it only downloads candles.

  Saves (in .\dhan_data\):
    intraday\<SYMBOL>.csv  5-minute candles of every F&O stock for the last SESSIONS trading days (default 120 = 90 tested + 30 history)
    daily\<SYMBOL>.csv     daily candles for the last DAILY_DAYS calendar days (default 550; for 52-week and 20-day inputs)
    index\NIFTY50_5m.csv, index\INDIAVIX_daily.csv
    symbols.csv            symbol -> Dhan security id that was used
  then zips it all to .\dhan_data.zip  -> send that zip back. Your token is NOT inside it.

  Run (Windows PowerShell or PowerShell 7), from this folder:
    powershell -ExecutionPolicy Bypass -File .\dhan_download.ps1
  Takes about 10-20 minutes (about 1,000 small requests, paced to stay inside Dhan's rate limits).
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location -Path $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---------- settings from dhan.env
$envFile = Join-Path $PSScriptRoot 'dhan.env'
if (-not (Test-Path $envFile)) { throw "dhan.env not found next to this script" }
$cfg = @{}
Get-Content $envFile | ForEach-Object {
  $l = $_.Trim()
  if ($l -and -not $l.StartsWith('#') -and $l.Contains('=')) { $k, $v = $l.Split('=', 2); $cfg[$k.Trim()] = $v.Trim() }
}
$clientId = $cfg['DHAN_CLIENT_ID']; $token = $cfg['DHAN_ACCESS_TOKEN']
if (-not $clientId -or $clientId -like 'your_*' -or -not $token -or $token -like 'your_*') { throw "Fill DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in dhan.env first" }
$sessions = if ($cfg['SESSIONS']) { [int]$cfg['SESSIONS'] } else { 120 }
$dailyDays = if ($cfg['DAILY_DAYS']) { [int]$cfg['DAILY_DAYS'] } else { 550 }
$headers = @{ 'access-token' = $token; 'client-id' = $clientId; 'Content-Type' = 'application/json'; 'Accept' = 'application/json' }

$out = Join-Path $PSScriptRoot 'dhan_data'
foreach ($d in 'intraday', 'daily', 'index') { New-Item -ItemType Directory -Force -Path (Join-Path $out $d) | Out-Null }
$log = Join-Path $out 'download_log.txt'; "started $(Get-Date -Format s)" | Set-Content $log

function Call-Dhan($url, $body) {
  $json = $body | ConvertTo-Json -Compress
  for ($try = 1; $try -le 5; $try++) {
    try {
      Start-Sleep -Milliseconds 260                                   # ~4 requests a second, under Dhan's data-API limit
      return Invoke-RestMethod -Method Post -Uri $url -Headers $headers -Body $json -TimeoutSec 60
    } catch {
      $code = $null; try { $code = [int]$_.Exception.Response.StatusCode } catch {}
      if ($code -eq 401 -or $code -eq 403) { throw "Dhan rejected the token (HTTP $code). Generate a fresh access token and update dhan.env." }
      if ($try -eq 5) { "FAILED $url $($body.securityId): $($_.Exception.Message)" | Add-Content $log; return $null }
      Start-Sleep -Seconds (2 * $try)
    }
  }
}

function Save-Candles($resp, $path) {
  if (-not $resp -or -not $resp.timestamp) { return 0 }
  $sb = New-Object System.Text.StringBuilder
  [void]$sb.AppendLine('ts,open,high,low,close,volume')
  for ($i = 0; $i -lt $resp.timestamp.Count; $i++) {
    [void]$sb.AppendLine([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0},{1},{2},{3},{4},{5}',
      [long]$resp.timestamp[$i], [double]$resp.open[$i], [double]$resp.high[$i], [double]$resp.low[$i], [double]$resp.close[$i], [long]$resp.volume[$i]))
  }
  if (Test-Path $path) { $sb.ToString().Split("`n") | Select-Object -Skip 1 | Where-Object { $_.Trim() } | Add-Content $path }
  else { [IO.File]::WriteAllText($path, $sb.ToString()) }
  return $resp.timestamp.Count
}

# ---------- F&O stock list and security ids from Dhan's scrip master
Write-Host 'Downloading Dhan scrip master...'
$masterPath = Join-Path $out 'scrip_master.csv'
Invoke-WebRequest -Uri 'https://images.dhan.co/api-data/api-scrip-master.csv' -OutFile $masterPath -UseBasicParsing
$master = Import-Csv $masterPath
$fno = $master | Where-Object { $_.SEM_EXM_EXCH_ID -eq 'NSE' -and $_.SEM_INSTRUMENT_NAME -eq 'FUTSTK' } |
       ForEach-Object { ($_.SEM_TRADING_SYMBOL -split '-')[0] } | Sort-Object -Unique
$eq = @{}
$master | Where-Object { $_.SEM_EXM_EXCH_ID -eq 'NSE' -and $_.SEM_INSTRUMENT_NAME -eq 'EQUITY' -and $_.SEM_SERIES -eq 'EQ' } |
  ForEach-Object { $eq[$_.SEM_TRADING_SYMBOL] = $_.SEM_SMST_SECURITY_ID }
Remove-Item $masterPath
$stocks = $fno | Where-Object { $eq.ContainsKey($_) }
$stocks | ForEach-Object { [pscustomobject]@{ symbol = $_; security_id = $eq[$_] } } | Export-Csv (Join-Path $out 'symbols.csv') -NoTypeInformation
Write-Host "F&O stocks found: $($stocks.Count)"
if ($stocks.Count -lt 100) { throw "Only $($stocks.Count) F&O stocks matched - the scrip master format may have changed; send me download_log.txt" }

# ---------- date windows: 5-minute data in <= 85-day chunks (Dhan allows 90 days per request)
$today = (Get-Date).Date
$calDays = [int][math]::Ceiling($sessions * 7 / 5) + 10
$start = $today.AddDays(-$calDays)
$chunks = @(); $a = $start
while ($a -lt $today) { $b = $a.AddDays(85); if ($b -gt $today) { $b = $today }; $chunks += , @($a, $b); $a = $b.AddDays(1) }

function Get-Intraday($secId, $seg, $instr, $path) {
  if (Test-Path $path) { Remove-Item $path }
  $n = 0
  foreach ($c in $chunks) {
    $body = @{ securityId = "$secId"; exchangeSegment = $seg; instrument = $instr; interval = '5'; oi = $false
               fromDate = $c[0].ToString('yyyy-MM-dd') + ' 09:15:00'; toDate = $c[1].ToString('yyyy-MM-dd') + ' 15:30:00' }
    $n += Save-Candles (Call-Dhan 'https://api.dhan.co/v2/charts/intraday' $body) $path
  }
  return $n
}

function Get-Daily($secId, $seg, $instr, $path) {
  $body = @{ securityId = "$secId"; exchangeSegment = $seg; instrument = $instr; expiryCode = 0; oi = $false
             fromDate = $today.AddDays(-$dailyDays).ToString('yyyy-MM-dd'); toDate = $today.ToString('yyyy-MM-dd') }
  if (Test-Path $path) { Remove-Item $path }
  return Save-Candles (Call-Dhan 'https://api.dhan.co/v2/charts/historical' $body) $path
}

# ---------- index series (Nifty 50 = 13, India VIX = 21 on IDX_I)
Write-Host 'Index series...'
[void](Get-Intraday 13 'IDX_I' 'INDEX' (Join-Path $out 'index\NIFTY50_5m.csv'))
[void](Get-Daily 21 'IDX_I' 'INDEX' (Join-Path $out 'index\INDIAVIX_daily.csv'))

# ---------- every F&O stock
$i = 0
foreach ($s in $stocks) {
  $i++
  $safe = $s -replace '[^A-Za-z0-9_\-&]', '_'
  $n5 = Get-Intraday $eq[$s] 'NSE_EQ' 'EQUITY' (Join-Path $out "intraday\$safe.csv")
  $nd = Get-Daily $eq[$s] 'NSE_EQ' 'EQUITY' (Join-Path $out "daily\$safe.csv")
  "$s 5m=$n5 daily=$nd" | Add-Content $log
  Write-Host ("[{0}/{1}] {2}  5m bars {3}, daily {4}" -f $i, $stocks.Count, $s, $n5, $nd)
}

"finished $(Get-Date -Format s)" | Add-Content $log
$zip = Join-Path $PSScriptRoot 'dhan_data.zip'
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $out '*') -DestinationPath $zip
Write-Host ''
Write-Host "Done. Send this file back: $zip  ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
