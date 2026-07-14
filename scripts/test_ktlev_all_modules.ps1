# KTLEV icin tum gateway ve server modullerini test edip bir rapor yazar.

$gwToken = $env:MATRIX_GATEWAY_TOKEN
$srvToken = $env:EVALUATION_API_TOKEN
if ([string]::IsNullOrWhiteSpace($gwToken)) {
    throw "MATRIX_GATEWAY_TOKEN must be set before running this script."
}
if ([string]::IsNullOrWhiteSpace($srvToken)) {
    throw "EVALUATION_API_TOKEN must be set before running this script."
}

$gwBase = "http://127.0.0.1:8787"
$srvBase = "http://127.0.0.1:8000"
$symbol = "KTLEV"
$outPath = "C:\Users\Administrator\Desktop\KTLEV_test_raporu.txt"

$lines = New-Object System.Collections.Generic.List[string]

function Add-Line($text) { $lines.Add($text) | Out-Null }
function Add-Section($title) {
    Add-Line ""
    Add-Line ("=" * 78)
    Add-Line $title
    Add-Line ("=" * 78)
}

function Test-Endpoint {
    param(
        [string]$Name,
        [string]$Url,
        [hashtable]$Headers,
        [string]$Method = "GET"
    )
    Add-Line ""
    Add-Line ("--- " + $Name + " ---")
    Add-Line ("URL: " + $Url)
    try {
        $resp = Invoke-RestMethod -Uri $Url -Method $Method -Headers $Headers -TimeoutSec 20
        Add-Line "DURUM: OK"
        Add-Line ($resp | ConvertTo-Json -Depth 8)
        return $resp
    } catch {
        Add-Line "DURUM: HATA"
        Add-Line $_.Exception.Message
        if ($_.ErrorDetails.Message) { Add-Line $_.ErrorDetails.Message }
        return $null
    }
}

Add-Line "KTLEV - TUM MODUL TEST RAPORU"
Add-Line ("Olusturulma: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ("Sembol: " + $symbol)

Add-Section "1. MATRIKS GATEWAY - GENEL DURUM (sembol bagimsiz)"
$gwHeaders = @{ Authorization = "Bearer $gwToken" }
Test-Endpoint -Name "Gateway Health" -Url "$gwBase/health" -Headers $gwHeaders
Test-Endpoint -Name "Bot Positions" -Url "$gwBase/positions" -Headers $gwHeaders
Test-Endpoint -Name "Capabilities" -Url "$gwBase/capabilities" -Headers $gwHeaders
Test-Endpoint -Name "MKK/Takas Durumu" -Url "$gwBase/mkk" -Headers $gwHeaders
Test-Endpoint -Name "Movers (Yukselen/Dusen/Hacimli)" -Url "$gwBase/movers?limit=20" -Headers $gwHeaders
Test-Endpoint -Name "Account (Trade User)" -Url "$gwBase/account" -Headers $gwHeaders
Test-Endpoint -Name "Real Positions" -Url "$gwBase/realpositions" -Headers $gwHeaders
Test-Endpoint -Name "Overall" -Url "$gwBase/overall" -Headers $gwHeaders
Test-Endpoint -Name "Method Catalog" -Url "$gwBase/capabilities/methods" -Headers $gwHeaders

Add-Section ("2. MATRIKS GATEWAY - " + $symbol + " OZEL MODULLER")
$snapshot = Test-Endpoint -Name "Snapshot (OHLCV+Derinlik+Teknik)" -Url "$gwBase/snapshot?symbol=$symbol" -Headers $gwHeaders
Test-Endpoint -Name "Depth (25 Kademe)" -Url "$gwBase/depth?symbol=$symbol&levels=25" -Headers $gwHeaders
Test-Endpoint -Name "Indicators (RSI/EMA/MACD)" -Url "$gwBase/indicators?symbol=$symbol" -Headers $gwHeaders
Test-Endpoint -Name "News (Matriks native)" -Url "$gwBase/news?symbol=$symbol&limit=20" -Headers $gwHeaders
Test-Endpoint -Name "News Details" -Url "$gwBase/news/details?symbol=$symbol" -Headers $gwHeaders
Test-Endpoint -Name "Institutions (AKD)" -Url "$gwBase/institutions?symbol=$symbol" -Headers $gwHeaders
Test-Endpoint -Name "Market Data - Last" -Url "$gwBase/marketdata?symbol=$symbol&field=Last" -Headers $gwHeaders
Test-Endpoint -Name "Market Data - All Fields" -Url "$gwBase/marketdata/all?symbol=$symbol" -Headers $gwHeaders
Test-Endpoint -Name "Symbol Info" -Url "$gwBase/symbol?symbol=$symbol" -Headers $gwHeaders
Test-Endpoint -Name "Session Times" -Url "$gwBase/session?symbol=$symbol" -Headers $gwHeaders

$lastPrice = 10.0
if ($snapshot -and $snapshot.payload -and $snapshot.payload.lastPrice) {
    $lastPrice = $snapshot.payload.lastPrice
}
Test-Endpoint -Name "Price Step" -Url "$gwBase/pricestep?symbol=$symbol&price=$lastPrice" -Headers $gwHeaders
Test-Endpoint -Name "Bars (OHLC + Kapanis Gecmisi)" -Url "$gwBase/bars?symbol=$symbol&count=50" -Headers $gwHeaders

Add-Section "3. FASTAPI SERVER - SIGNAL EVALUATE (uctan uca AI karari)"
$srvHeaders = @{ Authorization = "Bearer $srvToken"; "Content-Type" = "application/json" }
Add-Line ""
Add-Line "--- /api/signal/evaluate (PAPER mode, manuel veri) ---"
$body = @{
    requestId = "ktlev-test-" + (Get-Date -Format "yyyyMMddHHmmss")
    symbol = $symbol
    timeframe = "Min5"
    lastPrice = $lastPrice
    open = $lastPrice
    high = $lastPrice
    low = $lastPrice
    volume = 1000000
    rsi = 50.0
    mode = "PAPER"
} | ConvertTo-Json
try {
    $evalResp = Invoke-RestMethod -Uri "$srvBase/api/signal/evaluate" -Method Post -Headers $srvHeaders -Body $body -TimeoutSec 30
    Add-Line "DURUM: OK"
    Add-Line ($evalResp | ConvertTo-Json -Depth 8)
} catch {
    Add-Line "DURUM: HATA"
    Add-Line $_.Exception.Message
    if ($_.ErrorDetails.Message) { Add-Line $_.ErrorDetails.Message }
}

Add-Section "4. FASTAPI SERVER - GATEWAY CONFIG (server -> gateway senkron)"
Test-Endpoint -Name "Gateway Config (server tarafindan uretilen)" -Url "$srvBase/api/gateway/config" -Headers $srvHeaders

Add-Section "5. FASTAPI SERVER - HEALTH + DASHBOARD"
Test-Endpoint -Name "Server Health" -Url "$srvBase/api/health" -Headers @{}
Test-Endpoint -Name "Admin Dashboard (son kararlar)" -Url "$srvBase/api/admin/dashboard" -Headers $srvHeaders

Add-Line ""
Add-Line ("=" * 78)
Add-Line "RAPOR TAMAMLANDI"
Add-Line ("=" * 78)

$lines -join "`r`n" | Out-File -FilePath $outPath -Encoding utf8
Write-Output "Rapor yazildi: $outPath"
Write-Output ("Toplam satir: " + $lines.Count)
