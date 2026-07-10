# Plan: Full Inversion — Server Orkestratör, Matriks Gateway

**Created:** 2026-07-09
**Goal:** Kontrolü tersine çevir. FastAPI server (aynı makinede) beyin olur;
Matriks tarafı sadece veri + emir kapısı (thin gateway) olarak kalır.
FETCH_DATA oturum protokolü ve C# tarafındaki tüm karar/zamanlama mantığı silinir.

## Mevcut vs Hedef

```
MEVCUT (pull):                          HEDEF (inversiyon):
Matriks bot (2808 satır C#)             FastAPI server (beyin)
  timer → veri topla                      scheduler → her sembol için:
  → POST /evaluate-agent                    GET  gateway/snapshot?symbol=X
  ← FETCH_DATA ping-pong (sessionId)        (AI ek veri isterse senkron GET)
  → tekrar POST (contextHistory)            → AI karar + RiskEngine
  ← BUY/SELL/WAIT                           → POST gateway/order
  → SendLimitOrder                        Matriks gateway (~600 satır C#)
  → POST /order-result                      127.0.0.1:8787 HTTP listener
                                            veri sunar + LIMIT emir basar
                                            OnOrderUpdate → POST /api/order-result
```

Her iki süreç aynı Windows PC'de çalışır. Gateway **sadece loopback**'e bağlanır
(`IPAddress.Loopback`) — dışarıya port açılmaz.

## Gateway API Sözleşmesi (C# tarafı)

Auth: `Authorization: Bearer <GATEWAY_TOKEN>` (paylaşılan sır, iki tarafın config'inde).

| Method | Path | Açıklama |
|---|---|---|
| GET | `/health` | Matriks ayakta mı, market data akıyor mu, hesap bağlı mı |
| GET | `/snapshot?symbol=THYAO` | OHLCV + bid/ask/derinlik + teknik feature bloğu (RSI, EMA20/50, MACD, alphaTrend proxy, nATR, indicatorConsensus, marketRegime, depthQueueDropPct) — mevcut `BuildTechnicalFeatures` kodu aynen taşınır |
| GET | `/positions` | Bot pozisyonları + gerçek hesap pozisyonları + locked long-term lotlar (mevcut `SyncPositionsToServerAsync` payload'unun aynısı, yön ters) |
| POST | `/order` | `{requestId, symbol, side, qty, limitPrice, timeInForce}` → validasyon + `SendLimitOrder`. Yanıt: `{accepted, orderId?, status: "SENT_PENDING", reason?}` |

**Emir sonuçları:** Gateway'e polling endpoint'i eklenmez. `OnOrderUpdate`
mevcut outbound `POST /api/order-result` raporlamasını aynen korur —
zaten çalışıyor, gateway'de state tutmayı önler.

**Gateway'de kalan sabit güvenlik kilitleri (son savunma hattı, C# içinde):**
- MARKET emir asla (`AllowMarketOrders` parametresi bile kaldırılır — sadece LIMIT)
- `MaxQtyPerOrder`, `MaxOrderValueTl` üst sınırları
- `LockedLongTermQty` — kilitli lotlar satılamaz (SELL clamp)
- `MaxOrdersPerDay` / `MaxOrdersPerSymbolPerDay` hard cap
- `EnableDemoOrders` / `EnableRealOrders` / `DemoAccountConfirmed` bayrakları
- Duplicate `requestId` reddi (mevcut `_sentRequestIds` mantığı)
- Sembol whitelist (server config'ten değil, gateway parametresinden)

Kural: server ne isterse istesin, gateway bu sınırların dışına çıkamaz.
Zeka server'da, fren mekanizması C#'ta.

## Server Tarafı Değişiklikler

### Yeni dosyalar
- `app/services/matriks_gateway.py` — httpx AsyncClient sarmalayıcı:
  `get_snapshot(symbol)`, `get_positions()`, `send_order(...)`, `health()`.
  Timeout + tek retry; gateway ulaşılamazsa `GatewayUnavailable` exception.
- `app/services/scanner.py` — asyncio background task (lifespan'de başlar):
  - Her `SCAN_INTERVAL_MINUTES`'ta tradeable sembolleri döner
  - Gateway health kontrolü; Matriks kapalıysa döngüyü atla + logla
  - Sembol başına: snapshot çek → `evaluate_symbol()` → karar → emir
  - Kill switch / cutoff saati / günlük limit kontrolleri emirden ÖNCE
- `app/services/evaluator.py` — in-process agent döngüsü:
  - Eski FETCH_DATA ping-pong'unun yerine düz fonksiyon: AI ek veri isterse
    (depth, ek sembol, haber) **senkron olarak** gateway'den/servislerden çeker,
    max 3 tur, sonra AI kararı → RiskEngine → final karar.
  - Session/contextHistory/sessionId kavramları tamamen kalkar —
    tüm turlar tek fonksiyon çağrısı içinde, in-memory.

### Silinecekler (Phase 3'te)
- `app/services/agent_session.py` (147 satır)
- `app/services/session_store.py` (246 satır)
- `app/services/agent_planner.py` (161 satır)
- `app/routers/signal.py` içindeki `/api/signal/evaluate-agent` endpoint'i
  ve session yönetim kodu (~yarısı gider)
- `app/routers/bot_config.py` içindeki `/api/bot/positions/sync` ve
  `/api/bot/tradeable-symbols` (server artık pozisyonu kendisi çeker,
  sembol listesi scanner'ın iç config'i)
- `app/services/bot_runtime_config.py` — bot'a config itme mantığı;
  scanner config'i olarak sadeleşir (trade profile entegrasyonu kalır)
- `tests/test_agent_signal.py` içindeki session/FETCH_DATA testleri

### Korunanlar
- `/api/order-result` — gateway hâlâ buraya raporlar
- `/api/signal/evaluate` (tek atımlık) — manuel test/debug için kalabilir
- Admin panel, trade profiles, RiskEngine, ai_provider, news/fundamentals —
  değişmez; RiskEngine artık scanner'dan çağrılır, router'dan değil
- Kill switch davranışı: artık "WAIT döndür" değil, "scanner emir basmaz"

### Config (.env eklemeleri)
```
MATRIKS_GATEWAY_URL=http://127.0.0.1:8787
MATRIKS_GATEWAY_TOKEN=<paylaşılan-sır>
SCAN_INTERVAL_MINUTES=30
SCANNER_ENABLED=true          # false → server sadece API olarak çalışır
```

**Deployment kararı (2026-07-09):** Hedef makine Windows Server 2019
(Xeon E5-2699 v4, 10 GB RAM). Docker Desktop Server 2019'u desteklemediği
ve WSL2 bulunmadığı için **Docker kullanılmayacak** — her şey native:
- Python + uvicorn doğrudan Windows'ta (NSSM/Task Scheduler ile servis)
- PostgreSQL native Windows kurulumu
- Server → gateway doğrudan `127.0.0.1:8787` (host.docker.internal sorunu yok)
- Matriks GUI uygulama: açık bir oturumda çalışmalı (RDP disconnect OK,
  logoff değil); autologon + startup'ta Matriks açılışı planlanmalı
- `docker-compose.yml` ve `deploy-production.yml` bu mimaride kullanılmaz —
  Phase 4'te kaldırılır veya lokal kurulum dokümanıyla değiştirilir

**Uzaktan erişim (admin panel):** 8000 portu internete açılmaz
(port-forward/DDNS yok). Erişim **Tailscale** üzerinden:
sunucu + istemci cihazlara Tailscale kurulur, admin panele
`http://<tailscale-adı>:8000/admin` ile ulaşılır. Gateway (8787) her
koşulda loopback-only kalır — Tailscale'den bile görünmez.

**Kurulum aracı:** Sunucuya Claude Code CLI kurulur (Git for Windows +
Node.js LTS ön koşul); Python/PostgreSQL/NSSM/klonlama adımları bu planla
Claude Code'a yaptırılır. GUI gerektiren adımlar manuel: Matriks IQ
kurulumu/lisansı, autologon, Tailscale oturum açma.

## C# Tarafı Değişiklikler

- **Yeni:** `matriks/TradeAiGateway.cs` (~600 satır hedef). Kaynaklar:
  - HTTP server iskeleti → `TradeAiHttpApiTest.cs`'ten (TcpListener,
    ReadRequestAsync, WriteJsonAsync, auth — zaten kanıtlanmış)
  - Veri toplama → `TradeAiAgenticBot.cs`'ten: `AddSymbol` abonelikleri,
    close history, `_lastValidQuoteBySymbol`, `BuildTechnicalFeatures`,
    nATR/AlphaTrend proxy/consensus hesapları
  - Emir → `SendLimitOrderAsync`, `OnOrderUpdate`, `ReportOrderResultAsync`,
    güvenlik kilitleri
- **Silinen C# mantığı:** `OnTimer` tarama döngüsü, `SendEvaluateAsync`,
  `FetchRequestedDataAsync` (FETCH_DATA), `FetchBotConfigFromServer`,
  `RefreshPendingOverridesAsync`, `SyncPositionsToServerAsync` (pull'a döner),
  tüm session/contextHistory serileştirme sınıfları
- `TradeAiAgenticBot.cs` cutover'a kadar repoda kalır (paralel koşu için),
  Phase 4'te silinir. `TradeAiHttpApiTest.cs` de gateway'e evrilince silinir.

## Fazlar

### Phase 0 — Read-only gateway (risk: sıfır) ✅ 2026-07-09
- [x] `TradeAiGateway.cs`: `/health`, `/snapshot`, `/positions` (emir YOK)
- [x] `app/services/matriks_gateway.py` client (+ `MATRIKS_GATEWAY_*` config)
- [x] `scripts/gateway_smoke.py` — uçtan uca doğrulama (fake gateway'e karşı test edildi)
- [x] `tests/fake_gateway.py` + `tests/test_matriks_gateway.py` (14 test)
- [ ] Gerçek Matriks IQ'da derleme + smoke test (sunucuda yapılacak)
- [ ] Eski bot paralel çalışmaya devam eder

### Phase 1 — Server-side beyin, PAPER modda (risk: sıfır) ✅ 2026-07-09
- [x] `evaluator.py` — in-process agent döngüsü (FETCH_DATA'sız).
      Gateway snapshot'ı tüm veri tiplerini tek çağrıda döndürdüğü için eski
      DEPTH→OHLCV→TECHNICAL zinciri tek çağrıya indi; sadece RELATED_SYMBOLS
      için ek snapshot alınıyor. Admin override + kill switch + runtime
      config davranışları evaluate-agent ile birebir korundu.
- [x] `scanner.py` — background task, `force_paper=True` sabit: karar üret,
      `signal.log` + `ai_decisions`/`risk_decisions`'a yaz, emir basma.
      Lifespan'de `SCANNER_ENABLED=true` ile başlar (default: false).
- [x] Kill switch / cutoff / gateway-sağlık kapıları tick başında; pending
      override'lar interval'ı bypass eder (eski bot davranışı).
- [x] Testler: 21 yeni test (evaluator 12 + scanner 9), fake gateway +
      stub provider ile. E2E doğrulandı: fake gateway'e karşı uvicorn +
      scanner PAPER kararları log ve DB'ye yazdı; cutoff kapısının gerçekte
      turu atladığı gözlemlendi.
- [x] `main.py` Windows encoding düzeltmesi (cp1254 + emoji print çökmesi —
      NSSM servis kurulumunda da gerekliydi).
- [ ] Sunucuda birkaç gün eski bot'la paralel koşu — kararlar `ai_decisions`
      tablosundan karşılaştırılır (cutover ön şartı)

### Phase 2 — Emir yolu (risk: kontrollü, DEMO_LIVE) — kod tamam 2026-07-09
- [x] Gateway'e `POST /order` + tüm güvenlik kilitleri (TrySendOrderAsync
      kapıları birebir taşındı; SELL üst sınırı kilitli lotları düşecek
      şekilde GÜÇLENDİRİLDİ: qty ≤ botQty − lockedLongTermQty).
      Yeni parametreler: ServerBaseUrl/ServerApiToken (order-result raporu),
      EnableDemoOrders/EnableRealOrders/RequireDemoAccount/DemoAccountConfirmed,
      MaxOrderValueTl/MaxQtyPerOrder/MaxOrdersPerDay/MaxOrdersPerSymbolPerDay,
      OrderTimeInForce. Hepsi default kapalı/temkinli.
      Endpoint MARKET kavramını hiç tanımıyor — sadece limitPrice var.
- [x] `send_order` client metodu — bilinçli olarak RETRY YOK (çift emir +
      duplicate-koruması yanlış REJECTED riski); tek deneme.
- [x] Scanner → emir akışı: `SCANNER_ALLOW_ORDERS=false` (default) → Phase 1
      davranışı (her şey PAPER). `true` → mod admin panelden; sadece
      DEMO_LIVE emre dönüşür, REAL_LIVE kod seviyesinde bloklu.
      Senkron sonuçlar (SENT_PENDING/REJECTED/ERROR) `order_logs`'a yazılır.
- [x] Evaluator `EvaluationResult` (karar + efektif mod) döndürür.
- [x] Testler: send_order (3), scanner emir kapıları (11). Hızlı grup
      toplam 275 test yeşil.
- [x] E2E doğrulama (fake gateway ile): admin tradingMode=DEMO_LIVE +
      THYAO BUY override enjekte edildi → pending-override interval'ı
      bypass etti → RiskEngine allowOrder=true → scanner gateway'e
      `{side:BUY, qty:1, limitPrice:71.5, mode:DEMO_LIVE}` gönderdi →
      order_logs'a SENT_PENDING yazıldı. Zincirin tamamı çalışıyor.
- [ ] Gerçek Matriks'te DEMO_LIVE ile `OnOrderUpdate` → `/api/order-result`
      raporlaması doğrulanır (sunucu kurulumunda)
- [ ] README'deki canlı öncesi kontrol listesi yeni mimariye uyarlanıp
      baştan koşulur

### Phase 3 — Cutover ve söküm — kod tamam 2026-07-10
⚠️ **Bu kod, eski bot'un kullandığı endpoint'leri siler. Sunucuda paralel
koşu doğrulanmadan ve eski bot durdurulmadan DEPLOY EDİLMEMELİ.**

- [x] `/evaluate-agent` endpoint'i + agentic yardımcıları silindi.
- [x] Silinen dosyalar: `agent_session.py`, `session_store.py`,
      `agent_planner.py`, `bot_runtime_config.py`, `routers/bot_config.py`
      (~1000 satır). `RELATED_SYMBOLS` kuralı evaluator'a taşındı.
- [x] Bağımlılık tersine çevrildi: paylaşılan boru hattı yardımcıları
      (`build_payload`, `dict_to_risk_decision`, `with_runtime_controls`,
      `persist_evaluation`, `with_resolved_daily_trade_count`,
      `kill_switch_response`) artık evaluator'da; `routers/signal.py`
      ince bir HTTP sarmalayıcı. `AgenticSignalRequest` köprüsü yerine
      doğrudan `snapshot_to_signal_request`.
- [x] **Pozisyon sync push→pull:** `POST /api/bot/positions/sync` silindiği
      için admin Positions sayfası ve acil "tümünü sat" akışı veri kaynağını
      kaybedecekti. Yeni `position_sync.py` scanner turunda gateway'den
      pozisyonları çekip `bot_positions`'a upsert ediyor.
- [x] **Gizli hata düzeltildi:** eski agentic köprü `dailyTradeCount`'u her
      zaman payload'dan (0) alıp explicit set ediyordu; bu yüzden DB'den
      çözümleme atlanıyor ve günlük işlem limiti kapısı hiç devreye
      girmiyordu. `snapshot_to_signal_request` artık alanı hiç set etmiyor →
      `with_resolved_daily_trade_count` DB'den dolduruyor.
- [x] Testler: `test_agent_signal/session_store/agent_planner/bot_config`
      silindi; `test_signal_override` evaluator+fake gateway'e taşındı
      (SELL clamp ve REAL_LIVE koruması kapsamı korundu); yeni
      `test_position_sync.py` (5 test).
- [ ] Sunucuda eski `TradeAiAgenticBot` durdurulur (cutover anı)

### Phase 4 — Temizlik
- [ ] `TradeAiAgenticBot.cs` ve `TradeAiHttpApiTest.cs` silinir
- [ ] README + `docs/matriks-sample-request.md` yeni mimariye göre yazılır
- [ ] `deploy-production.yml` kaldırılır veya lokal kuruluma uyarlanır
- [ ] ruff + pytest + smoke test yeşil

## Test Stratejisi
- **pytest:** Gateway'i taklit eden mini FastAPI stub (`tests/fake_gateway.py`)
  ile evaluator/scanner testleri — snapshot ver, emir çağrısını yakala, doğrula
- **C# smoke:** curl ile `/health`, `/snapshot`, `/positions`;
  `/order` PAPER/DEMO'da manuel
- **Paralel koşu:** Phase 1'de eski/yeni karar karşılaştırması (en kritik doğrulama)

## Güvenlik Değişmezleri (her fazda geçerli)
- Gateway sadece `127.0.0.1`'e bind olur — asla `0.0.0.0`
- Bearer token zorunlu (health dahil edilebilir, `/ping` hariç)
- MARKET emir hiçbir katmanda üretilmez
- Server'ın istediği ≠ gateway'in yapacağı: C# limitleri her zaman üstün
- Matriks kapalı/ulaşılamaz → scanner o turu atlar, hata fırlatmaz, loglar
- `SCANNER_ENABLED=false` acil durumda tüm otomasyonu keser (kill switch'e ek)
