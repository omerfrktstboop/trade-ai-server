# Matriks IQ → POST /api/signal/evaluate

Matriks IQ (C# tarafı) teknik indikatör sinyali aldığında `POST /api/signal/evaluate`
endpoint'ine JSON istek atar. Sunucu isteği AI + Risk Engine'den geçirip BUY/SELL/WAIT
kararıyla döner.

---

## Endpoint

| Öğe | Değer |
|---|---|
| **URL** | `https://<sunucu>/api/signal/evaluate` |
| **Method** | `POST` |
| **Content-Type** | `application/json` |
| **Auth** | `Authorization: Bearer <API_TOKEN>` |
| **Timeout** | 15 saniye önerilir (AI çağrısı 3-10s sürebilir) |

---

## Request — JSON Body

```json
{
  "requestId": "uniq-2026-07-06-001",
  "symbol": "THYAO",
  "timeframe": "15m",
  "lastPrice": 71.25,
  "open": 70.80,
  "high": 71.90,
  "low": 70.40,
  "volume": 1_250_000,
  "rsi": 58.4,
  "ema20": 70.15,
  "ema50": 68.90,
  "macd": 0.45,
  "macdSignal": 0.30,
  "botPositionQty": 0,
  "totalAccountQty": 120,
  "lockedLongTermQty": 50,
  "dailyTradeCount": 0,
  "mode": "PAPER"
}
```

### Alanlar

| Alan | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `requestId` | string | ✅ | Tekil istek kimliği (log eşleşmesi için) |
| `symbol` | string | ✅ | Hisse/coin sembolü (örn. `THYAO`) |
| `timeframe` | string | ✅ | Zaman dilimi (`1m`, `5m`, `15m`, `1h`, `4h`, `1d`) |
| `lastPrice` | float | ✅ | Son fiyat |
| `open` | float | ✅ | Açılış |
| `high` | float | ✅ | Yüksek |
| `low` | float | ✅ | Düşük |
| `volume` | float | ✅ | Hacim |
| `rsi` | float | ❌ | RSI(14) göstergesi |
| `ema20` | float | ❌ | 20 periyot EMA |
| `ema50` | float | ❌ | 50 periyot EMA |
| `macd` | float | ❌ | MACD çizgisi |
| `macdSignal` | float | ❌ | MACD sinyal çizgisi |
| `botPositionQty` | float | ❌ | Bot'un şu anda tuttuğu lot (varsayılan: `0`) |
| `totalAccountQty` | float | ❌ | Hesaptaki toplam lot (varsayılan: `0`) |
| `lockedLongTermQty` | float | ❌ | Uzun vade kilitli lot (varsayılan: `0`) |
| `dailyTradeCount` | int | ❌ | Günlük işlem sayısı (varsayılan: `0`) — `maxDailyTradeCount` ile sınırlı |
| `mode` | string | ❌ | `"PAPER"` / `"MANUAL"` / `"LIVE"` (varsayılan: `"PAPER"`) |

### Mode açıklaması

| Mode | Emir izni | Onay gerekir | Kullanım |
|---|---|---|---|
| `PAPER` | ❌ Asla emir göndermez | ❌ | Geliştirme / test |
| `MANUAL` | ❌ Otomatik emir göndermez | ✅ `requiresConfirmation=true` | Onaylı yarı-otonom |
| `LIVE` | ✅ Risk geçerse izin verir | ❌ | Tam otonom |

---

## TLS & HTTPS

Tüm istekler **HTTPS** üzerinden gelmelidir. Self-signed sertifika ile test ediyorsanız
C# tarafında sertifika doğrulama hatalarını atlamanız gerekebilir (sadece geliştirme
ortamında, production'da değil).

---

## Request Örnekleri

### curl

```bash
curl -X POST https://localhost:8000/api/signal/evaluate \
  -H "Authorization: Bearer dev-token-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "requestId": "thyao-15m-001",
    "symbol": "THYAO",
    "timeframe": "15m",
    "lastPrice": 71.25,
    "open": 70.80,
    "high": 71.90,
    "low": 70.40,
    "volume": 1250000,
    "rsi": 58.4,
    "ema20": 70.15,
    "ema50": 68.90,
    "macd": 0.45,
    "macdSignal": 0.30,
    "botPositionQty": 0,
    "totalAccountQty": 120,
    "lockedLongTermQty": 50,
    "mode": "PAPER"
  }'
```

### C# HttpClient

```csharp
using System;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

public class SignalClient
{
    private readonly HttpClient _http;

    public SignalClient(string baseUrl, string apiToken)
    {
        _http = new HttpClient
        {
            BaseAddress = new Uri(baseUrl),
            Timeout = TimeSpan.FromSeconds(15)
        };
        _http.DefaultRequestHeaders.Add("Authorization", $"Bearer {apiToken}");
    }

    public async Task<SignalResponse> EvaluateSignalAsync(SignalRequest request)
    {
        var json = JsonSerializer.Serialize(request, new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase
        });

        var content = new StringContent(json, Encoding.UTF8, "application/json");

        using var response = await _http.PostAsync("/api/signal/evaluate", content);
        response.EnsureSuccessStatusCode();

        var body = await response.Content.ReadAsStringAsync();
        return JsonSerializer.Deserialize<SignalResponse>(body, new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true
        })!;
    }
}

// ── DTOs ─────────────────────────────────────────────────────────────────

public class SignalRequest
{
    public string RequestId { get; set; } = "";
    public string Symbol { get; set; } = "";
    public string Timeframe { get; set; } = "15m";
    public double LastPrice { get; set; }
    public double Open { get; set; }
    public double High { get; set; }
    public double Low { get; set; }
    public double Volume { get; set; }
    public double? Rsi { get; set; }
    public double? Ema20 { get; set; }
    public double? Ema50 { get; set; }
    public double? Macd { get; set; }
    public double? MacdSignal { get; set; }
    public double BotPositionQty { get; set; }
    public double TotalAccountQty { get; set; }
    public double LockedLongTermQty { get; set; }
    public string Mode { get; set; } = "PAPER";
}

public class SignalResponse
{
    public string RequestId { get; set; } = "";
    public string Symbol { get; set; } = "";
    public string Action { get; set; } = "WAIT";        // BUY | SELL | WAIT
    public double Qty { get; set; }
    public string OrderType { get; set; } = "NONE";     // MARKET | LIMIT | NONE
    public double? Price { get; set; }
    public double ConfidenceScore { get; set; }
    public double RiskScore { get; set; }
    public bool AllowOrder { get; set; }
    public bool RequiresConfirmation { get; set; }
    public string Reason { get; set; } = "";
}

// ── Kullanım ─────────────────────────────────────────────────────────────

var client = new SignalClient("https://sunucu-adresi", "API_TOKEN_HERE");

var request = new SignalRequest
{
    RequestId = $"thyao-{DateTime.UtcNow:yyyyMMdd-HHmmss}",
    Symbol = "THYAO",
    Timeframe = "15m",
    LastPrice = 71.25,
    Open = 70.80,
    High = 71.90,
    Low = 70.40,
    Volume = 1_250_000,
    Rsi = 58.4,
    Ema20 = 70.15,
    Ema50 = 68.90,
    Macd = 0.45,
    MacdSignal = 0.30,
    BotPositionQty = 0,
    TotalAccountQty = 120,
    LockedLongTermQty = 50,
    Mode = "PAPER"
};

var result = await client.EvaluateSignalAsync(request);

Console.WriteLine($"Action: {result.Action}");
Console.WriteLine($"Confidence: {result.ConfidenceScore}%");
Console.WriteLine($"AllowOrder: {result.AllowOrder}");
Console.WriteLine($"Reason: {result.Reason}");
```

---

## Response — JSON

Sunucu her zaman **HTTP 200** döner. Asıl karar `action` ve `allowOrder` alanlarındadır.

### PAPER modunda (mock AI, THYAO — always WAIT)

```json
HTTP 200 OK

{
  "requestId": "thyao-15m-001",
  "symbol": "THYAO",
  "action": "WAIT",
  "qty": 0,
  "orderType": "NONE",
  "price": null,
  "confidenceScore": 50,
  "riskScore": 0,
  "allowOrder": false,
  "requiresConfirmation": false,
  "reason": "MockProvider returns WAIT for all symbols",
  "entryRange": null,
  "stopLoss": null,
  "targetPrice": null
}
```

### MANUAL modunda (örnek — AI BUY önerisi, onay bekler)

```json
HTTP 200 OK

{
  "requestId": "thyao-15m-002",
  "symbol": "THYAO",
  "action": "BUY",
  "qty": 42,
  "orderType": "LIMIT",
  "price": 71.25,
  "confidenceScore": 82,
  "riskScore": 15,
  "allowOrder": false,
  "requiresConfirmation": true,
  "reason": "MANUAL mode — requires user confirmation; Strong buy signal.",
  "entryRange": {
    "min": 70.80,
    "max": 71.50
  },
  "stopLoss": 68.90,
  "targetPrice": 76.00
}
```

### LIVE modunda (örnek — gerçek AI kararı, otomatik emir)

```json
HTTP 200 OK

{
  "requestId": "thyao-15m-003",
  "symbol": "THYAO",
  "action": "BUY",
  "qty": 42,
  "orderType": "LIMIT",
  "price": 71.25,
  "confidenceScore": 82,
  "riskScore": 15,
  "allowOrder": true,
  "requiresConfirmation": false,
  "reason": "RSI oversold bounce with MACD golden cross on 15m. Strong buy signal.",
  "entryRange": {
    "min": 70.80,
    "max": 71.50
  },
  "stopLoss": 68.90,
  "targetPrice": 76.00
}
```

---

## Response Alanları

| Alan | Tip | Açıklama |
|---|---|---|
| `requestId` | string | İstekte gönderilen ID — eşleşme için |
| `symbol` | string | Sembol |
| `action` | enum | `"BUY"` / `"SELL"` / `"WAIT"` |
| `qty` | float | Emir adedi (`WAIT` → 0) |
| `orderType` | enum | `"MARKET"` / `"LIMIT"` / `"NONE"` |
| `price` | float\|null | Limit emir fiyatı (MARKET → null) |
| `confidenceScore` | float | AI güven skoru (0-100) |
| `riskScore` | float | Risk skoru (0–100, düşük = güvenli) |
| `allowOrder` | bool | **Risk engine emre izin verdi mi?** `true` ise emri gönder |
| `requiresConfirmation` | bool | **Kullanıcıdan onay istenmeli mi?** MANUAL modda BUY/SELL için `true` |
| `reason` | string | Kararın gerekçesi |
| `entryRange` | object\|null | Limit emir için fiyat aralığı (`min`, `max`) |
| `stopLoss` | float\|null | Önerilen zarar-kes seviyesi |
| `targetPrice` | float\|null | Önerilen hedef fiyat |

---

## Matriks IQ için Karar Akışı

```
Matriks IQ                           trade-ai-server
──────────                           ───────────────
  │                                        │
  │  POST /api/signal/evaluate             │
  │  { requestId, symbol, rsi, ... }       │
  │ ──────────────────────────────────────>│
  │                                        │─── get_news_context()
  │                                        │─── get_fund_context()
  │                                        │─── get_broker_flow_context()
  │                                        │─── AI provider (DeepSeek)
  │                                        │─── RiskEngine.evaluate()
  │                                        │
  │  200 { action, allowOrder,           │
  │        requiresConfirmation, ... }    │
  │ <──────────────────────────────────────│
  │                                        │
  │  if allowOrder == true:                │
  │    --> emri otomatik gonder (LIVE)    │
  │                                        │
  │  if requiresConfirmation == true:      │
  │    --> kullaniciya sor (MANUAL)       │
  │                                        │
  │  if allowOrder == false                │
  │     and requiresConfirmation == false: │
  │    --> skip (PAPER / risk blocked)    │
  │                                        │
  │  if action in (BUY, SELL)              │
  │    and (allowOrder or                  │
  │         requiresConfirmation):         │
  │    --> emir gonder veya onay goster   │
  │      (qty, orderType, price,           │
  │       entryRange)                      │
  │                                        │
  │   POST /api/order-result               │
  │   { requestId, status, ... }           │
  │ ──────────────────────────────────────>│
  │                                        │─── order_logs tablosuna kaydet
```

---

## Hata Durumları

| Durum | HTTP Kodu | Açıklama |
|---|---|---|
| Token yok / yanlış | 401 | `{"detail": "Not authenticated"}` |
| Geçersiz JSON / eksik alan | 422 | Pydantic validation error detayıyla |
| Sunucu iç hatası | 500 | Beklenmeyen hata (nadir) |

---

## Notlar

- **PAPER modunda** `allowOrder` her zaman `false`, `requiresConfirmation` her zaman `false` — emir göndermeye kalkmayın.
- **MANUAL modunda** `allowOrder` her zaman `false`'tır. AI BUY veya SELL önerirse `requiresConfirmation: true` döner — Matriks IQ kullanıcıya onay sormalı. WAIT ise `requiresConfirmation: false` döner.
- **LIVE modunda** risk kontrolleri geçerse `allowOrder: true` dönebilir, `requiresConfirmation: false`.
- **Cutoff kontrolü:** `disableTradingAfter` (varsayılan `17:30`) saatinden sonra BUY/SELL otomatik engellenir (`reason: "Trading blocked: after cutoff time 17:30"`). WAIT kararları etkilenmez.
- **Günlük işlem limiti:** `maxDailyTradeCount` (varsayılan `3`) aşıldığında BUY/SELL engellenir. Matriks IQ `dailyTradeCount` alanını göndererek günlük işlem sayısını bildirmeli.
- `confidenceScore < 70` genelde `allowOrder: false` ile sonuçlanır (risk eşikleri).
- Sunucu `requestId`'yi aynen döndürür — Matriks tarafında request/response eşleşmesi için kullanın.
- Timeout: AI çağrısı 3-10 saniye sürebilir. C# tarafında `HttpClient.Timeout` en az 15s olmalı.
