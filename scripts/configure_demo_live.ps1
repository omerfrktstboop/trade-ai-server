# Tek komutluk DEMO_LIVE hazirlik akisi (Task 11).
#
# 1. .env icindeki gerekli baslangic ayarlarini dogrular (secret yazdirmaz).
# 2. python -m app.services.configure_demo_live  (idempotent config yazimi)
# 3. Gateway config reload cagirir (configure_demo_live zaten bunu yapar;
#    burada ayrica ve acikca tekrar cagrilir - reload salt bir config
#    yenilemesidir, emir gondermez).
# 4. python -m app.services.demo_live_readiness  (salt-okunur readiness raporu)
#
# Bu script hicbir zaman gercek veya demo emir gondermez, REAL_LIVE acmaz,
# ve hicbir secret/token/parola degerini ekrana yazdirmaz.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Section($title) {
    Write-Host ""
    Write-Host ("=" * 78)
    Write-Host $title
    Write-Host ("=" * 78)
}

function Write-GateFailure($gate, $detail) {
    Write-Host ("KAPALI KAPI: " + $gate) -ForegroundColor Red
    if ($detail) { Write-Host ("  -> " + $detail) -ForegroundColor Red }
}

# ── 1. .env baslangic ayarlarini dogrula (deger karsilastirmasi; secret yok) ──
Write-Section "1/4 - .env baslangic ayarlari dogrulaniyor"

$envPath = Join-Path $repoRoot ".env"
if (-not (Test-Path $envPath)) {
    Write-GateFailure ".env bulunamadi" $envPath
    exit 1
}

# Yalnizca davranissal (secret olmayan) anahtarlar okunur ve karsilastirilir.
$expected = [ordered]@{
    "SCANNER_ENABLED"                 = "true"
    "SCANNER_ALLOW_ORDERS"            = "false"
    "DEFAULT_MODE"                    = "paper"
    "POSITION_SYNC_ENABLED"           = "true"
    "ORDER_SYNC_ENABLED"              = "true"
    "SCANNER_TICK_SECONDS"            = "60"
    "POSITION_SYNC_INTERVAL_SECONDS"  = "60"
    "ORDER_SYNC_INTERVAL_SECONDS"     = "900"
    "ORDER_PENDING_TIMEOUT_MINUTES"   = "15"
}

$envLines = Get-Content -Path $envPath -Encoding UTF8
$envValues = @{}
foreach ($line in $envLines) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
        $envValues[$matches[1]] = $matches[2].Trim()
    }
}

$envOk = $true
foreach ($key in $expected.Keys) {
    $actual = $envValues[$key]
    if ($null -eq $actual) {
        Write-GateFailure ".env eksik anahtar" $key
        $envOk = $false
        continue
    }
    if ($actual.ToLower() -ne $expected[$key].ToLower()) {
        Write-GateFailure (".env beklenmeyen deger: " + $key) ("beklenen=" + $expected[$key] + " mevcut=" + $actual)
        $envOk = $false
    } else {
        Write-Host ("OK  " + $key + "=" + $actual)
    }
}

if (-not $envOk) {
    Write-Host ""
    Write-Host "Bazi .env degerleri beklenenden farkli - yine de devam ediliyor (yalnizca rapor amacli)." -ForegroundColor Yellow
    Write-Host "Not: .env degistiyse FastAPI sureci (ve Matriks algoritmasi) yeniden baslatilmalidir." -ForegroundColor Yellow
}

# ── 2. Idempotent DEMO_LIVE config yazimi ────────────────────────────────────
Write-Section "2/4 - python -m app.services.configure_demo_live"
python -m app.services.configure_demo_live
if ($LASTEXITCODE -ne 0) {
    Write-GateFailure "configure_demo_live basarisiz oldu" "cikis kodu $LASTEXITCODE"
    exit 1
}

# ── 3. Gateway config reload (configure_demo_live zaten cagirir; burada ────
#      akisin kendi adimi olarak ayrica ve acikca tekrar tetiklenir) ────────
Write-Section "3/4 - Gateway config reload"
python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO)
from app.services.matriks_gateway import gateway_client, GatewayError, GatewayUnavailable

async def main():
    try:
        result = await gateway_client.reload_config()
        print('GATEWAY_RELOAD_OK', result.get('ok'), result.get('profileCode'))
    except (GatewayUnavailable, GatewayError) as exc:
        print('GATEWAY_RELOAD_FAILED', exc)

asyncio.run(main())
"

# ── 4. Salt-okunur DEMO_LIVE readiness raporu ────────────────────────────────
Write-Section "4/4 - python -m app.services.demo_live_readiness"
$readinessOutput = python -m app.services.demo_live_readiness 2>&1
$readinessOutput | ForEach-Object { Write-Host $_ }

Write-Section "Sonuc"
$verdictLine = $readinessOutput | Select-String -Pattern "DEMO_LIVE_READINESS_VERDICT" | Select-Object -Last 1
if ($verdictLine) {
    Write-Host $verdictLine.Line
    if ($verdictLine.Line -match "NO-GO") {
        Write-Host ""
        Write-Host "Yukaridaki WARNING/NOTE satirlari hangi kapinin kapali oldugunu gosterir." -ForegroundColor Red
        exit 2
    }
} else {
    Write-GateFailure "demo_live_readiness ciktisinda verdict satiri bulunamadi" ""
    exit 1
}
