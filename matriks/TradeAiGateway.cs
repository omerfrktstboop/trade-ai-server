using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Reflection;
using Matriks.Data.Symbol;
using Matriks.Engines;
using Matriks.Indicators;
using Matriks.Enumeration;
using Matriks.IntermediaryInstitutionAnalysis.Enums;
using Matriks.Lean.Algotrader.AlgoBase;
using Matriks.Lean.Algotrader.Models;
using Matriks.Lean.Algotrader.Trading;
using Matriks.Symbols;
using Matriks.Trader.Core;
using Matriks.Trader.Core.Fields;
using Matriks.Trader.Core.TraderModels;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System.Net.Http;
using System.Net.Http.Headers;

namespace Matriks.Lean.Algotrader
{
    /// <summary>
    /// TradeAI Gateway — Matriks IQ Algo (Phase 2: veri + emir kapısı).
    ///
    /// Full-inversion mimarisinin Matriks tarafı: server beyin, bu algo
    /// sadece kapı. 127.0.0.1'de HTTP dinler.
    ///
    /// Endpoints:
    ///   GET  /ping                    — auth'suz canlılık kontrolü
    ///   GET  /health                  — Matriks/veri/pozisyon durumu
    ///   GET  /snapshot?symbol=THYAO   — OHLCV + derinlik + teknik feature bloğu
    ///   GET  /positions               — pozisyon anlık görüntüsü
    ///   POST /order                   — LIMIT emir (tüm güvenlik kilitleriyle)
    ///
    /// Emir güvenlik kilitleri (server ne isterse istesin aşılamaz):
    ///   - Sadece LIMIT — endpoint MARKET kavramını hiç tanımaz
    ///   - MaxQtyPerOrder / MaxOrderValueTl üst sınırları
    ///   - MaxOrdersPerDay / MaxOrdersPerSymbolPerDay günlük tavanları
    ///   - SELL: kilitli uzun vade lotlar düşüldükten sonraki pozisyonu aşamaz
    ///   - Duplicate requestId reddi
    ///   - v2 dispatch kapısı (CheckDispatchGates): systemMode=AUTO_TRADE +
    ///     accountType tespiti (DEMO serbest) + REAL arming. Eski PAPER/MANUAL/
    ///     DEMO_LIVE/REAL_LIVE mod kapıları kaldırıldı.
    ///
    /// Emir sonuçları: OnOrderUpdate → server'ın /api/order-result endpoint'ine
    /// raporlanır (ServerBaseUrl + ServerApiToken parametreleri).
    /// Senkron redler ise /order yanıtında döner; server kendisi loglar.
    ///
    /// Güvenlik: listener SADECE IPAddress.Loopback'e bağlanır; dışarıya
    /// port açılmaz. Tüm endpoint'ler (/ping hariç) bearer token ister.
    /// </summary>
    public class TradeAiGateway : MatriksAlgo
    {
        // ── Parameters ──────────────────────────────────────────────

        [Parameter(8787)]
        public int Port;

        [Parameter("BURAYA_GATEWAY_TOKEN")]
        public string ApiToken;

        // ── Order path parameters (Phase 2) ─────────────────────────
        // Emir sonuçlarının raporlandığı FastAPI server (aynı makine).
        [Parameter("http://127.0.0.1:8000")]
        public string ServerBaseUrl;

        [Parameter("BURAYA_SERVER_TOKEN")]
        public string ServerApiToken;

        // News ayarları BİLEREK [Parameter] DEĞİL: haber tam metnini Python
        // tarafı (news_service.py, Google News RSS) sağlıyor; Matriks'in kendi
        // haber aboneliği yalnızca ikincil/pasif sembol yakalaması için var.
        // [Parameter("")] boş-string alanları algo panelinde zorunlu görünüp
        // başlatmayı engelliyordu — normal alan yapıldı, panelde görünmezler.
        // Gerekirse ileride tekrar [Parameter] yapılıp elle doldurulabilir.
        private string NewsKeywordsCsv = "";
        private string NewsSymbolKeywordRulesCsv = "";
        private bool NewsFiltersOnlyInHeaders = true;
        private bool NewsFiltersExactMatch = false;

        // Server config gelene kadar bütün emir kapıları fail-closed.
        private bool EnableDemoOrders;
        private bool EnableRealOrders;
        private bool RealLiveModeAllowed;
        private bool RealLiveArmed;
        private bool TradingKillSwitchActive;
        private bool ForceSafeMode;
        private string[] BuyAllowedSymbols = new string[0];
        private string[] SellExitAllowedSymbols = new string[0];
        private string[] DeclineSymbols = new string[0];
        private string MarketIndexSymbol = "XU100";
        private Dictionary<string, string> InstrumentTypes = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        private bool MarketDataDiagnosticsEnabled;
        private decimal MarketDataDiagnosticSampleRatePct = 10m;
        private int MarketDataWarningRateLimitSeconds = 60;
        private bool RequireDemoAccount = true;
        private bool DemoAccountConfirmed;
        private decimal MaxOrderValueTl;
        private decimal MaxQtyPerOrder;
        private int MaxOrdersPerDay;
        private int MaxOrdersPerSymbolPerDay;
        private string OrderTimeInForce = "Day";
        private string RuntimeMode = "PAPER";
        private string ActiveProfileCode = "UNAVAILABLE";
        private DateTime _lastConfigFetchUtc = DateTime.MinValue;
        private string _lastAppliedConfigSignature = string.Empty;
        private string _configVersion = "UNAVAILABLE";
        private GatewayConfigSnapshot _activeConfig = GatewayConfigSnapshot.SafeDefault();

        // ── v2 kontrat + mod/arming durumu (Faz 3) ───────────────────
        // ExpectedContractVersion: bu gateway'in konuştuğu config kontratı.
        // Server farklı bir contractVersion gönderirse (veya hiç göndermezse)
        // emir yolu fail-closed kapanır — iki taraf ancak atomik deploy ile
        // birlikte yükseltilir.
        private const int ExpectedContractVersion = 2;
        private int _serverContractVersion; // 0 = config'te alan yok (legacy)
        // SystemMode: OBSERVE_ONLY | AUTO_TRADE. Bilinmeyen değer fail-closed
        // olarak OBSERVE_ONLY'ye normalize edilir.
        private string SystemMode = "OBSERVE_ONLY";
        private bool RealAccountArmed;
        // Server'ın arm ettiği hesabın sha256 referansı — gateway kendi
        // hesapladığı accountRef ile BİREBİR aynı formatta karşılaştırır
        // (yeniden hash yok).
        private string ArmedAccountRef = string.Empty;
        private string _lastVerifiedAccountRef = string.Empty;
        private string _lastVerifiedSessionRef = string.Empty;
        private string _lastVerifiedAccountType = "UNKNOWN"; // DEMO|REAL|UNKNOWN
        private bool _lastAccountChanged;

        private SymbolPeriod IndicatorPeriod = SymbolPeriod.Min5;

        // ── Symbols ──────────────────────────────────────────────────

        private string[] AllowedSymbols = new string[0];
        private Dictionary<string, decimal> LockedLongTermQty = new Dictionary<string, decimal>();
        private Dictionary<string, decimal> BotOwnedQty = new Dictionary<string, decimal>();

        // ── Constants ────────────────────────────────────────────────

        private const int MaxCloseHistory = 240;
        private const int MaxHttpHeaderBytes = 16384;
        private const int MaxHttpBodyBytes = 1048576;
        private const int ConfigStaleSeconds = 180;
        private const int AccountVerificationMaxAgeSeconds = 5;
        private const int PositionSyncIntervalSeconds = 45;
        private const int MaxPositionSyncAgeSeconds = 90;
        private const int MaxQuoteAgeSecondsForOrder = 15;
        private const int MaxDepthAgeSecondsForOrder = 10;
        private const string IndexDepthSkipReason = "INDEX_SYMBOL_NOT_APPLICABLE";
        private static readonly TimeSpan IdempotencyTtl = TimeSpan.FromHours(24);

        // ── HTTP server state ────────────────────────────────────────

        private TcpListener _listener;
        private CancellationTokenSource _cts;
        private Task _serverTask;
        private DateTime _startedAt;
        private int _requestCount;

        // ── Market data state (TradeAiAgenticBot'tan birebir) ────────

        private ConcurrentDictionary<string, decimal> _accountNetQtyBySymbol = new ConcurrentDictionary<string, decimal>();
        private ConcurrentDictionary<string, decimal> _accountAvailableQtyBySymbol = new ConcurrentDictionary<string, decimal>();
        // History keys are symbol|actual-period. A symbol can be subscribed at
        // multiple periods without merging bars from different series.
        private readonly ConcurrentDictionary<string, List<decimal>> _closeHistoryBySymbol = new ConcurrentDictionary<string, List<decimal>>();
        private readonly ConcurrentDictionary<string, List<OhlcvBarPoint>> _ohlcvHistoryBySeries = new ConcurrentDictionary<string, List<OhlcvBarPoint>>();
        private readonly ConcurrentDictionary<string, string> _lastCloseBarKeyBySymbol = new ConcurrentDictionary<string, string>();
        private readonly ConcurrentDictionary<string, int> _lastBarIndexBySeries = new ConcurrentDictionary<string, int>();
        private readonly ConcurrentDictionary<string, DateTime> _lastTradeUtcBySymbol = new ConcurrentDictionary<string, DateTime>();
        private readonly ConcurrentDictionary<string, MarketQuoteSnapshot> _lastValidQuoteBySymbol = new ConcurrentDictionary<string, MarketQuoteSnapshot>();
        private readonly ConcurrentDictionary<string, OhlcvSnapshot> _lastOhlcvBySymbol = new ConcurrentDictionary<string, OhlcvSnapshot>();
        private readonly ConcurrentDictionary<string, DateTime> _marketDataWarningUtcByKey = new ConcurrentDictionary<string, DateTime>();
        private readonly ConcurrentDictionary<string, DateTime> _marketDataDiagnosticUtcBySymbol = new ConcurrentDictionary<string, DateTime>();
        private readonly ConcurrentDictionary<string, object> _volumeIndicatorBySymbol = new ConcurrentDictionary<string, object>();
        private readonly ConcurrentDictionary<string, object> _volumeTlIndicatorBySymbol = new ConcurrentDictionary<string, object>();
        private readonly ConcurrentDictionary<string, object> _atrIndicatorBySymbol = new ConcurrentDictionary<string, object>();
        private readonly ConcurrentDictionary<string, object> _mostIndicatorBySymbol = new ConcurrentDictionary<string, object>();
        private readonly ConcurrentDictionary<string, object> _adxIndicatorBySymbol = new ConcurrentDictionary<string, object>();
        private readonly ConcurrentDictionary<string, PositionMarketSnapshot> _positionMarketBySymbol = new ConcurrentDictionary<string, PositionMarketSnapshot>();
        private readonly ConcurrentDictionary<string, decimal> _maxBid1SizeBySymbol = new ConcurrentDictionary<string, decimal>();
        // Günlük referans fiyat (gün içinde görülen ilk geçerli last) — /movers
        // endpoint'inin değişim yüzdesi bu referansa göre hesaplanır.
        private readonly ConcurrentDictionary<string, decimal> _dailyRefPriceBySymbol = new ConcurrentDictionary<string, decimal>();
        private readonly ConcurrentDictionary<string, RSI> _rsiBySymbol = new ConcurrentDictionary<string, RSI>();
        private readonly ConcurrentDictionary<string, MOV> _ema20BySymbol = new ConcurrentDictionary<string, MOV>();
        private readonly ConcurrentDictionary<string, MOV> _ema50BySymbol = new ConcurrentDictionary<string, MOV>();
        private readonly ConcurrentDictionary<string, MACD> _macdBySymbol = new ConcurrentDictionary<string, MACD>();
        private readonly ConcurrentQueue<NewsSnapshot> _recentNews = new ConcurrentQueue<NewsSnapshot>();

        private readonly object _closeLock = new object();
        private readonly object _dailyCounterLock = new object();
        private readonly object _newsSubscriptionLock = new object();

        // ── Order path state (Phase 2, TradeAiAgenticBot'tan birebir) ─

        private HttpClient _http;
        private readonly JsonSerializerSettings _jsonSettings = new JsonSerializerSettings
        {
            NullValueHandling = NullValueHandling.Ignore
        };
        private readonly ConcurrentDictionary<string, IdempotencyEntry> _sentRequestIds = new ConcurrentDictionary<string, IdempotencyEntry>();
        private readonly ConcurrentDictionary<string, int> _dailyTradeCountBySymbol = new ConcurrentDictionary<string, int>();
        private readonly ConcurrentDictionary<string, PendingOrderContext> _pendingOrdersByRequestId = new ConcurrentDictionary<string, PendingOrderContext>();
        private readonly ConcurrentDictionary<string, PendingOrderContext> _pendingOrdersBySymbolSide = new ConcurrentDictionary<string, PendingOrderContext>();
        private readonly ConcurrentDictionary<string, PendingOrderContext> _pendingOrdersByOrderId = new ConcurrentDictionary<string, PendingOrderContext>();
        private readonly ConcurrentDictionary<string, GatewayOrderSnapshot> _recentOrderStatesByOrderId = new ConcurrentDictionary<string, GatewayOrderSnapshot>();

        private DateTime _dailyCounterDate = DateTime.Today;
        private bool _realPositionsLoadedFromSnapshot;
        private bool? _autoOrderEnabled;
        private bool? _testAutoOrderEnabled;
        private bool _demoAccountVerified;
        private DateTime _lastAccountVerificationUtc = DateTime.MinValue;
        private string _lastVerifiedAccountId = string.Empty;
        private bool _subscriptionsInitialized;
        private readonly object _symbolSubscriptionLock = new object();
        private readonly SemaphoreSlim _configFetchLock = new SemaphoreSlim(1, 1);
        private readonly SemaphoreSlim _orderGate = new SemaphoreSlim(1, 1);
        private readonly ConcurrentQueue<OrderResultEnvelope> _orderResultQueue = new ConcurrentQueue<OrderResultEnvelope>();
        private readonly SemaphoreSlim _orderResultSignal = new SemaphoreSlim(0);
        private readonly ConcurrentDictionary<string, string> _lastReportedStatusByKey = new ConcurrentDictionary<string, string>();
        private Task _orderResultWorker;
        private DateTime _lastPositionSyncUtc = DateTime.MinValue;
        private int _positionSnapshotGeneration;
        private bool _positionSnapshotCompleteFlag;
        private bool _positionSnapshotNonEmpty;
        private string _positionSnapshotConfidence = "NONE";
        private readonly ConcurrentDictionary<string, DateTime> _lastPositionEventUtcBySymbol = new ConcurrentDictionary<string, DateTime>();
        private DateTime _lastNewsReceivedUtc = DateTime.MinValue;
        private readonly List<string> _newsTrackedSymbols = new List<string>();
        private readonly List<NewsKeywordSubscription> _newsKeywordSubscriptions = new List<NewsKeywordSubscription>();
        private readonly List<NewsSymbolKeywordSubscription> _newsSymbolKeywordSubscriptions = new List<NewsSymbolKeywordSubscription>();

        // ── Matriks lifecycle ───────────────────────────────────────

        public override void OnInit()
        {
            _startedAt = DateTime.Now;

            _http = new HttpClient
            {
                BaseAddress = new Uri(ServerBaseUrl.TrimEnd('/') + "/"),
                Timeout = TimeSpan.FromSeconds(15)
            };
            _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", ServerApiToken);

            FetchAndApplyServerConfigAsync().GetAwaiter().GetResult();

            foreach (string symbol in AllowedSymbols)
            {
                string normalized = NormalizeSymbol(symbol);
                AddSymbol(normalized, SymbolPeriod.Min);
                if (IndicatorPeriod != SymbolPeriod.Min)
                {
                    AddSymbol(normalized, IndicatorPeriod);
                }
                AddSymbolMarketData(normalized);
                if (IsEquitySymbol(normalized))
                    AddSymbolMarketDepth(normalized);
                RegisterNewsSubscriptionsForSymbol(normalized);
                InitializeIndicators(normalized);

                _accountNetQtyBySymbol[normalized] = 0m;
                _accountAvailableQtyBySymbol[normalized] = 0m;
                _closeHistoryBySymbol[BuildSeriesKey(normalized, IndicatorPeriod.ToString())] = new List<decimal>();
            }
            _subscriptionsInitialized = true;
            RegisterGlobalNewsKeywordSubscriptions();

            SetTimerInterval(60);
            LogTradeUserInfo();
            LoadRealPositionsSnapshot();

            _cts = new CancellationTokenSource();
            _orderResultWorker = Task.Run(() => ProcessOrderResultQueueAsync(_cts.Token));
            try
            {
                _listener = new TcpListener(IPAddress.Loopback, Port);
                _listener.Start();
                _serverTask = Task.Run(() => AcceptLoopAsync(_cts.Token));
                SafeDebug("Gateway listener started url=http://127.0.0.1:" + Port + "/"
                    + " symbols=" + string.Join(",", AllowedSymbols)
                    + " indicatorPeriod=" + IndicatorPeriod
                    + " enableDemoOrders=" + EnableDemoOrders
                    + " enableRealOrders=" + EnableRealOrders
                    + " demoConfirmed=" + DemoAccountConfirmed
                    + " maxOrderValueTl=" + MaxOrderValueTl
                    + " maxQtyPerOrder=" + MaxQtyPerOrder
                    + " server=" + ServerBaseUrl);
            }
            catch (Exception ex)
            {
                SafeDebug("Gateway listener failed: " + ex.Message);
            }
        }

        public override void OnDataUpdate(BarDataEventArgs barData)
        {
            ResetDailyCachesIfNeeded();
            string eventSymbol = ResolveBarEventSymbol(barData.SymbolId);
            if (!string.IsNullOrWhiteSpace(eventSymbol))
            {
                DateTime lastTickTime = barData.LastTickTime;
                if (lastTickTime != DateTime.MinValue)
                    _lastTradeUtcBySymbol[eventSymbol] = lastTickTime.ToUniversalTime();
            }
            UpdateOhlcvSnapshotFromBarData(barData);
        }

        public override void OnTimer()
        {
            ResetDailyCachesIfNeeded();
            if ((DateTime.UtcNow - _lastConfigFetchUtc).TotalSeconds >= 60)
                _ = FetchAndApplyServerConfigAsync();
            CleanupIdempotencyCache();
            if ((DateTime.UtcNow - _lastPositionSyncUtc).TotalSeconds >= PositionSyncIntervalSeconds)
            {
                LoadRealPositionsSnapshot();
            }
        }

        public override void OnRealPositionUpdate(AlgoTraderPosition position)
        {
            UpdatePositionCache(position, "OnRealPositionUpdate");
            LoadRealPositionsSnapshot();
        }

        public override void OnNewsReceived(AlgoNewsModel newsModel)
        {
            if (newsModel == null)
                return;
            _lastNewsReceivedUtc = DateTime.UtcNow;

            _recentNews.Enqueue(new NewsSnapshot
            {
                NewsId = newsModel.NewsId,
                Header = newsModel.Header,
                DateTime = newsModel.DateTime,
                Categories = newsModel.Categories ?? new List<string>(),
                Symbols = newsModel.Symbols ?? new List<string>(),
                Sources = newsModel.Source ?? new List<string>(),
                FilterType = newsModel.FilterType,
                MatchedFilters = newsModel.MatchedFilters ?? new List<string>(),
                HasAttachments = newsModel.HasAttachments,
                HasDetail = newsModel.HasDetail,
                DailyNewsNo = newsModel.DailyNewsNo
            });

            while (_recentNews.Count > 500)
                _recentNews.TryDequeue(out _);
        }

        /// <summary>
        /// Borsadan gelen emir durumu güncellemeleri (TradeAiAgenticBot'tan
        /// birebir) — sonuç server'ın /api/order-result endpoint'ine raporlanır.
        /// </summary>
        public override void OnOrderUpdate(IOrder order)
        {
            if (order == null)
            {
                SafeDebug("OnOrderUpdate ignored: order is null");
                return;
            }

            string orderId = order.OrderID;
            string symbol = NormalizeSymbol(order.Symbol);
            decimal orderQty = order.OrderQty;
            decimal filledQty = order.FilledQty;
            decimal avgPx = order.AvgPx;
            string status = NormalizeOrderStatus(order.OrdStatus.Obj);
            string side = NormalizeOrderSide(order.Side.ToString());

            SafeDebug("OnOrderUpdate status=" + status
                + " orderId=" + orderId
                + " symbol=" + symbol
                + " orderQty=" + orderQty
                + " filledQty=" + filledQty
                + " avgPx=" + avgPx);

            PendingOrderContext? resolvedContext = ResolvePendingOrderContext(orderId, symbol, side);
            PendingOrderContext context;
            if (resolvedContext.HasValue)
            {
                context = resolvedContext.Value;
            }
            else
            {
                context = new PendingOrderContext
                {
                    RequestId = "MATRIKS-" + (string.IsNullOrWhiteSpace(orderId) ? BuildFallbackRequestId(symbol) : orderId),
                    Symbol = symbol,
                    Action = side,
                    Qty = orderQty > 0 ? orderQty : filledQty,
                    Price = avgPx > 0 ? avgPx : order.Price
                };
            }

            if (!string.IsNullOrWhiteSpace(orderId))
            {
                _pendingOrdersByOrderId[orderId] = context;
            }

            decimal lastFillQty = 0m;
            GatewayOrderSnapshot priorSnapshot;
            if (!string.IsNullOrWhiteSpace(orderId)
                && _recentOrderStatesByOrderId.TryGetValue(orderId, out priorSnapshot))
                lastFillQty = Math.Max(0m, filledQty - priorSnapshot.FilledQty);
            decimal reportPrice = avgPx > 0 ? avgPx : (order.Price > 0 ? order.Price : context.Price);
            string message = "Matriks order update status=" + status
                + " orderQty=" + orderQty
                + " filledQty=" + filledQty
                + " avgPx=" + avgPx;

            if (!string.IsNullOrWhiteSpace(orderId))
            {
                _recentOrderStatesByOrderId[orderId] = new GatewayOrderSnapshot
                {
                    OrderId = orderId,
                    RequestId = context.RequestId,
                    Symbol = symbol,
                    Side = context.Action,
                    Status = status,
                    Qty = orderQty > 0 ? orderQty : context.Qty,
                    FilledQty = filledQty,
                    Price = order.Price > 0 ? order.Price : context.Price,
                    AvgPrice = avgPx,
                    UpdatedAt = DateTime.Now
                };
            }

            EnqueueOrderResult(new OrderResultEnvelope
            {
                Context = context,
                Status = status,
                MatriksMessage = message,
                OrderId = orderId,
                OrderQty = orderQty > 0 ? orderQty : context.Qty,
                FilledQty = filledQty,
                LastFillQty = lastFillQty,
                AvgPrice = avgPx,
                LimitPrice = order.Price > 0 ? order.Price : context.Price,
                Price = reportPrice
            });

            if (IsFinalOrderStatus(status))
            {
                if (!string.IsNullOrWhiteSpace(orderId))
                {
                    _pendingOrdersByOrderId.TryRemove(orderId, out _);
                }
                PendingOrderContext removed;
                _pendingOrdersByRequestId.TryRemove(context.RequestId, out removed);
                string activeKey = BuildSymbolSideKey(symbol, context.Action);
                PendingOrderContext active;
                if (_pendingOrdersBySymbolSide.TryGetValue(activeKey, out active)
                    && active.RequestId == context.RequestId)
                    _pendingOrdersBySymbolSide.TryRemove(activeKey, out removed);
            }
        }

        public override void OnStopped()
        {
            try
            {
                if (_cts != null)
                {
                    _cts.Cancel();
                }
                if (_listener != null)
                {
                    _listener.Stop();
                }
                try { _orderResultSignal.Release(); }
                catch (ObjectDisposedException) { }
                if (_orderResultWorker != null)
                {
                    try
                    {
                        if (!_orderResultWorker.Wait(TimeSpan.FromSeconds(5)))
                            SafeDebug("Order result queue shutdown timed out; remaining events will be retried by backend reconciliation.");
                    }
                    catch (Exception ex) { SafeDebug("Order result queue shutdown error: " + ex.Message); }
                }
                if (_http != null)
                {
                    try { _http.Dispose(); }
                    catch (Exception ex) { SafeDebug("Http dispose error: " + ex.Message); }
                }
                SafeDebug("Gateway stopped.");
            }
            catch (Exception ex)
            {
                SafeDebug("Gateway stop error: " + ex.Message);
            }
        }

        // ── HTTP server (TradeAiHttpApiTest'ten birebir iskelet) ─────

        private async Task AcceptLoopAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                TcpClient client = null;
                try
                {
                    client = await _listener.AcceptTcpClientAsync();
                    _ = Task.Run(() => HandleClientAsync(client, token));
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (Exception ex)
                {
                    if (!token.IsCancellationRequested)
                    {
                        SafeDebug("HTTP accept error: " + ex.Message);
                    }
                    try
                    {
                        if (client != null)
                        {
                            client.Close();
                        }
                    }
                    catch
                    {
                    }
                }
            }
        }

        private async Task HandleClientAsync(TcpClient client, CancellationToken token)
        {
            using (client)
            using (NetworkStream stream = client.GetStream())
            {
                stream.ReadTimeout = 5000;
                stream.WriteTimeout = 5000;

                HttpRequest request = await ReadRequestAsync(stream, token);
                if (request == null)
                {
                    await WriteJsonAsync(stream, 400, new { ok = false, error = "bad request" });
                    return;
                }
                if (!string.IsNullOrWhiteSpace(request.ParseError))
                {
                    await WriteJsonAsync(stream, 400, new { ok = false, error = request.ParseError });
                    return;
                }

                Interlocked.Increment(ref _requestCount);

                try
                {
                    await RouteRequestAsync(stream, request);
                }
                catch (Exception ex)
                {
                    SafeDebug("HTTP handler error path=" + request.Path + " error=" + ex.Message);
                    try
                    {
                        await WriteJsonAsync(stream, 500, new { ok = false, error = "internal error" });
                    }
                    catch
                    {
                    }
                }
            }
        }

        private async Task RouteRequestAsync(NetworkStream stream, HttpRequest request)
        {
            if (request.Method == "GET" && request.Path == "/ping")
            {
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    message = "pong",
                    server = "TradeAiGateway",
                    now = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz")
                });
                return;
            }

            if (!IsAuthorized(request))
            {
                await WriteJsonAsync(stream, 401, new { ok = false, error = "unauthorized" });
                return;
            }

            if (request.Method == "GET" && request.Path == "/health")
            {
                await HandleHealthAsync(stream);
                return;
            }

            if (request.Method == "GET" && request.Path == "/snapshot")
            {
                await HandleSnapshotAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/positions")
            {
                await HandlePositionsAsync(stream);
                return;
            }

            if (request.Method == "GET" && request.Path == "/orders/active")
            {
                await HandleActiveOrdersAsync(stream);
                return;
            }

            if (request.Method == "GET" && request.Path == "/capabilities")
            {
                await HandleCapabilitiesAsync(stream);
                return;
            }

            if (request.Method == "GET" && request.Path == "/depth")
            {
                await HandleDepthAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/indicators")
            {
                await HandleIndicatorsAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/news")
            {
                await HandleNewsAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/news/details")
            {
                await HandleNewsDetailsAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/kap")
            {
                await HandleKapAsync(stream, request, false);
                return;
            }

            if (request.Method == "GET" && request.Path == "/kap/risk")
            {
                await HandleKapAsync(stream, request, true);
                return;
            }

            if (request.Method == "GET" && request.Path == "/institutions")
            {
                await HandleInstitutionsAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/mkk")
            {
                await HandleMkkAsync(stream);
                return;
            }

            if (request.Method == "GET" && request.Path == "/movers")
            {
                await HandleMoversAsync(stream, request);
                return;
            }

            // ── Genişletilmiş read-only veri yüzeyi (data surface) ──────────
            // Matriks AlgoTrader'ın belgelenmiş veri-döndüren metodlarının
            // her biri için ince bir HTTP sarmalayıcı. Hepsi read-only,
            // hepsi fail-soft: metod patlarsa {ok:true, available:false} döner.
            if (request.Method == "GET" && request.Path == "/marketdata")
            {
                await HandleMarketDataFieldAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/marketdata/all")
            {
                await HandleMarketDataAllAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/symbol")
            {
                await HandleSymbolInfoAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/session")
            {
                await HandleSessionTimesAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/pricestep")
            {
                await HandlePriceStepAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/bars")
            {
                await HandleBarsAsync(stream, request);
                return;
            }

            if (request.Method == "GET" && request.Path == "/account")
            {
                await HandleAccountAsync(stream);
                return;
            }

            if (request.Method == "GET" && request.Path == "/realpositions")
            {
                await HandleRealPositionsAsync(stream);
                return;
            }

            if (request.Method == "GET" && request.Path == "/overall")
            {
                await HandleOverallAsync(stream);
                return;
            }

            if (request.Method == "GET" && request.Path == "/capabilities/methods")
            {
                await HandleMethodCatalogAsync(stream);
                return;
            }

            if (request.Method == "GET" && request.Path == "/methods/search")
            {
                await HandleMethodSearchAsync(stream, request);
                return;
            }

            if (request.Method == "POST" && request.Path == "/config/reload")
            {
                SafeDebug("Server config reload requested");
                bool loaded = await FetchAndApplyServerConfigAsync(true);
                SafeDebug("Server config reload completed ok=" + loaded
                    + " profile=" + ActiveProfileCode
                    + " mode=" + RuntimeMode
                    + " symbols=" + string.Join(",", AllowedSymbols));
                await WriteJsonAsync(stream, loaded ? 200 : 503, new
                {
                    ok = loaded,
                    profileCode = ActiveProfileCode,
                    symbols = AllowedSymbols
                });
                return;
            }

            if (request.Method == "POST" && request.Path == "/order")
            {
                await HandleOrderAsync(stream, request);
                return;
            }

            if (request.Method == "POST" && request.Path == "/order/cancel")
            {
                await HandleCancelOrderAsync(stream, request);
                return;
            }

            await WriteJsonAsync(stream, 404, new { ok = false, error = "not found", path = request.Path });
        }

        // ── Endpoint handlers ────────────────────────────────────────

        private async Task<bool> FetchAndApplyServerConfigAsync(bool waitForLock = false)
        {
            if (waitForLock)
                await _configFetchLock.WaitAsync();
            else if (!await _configFetchLock.WaitAsync(0))
                return false;
            try
            {
                using (var result = await _http.GetAsync("api/gateway/config"))
                {
                    string body = await result.Content.ReadAsStringAsync();
                    if (!result.IsSuccessStatusCode)
                    {
                        SafeDebug("Server config fetch failed HTTP " + (int)result.StatusCode);
                        return false;
                    }

                    JObject cfg = JObject.Parse(body);
                    if (!(cfg.Value<bool?>("ok") ?? false))
                        return false;

                    string[] symbols = (cfg["symbols"] as JArray ?? new JArray())
                        .Select(x => NormalizeSymbol(Convert.ToString(x)))
                        .Where(x => !string.IsNullOrWhiteSpace(x))
                        .Distinct()
                        .ToArray();
                    SymbolPeriod indicatorPeriod = ParseSymbolPeriod(cfg.Value<string>("indicatorPeriod"));
                    Dictionary<string, decimal> lockedLongTermQty = cfg["lockedLongTermQty"] != null
                        ? cfg["lockedLongTermQty"].ToObject<Dictionary<string, decimal>>()
                        : new Dictionary<string, decimal>();
                    Dictionary<string, decimal> botOwnedQty = cfg["botOwnedQty"] != null
                        ? cfg["botOwnedQty"].ToObject<Dictionary<string, decimal>>()
                        : new Dictionary<string, decimal>();
                    if (lockedLongTermQty.Any(x => x.Value < 0m))
                    { SafeDebug("Server config rejected: negative locked quantity"); return false; }
                    // v2: eski mode/enableDemoOrders/enableRealOrders/realLive*
                    // alanları kaldırıldı. Tek kill switch: killSwitchActive
                    // (eksikse fail-closed true). runtimeMode yalnızca kayıt/log
                    // için systemMode'a eşitlenir.
                    bool killSwitchActive = cfg.Value<bool?>("killSwitchActive") ?? true;
                    bool enableDemoOrders = false;
                    bool enableRealOrders = false;
                    bool realLiveModeAllowed = false;
                    bool realLiveArmed = false;
                    bool tradingKillSwitchActive = killSwitchActive;
                    bool forceSafeMode = false;
                    string[] buyAllowedSymbols = (cfg["buyAllowedSymbols"] as JArray ?? new JArray()).Select(x => NormalizeSymbol(Convert.ToString(x))).ToArray();
                    string[] sellExitAllowedSymbols = (cfg["sellExitAllowedSymbols"] as JArray ?? new JArray()).Select(x => NormalizeSymbol(Convert.ToString(x))).ToArray();
                    string[] declineSymbols = (cfg["declineSymbols"] as JArray ?? new JArray()).Select(x => NormalizeSymbol(Convert.ToString(x))).ToArray();
                    // v2: requireDemoAccount/demoAccountConfirmed kaldırıldı.
                    bool requireDemoAccount = false;
                    bool demoAccountConfirmed = false;
                    decimal maxOrderValueTl = cfg.Value<decimal?>("maxOrderValueTl") ?? 0m;
                    decimal maxQtyPerOrder = cfg.Value<decimal?>("maxQtyPerOrder") ?? 0m;
                    int maxOrdersPerDay = cfg.Value<int?>("maxOrdersPerDay") ?? 0;
                    int maxOrdersPerSymbolPerDay = cfg.Value<int?>("maxOrdersPerSymbolPerDay") ?? 0;
                    string orderTimeInForce = cfg.Value<string>("orderTimeInForce") ?? "Day";
                    string activeProfileCode = cfg.Value<string>("profileCode") ?? "UNKNOWN";
                    string configVersion = cfg.Value<string>("configHash") ?? "UNKNOWN";
                    // v2 kontrat alanları — eksikse fail-closed default'lar.
                    int contractVersion = cfg.Value<int?>("contractVersion") ?? 0;
                    string systemMode = NormalizeSystemMode(cfg.Value<string>("systemMode"));
                    bool realAccountArmed = cfg.Value<bool?>("realAccountArmed") ?? false;
                    string armedAccountRef = (cfg.Value<string>("armedAccountRef") ?? string.Empty).Trim();
                    string marketIndexSymbol = NormalizeSymbol(cfg.Value<string>("marketIndexSymbol"));
                    Dictionary<string, string> instrumentTypes = cfg["instrumentTypes"] != null
                        ? cfg["instrumentTypes"].ToObject<Dictionary<string, string>>()
                        : new Dictionary<string, string>();
                    bool marketDataDiagnosticsEnabled = cfg.Value<bool?>("marketDataDiagnosticsEnabled") ?? false;
                    decimal marketDataDiagnosticSampleRatePct = Math.Max(0m, Math.Min(100m,
                        cfg.Value<decimal?>("marketDataDiagnosticSampleRatePct") ?? 10m));
                    int marketDataWarningRateLimitSeconds = Math.Max(1, Math.Min(3600,
                        cfg.Value<int?>("marketDataWarningRateLimitSeconds") ?? 60));
                    Dictionary<string, int> dailyReservedCounts = cfg["dailyReservedOrderCountsBySymbol"] != null
                        ? cfg["dailyReservedOrderCountsBySymbol"].ToObject<Dictionary<string, int>>()
                        : new Dictionary<string, int>();
                    DateTime dailyCounterConfigDate;
                    bool hasDailyCounterDate = DateTime.TryParse(
                        cfg.Value<string>("dailyCounterDate"), out dailyCounterConfigDate);

                    bool liveMode = runtimeMode == "DEMO_LIVE" || runtimeMode == "REAL_LIVE";
                    if (liveMode && (symbols.Length == 0 || maxOrderValueTl <= 0m
                        || maxQtyPerOrder <= 0m || maxOrdersPerDay <= 0
                        || maxOrdersPerSymbolPerDay <= 0))
                    {
                        SafeDebug("Server config rejected: live mode requires allowed symbols and positive risk limits");
                        return false;
                    }

                    // Parsing above is deliberately completed before any live
                    // field is changed. Invalid live limits remain fail-closed.
                    MarketIndexSymbol = marketIndexSymbol;
                    InstrumentTypes = instrumentTypes
                        .Where(x => !string.IsNullOrWhiteSpace(x.Key))
                        .ToDictionary(
                            x => NormalizeSymbol(x.Key),
                            x => NormalizeInstrumentType(x.Value),
                            StringComparer.OrdinalIgnoreCase);
                    MarketDataDiagnosticsEnabled = marketDataDiagnosticsEnabled;
                    MarketDataDiagnosticSampleRatePct = marketDataDiagnosticSampleRatePct;
                    MarketDataWarningRateLimitSeconds = marketDataWarningRateLimitSeconds;
                    buyAllowedSymbols = buyAllowedSymbols.Where(x => !IsIndexSymbol(x)).ToArray();
                    sellExitAllowedSymbols = sellExitAllowedSymbols.Where(x => !IsIndexSymbol(x)).ToArray();
                    declineSymbols = declineSymbols.Where(x => !string.IsNullOrWhiteSpace(x)).ToArray();
                    if (_subscriptionsInitialized)
                    {
                        foreach (string symbol in symbols)
                            EnsurePortfolioSymbolSubscribed(symbol);
                    }
                    bool indicatorPeriodChanged = IndicatorPeriod != indicatorPeriod;
                    IndicatorPeriod = indicatorPeriod;
                    if (_subscriptionsInitialized && indicatorPeriodChanged)
                    {
                        foreach (string subscribedSymbol in symbols)
                        {
                            AddSymbol(subscribedSymbol, IndicatorPeriod);
                            InitializeIndicators(subscribedSymbol);
                        }
                        SafeDebug("Indicator/bar period changed; subscriptions refreshed period=" + IndicatorPeriod);
                    }
                    // Config reload must not remove market-data subscriptions
                    // discovered from the real account portfolio. They remain
                    // data-only unless explicitly present in BUY/SELL allow-lists.
                    string[] portfolioSymbols = _accountNetQtyBySymbol
                        .Where(x => x.Value != 0m)
                        .Select(x => NormalizeSymbol(x.Key))
                        .Concat(_accountAvailableQtyBySymbol
                            .Where(x => x.Value != 0m)
                            .Select(x => NormalizeSymbol(x.Key)))
                        .Where(x => !string.IsNullOrWhiteSpace(x))
                        .Distinct(StringComparer.OrdinalIgnoreCase)
                        .ToArray();
                    AllowedSymbols = symbols
                        .Concat(portfolioSymbols)
                        .Distinct(StringComparer.OrdinalIgnoreCase)
                        .ToArray();
                    LockedLongTermQty = lockedLongTermQty;
                    BotOwnedQty = botOwnedQty;
                    // v2: RuntimeMode yalnızca log/kayıt için systemMode'a eşit.
                    RuntimeMode = systemMode;
                    EnableDemoOrders = enableDemoOrders;
                    EnableRealOrders = enableRealOrders;
                    RealLiveModeAllowed = realLiveModeAllowed;
                    RealLiveArmed = realLiveArmed;
                    TradingKillSwitchActive = tradingKillSwitchActive;
                    ForceSafeMode = forceSafeMode;
                    BuyAllowedSymbols = buyAllowedSymbols;
                    SellExitAllowedSymbols = sellExitAllowedSymbols;
                    DeclineSymbols = declineSymbols;
                    if (hasDailyCounterDate && dailyCounterConfigDate.Date == DateTime.Today)
                    {
                        lock (_dailyCounterLock)
                        {
                            foreach (var item in dailyReservedCounts)
                            {
                                string counterSymbol = NormalizeSymbol(item.Key);
                                int restored = Math.Max(0, item.Value);
                                _dailyTradeCountBySymbol.AddOrUpdate(
                                    counterSymbol,
                                    restored,
                                    (_, existing) => Math.Max(existing, restored));
                            }
                        }
                    }
                    // Publish one immutable reference only after every parsed
                    // field was validated. Order handlers read this snapshot
                    // once, so config reloads cannot mix versions.
                    _activeConfig = new GatewayConfigSnapshot(systemMode, enableDemoOrders, enableRealOrders, realLiveModeAllowed, realLiveArmed, requireDemoAccount, demoAccountConfirmed, tradingKillSwitchActive, forceSafeMode, buyAllowedSymbols, sellExitAllowedSymbols, declineSymbols, configVersion, activeProfileCode);
                    bool accountVerificationPolicyChanged =
                        RequireDemoAccount != requireDemoAccount
                        || DemoAccountConfirmed != demoAccountConfirmed;
                    RequireDemoAccount = requireDemoAccount;
                    DemoAccountConfirmed = demoAccountConfirmed;
                    // A server-side confirmation/account policy change must
                    // never inherit a previous five-second verification.
                    if (accountVerificationPolicyChanged)
                    {
                        _demoAccountVerified = false;
                        _lastAccountVerificationUtc = DateTime.MinValue;
                    }
                    MaxOrderValueTl = maxOrderValueTl;
                    MaxQtyPerOrder = maxQtyPerOrder;
                    MaxOrdersPerDay = maxOrdersPerDay;
                    MaxOrdersPerSymbolPerDay = maxOrdersPerSymbolPerDay;
                    OrderTimeInForce = orderTimeInForce;
                    ActiveProfileCode = activeProfileCode;
                    _configVersion = configVersion;
                    _serverContractVersion = contractVersion;
                    SystemMode = systemMode;
                    RealAccountArmed = realAccountArmed;
                    ArmedAccountRef = armedAccountRef;

                    // Haber ayarları da server'dan gelir (algo panelinde değil).
                    // Boş bırakılırsa: keyword aboneliği yok, sembol bazlı pasif
                    // haber yakalama yine çalışır. Alan gelmezse eski değeri korur.
                    NewsKeywordsCsv = cfg.Value<string>("newsKeywordsCsv") ?? NewsKeywordsCsv;
                    NewsSymbolKeywordRulesCsv = cfg.Value<string>("newsSymbolKeywordRulesCsv") ?? NewsSymbolKeywordRulesCsv;
                    NewsFiltersOnlyInHeaders = cfg.Value<bool?>("newsFiltersOnlyInHeaders") ?? NewsFiltersOnlyInHeaders;
                    NewsFiltersExactMatch = cfg.Value<bool?>("newsFiltersExactMatch") ?? NewsFiltersExactMatch;
                    if (_subscriptionsInitialized)
                    {
                        try { RegisterGlobalNewsKeywordSubscriptions(); }
                        catch (Exception nex) { SafeDebug("News keyword re-subscribe failed: " + nex.Message); }
                    }

                    _lastConfigFetchUtc = DateTime.UtcNow;

                    string configSignature = string.Join("|", new[]
                    {
                        ActiveProfileCode,
                        RuntimeMode,
                        string.Join(",", AllowedSymbols.OrderBy(x => x, StringComparer.Ordinal)),
                        string.Join(",", BuyAllowedSymbols.OrderBy(x => x, StringComparer.Ordinal)),
                        string.Join(",", SellExitAllowedSymbols.OrderBy(x => x, StringComparer.Ordinal)),
                        string.Join(",", DeclineSymbols.OrderBy(x => x, StringComparer.Ordinal)),
                        EnableDemoOrders.ToString(),
                        EnableRealOrders.ToString(),
                        RealLiveModeAllowed.ToString(),
                        RealLiveArmed.ToString(),
                        RequireDemoAccount.ToString(),
                        DemoAccountConfirmed.ToString(),
                        MaxOrderValueTl.ToString(),
                        MaxQtyPerOrder.ToString(),
                        MaxOrdersPerDay.ToString(),
                        MaxOrdersPerSymbolPerDay.ToString(),
                        OrderTimeInForce,
                        IndicatorPeriod.ToString(),
                        MarketIndexSymbol,
                        MarketDataDiagnosticsEnabled.ToString(),
                        MarketDataDiagnosticSampleRatePct.ToString(),
                        MarketDataWarningRateLimitSeconds.ToString(),
                    });
                    if (!string.Equals(_lastAppliedConfigSignature, configSignature, StringComparison.Ordinal))
                    {
                        _lastAppliedConfigSignature = configSignature;
                        SafeDebug("Server config applied profile=" + ActiveProfileCode
                            + " mode=" + RuntimeMode
                            + " symbols=" + string.Join(",", AllowedSymbols)
                            + " enableDemoOrders=" + EnableDemoOrders
                            + " enableRealOrders=" + EnableRealOrders
                            + " realLiveModeAllowed=" + RealLiveModeAllowed
                            + " realLiveArmed=" + RealLiveArmed
                            + " maxOrderValueTl=" + MaxOrderValueTl
                            + " maxQtyPerOrder=" + MaxQtyPerOrder);
                    }
                    return true;
                }
            }
            catch (Exception ex)
            {
                SafeDebug("Server config fetch failed: " + ex.Message);
                return false;
            }
            finally
            {
                _configFetchLock.Release();
            }
        }

        private static SymbolPeriod ParseSymbolPeriod(string raw)
        {
            if (string.Equals(raw, "Min", StringComparison.OrdinalIgnoreCase)) return SymbolPeriod.Min;
            if (string.Equals(raw, "Min15", StringComparison.OrdinalIgnoreCase)) return SymbolPeriod.Min15;
            if (string.Equals(raw, "Min30", StringComparison.OrdinalIgnoreCase)) return SymbolPeriod.Min30;
            if (string.Equals(raw, "Hour", StringComparison.OrdinalIgnoreCase)) return SymbolPeriod.Min60;
            if (string.Equals(raw, "Min60", StringComparison.OrdinalIgnoreCase)) return SymbolPeriod.Min60;
            if (string.Equals(raw, "Day", StringComparison.OrdinalIgnoreCase)) return SymbolPeriod.Day;
            return SymbolPeriod.Min5;
        }

        private async Task HandleHealthAsync(NetworkStream stream)
        {
            // Readiness must be able to observe a fresh demo-account check even
            // before the first order attempt. This is the same compile-safe
            // GetTradeUser verification used immediately before /order.
            // v2: AUTO_TRADE modunda da hesap kimliği raporlanabilsin diye
            // aynı doğrulama ısıtılır (account watcher /health'ten okur).
            // v2: AUTO_TRADE'de hesap kimliğini ısıt (watcher /health'ten okur).
            if (SystemMode == "AUTO_TRADE")
                VerifyDemoAccountFresh();

            var quoteAgeSeconds = new Dictionary<string, double?>();
            var depthAgeSeconds = new Dictionary<string, double?>();
            foreach (string symbolRaw in AllowedSymbols)
            {
                string symbol = NormalizeSymbol(symbolRaw);
                if (_lastValidQuoteBySymbol.TryGetValue(symbol, out var quote))
                {
                    quoteAgeSeconds[symbol] = quote.LastTradeUtc == DateTime.MinValue
                        ? (double?)null
                        : Math.Round((DateTime.UtcNow - quote.LastTradeUtc).TotalSeconds, 1);
                    depthAgeSeconds[symbol] = null;
                }
                else
                {
                    quoteAgeSeconds[symbol] = null;
                    depthAgeSeconds[symbol] = null;
                }
            }

            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                server = "TradeAiGateway",
                phase = "order-enabled",
                startedAt = _startedAt.ToString("yyyy-MM-ddTHH:mm:sszzz"),
                requestCount = _requestCount,
                symbols = AllowedSymbols,
                subscriptionsInitialized = _subscriptionsInitialized,
                configLoaded = _lastConfigFetchUtc != DateTime.MinValue,
                configAgeSeconds = _lastConfigFetchUtc == DateTime.MinValue
                    ? (double?)null
                    : Math.Round((DateTime.UtcNow - _lastConfigFetchUtc).TotalSeconds, 1),
                configStale = IsConfigStale(),
                profileCode = ActiveProfileCode,
                configVersion = _configVersion,
                runtimeMode = RuntimeMode,
                // ── v2 kontrat + hesap kimliği raporlama (Faz 3) ──
                gatewayContractVersion = ExpectedContractVersion,
                serverContractVersion = _serverContractVersion,
                systemMode = SystemMode,
                realAccountArmed = RealAccountArmed,
                accountRef = string.IsNullOrEmpty(_lastVerifiedAccountRef) ? null : _lastVerifiedAccountRef,
                accountSessionRef = string.IsNullOrEmpty(_lastVerifiedSessionRef) ? null : _lastVerifiedSessionRef,
                accountIdMasked = string.IsNullOrEmpty(_lastVerifiedAccountId) ? null : MaskAccountId(_lastVerifiedAccountId),
                accountType = _lastVerifiedAccountType,
                accountVerifiedAgoSeconds = _lastAccountVerificationUtc == DateTime.MinValue
                    ? (double?)null
                    : Math.Round((DateTime.UtcNow - _lastAccountVerificationUtc).TotalSeconds, 1),
                positionsLoaded = _realPositionsLoadedFromSnapshot,
                lastPositionSyncUtc = _lastPositionSyncUtc == DateTime.MinValue ? null : _lastPositionSyncUtc.ToString("o"),
                positionSyncAgeSeconds = _lastPositionSyncUtc == DateTime.MinValue
                    ? (double?)null
                    : Math.Round((DateTime.UtcNow - _lastPositionSyncUtc).TotalSeconds, 1),
                lastNewsReceivedUtc = _lastNewsReceivedUtc == DateTime.MinValue ? null : _lastNewsReceivedUtc.ToString("o"),
                autoOrderEnabled = _autoOrderEnabled,
                testAutoOrderEnabled = _testAutoOrderEnabled,
                accountVerificationAgeSeconds = _lastAccountVerificationUtc == DateTime.MinValue ? (double?)null : Math.Round((DateTime.UtcNow - _lastAccountVerificationUtc).TotalSeconds, 1),
                quoteAgeSeconds = quoteAgeSeconds,
                depthAgeSeconds = depthAgeSeconds,
                callbackQueueDepth = _orderResultQueue.Count,
                callbackOutboxBacklog = _orderResultQueue.Count,
                orderLimits = new
                {
                    enableDemoOrders = EnableDemoOrders,
                    enableRealOrders = EnableRealOrders,
                    realLiveModeAllowed = RealLiveModeAllowed,
                    realLiveArmed = RealLiveArmed,
                    requireDemoAccount = RequireDemoAccount,
                    demoAccountConfirmed = DemoAccountConfirmed,
                    maxOrderValueTl = ToDouble(MaxOrderValueTl),
                    maxQtyPerOrder = ToDouble(MaxQtyPerOrder),
                    maxOrdersPerDay = MaxOrdersPerDay,
                    maxOrdersPerSymbolPerDay = MaxOrdersPerSymbolPerDay,
                    ordersSentToday = GetTotalDailyOrderCount()
                }
            });
        }

        private async Task HandleSnapshotAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            if (string.IsNullOrWhiteSpace(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "missing query parameter: symbol" });
                return;
            }

            if (!IsAllowedSymbol(symbol))
            {
                await WriteJsonAsync(stream, 400, new
                {
                    ok = false,
                    error = "symbol not in allowed list",
                    symbol = symbol,
                    allowedSymbols = AllowedSymbols
                });
                return;
            }

            string requestedTimeframe = request.GetQueryValue("timeframe");
            MarketDataPayload data = BuildMarketData(symbol, "OHLCV", requestedTimeframe);
            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                symbol = data.Symbol,
                dataType = data.DataType,
                timestamp = data.Timestamp,
                payload = data.Payload
            });
        }

        private async Task HandlePositionsAsync(NetworkStream stream)
        {
            var entries = new List<object>();
            var seen = new HashSet<string>();

            foreach (var kv in _accountAvailableQtyBySymbol)
            {
                seen.Add(kv.Key);
                decimal netQty;
                _accountNetQtyBySymbol.TryGetValue(kv.Key, out netQty);
                decimal locked = GetLockedLongTermQty(kv.Key);
                if (locked > netQty)
                    SafeDebug("LOCKED_QTY_ALARM symbol=" + kv.Key + " locked=" + locked + " accountNet=" + netQty);
                decimal botOwned = GetBotOwnedQty(kv.Key);
                decimal botSellable = Math.Max(0m, Math.Min(botOwned, kv.Value - locked));
                DateTime eventUtc;
                bool hasRealtimeEvent = _lastPositionEventUtcBySymbol.TryGetValue(kv.Key, out eventUtc);
                PositionMarketSnapshot marketPosition;
                bool hasMarketPosition = _positionMarketBySymbol.TryGetValue(kv.Key, out marketPosition);
                entries.Add(new
                {
                    symbol = kv.Key,
                    accountNetQty = ToDouble(netQty),
                    accountAvailableQty = ToDouble(kv.Value),
                    lockedLongTermQty = ToDouble(locked),
                    botOwnedQty = ToDouble(botOwned),
                    botSellableQty = ToDouble(botSellable),
                    sellableQty = ToDouble(botSellable),
                    botQty = ToDouble(botOwned), // deprecated compatibility alias
                    totalQty = ToDouble(netQty),
                    avgCost = hasMarketPosition && marketPosition.AvgCost > 0m ? (object)ToDouble(marketPosition.AvgCost) : null,
                    accountAvgCost = hasMarketPosition && marketPosition.AvgCost > 0m ? (object)ToDouble(marketPosition.AvgCost) : null,
                    openingAveragePrice = hasMarketPosition ? (object)ToDouble(marketPosition.OpeningAveragePrice) : null,
                    amount = hasMarketPosition ? (object)ToDouble(marketPosition.Amount) : null,
                    settlementPx = hasMarketPosition ? (object)ToDouble(marketPosition.SettlementPx) : null,
                    currency = hasMarketPosition ? marketPosition.Currency : null,
                    accountId = hasMarketPosition ? marketPosition.AccountId : null,
                    costSource = hasMarketPosition && marketPosition.AvgCost > 0m ? "MATRIX_ACCOUNT_AVG_COST" : "UNAVAILABLE",
                    positionUpdatedUtc = hasMarketPosition ? marketPosition.UpdatedAt.ToString("o") : null,
                    positionValueSource = hasMarketPosition ? "AlgoTraderPosition.Amount" : "UNAVAILABLE",
                    lastPriceSource = hasMarketPosition ? "AlgoTraderPosition.SettlementPx" : "UNAVAILABLE",
                    source = hasRealtimeEvent ? "REALTIME_EVENT" : "SNAPSHOT",
                    eventTimestamp = hasRealtimeEvent ? eventUtc.ToString("o") : null,
                    snapshotGeneration = _positionSnapshotGeneration,
                    receivedAt = _lastPositionSyncUtc == DateTime.MinValue ? null : _lastPositionSyncUtc.ToString("o")
                });
            }

            // Pozisyon cache'inde olmayan ama kilitli lot tanımlı semboller de görünsün.
            foreach (var kv in LockedLongTermQty)
            {
                if (seen.Contains(kv.Key))
                    continue;
                entries.Add(new
                {
                    symbol = kv.Key,
                    botQty = 0.0,
                    lockedLongTermQty = ToDouble(kv.Value),
                    accountNetQty = 0.0,
                    accountAvailableQty = 0.0,
                    sellableQty = 0.0,
                    totalQty = 0.0
                });
            }

            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                positionsLoaded = _realPositionsLoadedFromSnapshot,
                snapshotCompleteFlag = _positionSnapshotCompleteFlag,
                snapshotNonEmpty = _positionSnapshotNonEmpty,
                snapshotAgeSeconds = _lastPositionSyncUtc == DateTime.MinValue ? (double?)null : Math.Round((DateTime.UtcNow - _lastPositionSyncUtc).TotalSeconds, 1),
                snapshotGeneration = _positionSnapshotGeneration,
                confidence = _positionSnapshotConfidence,
                positions = entries
            });
        }

        // ── Order path (Phase 2 — kilitler TradeAiAgenticBot'tan) ───

        private async Task HandleActiveOrdersAsync(NetworkStream stream)
        {
            bool exchangeAvailable;
            string exchangeError;
            List<GatewayOrderSnapshot> orders = ReadRealOrdersSnapshot(out exchangeAvailable, out exchangeError);
            var byOrderId = orders
                .Where(x => !string.IsNullOrWhiteSpace(x.OrderId))
                .ToDictionary(x => x.OrderId, x => x, StringComparer.OrdinalIgnoreCase);
            foreach (GatewayOrderSnapshot cached in _recentOrderStatesByOrderId.Values)
            {
                if (!string.IsNullOrWhiteSpace(cached.OrderId))
                    byOrderId[cached.OrderId] = cached;
            }
            List<GatewayOrderSnapshot> merged = byOrderId.Values
                .OrderByDescending(x => x.UpdatedAt).ToList();
            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                available = exchangeAvailable || merged.Count > 0,
                exchangeAvailable = exchangeAvailable,
                error = exchangeError,
                orders = merged,
                activeOrderIds = merged.Where(x => !IsFinalOrderStatus(x.Status))
                    .Select(x => x.OrderId).Where(x => !string.IsNullOrWhiteSpace(x)).ToList()
            });
        }

        private async Task HandleCancelOrderAsync(NetworkStream stream, HttpRequest request)
        {
            CancelOrderRequest cancel;
            try
            {
                cancel = JsonConvert.DeserializeObject<CancelOrderRequest>(request.Body ?? "");
            }
            catch (Exception ex)
            {
                SafeDebug("Invalid order JSON: " + ex.Message);
                await WriteJsonAsync(stream, 400, new { ok = false, error = "invalid JSON body" });
                return;
            }
            string orderId = (cancel.OrderId ?? "").Trim();
            if (string.IsNullOrWhiteSpace(orderId))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "missing required field: orderId" });
                return;
            }
            try
            {
                SendCancelOrder(orderId);
                SafeDebug("Cancel requested orderId=" + orderId);
                await WriteJsonAsync(stream, 200, new { ok = true, accepted = true, status = "CANCEL_REQUESTED", orderId = orderId });
            }
            catch (Exception ex)
            {
                SafeDebug("Cancel failed orderId=" + orderId + " error=" + ex.Message);
                await WriteJsonAsync(stream, 200, new { ok = true, accepted = false, status = "CANCEL_FAILED", orderId = orderId, reason = ex.Message });
            }
        }

        private List<GatewayOrderSnapshot> ReadRealOrdersSnapshot(out bool available, out string error)
        {
            available = false;
            error = null;
            var result = new List<GatewayOrderSnapshot>();
            try
            {
                MethodInfo method = FindZeroArgumentMethod("GetRealOrders");
                if (method == null)
                {
                    error = "GetRealOrders is not available in this Matriks IQ build";
                    return result;
                }
                object raw = method.Invoke(this, null);
                IEnumerable entries = raw as IEnumerable;
                if (entries == null)
                {
                    error = "GetRealOrders returned a non-enumerable result";
                    return result;
                }
                available = true;
                foreach (object entry in entries)
                {
                    object order = ReadMember(entry, "Value") ?? entry;
                    GatewayOrderSnapshot snapshot = BuildOrderSnapshot(order);
                    if (snapshot != null)
                        result.Add(snapshot);
                }
            }
            catch (Exception ex)
            {
                error = ex.GetBaseException().Message;
            }
            return result;
        }

        private MethodInfo FindZeroArgumentMethod(string name)
        {
            Type type = GetType();
            while (type != null)
            {
                MethodInfo method = type.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                    .FirstOrDefault(x => x.Name == name && x.GetParameters().Length == 0);
                if (method != null)
                    return method;
                type = type.BaseType;
            }
            return null;
        }

        private GatewayOrderSnapshot BuildOrderSnapshot(object order)
        {
            if (order == null)
                return null;
            string orderId = Convert.ToString(ReadMember(order, "OrderID") ?? ReadMember(order, "OrderId"));
            object rawStatus = ReadMember(order, "OrdStatus") ?? ReadMember(order, "Status");
            object statusValue = ReadMember(rawStatus, "Obj") ?? rawStatus;
            PendingOrderContext context;
            _pendingOrdersByOrderId.TryGetValue(orderId ?? "", out context);
            return new GatewayOrderSnapshot
            {
                OrderId = orderId,
                RequestId = !string.IsNullOrWhiteSpace(context.RequestId) ? context.RequestId : Convert.ToString(ReadMember(order, "CliOrdID") ?? ReadMember(order, "ClOrdID")),
                Symbol = NormalizeSymbol(Convert.ToString(ReadMember(order, "Symbol"))),
                Side = NormalizeOrderSide(Convert.ToString(ReadMember(order, "Side"))),
                Status = NormalizeOrderStatus(statusValue),
                Qty = ToSafeDecimal(ReadMember(order, "OrderQty")),
                FilledQty = ToSafeDecimal(ReadMember(order, "FilledQty") ?? ReadMember(order, "CumQty")),
                Price = ToSafeDecimal(ReadMember(order, "Price")),
                AvgPrice = ToSafeDecimal(ReadMember(order, "AvgPx")),
                UpdatedAt = DateTime.Now
            };
        }

        private static object ReadMember(object source, string name)
        {
            if (source == null)
                return null;
            Type type = source.GetType();
            PropertyInfo property = type.GetProperties(BindingFlags.Instance | BindingFlags.Public)
                .FirstOrDefault(x => string.Equals(x.Name, name, StringComparison.OrdinalIgnoreCase));
            if (property != null)
                return property.GetValue(source, null);
            FieldInfo field = type.GetFields(BindingFlags.Instance | BindingFlags.Public)
                .FirstOrDefault(x => string.Equals(x.Name, name, StringComparison.OrdinalIgnoreCase));
            return field == null ? null : field.GetValue(source);
        }

        private static decimal ToSafeDecimal(object value)
        {
            try { return value == null ? 0m : Convert.ToDecimal(value); }
            catch { return 0m; }
        }

        private async Task HandleCapabilitiesAsync(NetworkStream stream)
        {
            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                capabilities = new
                {
                    quotes = true, ohlcv = true, nativeIndicators = true,
                    marketDepth = true, maxDepthLevels = 25, news = true,
                    institutionDistribution = true,
                    institutionDistributionRequiresLicense = true,
                    marketRankings = new
                    {
                        // Matriks documents per-symbol surface data, not a
                        // market-wide ranking call. Do not substitute a
                        // speculative method name here.
                        nativeMarketWide = false,
                        source = "SUBSCRIBED_UNIVERSE_FALLBACK",
                        universe = "CONFIGURED_SUBSCRIBED_EQUITY_ONLY",
                        nativeMarketWideReason = "No documented Matriks IQ AlgoTrader market-wide ranking method was verified.",
                        verifiedMethods = new[]
                        {
                            "AddSymbolMarketData(string)",
                            "GetMarketData(string, SymbolUpdateField)",
                            "SymbolUpdateField.WeekClose",
                            "SymbolUpdateField.TotalVol"
                        },
                        weeklyGainers = new
                        {
                            available = true,
                            source = "SUBSCRIBED_UNIVERSE_FALLBACK",
                            calculation = "(Last - WeekClose) / WeekClose * 100",
                            requiresWeekClose = true,
                            referencePeriod = "SEVEN_SESSIONS",
                            calendarWeekEquivalent = false,
                            note = "WeekClose is Matriks' seven-session reference, not a native calendar-week market ranking."
                        },
                        turnoverLeaders = new
                        {
                            available = true,
                            source = "SUBSCRIBED_UNIVERSE_FALLBACK",
                            field = "SymbolUpdateField.TotalVol"
                        },
                        relativeVolumeLeaders = new
                        {
                            available = false,
                            source = "UNAVAILABLE",
                            reason = "No documented market-wide relative-volume ranking method or historical baseline field was verified."
                        }
                    },
                    mkkCustody = false,
                    mkkReason = "No documented AlgoTrader C# access method"
                }
            });
        }

        private async Task HandleDepthAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            if (!IsAllowedSymbol(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "symbol not allowed", symbol = symbol });
                return;
            }
            int levels = 25;
            int parsed;
            if (int.TryParse(request.GetQueryValue("levels"), out parsed))
                levels = Math.Max(1, Math.Min(25, parsed));
            try
            {
                if (!IsEquitySymbol(symbol))
                {
                    string skipReason = IsIndexSymbol(symbol)
                        ? IndexDepthSkipReason
                        : "INSTRUMENT_ADAPTER_REQUIRED";
                    await WriteJsonAsync(stream, 200, new
                    {
                        ok = true,
                        symbol = symbol,
                        levels = levels,
                        available = false,
                        bids = new List<DepthLevelSnapshot>(),
                        asks = new List<DepthLevelSnapshot>(),
                        depthAvailable = false,
                        depthReliable = false,
                        depthSkipReason = skipReason,
                        timestamp = (string)null
                    });
                    return;
                }
                DepthSnapshot depth = ReadDepthSnapshot(symbol, levels);
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true, symbol = symbol, levels = levels,
                    available = depth.Analysis.Available,
                    bids = depth.Bids, asks = depth.Asks,
                    depthLevels = new { bids = depth.Bids, asks = depth.Asks },
                    depthAnalysis = depth.Analysis,
                    analysis = depth.Analysis,
                    timestamp = depth.Analysis.DepthTimestamp
                });
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 200, new { ok = true, symbol = symbol, available = false, error = ex.Message });
            }
        }

        private async Task HandleIndicatorsAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            if (!IsAllowedSymbol(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "symbol not allowed", symbol = symbol });
                return;
            }
            MarketDataPayload market = BuildMarketData(symbol, "INDICATORS");
            string[] keys = { "rsi", "ema20", "ema50", "macd", "macdSignal", "indicatorSource", "technicalFeatures", "ohlcReliable", "ohlcSource" };
            var indicators = market.Payload.Where(x => keys.Contains(x.Key)).ToDictionary(x => x.Key, x => x.Value);
            await WriteJsonAsync(stream, 200, new { ok = true, symbol = symbol, indicators = indicators, timestamp = market.Timestamp });
        }

        private async Task HandleNewsAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            int limit = 50;
            int parsed;
            if (int.TryParse(request.GetQueryValue("limit"), out parsed))
                limit = Math.Max(1, Math.Min(200, parsed));
            IEnumerable<NewsSnapshot> query = _recentNews.ToArray().Reverse();
            if (!string.IsNullOrWhiteSpace(symbol))
                query = query.Where(x => x.Symbols.Any(s => NormalizeSymbol(s) == symbol));
            await WriteJsonAsync(stream, 200, new
            {
                ok = true, symbol = string.IsNullOrWhiteSpace(symbol) ? null : symbol,
                news = query.Take(limit).ToList(), cacheSize = _recentNews.Count,
                note = "Only live news received after gateway startup is cached."
            });
        }

        private async Task HandleNewsDetailsAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            string keyword = (request.GetQueryValue("keyword") ?? "").Trim();
            string filterType = (request.GetQueryValue("filterType") ?? "").Trim();
            int limit = 50;
            int parsed;
            if (int.TryParse(request.GetQueryValue("limit"), out parsed))
                limit = Math.Max(1, Math.Min(200, parsed));

            IEnumerable<NewsSnapshot> query = _recentNews.ToArray().Reverse();
            if (!string.IsNullOrWhiteSpace(symbol))
                query = query.Where(x => x.Symbols.Any(s => NormalizeSymbol(s) == symbol));
            if (!string.IsNullOrWhiteSpace(keyword))
                query = query.Where(x => NewsMatchesKeyword(x, keyword));
            if (!string.IsNullOrWhiteSpace(filterType))
                query = query.Where(x => string.Equals(x.FilterType ?? "", filterType, StringComparison.OrdinalIgnoreCase));

            List<NewsSnapshot> items = query.Take(limit).ToList();
            List<object> filterSummary = items
                .GroupBy(x => string.IsNullOrWhiteSpace(x.FilterType) ? "UNKNOWN" : x.FilterType)
                .Select(g => (object)new { filterType = g.Key, count = g.Count() })
                .ToList();

            List<string> trackedSymbols;
            List<NewsKeywordSubscription> keywordSubscriptions;
            List<NewsSymbolKeywordSubscription> symbolKeywordSubscriptions;
            lock (_newsSubscriptionLock)
            {
                trackedSymbols = _newsTrackedSymbols.ToList();
                keywordSubscriptions = _newsKeywordSubscriptions.ToList();
                symbolKeywordSubscriptions = _newsSymbolKeywordSubscriptions.ToList();
            }

            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                symbol = string.IsNullOrWhiteSpace(symbol) ? null : symbol,
                keyword = string.IsNullOrWhiteSpace(keyword) ? null : keyword,
                filterType = string.IsNullOrWhiteSpace(filterType) ? null : filterType,
                cacheSize = _recentNews.Count,
                returned = items.Count,
                filters = new
                {
                    trackedSymbols = trackedSymbols,
                    keywordSubscriptions = keywordSubscriptions,
                    symbolKeywordSubscriptions = symbolKeywordSubscriptions
                },
                summary = new
                {
                    filterTypes = filterSummary,
                    onlyInHeaders = NewsFiltersOnlyInHeaders,
                    exactMatch = NewsFiltersExactMatch
                },
                news = items,
                note = "Returns all cached AlgoNewsModel-derived fields plus active Matriks news subscriptions."
            });
        }

        private async Task HandleKapAsync(NetworkStream stream, HttpRequest request, bool riskOnly)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            int limit = 50;
            int parsed;
            if (int.TryParse(request.GetQueryValue("limit"), out parsed))
                limit = Math.Max(1, Math.Min(200, parsed));
            int lookbackHours = 48;
            if (int.TryParse(request.GetQueryValue("lookbackHours"), out parsed))
                lookbackHours = Math.Max(1, Math.Min(720, parsed));

            // This is intentionally a cache fallback. A discovered, licensed
            // KAP method can be wrapped later without a compile-time dependency.
            IEnumerable<NewsSnapshot> query = _recentNews.ToArray().Reverse();
            if (!string.IsNullOrWhiteSpace(symbol))
                query = query.Where(x => x.Symbols.Any(s => NormalizeSymbol(s) == symbol));
            query = query.Where(IsKapLikeNews);
            DateTime cutoffUtc = DateTime.UtcNow.AddHours(-lookbackHours);
            query = query.Where(x => x.DateTime != DateTime.MinValue
                && x.DateTime.ToUniversalTime() >= cutoffUtc);
            if (riskOnly)
                query = query.Where(IsKapRiskLikeNews);

            List<NewsSnapshot> items = query.Take(limit).ToList();
            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                available = true,
                source = "news-details-fallback",
                symbol = string.IsNullOrWhiteSpace(symbol) ? null : symbol,
                lookbackHours = lookbackHours,
                lastNewsReceivedUtc = _lastNewsReceivedUtc == DateTime.MinValue ? null : _lastNewsReceivedUtc.ToString("o"),
                news = items,
                note = "Fallback contains only Matriks news events received since gateway startup; no historical KAP completeness is implied."
            });
        }

        private static bool IsKapLikeNews(NewsSnapshot item)
        {
            string filterType = item.FilterType ?? "";
            return filterType.IndexOf("KAP", StringComparison.OrdinalIgnoreCase) >= 0
                || NewsMatchesKeyword(item, "KAP")
                || NewsMatchesKeyword(item, "kamuyu aydinlatma")
                || NewsMatchesKeyword(item, "kamuyu aydınlatma");
        }

        private static bool IsKapRiskLikeNews(NewsSnapshot item)
        {
            string[] keywords = { "tedbir", "brut takas", "brüt takas", "kredili", "aciga satis", "açığa satış", "bedelli", "pay satisi", "pay satışı", "SPK", "dava", "ceza", "iflas", "haciz" };
            return keywords.Any(keyword => NewsMatchesKeyword(item, keyword));
        }

        private async Task HandleInstitutionsAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            if (!IsAllowedSymbol(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "symbol not allowed", symbol = symbol });
                return;
            }
            int limit = 5;
            int parsed;
            if (int.TryParse(request.GetQueryValue("limit"), out parsed))
                limit = Math.Max(1, Math.Min(20, parsed));
            string periodRaw = (request.GetQueryValue("period") ?? "Daily").Trim();
            MoneyIncomePeriod period;
            if (!Enum.TryParse<MoneyIncomePeriod>(periodRaw, true, out period))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "invalid institution period", period = periodRaw });
                return;
            }
            bool includeReportedOrders = true;
            bool parsedBool;
            if (bool.TryParse(request.GetQueryValue("includeReportedOrders"), out parsedBool)) includeReportedOrders = parsedBool;
            try
            {
                var buyers = new List<object>();
                var sellers = new List<object>();
                for (int rank = 1; rank <= limit; rank++)
                {
                    var buyer = GetBestInstitution(symbol, TransactionDataField.Size, TransactionSide.Net, BestBuyerSellerOrder.NetBuyerLot, period, rank, includeReportedOrders);
                    var seller = GetBestInstitution(symbol, TransactionDataField.Size, TransactionSide.Net, BestBuyerSellerOrder.NetSellerLot, period, rank, includeReportedOrders);
                    if (buyer != null) buyers.Add(new { id = buyer.Id, name = buyer.Name, rank = buyer.Rank, value = ToDouble(buyer.Value) });
                    if (seller != null) sellers.Add(new { id = seller.Id, name = seller.Name, rank = seller.Rank, value = ToDouble(seller.Value) });
                }
                bool effectiveIncludeReportedOrders = includeReportedOrders;
                if (buyers.Count == 0 && sellers.Count == 0 && includeReportedOrders)
                {
                    effectiveIncludeReportedOrders = false;
                    for (int rank = 1; rank <= limit; rank++)
                    {
                        var buyer = GetBestInstitution(symbol, TransactionDataField.Size, TransactionSide.Net, BestBuyerSellerOrder.NetBuyerLot, period, rank, false);
                        var seller = GetBestInstitution(symbol, TransactionDataField.Size, TransactionSide.Net, BestBuyerSellerOrder.NetSellerLot, period, rank, false);
                        if (buyer != null) buyers.Add(new { id = buyer.Id, name = buyer.Name, rank = buyer.Rank, value = ToDouble(buyer.Value) });
                        if (seller != null) sellers.Add(new { id = seller.Id, name = seller.Name, rank = seller.Rank, value = ToDouble(seller.Value) });
                    }
                }
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true, available = buyers.Count > 0 || sellers.Count > 0,
                    symbol = symbol, period = period.ToString().ToUpperInvariant(), includeReportedOrders = effectiveIncludeReportedOrders,
                    attemptedRanks = limit, buyersReturned = buyers.Count, sellersReturned = sellers.Count,
                    methodAvailable = true, dataStatus = buyers.Count > 0 || sellers.Count > 0 ? "AVAILABLE" : "EMPTY",
                    possibleReasons = buyers.Count > 0 || sellers.Count > 0 ? new string[0] : new[] { "market closed", "license unavailable", "session data unavailable" },
                    buyers = buyers, sellers = sellers,
                    requiresLicense = "AKDE/AKD for equities; VAKD for VIOP end-of-day data"
                });
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 200, new { ok = true, available = false, symbol = symbol, error = ex.Message, requiresLicense = "AKDE/AKD or VAKD" });
            }
        }

        // ══════════════════════════════════════════════════════════════════
        // Genişletilmiş read-only veri yüzeyi
        // Her handler fail-soft: Matriks metodu patlarsa 200 + available:false.
        // ══════════════════════════════════════════════════════════════════

        // Tek bir SymbolUpdateField değerini döndürür: /marketdata?symbol=X&field=Last
        private async Task HandleMarketDataFieldAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            string fieldName = (request.GetQueryValue("field") ?? "Last").Trim();
            if (!IsAllowedSymbol(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "symbol not allowed", symbol = symbol });
                return;
            }
            SymbolUpdateField field;
            if (!Enum.TryParse<SymbolUpdateField>(fieldName, true, out field))
            {
                await WriteJsonAsync(stream, 400, new
                {
                    ok = false,
                    error = "unknown field",
                    field = fieldName,
                    availableFields = Enum.GetNames(typeof(SymbolUpdateField))
                });
                return;
            }
            try
            {
                decimal value = GetMarketData(symbol, field);
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true, available = true, symbol = symbol,
                    field = field.ToString(), value = ToDouble(value)
                });
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 200, new { ok = true, available = false, symbol = symbol, field = fieldName, error = ex.Message });
            }
        }

        // Tüm SymbolUpdateField değerlerini tek seferde döker: /marketdata/all?symbol=X
        private async Task HandleMarketDataAllAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            if (!IsAllowedSymbol(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "symbol not allowed", symbol = symbol });
                return;
            }
            var fields = new Dictionary<string, object>();
            foreach (SymbolUpdateField field in Enum.GetValues(typeof(SymbolUpdateField)))
            {
                try
                {
                    fields[field.ToString()] = ToDouble(GetMarketData(symbol, field));
                }
                catch
                {
                    fields[field.ToString()] = null;
                }
            }
            await WriteJsonAsync(stream, 200, new { ok = true, available = true, symbol = symbol, fields = fields });
        }

        // Sembol id + kayıtlı meta: /symbol?symbol=X
        // Not: GetSymbolDef/GetSymbolDetail'in Matriks'teki gerçek imzası
        // (string) değil; derlenemedikleri için çıkarıldı. GetSymbolId
        // doğrulanmış tek metod (ResolveBarEventSymbol'de de kullanılıyor).
        private async Task HandleSymbolInfoAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            if (string.IsNullOrWhiteSpace(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "symbol required" });
                return;
            }
            var result = new Dictionary<string, object>
            {
                { "symbol", symbol },
                { "allowed", IsAllowedSymbol(symbol) }
            };
            SafeInvoke(result, "symbolId", () => GetSymbolId(symbol));
            await WriteJsonAsync(stream, 200, new { ok = true, available = true, symbol = symbol, info = result });
        }

        // Seans saatleri: /session?symbol=X
        private async Task HandleSessionTimesAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            if (string.IsNullOrWhiteSpace(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "symbol required" });
                return;
            }
            try
            {
                object sessions = InvokeSelf("GetSessionTimes", symbol);
                await WriteJsonAsync(stream, 200, new { ok = true, available = true, symbol = symbol, sessions = ReflectAny(sessions) });
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 200, new { ok = true, available = false, symbol = symbol, error = Unwrap(ex) });
            }
        }

        // Fiyat adımı + yuvarlama: /pricestep?symbol=X&price=P
        private async Task HandlePriceStepAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            decimal price;
            decimal.TryParse(request.GetQueryValue("price"), System.Globalization.NumberStyles.Any,
                System.Globalization.CultureInfo.InvariantCulture, out price);
            if (string.IsNullOrWhiteSpace(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "symbol required" });
                return;
            }
            var result = new Dictionary<string, object> { { "symbol", symbol }, { "price", ToDouble(price) } };
            SafeInvoke(result, "priceStep", () => ToDouble(Convert.ToDecimal(InvokeSelf("GetPriceStepForBistViop", symbol, price))));
            SafeInvoke(result, "rounded", () => ToDouble(Convert.ToDecimal(InvokeSelf("RoundPriceStepBistViop", symbol, price))));
            await WriteJsonAsync(stream, 200, new { ok = true, available = true, result = result });
        }

        // OHLC bar geçmişi (güncel bar + tuttuğumuz kapanış serisi): /bars?symbol=X&count=N
        private async Task HandleBarsAsync(NetworkStream stream, HttpRequest request)
        {
            string symbol = NormalizeSymbol(request.GetQueryValue("symbol"));
            if (!IsAllowedSymbol(symbol))
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "symbol not allowed", symbol = symbol });
                return;
            }
            int count = 50;
            int parsed;
            if (int.TryParse(request.GetQueryValue("count"), out parsed))
                count = Math.Max(1, Math.Min(500, parsed));

            // Güncel bar, OnDataUpdate'te doldurduğumuz OHLCV cache'inden okunur
            // (GetBarData() return tipine bağımlı değil — compile-safe).
            object currentBar = null;
            OhlcvSnapshot ohlc = new OhlcvSnapshot();
            if (_lastOhlcvBySymbol.TryGetValue(symbol, out ohlc))
            {
                currentBar = new
                {
                    open = ToDouble(ohlc.Open),
                    high = ToDouble(ohlc.High),
                    low = ToDouble(ohlc.Low),
                    close = ToDouble(ohlc.Close),
                    volume = ToDouble(ohlc.Volume),
                    reliable = ohlc.Reliable
                };
            }

            string actualPeriod = ohlc.ActualBarPeriod;
            if (string.IsNullOrWhiteSpace(actualPeriod))
                actualPeriod = NormalizePeriodName(IndicatorPeriod.ToString());
            string seriesKey = BuildSeriesKey(symbol, actualPeriod);
            var bars = new List<object>();
            var closeHistory = new List<double>();
            List<OhlcvBarPoint> history;
            if (_ohlcvHistoryBySeries.TryGetValue(seriesKey, out history) && history != null)
            {
                lock (_closeLock)
                {
                    int start = Math.Max(0, history.Count - count);
                    for (int i = start; i < history.Count; i++)
                    {
                        OhlcvBarPoint point = history[i];
                        bars.Add(new
                        {
                            open = ToDouble(point.Open),
                            high = ToDouble(point.High),
                            low = ToDouble(point.Low),
                            close = ToDouble(point.Close),
                            volume = ToDouble(point.Volume),
                            reliable = point.Reliable,
                            closed = point.Closed
                        });
                        closeHistory.Add(ToDouble(point.Close));
                    }
                }
            }

            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                available = currentBar != null || bars.Count > 0,
                symbol = symbol,
                period = actualPeriod,
                actualBarPeriod = actualPeriod,
                currentBar = currentBar,
                bars = bars,
                closeHistory = closeHistory
            });
        }

        // Hesap / kullanıcı bilgisi: /account (GetTradeUser doğrulanmış — direkt)
        private async Task HandleAccountAsync(NetworkStream stream)
        {
            try
            {
                object user = GetTradeUser();
                Dictionary<string, object> account = BuildTradeUserAccountPayload(user);
                decimal overall;
                decimal availableMargin;
                bool accountDataReliable = user != null
                    && TryGetFiniteDecimal(account, "Overall", out overall)
                    && overall > 0m
                    && TryGetFiniteDecimal(account, "AvailableMargin", out availableMargin)
                    && availableMargin >= 0m;
                // v2 hesap kimliği alanları: taze GetTradeUser okumasından
                // doğrudan hesaplanır (cache'e bağlı değil); ham id maskeli.
                string rawAccountId = user == null
                    ? string.Empty
                    : (Convert.ToString(ReadPublicMember(user, "AccountId")) ?? string.Empty);
                bool? testAutoOrder = user == null
                    ? (bool?)null
                    : ReadPublicMember(user, "TestAutoOrder") as bool?;
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    available = user != null,
                    sourceProvider = "MATRIKS_IQ",
                    receivedAtUtc = DateTime.UtcNow.ToString("o"),
                    accountDataReliable = accountDataReliable,
                    accountRef = string.IsNullOrEmpty(rawAccountId) ? null : Sha256Hex(rawAccountId),
                    accountSessionRef = string.IsNullOrEmpty(rawAccountId)
                        ? null
                        : Sha256Hex(rawAccountId + "|" + _startedAt.Ticks.ToString()),
                    accountIdMasked = string.IsNullOrEmpty(rawAccountId) ? null : MaskAccountId(rawAccountId),
                    accountType = user == null || testAutoOrder == null
                        ? "UNKNOWN"
                        : (testAutoOrder.Value ? "DEMO" : "REAL"),
                    account = account
                });
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    available = false,
                    sourceProvider = "MATRIKS_IQ",
                    receivedAtUtc = DateTime.UtcNow.ToString("o"),
                    accountDataReliable = false,
                    error = Unwrap(ex)
                });
            }
        }

        // Gerçek (borsa) pozisyon snapshot'ı: /realpositions (imza reflection ile)
        private async Task HandleRealPositionsAsync(NetworkStream stream)
        {
            try
            {
                object positions = InvokeSelf("GetRealPositions");
                await WriteJsonAsync(stream, 200, new { ok = true, available = true, positions = ReflectAny(positions) });
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 200, new { ok = true, available = false, error = Unwrap(ex) });
            }
        }

        // Piyasa geneli özet: /overall (imza reflection ile)
        private async Task HandleOverallAsync(NetworkStream stream)
        {
            try
            {
                object overall = InvokeSelf("GetOverall");
                await WriteJsonAsync(stream, 200, new { ok = true, available = true, overall = ReflectAny(overall) });
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 200, new { ok = true, available = false, error = Unwrap(ex) });
            }
        }

        // Bu gateway'in sunduğu tüm endpoint'lerin kataloğu: /capabilities/methods
        private async Task HandleMethodSearchAsync(NetworkStream stream, HttpRequest request)
        {
            string keyword = (request.GetQueryValue("keyword") ?? "").Trim();
            try
            {
                var methods = FindMethodsByKeyword(keyword);
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    available = true,
                    keyword = keyword,
                    methods = methods
                });
            }
            catch (Exception ex)
            {
                // Discovery is metadata-only. Reflection failures must not
                // affect gateway availability or invoke a Matriks API method.
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    available = false,
                    keyword = keyword,
                    methods = new object[0],
                    error = Unwrap(ex)
                });
            }
        }

        private async Task HandleMethodCatalogAsync(NetworkStream stream)
        {
            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                endpoints = new object[]
                {
                    new { path = "/methods/search?keyword=kap", desc = "Matriks method metadata discovery (does not invoke methods)" },
                    new { path = "/health", desc = "Gateway + veri + pozisyon durumu" },
                    new { path = "/snapshot?symbol=X", desc = "OHLCV + derinlik + teknik feature bloğu" },
                    new { path = "/positions", desc = "Bot pozisyon snapshot'ı" },
                    new { path = "/depth?symbol=X&levels=25", desc = "25 kademe derinlik + imbalance" },
                    new { path = "/indicators?symbol=X", desc = "RSI/EMA/MACD anlık değerler" },
                    new { path = "/news?symbol=X&limit=50", desc = "Matriks haber cache" },
                    new { path = "/news/details?symbol=X", desc = "Detaylı haber + abonelikler" },
                    new { path = "/institutions?symbol=X", desc = "AKD net alıcı/satıcı sıralaması" },
                    new { path = "/mkk", desc = "MKK/Takas yetenek durumu" },
                    new { path = "/movers?limit=20", desc = "Günlük yükselen/düşen/hacimliler" },
                    new { path = "/marketdata?symbol=X&field=Last", desc = "Tek SymbolUpdateField değeri" },
                    new { path = "/marketdata/all?symbol=X", desc = "Tüm SymbolUpdateField değerleri" },
                    new { path = "/symbol?symbol=X", desc = "Sembol tanımı + detay + id" },
                    new { path = "/session?symbol=X", desc = "Seans saatleri" },
                    new { path = "/pricestep?symbol=X&price=P", desc = "Fiyat adımı + yuvarlama" },
                    new { path = "/bars?symbol=X&count=50", desc = "Güncel bar + kapanış geçmişi" },
                    new { path = "/account", desc = "Trade user / hesap bilgisi" },
                    new { path = "/realpositions", desc = "Gerçek borsa pozisyonları" },
                    new { path = "/overall", desc = "Piyasa geneli özet" },
                    new { path = "/order (POST)", desc = "LIMIT emir gönderimi" },
                    new { path = "/config/reload (POST)", desc = "Sunucu config'ini yeniden çek" }
                }
            });
        }

        // ── Reflection yardımcıları (fail-soft, sığ) ────────────────────────

        // Bir objeyi düz bir sözlüğe indirger: public property'ler, per-property
        // try/catch. Nested objeler ToString()'e düşer — döngüsel referans /
        // dev graf patlaması olmaz. null → boş sözlük.
        private List<Dictionary<string, object>> FindMethodsByKeyword(string keyword)
        {
            string needle = (keyword ?? "").Trim();
            var matches = new List<Dictionary<string, object>>();
            Type type = this.GetType();

            while (type != null)
            {
                var methods = type.GetMethods(
                    System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic
                    | System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.DeclaredOnly);
                foreach (var method in methods)
                {
                    if (!string.IsNullOrEmpty(needle)
                        && method.Name.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
                        continue;
                    matches.Add(ReflectMethodSignature(method));
                }
                type = type.BaseType;
            }

            return matches;
        }

        private static Dictionary<string, object> ReflectMethodSignature(System.Reflection.MethodInfo method)
        {
            var parameters = new List<Dictionary<string, string>>();
            foreach (var parameter in method.GetParameters())
            {
                parameters.Add(new Dictionary<string, string>
                {
                    { "name", parameter.Name ?? "" },
                    { "type", parameter.ParameterType.FullName ?? parameter.ParameterType.Name }
                });
            }

            return new Dictionary<string, object>
            {
                { "name", method.Name },
                { "returnType", method.ReturnType.FullName ?? method.ReturnType.Name },
                { "parameters", parameters },
                { "declaringType", method.DeclaringType == null ? "" : (method.DeclaringType.FullName ?? method.DeclaringType.Name) },
                { "isPublic", method.IsPublic },
                { "isStatic", method.IsStatic }
            };
        }

        private static Dictionary<string, object> ReflectToDict(object obj)
        {
            var result = new Dictionary<string, object>();
            if (obj == null)
                return result;
            try
            {
                foreach (var prop in obj.GetType().GetProperties(
                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance))
                {
                    if (prop.GetIndexParameters().Length > 0)
                        continue; // indexer'ları atla
                    try
                    {
                        object value = prop.GetValue(obj, null);
                        result[prop.Name] = SimplifyValue(value);
                    }
                    catch
                    {
                        result[prop.Name] = null;
                    }
                }
            }
            catch
            {
            }
            return result;
        }

        // Koleksiyon → eleman başına ReflectToDict; tekil obje → ReflectToDict;
        // primitif → aynen. Serileştirmenin asla patlamamasını garanti eder.
        private static object ReflectAny(object obj)
        {
            if (obj == null)
                return null;
            if (IsSimple(obj.GetType()))
                return obj;
            // Sözlük (ör. GetRealPositions → sembol anahtarlı) → key/value map.
            var dict = obj as System.Collections.IDictionary;
            if (dict != null)
            {
                var map = new Dictionary<string, object>();
                try
                {
                    int i = 0;
                    foreach (System.Collections.DictionaryEntry entry in dict)
                    {
                        if (i++ >= 200) break;
                        string key = entry.Key == null ? "null" : entry.Key.ToString();
                        object v = entry.Value;
                        map[key] = (v != null && IsSimple(v.GetType())) ? v : ReflectToDict(v);
                    }
                }
                catch
                {
                }
                return map;
            }
            var enumerable = obj as System.Collections.IEnumerable;
            if (enumerable != null && !(obj is string))
            {
                var list = new List<object>();
                try
                {
                    int i = 0;
                    foreach (var item in enumerable)
                    {
                        if (i++ >= 200) break; // güvenlik sınırı
                        list.Add(IsSimple(item == null ? typeof(object) : item.GetType())
                            ? item : ReflectToDict(item));
                    }
                }
                catch
                {
                }
                return list;
            }
            return ReflectToDict(obj);
        }

        private static object SimplifyValue(object value)
        {
            if (value == null)
                return null;
            return IsSimple(value.GetType()) ? value : value.ToString();
        }

        private static bool IsSimple(Type type)
        {
            if (type == null)
                return false;
            return type.IsPrimitive
                || type.IsEnum
                || type == typeof(string)
                || type == typeof(decimal)
                || type == typeof(DateTime)
                || type == typeof(DateTimeOffset)
                || type == typeof(TimeSpan)
                || type == typeof(Guid);
        }

        // Bir Matriks metodunu ADIYLA reflection ile çağırır. Belgelenen ama
        // imzası bu ortamda doğrulanamayan metodlar için compile riskini
        // ortadan kaldırır: yanlış imza/eksik metod derlemeyi değil, yalnızca
        // runtime'ı etkiler (çağıran try/catch ile available:false döner).
        // Ad + parametre SAYISI ile eşleştirir; strateji base sınıfları
        // boyunca (this.GetType() → BaseType zinciri) arar.
        private object InvokeSelf(string methodName, params object[] args)
        {
            Type type = this.GetType();
            while (type != null)
            {
                var methods = type.GetMethods(
                    System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic
                    | System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.DeclaredOnly);
                foreach (var m in methods)
                {
                    if (m.Name != methodName)
                        continue;
                    if (m.GetParameters().Length != (args == null ? 0 : args.Length))
                        continue;
                    return m.Invoke(this, args);
                }
                type = type.BaseType;
            }
            throw new MissingMethodException(GetType().Name, methodName);
        }

        // Reflection çağrıları gerçek hatayı TargetInvocationException içine
        // sarar; kullanıcıya anlamlı mesaj dönebilmek için iç istisnayı açar.
        private static string Unwrap(Exception ex)
        {
            var inner = ex is System.Reflection.TargetInvocationException && ex.InnerException != null
                ? ex.InnerException
                : ex;
            return inner.Message;
        }

        // Bir üretici delegesini güvenle çağırıp sonucu sözlüğe yazar.
        private void SafeInvoke(Dictionary<string, object> target, string key, Func<object> producer)
        {
            try
            {
                target[key] = producer();
            }
            catch (Exception ex)
            {
                target[key] = null;
                SafeDebug("SafeInvoke failed key=" + key + " err=" + ex.Message);
            }
        }

        private async Task HandleMoversAsync(NetworkStream stream, HttpRequest request)
        {
            // Kayıtlı (abone) semboller üzerinden günlük değişim ve hacim
            // sıralaması. Değişim, gün içinde görülen ilk geçerli fiyata
            // (_dailyRefPriceBySymbol) göredir — Matriks API'si abone olunmayan
            // sembol için ranking vermediğinden evren AllowedSymbols'tür;
            // server, keşif evrenini config'teki symbols listesiyle genişletir.
            int limit = 10;
            int parsedLimit;
            if (int.TryParse(request.GetQueryValue("limit"), out parsedLimit))
                limit = Math.Max(1, Math.Min(50, parsedLimit));

            var items = new List<Dictionary<string, object>>();
            try
            {
                foreach (string symbolRaw in AllowedSymbols)
                {
                    string symbol = NormalizeSymbol(symbolRaw);
                    // Discovery can be the first consumer after a gateway
                    // restart.  Populate the quote cache here rather than
                    // requiring a prior /snapshot request; otherwise the
                    // movers -> research pipeline can never bootstrap.
                    // Indices are macro-only and do not belong to the equity
                    // discovery ranking.
                    if (!IsEquitySymbol(symbol))
                        continue;

                    MarketQuoteSnapshot quote;
                    try
                    {
                        quote = ReadMarketQuote(symbol);
                    }
                    catch (Exception ex)
                    {
                        SafeDebug("Movers quote unavailable symbol=" + symbol + " error=" + ex.Message);
                        continue;
                    }
                    if (quote.Last <= 0m)
                        continue;

                    decimal refPrice;
                    if (!_dailyRefPriceBySymbol.TryGetValue(symbol, out refPrice) || refPrice <= 0m)
                        refPrice = quote.Last;

                    double changePct = refPrice > 0m
                        ? (double)((quote.Last - refPrice) / refPrice * 100m)
                        : 0.0;
                    decimal weekClose = SafeMarketData(symbol, SymbolUpdateField.WeekClose);
                    object weeklyChangePct = weekClose > 0m
                        ? (object)Math.Round((double)((quote.Last - weekClose) / weekClose * 100m), 2)
                        : null;

                    items.Add(new Dictionary<string, object>
                    {
                        { "symbol", symbol },
                        { "lastPrice", ToDouble(quote.Last) },
                        { "refPrice", ToDouble(refPrice) },
                        { "changePct", Math.Round(changePct, 2) },
                        { "weekClose", weekClose > 0m ? (object)ToDouble(weekClose) : null },
                        { "weeklyChangePct", weeklyChangePct },
                        { "weeklyMomentumAvailable", weeklyChangePct != null },
                        // Movers historically calls this field volume and the
                        // discovery service treats it as TL turnover. Keep the
                        // alias, but publish the semantic fields explicitly.
                        { "volume", ToDouble(quote.TotalVol) },
                        { "sessionTurnoverTl", ToDouble(quote.TotalVol) },
                        { "volumeSemantic", "CUMULATIVE_SESSION_TURNOVER_TL" },
                        { "volumeSource", "SymbolUpdateField.TotalVol" },
                        { "quoteAgeSeconds", quote.LastTradeUtc == DateTime.MinValue ? (object)null : Math.Round((DateTime.UtcNow - quote.LastTradeUtc).TotalSeconds, 1) }
                    });
                }

                var gainers = items
                    .OrderByDescending(x => (double)x["changePct"])
                    .Take(limit).Select(x => (string)x["symbol"]).ToList();
                var losers = items
                    .OrderBy(x => (double)x["changePct"])
                    .Take(limit).Select(x => (string)x["symbol"]).ToList();
                var weeklyGainers = items
                    .Where(x => x["weeklyChangePct"] != null)
                    .OrderByDescending(x => (double)x["weeklyChangePct"])
                    .Take(limit).Select(x => (string)x["symbol"]).ToList();
                var volumeLeaders = items
                    .OrderByDescending(x => (double)x["volume"])
                    .Take(limit).Select(x => (string)x["symbol"]).ToList();

                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    available = items.Count > 0,
                    updatedAt = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz"),
                    universeSize = items.Count,
                    items = items,
                    gainers = gainers,
                    weeklyGainers = weeklyGainers,
                    losers = losers,
                    volumeLeaders = volumeLeaders,
                    rankingCapabilities = new
                    {
                        nativeMarketWide = false,
                        source = "SUBSCRIBED_UNIVERSE_FALLBACK",
                        universe = "CONFIGURED_SUBSCRIBED_EQUITY_ONLY",
                        weeklyGainers = new
                        {
                            available = weeklyGainers.Count > 0,
                            source = "SUBSCRIBED_UNIVERSE_FALLBACK",
                            calculation = "(Last - WeekClose) / WeekClose * 100",
                            referencePeriod = "SEVEN_SESSIONS",
                            calendarWeekEquivalent = false
                        },
                        turnoverLeaders = new
                        {
                            available = true,
                            source = "SUBSCRIBED_UNIVERSE_FALLBACK",
                            field = "SymbolUpdateField.TotalVol",
                            semantic = "CUMULATIVE_SESSION_TURNOVER_TL"
                        },
                        relativeVolumeLeaders = new
                        {
                            available = false,
                            source = "UNAVAILABLE",
                            reason = "No documented native relative-volume ranking method or baseline is available."
                        }
                    }
                });
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 200, new { ok = true, available = false, error = ex.Message });
            }
        }

        private async Task HandleMkkAsync(NetworkStream stream)
        {
            await WriteJsonAsync(stream, 200, new
            {
                ok = true, supported = false, available = false,
                reason = "No public AlgoTrader C# method for MKK/Takas data is documented by Matriks IQ.",
                alternative = "Use a separately licensed/exported Matriks source when an official API is supplied."
            });
        }

        private async Task HandleOrderAsync(NetworkStream stream, HttpRequest request)
        {
            GatewayConfigSnapshot orderConfig = _activeConfig;
            OrderRequest order;
            try
            {
                order = JsonConvert.DeserializeObject<OrderRequest>(request.Body ?? "");
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 400, new { ok = false, error = "invalid JSON body: " + ex.Message });
                return;
            }

            if (string.IsNullOrWhiteSpace(order.RequestId)
                || string.IsNullOrWhiteSpace(order.Symbol)
                || string.IsNullOrWhiteSpace(order.Side))
            {
                await WriteJsonAsync(stream, 400, new
                {
                    ok = false,
                    error = "missing required fields: requestId, symbol, side"
                });
                return;
            }

            await _orderGate.WaitAsync();
            try
            {
            ResetDailyCachesIfNeeded();
            CleanupIdempotencyCache();

            string side = NormalizeAction(order.Side);
            string symbol = NormalizeSymbol(order.Symbol);
            IdempotencyEntry cachedResult;
            if (_sentRequestIds.TryGetValue(order.RequestId, out cachedResult))
            {
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    accepted = cachedResult.Accepted,
                    status = cachedResult.Status,
                    requestId = order.RequestId,
                    symbol = symbol,
                    reason = cachedResult.Message,
                    duplicate = true
                });
                return;
            }
            // v2: order.Mode yalnızca kayıt amaçlı; emir yetkisi
            // CheckDispatchGates ile verilir (RuntimeMode gating kaldırıldı).
            bool finiteOrderValues = !double.IsNaN(order.Qty) && !double.IsInfinity(order.Qty)
                && !double.IsNaN(order.LimitPrice) && !double.IsInfinity(order.LimitPrice)
                && Math.Abs(order.Qty) <= (double)decimal.MaxValue
                && Math.Abs(order.LimitPrice) <= (double)decimal.MaxValue;
            decimal qty = finiteOrderValues ? ToDecimal(order.Qty) : 0m;
            decimal price = finiteOrderValues ? ToDecimal(order.LimitPrice) : 0m;
            int finalQty;
            string quantityError;
            decimal roundedPrice = RoundPriceStepBistViop(symbol, price);
            decimal orderValue = 0m;
            if (TryConvertOrderQuantity(qty, out finalQty, out quantityError))
                orderValue = finalQty * roundedPrice;

            SafeDebug("Order request received requestId=" + order.RequestId
                + " symbol=" + symbol
                + " side=" + side
                + " qty=" + qty
                + " limitPrice=" + price
                + " runtimeMode=" + RuntimeMode);

            // ── Güvenlik kapıları (sıra TradeAiAgenticBot.TrySendOrderAsync) ──

            string rejection = null;

            if (side != "BUY" && side != "SELL")
                rejection = "unknown side=" + order.Side;
            else if (!finiteOrderValues)
                rejection = "qty/limitPrice must be finite";
            else if (orderConfig.TradingKillSwitchActive || orderConfig.ForceSafeMode)
                rejection = "trading kill switch / force safe mode is active";
            else if (IsIndexSymbol(symbol))
                rejection = "index symbols are data-only and cannot receive orders: " + symbol;
            else if (side == "BUY" && orderConfig.DeclineSymbols.Contains(symbol))
                rejection = "BUY symbol is on the decline blacklist: " + symbol;
            // BUY eligibility is an explicit, server-generated trade
            // watchlist. An empty list means no BUY is authorized; it must
            // never degrade to an allow-all wildcard for direct /order calls.
            else if (side == "BUY" && !orderConfig.BuyAllowedSymbols.Contains(symbol))
                rejection = "BUY symbol is not in the active trade watchlist: " + symbol;
            else if (side == "SELL" && orderConfig.SellExitAllowedSymbols.Length > 0 && !orderConfig.SellExitAllowedSymbols.Contains(symbol))
                rejection = "SELL_EXIT symbol not allowed: " + symbol;
            else if (!TryConvertOrderQuantity(qty, out finalQty, out quantityError))
                rejection = quantityError;
            else if (qty != finalQty)
                rejection = "qty has fractional component";
            else if (roundedPrice <= 0m)
                rejection = "limitPrice is null or <= 0";
            else if (qty <= 0m)
                rejection = "qty <= 0";
            else if (!IsAllowedSymbol(symbol))
                rejection = "symbol not allowed: " + symbol;
            else if (!_realPositionsLoadedFromSnapshot)
                rejection = "real positions are not loaded yet";
            else if (IsConfigStale())
                rejection = "gateway config is stale";
            else if (MaxOrderValueTl <= 0m || MaxQtyPerOrder <= 0m || MaxOrdersPerDay <= 0 || MaxOrdersPerSymbolPerDay <= 0)
                rejection = "live risk limits are not valid";
            // v2: eski "request mode == RuntimeMode" kontrolü kaldırıldı. Emirin
            // "mode" alanı artık yalnızca kayıt amaçlıdır; emir yetkisi
            // systemMode + accountType + REAL arming (CheckDispatchGates) ile
            // belirlenir.
            else if (finalQty > MaxQtyPerOrder)
                rejection = "qty exceeds MaxQtyPerOrder: " + finalQty;
            else if (orderValue > MaxOrderValueTl)
                rejection = "orderValue exceeds MaxOrderValueTl after price rounding: " + orderValue;
            else if (GetTotalDailyOrderCount() >= MaxOrdersPerDay)
                rejection = "MaxOrdersPerDay reached: " + MaxOrdersPerDay;
            else if (GetDailyTradeCount(symbol) >= MaxOrdersPerSymbolPerDay)
                rejection = "MaxOrdersPerSymbolPerDay reached for " + symbol;
            else if (_pendingOrdersBySymbolSide.ContainsKey(BuildSymbolSideKey(symbol, side)))
                rejection = "active pending order already exists for symbol and side";
            else if (side == "SELL" && GetSellableQty(symbol) < qty)
                rejection = "SELL qty exceeds sellable position (bot="
                    + GetAccountAvailableQty(symbol)
                    + " locked=" + GetLockedLongTermQty(symbol) + ")";
            else
                rejection = ValidateOrderMarketData(symbol, side, roundedPrice);
            // ── v2 kontrat + dispatch kapıları (TEK mod kapısı) ──
            // Sürüm uyuşmazlığı (alan eksikliği dahil) fail-closed emir reddi.
            // Eski CheckModeGates (PAPER/MANUAL/DEMO_LIVE/REAL_LIVE) kaldırıldı;
            // yerini systemMode + accountType + REAL arming (CheckDispatchGates)
            // aldı.
            if (rejection == null && _serverContractVersion != ExpectedContractVersion)
                rejection = "contract version mismatch — dispatch disabled (expected "
                    + ExpectedContractVersion + ", got " + _serverContractVersion + ")";
            if (rejection == null)
                rejection = CheckDispatchGates();

            if (rejection != null)
            {
                SafeDebug("Order blocked: " + rejection);
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    accepted = false,
                    status = "REJECTED",
                    requestId = order.RequestId,
                    symbol = symbol,
                    reason = rejection
                });
                return;
            }

            var reservation = new IdempotencyEntry
            {
                CreatedUtc = DateTime.UtcNow,
                Accepted = false,
                Status = "RESERVED",
                Message = "order slot reserved"
            };
            if (!_sentRequestIds.TryAdd(order.RequestId, reservation))
            {
                await WriteJsonAsync(stream, 200, new { ok = true, accepted = false,
                    status = "REJECTED", requestId = order.RequestId, symbol = symbol,
                    reason = "duplicate requestId" });
                return;
            }
            IncrementDailyTradeCount(symbol); // reserve the daily/symbol slot inside the gate

            // ── Tüm kapılar geçildi — LIMIT emir gönder ──

            try
            {
                OrderExecutionResult execution = SendGatewayLimitOrder(order.RequestId, symbol, side, qty, price);
                if (execution.Success)
                {
                    reservation.Accepted = true;
                    reservation.Status = "SENT_PENDING";
                    reservation.Message = execution.Message;
                    SafeDebug("Order SENT_PENDING symbol=" + symbol
                        + " side=" + side
                        + " qty=" + qty
                        + " price=" + price);
                    await WriteJsonAsync(stream, 200, new
                    {
                        ok = true,
                        accepted = true,
                        status = "SENT_PENDING",
                        requestId = order.RequestId,
                        symbol = symbol,
                        reason = execution.Message
                    });
                    return;
                }

                SafeDebug("Order send failed: " + execution.Message);
                RollbackOrderReservation(order.RequestId, symbol, side, true);
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    accepted = false,
                    status = "REJECTED",
                    requestId = order.RequestId,
                    symbol = symbol,
                    reason = execution.Message
                });
            }
            catch (Exception ex)
            {
                reservation.Accepted = false;
                reservation.Status = "SEND_UNKNOWN";
                reservation.Message = "SendLimitOrder outcome is unknown; reconciliation required";
                SafeDebug("Order send outcome unknown requestId=" + order.RequestId + " error=" + ex.Message);
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    accepted = false,
                    status = "SEND_UNKNOWN",
                    requestId = order.RequestId,
                    symbol = symbol,
                    reason = reservation.Message
                });
            }
            }
            finally
            {
                _orderGate.Release();
            }
        }

        /// <summary>
        /// Mode kapıları — server'ın bildirdiği mode'a göre son savunma hattı.
        /// null → geçti; aksi halde red gerekçesi.
        /// </summary>
        private string ValidateOrderMarketData(string symbol, string side, decimal roundedLimitPrice)
        {
            if (!IsOrderSessionOpen())
                return "trading session is closed";
            DateTime eventUtc;
            if (!_lastTradeUtcBySymbol.TryGetValue(symbol, out eventUtc))
                return "last trade timestamp is unknown";
            double quoteAge = (DateTime.UtcNow - eventUtc).TotalSeconds;
            if (quoteAge < 0 || quoteAge > MaxQuoteAgeSecondsForOrder)
                return "quote is stale ageSeconds=" + quoteAge;
            MarketQuoteSnapshot quote = ReadMarketQuote(symbol);
            DepthSnapshot depth = ReadDepthSnapshot(symbol, 25);
            if (!quote.Reliable || quote.Bid <= 0m || quote.Ask <= 0m)
                return "quote is unavailable or unreliable";
            if (depth.Analysis == null || !depth.Analysis.DepthReliable
                || depth.Analysis.DepthAgeSeconds > MaxDepthAgeSecondsForOrder)
                return "depth is unavailable or stale";
            if (depth.Analysis.BestBid >= depth.Analysis.BestAsk)
                return "crossed order book";
            if (depth.Analysis.BidSizeTop1 <= 0m || depth.Analysis.AskSizeTop1 <= 0m)
                return "depth sizes are not positive";
            if (side == "BUY" && depth.Analysis.SpreadPct > 0.50m)
                return "BUY spread exceeds gateway hard limit";
            decimal reference = side == "BUY" ? depth.Analysis.BestAsk : depth.Analysis.BestBid;
            decimal driftPct = Math.Abs(roundedLimitPrice - reference) / reference * 100m;
            if (driftPct > 0.75m)
                return "limit price drift exceeds 0.75 percent";
            return null;
        }

        private static bool IsOrderSessionOpen()
        {
            DateTime now = DateTime.Now;
            if (now.DayOfWeek == System.DayOfWeek.Saturday
                || now.DayOfWeek == System.DayOfWeek.Sunday)
                return false;
            TimeSpan time = now.TimeOfDay;
            return time >= new TimeSpan(9, 30, 0) && time <= new TimeSpan(18, 15, 0);
        }

        // v2: CheckModeGates (PAPER/MANUAL/DEMO_LIVE/REAL_LIVE) kaldırıldı.
        // Emir yetkisi tek kapıdan verilir: CheckDispatchGates (systemMode=
        // AUTO_TRADE + accountType tespiti + REAL arming). DEMO/REAL artık
        // çalışma modu değil, GetTradeUser().TestAutoOrder ile tespit edilen
        // accountType'tır.

        /// <summary>
        /// SELL üst sınırı: bot pozisyonundan kilitli uzun vade lotlar
        /// düşülür — kilitli lotlar hiçbir koşulda satılamaz.
        /// </summary>
        private decimal GetSellableQty(string symbol)
        {
            if (_lastPositionSyncUtc == DateTime.MinValue
                || (DateTime.UtcNow - _lastPositionSyncUtc).TotalSeconds > MaxPositionSyncAgeSeconds
                || (_positionSnapshotConfidence != "HIGH" && _positionSnapshotConfidence != "MEDIUM"))
                return 0m;
            decimal accountFree = GetAccountAvailableQty(symbol) - GetLockedLongTermQty(symbol);
            return Math.Max(0m, Math.Min(GetBotOwnedQty(symbol), accountFree));
        }

        private OrderExecutionResult SendGatewayLimitOrder(string requestId, string symbol, string side, decimal qty, decimal limitPrice)
        {
            if (!TryConvertOrderQuantity(qty, out int quantity, out string quantityError))
            {
                return new OrderExecutionResult { Success = false, OrderId = null, Message = quantityError };
            }

            if (quantity != qty)
                return new OrderExecutionResult { Success = false, Message = "qty has fractional component" };

            OrderSide orderSide = side == "BUY" ? OrderSide.Buy : OrderSide.Sell;
            ChartIcon chartIcon = orderSide == OrderSide.Buy ? ChartIcon.Buy : ChartIcon.Sell;
            TimeInForce timeInForce = ResolveTimeInForce();
            decimal roundedPrice = RoundPriceStepBistViop(symbol, limitPrice);
            if (roundedPrice <= 0m)
            {
                return new OrderExecutionResult { Success = false, OrderId = null, Message = "rounded limit price <= 0" };
            }

            if (roundedPrice != limitPrice)
            {
                SafeDebug("Limit price rounded symbol=" + symbol + " original=" + limitPrice + " rounded=" + roundedPrice);
            }

            var pending = new PendingOrderContext
            {
                RequestId = requestId,
                Symbol = symbol,
                Action = side,
                Qty = quantity,
                Price = roundedPrice
            };
            string symbolSideKey = BuildSymbolSideKey(symbol, side);
            if (!_pendingOrdersByRequestId.TryAdd(requestId, pending))
                return new OrderExecutionResult { Success = false, Message = "duplicate requestId" };
            if (!_pendingOrdersBySymbolSide.TryAdd(symbolSideKey, pending))
            {
                PendingOrderContext ignored;
                _pendingOrdersByRequestId.TryRemove(requestId, out ignored);
                return new OrderExecutionResult { Success = false, Message = "active pending order already exists for symbol and side" };
            }

            SafeDebug("Sending limit order: " + side
                + " " + symbol
                + " qty=" + quantity
                + " price=" + roundedPrice
                + " timeInForce=" + NormalizeTimeInForce(OrderTimeInForce));

            try
            {
                SendLimitOrder(symbol, quantity, orderSide, roundedPrice, timeInForce, chartIcon);
            }
            catch
            {
                // The broker may have accepted the call before throwing.  Do
                // not release the pending indexes or permit a resend.
                throw;
            }

            return new OrderExecutionResult
            {
                Success = true,
                OrderId = null,
                Message = "Limit order SENT_PENDING; final status will be reported by OnOrderUpdate"
            };
        }

        private PendingOrderContext? ResolvePendingOrderContext(string orderId, string symbol, string side)
        {
            if (!string.IsNullOrWhiteSpace(orderId)
                && _pendingOrdersByOrderId.TryGetValue(orderId, out var byOrderId))
            {
                return byOrderId;
            }

            string normalizedSide = NormalizeAction(side);
            string symbolSideKey = BuildSymbolSideKey(symbol, normalizedSide);
            if (_pendingOrdersBySymbolSide.TryGetValue(symbolSideKey, out var bySymbolSide))
            {
                if (!string.IsNullOrWhiteSpace(orderId))
                {
                    _pendingOrdersByOrderId[orderId] = bySymbolSide;
                }
                return bySymbolSide;
            }

            // Some Matriks builds stringify OrderSide as a numeric/enum value
            // that NormalizeOrderSide cannot map to BUY/SELL. A single pending
            // order for this symbol is still an unambiguous match.
            var symbolMatches = _pendingOrdersBySymbolSide.Values
                .Where(x => NormalizeSymbol(x.Symbol) == NormalizeSymbol(symbol))
                .ToList();
            if (symbolMatches.Count == 1)
            {
                PendingOrderContext bySymbol = symbolMatches[0];
                if (!string.IsNullOrWhiteSpace(orderId))
                {
                    _pendingOrdersByOrderId[orderId] = bySymbol;
                }
                return bySymbol;
            }

            if (symbolMatches.Count > 1)
                SafeDebug("Ambiguous pending order match; requestId not guessed symbol="
                    + symbol + " side=" + side + " candidates=" + symbolMatches.Count);

            return null;
        }

        private async Task ReportOrderResultAsync(
            PendingOrderContext context,
            string status,
            string matriksMessage,
            string orderId,
            decimal orderQty,
            decimal filledQty,
            decimal lastFillQty,
            decimal avgPrice,
            decimal limitPrice)
        {
            var payload = new OrderResultRequest
            {
                RequestId = context.RequestId,
                Symbol = NormalizeSymbol(context.Symbol),
                Action = NormalizeAction(context.Action),
                Qty = ToDouble(filledQty),
                Price = ToDouble(avgPrice > 0m ? avgPrice : limitPrice),
                OrderQty = ToDouble(orderQty),
                FilledQty = ToDouble(filledQty),
                LastFillQty = ToDouble(lastFillQty),
                AvgPrice = ToDouble(avgPrice),
                LimitPrice = ToDouble(limitPrice),
                Status = status,
                MatriksMessage = matriksMessage,
                OrderId = orderId
            };

            try
            {
                string json = JsonConvert.SerializeObject(payload, _jsonSettings);
                using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
                using (var result = await _http.PostAsync("api/order-result", content))
                {
                    string body = await result.Content.ReadAsStringAsync();
                    if (!result.IsSuccessStatusCode)
                    {
                        SafeDebug("Order result post failed HTTP " + (int)result.StatusCode + " body=" + body);
                        return;
                    }
                }

                SafeDebug("Order result posted status=" + status
                    + " requestId=" + context.RequestId
                    + " orderId=" + orderId);
            }
            catch (Exception ex)
            {
                SafeDebug("Order result post exception requestId=" + context.RequestId + " error=" + ex.Message);
            }
        }

        private void EnqueueOrderResult(OrderResultEnvelope item)
        {
            // requestId is stable before and after Matriks assigns orderId, so
            // all lifecycle events share one progression chain.
            string key = item.Context.RequestId;
            string previous;
            if (_lastReportedStatusByKey.TryGetValue(key, out previous))
            {
                if (previous == item.Status || IsFinalOrderStatus(previous)
                    || GetOrderStatusRank(item.Status) < GetOrderStatusRank(previous))
                    return;
            }
            _lastReportedStatusByKey[key] = item.Status;
            _orderResultQueue.Enqueue(item);
            _orderResultSignal.Release();
        }

        private async Task ProcessOrderResultQueueAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested || !_orderResultQueue.IsEmpty)
            {
                try { await _orderResultSignal.WaitAsync(token); }
                catch (OperationCanceledException) { }
                OrderResultEnvelope item;
                while (_orderResultQueue.TryDequeue(out item))
                    await ReportOrderResultAsync(item.Context, item.Status, item.MatriksMessage,
                        item.OrderId, item.OrderQty, item.FilledQty, item.LastFillQty,
                        item.AvgPrice, item.LimitPrice);
            }
        }

        private bool IsConfigStale()
        {
            return _lastConfigFetchUtc == DateTime.MinValue
                || (DateTime.UtcNow - _lastConfigFetchUtc).TotalSeconds > ConfigStaleSeconds;
        }

        private void CleanupIdempotencyCache()
        {
            DateTime cutoff = DateTime.UtcNow.Subtract(IdempotencyTtl);
            foreach (var item in _sentRequestIds)
            {
                IdempotencyEntry ignored;
                if (item.Value.CreatedUtc < cutoff)
                    _sentRequestIds.TryRemove(item.Key, out ignored);
            }
        }

        private void RollbackOrderReservation(string requestId, string symbol, string side, bool decrementCounter)
        {
            PendingOrderContext ignored;
            _pendingOrdersByRequestId.TryRemove(requestId, out ignored);
            string key = BuildSymbolSideKey(symbol, side);
            PendingOrderContext active;
            if (_pendingOrdersBySymbolSide.TryGetValue(key, out active) && active.RequestId == requestId)
                _pendingOrdersBySymbolSide.TryRemove(key, out ignored);
            IdempotencyEntry idempotency;
            _sentRequestIds.TryRemove(requestId, out idempotency); // synchronous failure is retryable
            if (decrementCounter)
            {
                int count;
                if (_dailyTradeCountBySymbol.TryGetValue(symbol, out count) && count > 0)
                    _dailyTradeCountBySymbol[symbol] = count - 1;
            }
        }

        private void IncrementDailyTradeCount(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            _dailyTradeCountBySymbol.AddOrUpdate(symbol, 1, (_, existing) => existing + 1);
        }

        private int GetDailyTradeCount(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            return _dailyTradeCountBySymbol.TryGetValue(symbol, out var count) ? count : 0;
        }

        private int GetTotalDailyOrderCount()
        {
            return _dailyTradeCountBySymbol.Values.Sum();
        }

        private static string BuildFallbackRequestId(string symbol)
        {
            return NormalizeSymbol(symbol) + "-" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
        }

        private static string BuildSymbolSideKey(string symbol, string side)
        {
            return NormalizeSymbol(symbol) + "|" + NormalizeAction(side);
        }

        private static bool IsFinalOrderStatus(string status)
        {
            string value = (status ?? "").Trim().ToUpperInvariant();
            return value == "FILLED" || value == "CANCELED" || value == "CANCELLED"
                || value == "REJECTED" || value == "EXPIRED" || value == "ERROR";
        }

        /// <summary>
        /// Re-check the only compile-safe account API immediately before a
        /// DEMO_LIVE order when the cached result is older than five seconds.
        /// A failed read never reuses a previous successful verification.
        /// </summary>
        private bool VerifyDemoAccountFresh()
        {
            if ((DateTime.UtcNow - _lastAccountVerificationUtc).TotalSeconds <= AccountVerificationMaxAgeSeconds)
                return _demoAccountVerified;

            try
            {
                var tradeUser = GetTradeUser();
                if (tradeUser == null)
                    throw new InvalidOperationException("GetTradeUser returned null");

                string accountId = Convert.ToString(tradeUser.AccountId) ?? string.Empty;
                bool testAutoOrder = tradeUser.TestAutoOrder;
                bool accountChanged = !string.IsNullOrWhiteSpace(_lastVerifiedAccountId)
                    && !string.Equals(_lastVerifiedAccountId, accountId, StringComparison.Ordinal);
                if (accountChanged)
                    SafeDebug("Demo account changed from " + MaskAccountId(_lastVerifiedAccountId) + " to " + MaskAccountId(accountId));

                _autoOrderEnabled = tradeUser.AutoOrder;
                _testAutoOrderEnabled = testAutoOrder;
                _lastVerifiedAccountId = accountId;
                // v2 hesap kimliği raporlama: ham id asla dışarı çıkmaz, sadece
                // sha256 referansları. SessionRef, hesap kimliği + algo başlangıç
                // anından türetilir: hesap değişiminde ve gateway yeniden
                // başlatıldığında değişir. Matriks SDK'sı aynı hesapla yeniden
                // login'i ayrıca raporlamadığı için bu, erişilebilir en güçlü
                // oturum sinyalidir.
                _lastVerifiedAccountRef = Sha256Hex(accountId);
                _lastVerifiedSessionRef = Sha256Hex(accountId + "|" + _startedAt.Ticks.ToString());
                _lastVerifiedAccountType = testAutoOrder ? "DEMO" : "REAL";
                _lastAccountChanged = accountChanged;
                _lastAccountVerificationUtc = DateTime.UtcNow;
                // Reject the first order after an account switch. A new
                // verification is required before a subsequent order can run.
                _demoAccountVerified = DemoAccountConfirmed && testAutoOrder && !accountChanged;
                return _demoAccountVerified;
            }
            catch (Exception ex)
            {
                _demoAccountVerified = false;
                _lastAccountVerificationUtc = DateTime.MinValue;
                _lastVerifiedAccountType = "UNKNOWN";
                SafeDebug("Demo account verification failed: " + ex.Message);
                return false;
            }
        }

        private sealed class AccountVerification
        {
            public string AccountRef;
            public string SessionRef;
            public string AccountType; // DEMO | REAL | UNKNOWN
            public bool AccountChanged;
        }

        /// <summary>
        /// v2 hesap doğrulaması: VerifyDemoAccountFresh ile AYNI 5 saniyelik
        /// tazelik penceresini ve GetTradeUser okumasını paylaşır; başarısız
        /// veya bayat doğrulamada null döner (fail-closed).
        /// </summary>
        private AccountVerification VerifyAccountFresh()
        {
            if ((DateTime.UtcNow - _lastAccountVerificationUtc).TotalSeconds > AccountVerificationMaxAgeSeconds)
                VerifyDemoAccountFresh();
            if (_lastAccountVerificationUtc == DateTime.MinValue
                || (DateTime.UtcNow - _lastAccountVerificationUtc).TotalSeconds > AccountVerificationMaxAgeSeconds
                || string.IsNullOrEmpty(_lastVerifiedAccountRef)
                || _lastVerifiedAccountType == "UNKNOWN")
                return null;
            return new AccountVerification
            {
                AccountRef = _lastVerifiedAccountRef,
                SessionRef = _lastVerifiedSessionRef,
                AccountType = _lastVerifiedAccountType,
                AccountChanged = _lastAccountChanged
            };
        }

        /// <summary>
        /// v2 emir kapısı (contractVersion=2): SystemMode + otomatik hesap
        /// türü tespiti. DEMO hesapta AUTO_TRADE serbest akar; REAL hesap
        /// yalnızca RealAccountArmed VE gateway'in kendi hesapladığı
        /// accountRef == ArmedAccountRef (aynı sha256 formatı, tekrar hash
        /// yok) iken emir gönderebilir. Her belirsizlik fail-closed bloktur.
        /// </summary>
        private string CheckDispatchGates()
        {
            if (SystemMode != "AUTO_TRADE")
                return "SystemMode=OBSERVE_ONLY — dispatch disabled";
            var acct = VerifyAccountFresh();
            if (acct == null)
                return "account verification failed or stale";
            if (acct.AccountChanged)
                return "account changed since last verification — order rejected";
            if (acct.AccountType == "DEMO")
                return null;
            if (!RealAccountArmed)
                return "REAL account blocked: not armed";
            if (!string.Equals(acct.AccountRef, ArmedAccountRef, StringComparison.OrdinalIgnoreCase))
                return "REAL account blocked: armed account mismatch";
            return null;
        }

        private static string NormalizeSystemMode(string mode)
        {
            string value = (mode ?? "").Trim().ToUpperInvariant();
            // Bilinmeyen/eksik değer fail-closed: OBSERVE_ONLY.
            return value == "AUTO_TRADE" ? "AUTO_TRADE" : "OBSERVE_ONLY";
        }

        private static string Sha256Hex(string value)
        {
            using (var sha = System.Security.Cryptography.SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(value ?? string.Empty));
                var builder = new StringBuilder(hash.Length * 2);
                foreach (byte b in hash)
                    builder.Append(b.ToString("x2"));
                return builder.ToString();
            }
        }

        private static int GetOrderStatusRank(string status)
        {
            string value = (status ?? "").Trim().ToUpperInvariant();
            if (IsFinalOrderStatus(value)) return 100;
            if (value == "PARTIALLY_FILLED") return 40;
            if (value == "CANCEL_REQUESTED") return 30;
            if (value == "SENT") return 25;
            if (value == "NEW") return 20;
            if (value == "SENT_PENDING") return 10;
            return 0;
        }

        private static bool TryConvertOrderQuantity(decimal qty, out int quantity, out string error)
        {
            quantity = 0;
            error = null;

            if (qty <= 0m)
            {
                error = "qty <= 0";
                return false;
            }

            if (qty > int.MaxValue)
            {
                error = "qty exceeds Int32 max: " + qty;
                return false;
            }

            decimal truncated = decimal.Truncate(qty);
            if (truncated <= 0m)
            {
                error = "qty becomes <= 0 after integer conversion: " + qty;
                return false;
            }

            quantity = Convert.ToInt32(truncated);
            return true;
        }

        private TimeInForce ResolveTimeInForce()
        {
            string value = NormalizeTimeInForce(OrderTimeInForce);
            if (value == "GOOD_TILL_CANCEL")
                return new TimeInForce(TimeInForce.GoodTillCancel);
            return new TimeInForce(TimeInForce.Day);
        }

        // ── Market data collection (TradeAiAgenticBot'tan birebir) ───

        private DepthSnapshot ReadDepthSnapshot(string symbol, int requestedLevels)
        {
            DateTime readStartedUtc = DateTime.UtcNow;
            int levels = Math.Max(1, Math.Min(25, requestedLevels));
            var result = new DepthSnapshot { Bids = new List<DepthLevelSnapshot>(), Asks = new List<DepthLevelSnapshot>() };
            var depth = GetMarketDepth(symbol);
            if (depth != null && depth.BidRows != null)
                result.Bids = depth.BidRows.Take(levels).Select((row, index) => new DepthLevelSnapshot { Level = index + 1, Price = row.Price, Size = row.Size, OrderCount = row.OrderCount }).ToList();
            if (depth != null && depth.AskRows != null)
                result.Asks = depth.AskRows.Take(levels).Select((row, index) => new DepthLevelSnapshot { Level = index + 1, Price = row.Price, Size = row.Size, OrderCount = row.OrderCount }).ToList();
            result.Analysis = AnalyzeDepth(result.Bids, result.Asks);
            // GetMarketDepth is a synchronous poll, not a push event, and
            // Matriks exposes no depth-specific event timestamp anywhere in
            // this SDK surface (no OnDepthUpdate/OnMarketDepthChanged
            // callback, no timestamp field on the row objects) - read time
            // must never masquerade as depth freshness.
            //
            // The best REAL signal available instead: _lastTradeUtcBySymbol,
            // populated only from the genuine OnDataUpdate push event
            // (barData.LastTickTime) for this symbol. Quote ticks and the
            // depth/order-book cache are delivered by the same live Matriks
            // subscription for a subscribed symbol, so a fresh quote tick is
            // real, verifiable evidence the depth cache is correspondingly
            // current - it is a same-session proxy for depth freshness, not
            // a depth-specific guarantee, and it never overrides
            // AnalyzeDepth's own structural reliability checks (bid<ask,
            // positive sizes) above, which still gate DepthReliable
            // independently. ValidateOrderMarketData already requires the
            // quote tick itself to be within MaxQuoteAgeSecondsForOrder (15s)
            // before this function is even called, so this proxy age can
            // never appear fresher than that already-verified bound.
            DateTime readCompletedUtc = DateTime.UtcNow;
            DateTime lastQuoteEventUtc;
            bool hasQuoteEvent = _lastTradeUtcBySymbol.TryGetValue(symbol, out lastQuoteEventUtc);
            if (hasQuoteEvent)
            {
                double ageFromQuoteTickSeconds = Math.Max(
                    0.0, (readCompletedUtc - lastQuoteEventUtc).TotalSeconds);
                result.Analysis.DepthTimestamp = lastQuoteEventUtc.ToString("o");
                result.Analysis.DepthAgeSeconds = ageFromQuoteTickSeconds;
                result.Analysis.DepthTimestampSource = "SAME_SESSION_QUOTE_TICK_TIME";
                result.Analysis.DepthEventTimestampAvailable = true;
                // DepthReliable keeps whatever AnalyzeDepth computed from the
                // real bid/ask structure above - never forced true here.
            }
            else
            {
                result.Analysis.DepthTimestamp = null;
                result.Analysis.DepthAgeSeconds = double.MaxValue;
                result.Analysis.DepthTimestampSource = "READ_TIME_ONLY";
                result.Analysis.DepthEventTimestampAvailable = false;
                // No real freshness signal at all for this symbol - fail closed.
                result.Analysis.DepthReliable = false;
            }
            result.Analysis.DepthReadUtc = readCompletedUtc.ToString("o");
            result.Analysis.DepthReadLatencySeconds = Math.Max(0.0, (readCompletedUtc - readStartedUtc).TotalSeconds);
            return result;
        }

        private static Dictionary<string, object> BuildTradeUserAccountPayload(object user)
        {
            var result = ReflectToDict(user);
            var marketAccounts = new List<Dictionary<string, object>>();
            Dictionary<string, object> selected = null;
            Dictionary<string, object> first = null;
            object accountsValue = ReadPublicMember(user, "Accounts");
            var accounts = accountsValue as System.Collections.IEnumerable;
            if (accounts != null && !(accountsValue is string))
            {
                int index = 0;
                foreach (object item in accounts)
                {
                    if (item == null || index >= 20)
                        break;
                    var entry = new Dictionary<string, object>
                    {
                        { "Index", index },
                        { "ExchangeID", ReadPublicMember(item, "ExchangeID") },
                        { "Overall", ReadPublicMember(item, "Overall") },
                        { "AvailableMargin", ReadPublicMember(item, "AvailableMargin") }
                    };
                    marketAccounts.Add(entry);
                    if (first == null)
                        first = entry;
                    string exchange = Convert.ToString(entry["ExchangeID"]) ?? string.Empty;
                    if (selected == null && IsBistAccountExchange(exchange))
                        selected = entry;
                    index++;
                }
            }

            // Matriks' official account example defines Accounts[0] as BIST and
            // Accounts[1] as VIOP. Prefer the explicit exchange label and use
            // that documented first-entry rule only when the label is unavailable.
            string selectionPolicy = "BIST_EXCHANGE_LABEL";
            if (selected == null)
            {
                selected = first;
                selectionPolicy = "DOCUMENTED_BIST_FIRST_ACCOUNT";
            }
            result["Accounts"] = marketAccounts;
            result["AccountSelectionPolicy"] = selectionPolicy;
            if (selected != null)
            {
                result["ExchangeID"] = selected["ExchangeID"];
                result["Overall"] = selected["Overall"];
                result["AvailableMargin"] = selected["AvailableMargin"];
            }
            return result;
        }

        private static object ReadPublicMember(object obj, string name)
        {
            if (obj == null || string.IsNullOrWhiteSpace(name))
                return null;
            try
            {
                Type type = obj.GetType();
                PropertyInfo property = type.GetProperty(
                    name,
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.IgnoreCase);
                if (property != null && property.GetIndexParameters().Length == 0)
                    return property.GetValue(obj, null);
                FieldInfo field = type.GetField(
                    name,
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.IgnoreCase);
                return field == null ? null : field.GetValue(obj);
            }
            catch
            {
                return null;
            }
        }

        private static bool IsBistAccountExchange(string exchange)
        {
            string value = (exchange ?? string.Empty).Trim().ToUpperInvariant();
            return value.Contains("BIST")
                || value.Contains("BORSAISTANBUL")
                || value == "PAY"
                || value == "STOCK";
        }

        private static bool TryGetFiniteDecimal(
            Dictionary<string, object> values,
            string key,
            out decimal parsed)
        {
            parsed = 0m;
            object raw;
            if (values == null || !values.TryGetValue(key, out raw) || raw == null)
                return false;
            try
            {
                parsed = Convert.ToDecimal(raw);
                return true;
            }
            catch
            {
                return false;
            }
        }

        private static DepthAnalysisSnapshot AnalyzeDepth(List<DepthLevelSnapshot> bids, List<DepthLevelSnapshot> asks)
        {
            var a = new DepthAnalysisSnapshot { DepthTimestamp = null, OrderBookSignal = "UNAVAILABLE" };
            a.LevelsUsed = Math.Min(25, Math.Max(bids.Count, asks.Count));
            a.Available = bids.Count > 0 && asks.Count > 0;
            a.DepthReliable = a.Available && bids[0].Price > 0m && asks[0].Price > 0m
                && bids[0].Size > 0m && asks[0].Size > 0m && bids[0].Price < asks[0].Price;
            if (!a.DepthReliable) return a;
            a.BestBid = bids[0].Price; a.BestAsk = asks[0].Price;
            a.BidSizeTop1 = bids[0].Size; a.AskSizeTop1 = asks[0].Size;
            a.Spread = Math.Max(0m, a.BestAsk - a.BestBid);
            a.SpreadPct = SafePercent(a.Spread, (a.BestAsk + a.BestBid) / 2m);
            a.Top1 = SummarizeDepth(bids, asks, 1); a.Top3 = SummarizeDepth(bids, asks, 3);
            a.Top5 = SummarizeDepth(bids, asks, 5); a.Top10 = SummarizeDepth(bids, asks, 10); a.Top25 = SummarizeDepth(bids, asks, 25);
            a.BidAskRatioTop5 = a.Top5.BidAskRatio; a.BidAskRatioTop10 = a.Top10.BidAskRatio; a.BidAskRatioTop25 = a.Top25.BidAskRatio;
            a.ImbalanceTop5 = a.Top5.Imbalance; a.ImbalanceTop10 = a.Top10.Imbalance; a.ImbalanceTop25 = a.Top25.Imbalance;
            a.WeightedBidPrice = WeightedDepthPrice(bids); a.WeightedAskPrice = WeightedDepthPrice(asks);
            a.BidConcentrationTop3Pct = SafePercent(a.Top3.TotalBidSize, a.Top25.TotalBidSize);
            a.AskConcentrationTop3Pct = SafePercent(a.Top3.TotalAskSize, a.Top25.TotalAskSize);
            a.BidWallConcentrationRisk = a.BidConcentrationTop3Pct >= 70m; a.AskWallConcentrationRisk = a.AskConcentrationTop3Pct >= 70m;
            decimal referencePrice = (a.BestBid + a.BestAsk) / 2m;
            List<DepthWallSnapshot> bw = FindDepthWalls(bids, referencePrice); List<DepthWallSnapshot> aw = FindDepthWalls(asks, referencePrice);
            a.LargestBidWall = bw.OrderByDescending(x => x.Size).FirstOrDefault(); a.LargestAskWall = aw.OrderByDescending(x => x.Size).FirstOrDefault();
            a.NearestLargeBidWall = bw.OrderBy(x => Math.Abs(x.DistancePct)).FirstOrDefault(); a.NearestLargeAskWall = aw.OrderBy(x => Math.Abs(x.DistancePct)).FirstOrDefault();
            a.BidWallCountTop5 = bw.Count(x => x.Level <= 5); a.AskWallCountTop5 = aw.Count(x => x.Level <= 5);
            a.BidWallCountTop10 = bw.Count(x => x.Level <= 10); a.AskWallCountTop10 = aw.Count(x => x.Level <= 10);
            decimal ratio = a.BidAskRatioTop10;
            decimal buy = Math.Max(0m, Math.Min(100m, ratio <= 0m ? 0m : ratio / (ratio + 1m) * 100m));
            bool cb = a.ImbalanceTop5 > 0m && a.ImbalanceTop10 > 0m && a.ImbalanceTop25 > 0m;
            bool cs = a.ImbalanceTop5 < 0m && a.ImbalanceTop10 < 0m && a.ImbalanceTop25 < 0m;
            if (cb) buy += 8m; if (a.AskWallConcentrationRisk) buy -= 8m;
            a.BuyPressureScore = Math.Max(0m, Math.Min(100m, buy)); a.SellPressureScore = Math.Max(0m, Math.Min(100m, 100m - buy));
            if (ratio >= 2m && cb && !a.BidWallConcentrationRisk) a.OrderBookSignal = "STRONG_BUY_PRESSURE";
            else if (ratio >= 1.25m && a.ImbalanceTop10 > 0m) a.OrderBookSignal = "BUY_PRESSURE";
            else if (ratio <= 0.5m && cs && !a.AskWallConcentrationRisk) a.OrderBookSignal = "STRONG_SELL_PRESSURE";
            else if (ratio <= 0.8m && a.ImbalanceTop10 < 0m) a.OrderBookSignal = "SELL_PRESSURE";
            else a.OrderBookSignal = "BALANCED";
            return a;
        }

        private static DepthBandSnapshot SummarizeDepth(List<DepthLevelSnapshot> bids, List<DepthLevelSnapshot> asks, int levels)
        {
            var r = new DepthBandSnapshot();
            r.TotalBidSize = bids.Take(levels).Sum(x => x.Size); r.TotalAskSize = asks.Take(levels).Sum(x => x.Size);
            r.BidOrderCount = bids.Take(levels).Sum(x => x.OrderCount); r.AskOrderCount = asks.Take(levels).Sum(x => x.OrderCount);
            r.BidAskRatio = r.TotalAskSize > 0m ? r.TotalBidSize / r.TotalAskSize : (r.TotalBidSize > 0m ? 100m : 0m);
            decimal total = r.TotalBidSize + r.TotalAskSize; r.Imbalance = total > 0m ? (r.TotalBidSize - r.TotalAskSize) / total : 0m;
            r.AverageBidOrderSize = r.BidOrderCount > 0 ? r.TotalBidSize / r.BidOrderCount : 0m;
            r.AverageAskOrderSize = r.AskOrderCount > 0 ? r.TotalAskSize / r.AskOrderCount : 0m;
            return r;
        }

        private static decimal WeightedDepthPrice(List<DepthLevelSnapshot> rows)
        { decimal size = rows.Sum(x => x.Size); return size > 0m ? rows.Sum(x => x.Price * x.Size) / size : 0m; }

        private static List<DepthWallSnapshot> FindDepthWalls(List<DepthLevelSnapshot> rows, decimal referencePrice)
        {
            var result = new List<DepthWallSnapshot>(); if (rows.Count == 0) return result;
            List<decimal> sizes = rows.Select(x => x.Size).OrderBy(x => x).ToList();
            decimal median = sizes.Count % 2 == 1 ? sizes[sizes.Count / 2] : (sizes[sizes.Count / 2 - 1] + sizes[sizes.Count / 2]) / 2m;
            decimal threshold = Math.Max(median * 3m, sizes.Average() * 2.5m);
            foreach (DepthLevelSnapshot row in rows.Where(x => x.Size >= threshold && x.Size > 0m))
                result.Add(new DepthWallSnapshot { Level = row.Level, Price = row.Price, Size = row.Size, OrderCount = row.OrderCount, DistancePct = referencePrice > 0m ? (row.Price - referencePrice) / referencePrice * 100m : 0m });
            return result;
        }

        private static decimal SafePercent(decimal numerator, decimal denominator)
        { return denominator > 0m ? numerator / denominator * 100m : 0m; }

        private MarketDataPayload BuildMarketData(string symbolRaw, string dataType, string requestedTimeframe = null)
        {
            string symbol = NormalizeSymbol(symbolRaw);
            bool isIndexSymbol = IsIndexSymbol(symbol);
            bool supportsEquityDepth = IsEquitySymbol(symbol);

            MarketQuoteSnapshot quote = ReadMarketQuote(symbol);
            decimal lastPrice = quote.Last;
            decimal bidPrice = quote.Bid;
            decimal askPrice = quote.Ask;
            decimal totalVol = quote.TotalVol;

            OhlcvSnapshot ohlc = ResolveOhlcvSnapshot(symbol, lastPrice, 0m);
            if (lastPrice <= 0m && ohlc.Close > 0m)
            {
                lastPrice = ohlc.Close;
            }
            decimal barVolume = ohlc.Volume;
            decimal open = ohlc.Open;
            decimal high = ohlc.High;
            decimal low = ohlc.Low;
            bool ohlcReliable = ohlc.Reliable;


            // Depth data: one read, shared aggregate model; raw levels are not
            // included in /snapshot to keep the AI payload token-efficient.
            decimal bestBid = 0m;
            decimal secondBid = 0m;
            decimal thirdBid = 0m;
            decimal bid1Size = 0m;
            decimal ask1Size = 0m;
            decimal maxBid1Size = 0m;
            decimal depthQueueDropPct = 0m;
            bool depthReliable = false;
            string depthSummary = "";
            string depthSkipReason = null;
            DepthAnalysisSnapshot depthAnalysis = new DepthAnalysisSnapshot { OrderBookSignal = "UNAVAILABLE", DepthReliable = false };
            if (!supportsEquityDepth)
            {
                depthSkipReason = isIndexSymbol
                    ? IndexDepthSkipReason
                    : "INSTRUMENT_ADAPTER_REQUIRED";
                depthSummary = "depth skipped: " + depthSkipReason;
            }
            else
            {
                try
                {
                    DepthSnapshot depth = ReadDepthSnapshot(symbol, 25);
                    depthAnalysis = depth.Analysis;
                    bestBid = depthAnalysis.BestBid;
                    bid1Size = depthAnalysis.BidSizeTop1;
                    ask1Size = depthAnalysis.AskSizeTop1;
                    if (depth.Bids.Count >= 2) secondBid = depth.Bids[1].Price;
                    if (depth.Bids.Count >= 3) thirdBid = depth.Bids[2].Price;
                    if (bestBid > 0m && bid1Size > 0m)
                    {
                        maxBid1Size = _maxBid1SizeBySymbol.AddOrUpdate(
                            symbol,
                            bid1Size,
                            (_, existing) => bid1Size > existing ? bid1Size : existing);

                        if (maxBid1Size > 0m)
                        {
                            depthQueueDropPct = Math.Max(0m, (maxBid1Size - bid1Size) / maxBid1Size * 100m);
                        }
                    }
                    depthReliable = depthAnalysis.DepthReliable;
                    if (!depthReliable)
                    {
                        depthQueueDropPct = 0m;
                        // A valid book can still be marked unreliable solely because
                        // its last event is older than the order-time freshness limit.
                        // That state remains fail-closed but is not a malformed-depth
                        // warning. Empty, zero-sized and crossed books still warn.
                        bool depthUnavailableOrInvalid = !depthAnalysis.Available
                            || depthAnalysis.BestBid <= 0m
                            || depthAnalysis.BestAsk <= 0m
                            || depthAnalysis.BidSizeTop1 <= 0m
                            || depthAnalysis.AskSizeTop1 <= 0m
                            || depthAnalysis.BestBid >= depthAnalysis.BestAsk;
                        if (depthUnavailableOrInvalid)
                            LogMarketDataWarning(symbol, "DEPTH", "Depth unavailable or invalid; depthReliable=false");
                    }

                    depthSummary = "bestBid=" + bestBid
                        + ";secondBid=" + secondBid
                        + ";thirdBid=" + thirdBid
                        + ";bid1Size=" + bid1Size
                        + ";maxBid1Size=" + maxBid1Size
                        + ";depthQueueDropPct=" + depthQueueDropPct
                        + ";depthReliable=" + depthReliable;
                }
                catch (Exception ex)
                {
                    depthSummary = "depth unavailable: " + ex.Message;
                    depthReliable = false;
                    LogMarketDataWarning(symbol, "DEPTH", ex.Message);
                }
            }

            double? rsi = GetNativeRsi(symbol) ?? CalculateRsi(symbol, 14);
            double? ema20 = GetNativeEma20(symbol) ?? CalculateEma(symbol, 20);
            double? ema50 = GetNativeEma50(symbol) ?? CalculateEma(symbol, 50);
            double? macd = GetNativeMacdLine(symbol) ?? CalculateMacdLine(symbol);
            double? macdSignal = GetNativeMacdSignal(symbol) ?? CalculateMacdSignal(symbol);
            string indicatorSource = ResolveIndicatorSource(rsi, ema20, ema50, macd, macdSignal);
            string instrumentType = ResolveInstrumentType(symbol);
            string configuredPeriod = NormalizePeriodName(IndicatorPeriod.ToString());
            string requestedPeriod = NormalizePeriodName(
                string.IsNullOrWhiteSpace(requestedTimeframe)
                    ? configuredPeriod
                    : requestedTimeframe);
            string actualBarPeriod = ohlc.ActualBarPeriod;
            int? actualBarPeriodSeconds = ohlc.ActualBarPeriodSeconds;
            bool timeframeMismatch = !string.IsNullOrWhiteSpace(actualBarPeriod)
                && !PeriodsEquivalent(requestedPeriod, actualBarPeriod);
            decimal? volumeIndicatorValue = ReadIndicatorCurrentValue(_volumeIndicatorBySymbol, symbol);
            decimal? volumeTlIndicatorValue = ReadIndicatorCurrentValue(_volumeTlIndicatorBySymbol, symbol);
            var technicalFeatures = BuildTechnicalFeaturePayload(
                symbol,
                lastPrice,
                rsi,
                ema20,
                ema50,
                macd,
                macdSignal,
                indicatorSource,
                bid1Size,
                maxBid1Size,
                depthQueueDropPct,
                depthReliable);

            var payload = new Dictionary<string, object>();
            payload["marketDataContractVersion"] = "matriks-market-data-v2";
            payload["schemaVersion"] = "technical-features-v2";
            payload["deprecatedFields"] = new[] { "volume", "timeframe", "marketRegime", "dailyTradeCount", "botPositionQty", "totalAccountQty" };
            payload["instrumentType"] = instrumentType;
            payload["requestedTimeframe"] = requestedPeriod;
            payload["actualBarPeriod"] = actualBarPeriod;
            payload["actualBarPeriodSeconds"] = actualBarPeriodSeconds;
            payload["barPeriodSource"] = ohlc.BarPeriodSource;
            payload["timeframeMismatch"] = timeframeMismatch;
            payload["timeframe"] = string.IsNullOrWhiteSpace(actualBarPeriod) ? "UNKNOWN" : actualBarPeriod;
            payload["indicatorPeriod"] = configuredPeriod;
            payload["indicatorPeriodSeconds"] = PeriodSeconds(configuredPeriod);
            payload["indicatorPeriodSource"] = "ACTIVE_TRADE_PROFILE";
            payload["ohlcPeriod"] = actualBarPeriod;
            payload["barVolumePeriod"] = actualBarPeriod;
            payload["lastPrice"] = ToDouble(lastPrice);
            payload["open"] = ToDouble(open);
            payload["high"] = ToDouble(high);
            payload["low"] = ToDouble(low);
            payload["barOpen"] = ToDouble(open);
            payload["barHigh"] = ToDouble(high);
            payload["barLow"] = ToDouble(low);
            payload["barClose"] = ToDouble(ohlc.Close);
            payload["ohlcReliable"] = ohlcReliable;
            payload["ohlcSource"] = ohlc.Source;
            payload["priceSource"] = quote.Source;
            payload["quoteReliable"] = quote.Reliable;
            payload["quoteAvailable"] = quote.Last > 0m || (quote.Bid > 0m && quote.Ask > 0m);
            payload["quoteFresh"] = quote.Reliable && quote.LastTradeUtc != DateTime.MinValue
                && (DateTime.UtcNow - quote.LastTradeUtc).TotalSeconds <= MaxQuoteAgeSecondsForOrder;
            payload["lastTradeUtc"] = quote.LastTradeUtc == DateTime.MinValue ? null : quote.LastTradeUtc.ToString("o");
            payload["quoteReadUtc"] = quote.ReadUtc == DateTime.MinValue ? null : quote.ReadUtc.ToString("o");
            payload["quoteTimestampSource"] = quote.TimestampSource;
            payload["depthReadUtc"] = depthAnalysis.DepthReadUtc;
            payload["depthTimestampSource"] = depthAnalysis.DepthTimestampSource;
            payload["depthEventTimestampAvailable"] = depthAnalysis.DepthEventTimestampAvailable;
            payload["depthReadLatencySeconds"] = depthAnalysis.DepthReadLatencySeconds;
            payload["barEventUtc"] = ohlc.BarEventUtc == DateTime.MinValue ? null : ohlc.BarEventUtc.ToString("o");
            payload["barTimestampSource"] = ohlc.BarTimestampSource;
            payload["barTimeReliable"] = ohlc.BarTimeReliable;
            payload["barTimestampFallbackObservationUtc"] = !ohlc.BarTimeReliable && ohlc.ResolvedTimestamp != DateTime.MinValue
                ? ohlc.ResolvedTimestamp.ToUniversalTime().ToString("o")
                : null;
            payload["barTimestampFallbackObservationSource"] = ohlc.BarObservationSource;
            payload["snapshotBuiltUtc"] = DateTime.UtcNow.ToString("o");
            payload["sessionOpen"] = IsOrderSessionOpen();
            payload["depthAvailable"] = supportsEquityDepth && depthAnalysis.Available;
            payload["depthReliable"] = depthReliable;
            payload["depthSkipReason"] = depthSkipReason;
            // Compatibility alias: from v2 onward `volume` always means the
            // current Matriks bar volume. TotalVol is cumulative session TL
            // turnover and is deliberately published under a separate name.
            payload["volume"] = ToDouble(barVolume);
            payload["volumeSemantic"] = "BAR_VOLUME";
            payload["barVolume"] = ToDouble(barVolume);
            payload["barVolumeSource"] = ohlc.Source == "QUOTE_FALLBACK"
                ? "UNAVAILABLE"
                : "BarData.BarData.Volume";
            payload["barVolumeUnit"] = "LOTS";
            payload["barVolumeReliable"] = ohlc.Reliable && ohlc.Source != "QUOTE_FALLBACK";
            payload["totalVol"] = supportsEquityDepth ? (object)ToDouble(totalVol) : null;
            payload["sessionTurnoverTl"] = supportsEquityDepth ? (object)ToDouble(totalVol) : null;
            payload["totalVolSemantic"] = "CUMULATIVE_SESSION_TURNOVER_TL";
            payload["totalVolSource"] = supportsEquityDepth ? "SymbolUpdateField.TotalVol" : "NOT_APPLICABLE";
            payload["totalVolUnit"] = "TRY";
            payload["totalVolReliable"] = supportsEquityDepth && totalVol > 0m;
            payload["volumeIndicatorValue"] = volumeIndicatorValue;
            payload["volumeIndicatorSource"] = volumeIndicatorValue.HasValue
                ? "VolumeIndicator.CurrentValue"
                : "UNAVAILABLE";
            payload["volumeIndicatorUnit"] = "LOTS";
            payload["volumeTlIndicatorValue"] = volumeTlIndicatorValue;
            payload["volumeTlIndicatorSource"] = volumeTlIndicatorValue.HasValue
                ? "VolumeTLIndicator.CurrentValue"
                : "UNAVAILABLE";
            payload["volumeTlIndicatorUnit"] = "TRY";
            payload["barClosed"] = ohlc.BarClosed;
            payload["barIsNew"] = ohlc.BarIsNew;
            payload["barDataIndex"] = ohlc.BarDataIndex;
            payload["lastTickTime"] = ohlc.LastTickTime == DateTime.MinValue
                ? null
                : ohlc.LastTickTime.ToUniversalTime().ToString("o");
            payload["rsi"] = rsi;
            payload["ema20"] = ema20;
            payload["ema50"] = ema50;
            payload["macd"] = macd;
            payload["macdSignal"] = macdSignal;
            payload["indicatorSource"] = indicatorSource;
            payload["bidPrice"] = ToDouble(bidPrice);
            payload["askPrice"] = ToDouble(askPrice);
            payload["bidVolume"] = ToDouble(bid1Size);
            payload["askVolume"] = ToDouble(ask1Size);
            payload["bestBid"] = ToDouble(bestBid);
            payload["secondBid"] = ToDouble(secondBid);
            payload["thirdBid"] = ToDouble(thirdBid);
            payload["depthSummary"] = depthSummary;
            payload["depthAnalysis"] = depthAnalysis;
            payload["quoteAgeSeconds"] = quote.LastTradeUtc == DateTime.MinValue
                ? null
                : (object)Math.Max(0.0, (DateTime.UtcNow - quote.LastTradeUtc).TotalSeconds);
            payload["ohlcvAgeSeconds"] = ohlc.BarEventUtc == DateTime.MinValue
                ? null
                : (object)Math.Max(0.0, (DateTime.UtcNow - ohlc.BarEventUtc).TotalSeconds);
            payload["depthAgeSeconds"] = null;
            decimal botOwnedQty = GetBotOwnedQty(symbol);
            decimal accountNetQty = GetTotalAccountQty(symbol);
            decimal accountAvailableQty = GetAccountAvailableQty(symbol);
            payload["botPositionQty"] = ToDouble(botOwnedQty);
            payload["totalAccountQty"] = ToDouble(accountNetQty);
            payload["accountAvailableQty"] = ToDouble(accountAvailableQty);
            payload["lockedLongTermQty"] = ToDouble(GetLockedLongTermQty(symbol));
            PositionMarketSnapshot positionMarket;
            bool hasPositionMarket = _positionMarketBySymbol.TryGetValue(symbol, out positionMarket);
            decimal accountAvgCost = hasPositionMarket ? positionMarket.AvgCost : 0m;
            decimal currentPositionPrice = hasPositionMarket && positionMarket.SettlementPx > 0m
                ? positionMarket.SettlementPx
                : lastPrice;
            decimal accountPnl = accountAvgCost > 0m && accountNetQty != 0m && currentPositionPrice > 0m
                ? (currentPositionPrice - accountAvgCost) * accountNetQty
                : 0m;
            payload["positionContext"] = new Dictionary<string, object>
            {
                { "botQty", ToDouble(botOwnedQty) },
                { "accountQtyNet", ToDouble(accountNetQty) },
                { "accountQtyAvailable", ToDouble(accountAvailableQty) },
                { "accountAvgCost", accountAvgCost > 0m ? (object)ToDouble(accountAvgCost) : null },
                { "openingAveragePrice", hasPositionMarket && positionMarket.OpeningAveragePrice > 0m ? (object)ToDouble(positionMarket.OpeningAveragePrice) : null },
                { "currentPrice", currentPositionPrice > 0m ? (object)ToDouble(currentPositionPrice) : null },
                { "accountPositionValueTl", hasPositionMarket ? (object)ToDouble(positionMarket.Amount) : null },
                { "unrealizedPnlTl", accountAvgCost > 0m ? (object)ToDouble(accountPnl) : null },
                { "unrealizedPnlPct", accountAvgCost > 0m && currentPositionPrice > 0m ? (object)ToDouble((currentPositionPrice - accountAvgCost) / accountAvgCost * 100m) : null },
                { "costSource", accountAvgCost > 0m ? "MATRIX_ACCOUNT_AVG_COST" : "UNAVAILABLE" },
                { "positionUpdatedUtc", hasPositionMarket ? positionMarket.UpdatedAt.ToString("o") : null },
                { "source", hasPositionMarket ? positionMarket.Source : "UNAVAILABLE" }
            };
            foreach (var item in technicalFeatures)
            {
                payload[item.Key] = item.Value;
            }
            payload["technicalFeatures"] = technicalFeatures;

            ValidateAndLogMarketDataSemantics(
                symbol,
                requestedPeriod,
                actualBarPeriod,
                totalVol,
                barVolume,
                volumeIndicatorValue,
                volumeTlIndicatorValue,
                instrumentType);

            return new MarketDataPayload
            {
                Symbol = symbol,
                DataType = NormalizeDataType(dataType),
                Payload = payload,
                Timestamp = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz")
            };
        }

        private void InitializeIndicators(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            try
            {
                _rsiBySymbol[symbol] = RSIIndicator(symbol, IndicatorPeriod, OHLCType.Close, 14);
                _ema20BySymbol[symbol] = MOVIndicator(symbol, IndicatorPeriod, OHLCType.Close, 20, MovMethod.Exponential);
                _ema50BySymbol[symbol] = MOVIndicator(symbol, IndicatorPeriod, OHLCType.Close, 50, MovMethod.Exponential);
                _macdBySymbol[symbol] = MACDIndicator(symbol, IndicatorPeriod, OHLCType.Close, 26, 12, 9);
                object atrIndicator = TryCreateNativeAtrIndicator(symbol, 14);
                if (atrIndicator != null)
                    _atrIndicatorBySymbol[symbol] = atrIndicator;
                object mostIndicator = TryCreateNativeMostIndicator(symbol);
                if (mostIndicator != null)
                    _mostIndicatorBySymbol[symbol] = mostIndicator;
                object adxIndicator = TryCreateNativeAdxIndicator(symbol, 14);
                if (adxIndicator != null)
                    _adxIndicatorBySymbol[symbol] = adxIndicator;
                if (IsEquitySymbol(symbol))
                {
                    object volumeIndicator = TryCreateVolumeIndicator("VolumeIndicator", symbol);
                    if (volumeIndicator != null)
                        _volumeIndicatorBySymbol[symbol] = volumeIndicator;
                    object volumeTlIndicator = TryCreateVolumeIndicator("VolumeTLIndicator", symbol);
                    if (volumeTlIndicator != null)
                        _volumeTlIndicatorBySymbol[symbol] = volumeTlIndicator;
                }
                SafeDebug("Native indicators initialized symbol=" + symbol + " period=" + IndicatorPeriod);
            }
            catch (Exception ex)
            {
                SafeDebug("Native indicator init failed symbol=" + symbol + " error=" + ex.Message);
            }
        }

        private object TryCreateVolumeIndicator(string methodName, string symbol)
        {
            try
            {
                MethodInfo factory = GetType()
                    .GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                    .FirstOrDefault(method =>
                    {
                        if (!string.Equals(method.Name, methodName, StringComparison.Ordinal))
                            return false;
                        ParameterInfo[] parameters = method.GetParameters();
                        return parameters.Length == 2
                            && parameters[0].ParameterType == typeof(string)
                            && parameters[1].ParameterType == typeof(SymbolPeriod);
                    });
                return factory == null
                    ? null
                    : factory.Invoke(this, new object[] { symbol, IndicatorPeriod });
            }
            catch (Exception ex)
            {
                if (MarketDataDiagnosticsEnabled)
                    SafeDebug(methodName + " unavailable symbol=" + symbol + " error=" + ex.Message);
                return null;
            }
        }

        private object TryCreateNativeAtrIndicator(string symbol, int period)
        {
            try
            {
                foreach (MethodInfo method in GetType().GetMethods(
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                {
                    if (!string.Equals(method.Name, "ATRIndicator", StringComparison.Ordinal)
                        && !string.Equals(method.Name, "ATR", StringComparison.Ordinal))
                        continue;
                    ParameterInfo[] parameters = method.GetParameters();
                    if (parameters.Length == 4
                        && parameters[0].ParameterType == typeof(string)
                        && parameters[1].ParameterType == typeof(SymbolPeriod)
                        && parameters[2].ParameterType == typeof(OHLCType)
                        && parameters[3].ParameterType == typeof(int))
                        return method.Invoke(this, new object[] { symbol, IndicatorPeriod, OHLCType.Close, period });
                    if (parameters.Length == 3
                        && parameters[0].ParameterType == typeof(string)
                        && parameters[1].ParameterType == typeof(SymbolPeriod)
                        && parameters[2].ParameterType == typeof(int))
                        return method.Invoke(this, new object[] { symbol, IndicatorPeriod, period });
                }
            }
            catch (Exception ex)
            {
                if (MarketDataDiagnosticsEnabled)
                    SafeDebug("Verified ATR factory unavailable symbol=" + symbol + " error=" + ex.Message);
            }
            return null;
        }

        /// <summary>
        /// MOST (Anıl Özekşi moving stop-loss) native göstergesini reflection
        /// probe ile oluşturur — ATR probe deseninin (TryCreateNativeAtrIndicator)
        /// esnek imzalı genellemesi. Varsayılanlar Matriks'in yaygın MOST
        /// parametreleriyle uyumlu: MOV periyodu 3 (Exponential), yüzde 2.
        /// Bulunamazsa null döner; /indicators alanı hiç üretilmez (fail-open).
        /// </summary>
        private object TryCreateNativeMostIndicator(string symbol)
        {
            return TryCreateFlexibleIndicator(
                new[] { "MOSTIndicator", "MOST" }, symbol,
                defaultInt: 3, defaultDouble: 2.0, defaultDecimal: 2m);
        }

        /// <summary>
        /// ADX native göstergesi için reflection probe (aynı desen). Period 14.
        /// </summary>
        private object TryCreateNativeAdxIndicator(string symbol, int period)
        {
            return TryCreateFlexibleIndicator(
                new[] { "ADXIndicator", "ADX" }, symbol,
                defaultInt: period, defaultDouble: period, defaultDecimal: period);
        }

        /// <summary>
        /// Esnek imzalı gösterge fabrikası: adları verilen metodlar arasından
        /// ilk parametresi string (symbol) olanı seçer ve kalan parametreleri
        /// tipe göre doldurur (SymbolPeriod→IndicatorPeriod, OHLCType→Close,
        /// MovMethod→Exponential, int/double/decimal→verilen default'lar).
        /// Doldurulamayan parametre tipi varsa o aday atlanır. Her hata null
        /// döner — gösterge yoksa veri üretilmez, asla uydurulmaz.
        /// </summary>
        private object TryCreateFlexibleIndicator(
            string[] methodNames, string symbol,
            int defaultInt, double defaultDouble, decimal defaultDecimal)
        {
            try
            {
                foreach (MethodInfo method in GetType().GetMethods(
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                {
                    if (!methodNames.Any(name => string.Equals(method.Name, name, StringComparison.Ordinal)))
                        continue;
                    ParameterInfo[] parameters = method.GetParameters();
                    if (parameters.Length < 2 || parameters[0].ParameterType != typeof(string))
                        continue;
                    var args = new object[parameters.Length];
                    args[0] = symbol;
                    bool resolvable = true;
                    for (int i = 1; i < parameters.Length; i++)
                    {
                        Type type = parameters[i].ParameterType;
                        if (type == typeof(SymbolPeriod)) args[i] = IndicatorPeriod;
                        else if (type == typeof(OHLCType)) args[i] = OHLCType.Close;
                        else if (type == typeof(MovMethod)) args[i] = MovMethod.Exponential;
                        else if (type == typeof(int)) args[i] = defaultInt;
                        else if (type == typeof(double)) args[i] = defaultDouble;
                        else if (type == typeof(decimal)) args[i] = defaultDecimal;
                        else if (parameters[i].HasDefaultValue) args[i] = parameters[i].DefaultValue;
                        else { resolvable = false; break; }
                    }
                    if (!resolvable)
                        continue;
                    object created = method.Invoke(this, args);
                    if (created != null)
                        return created;
                }
            }
            catch (Exception ex)
            {
                if (MarketDataDiagnosticsEnabled)
                    SafeDebug(string.Join("/", methodNames) + " unavailable symbol=" + symbol + " error=" + ex.Message);
            }
            return null;
        }

        private static decimal? ReadIndicatorCurrentValue(
            ConcurrentDictionary<string, object> indicators,
            string symbol)
        {
            object indicator;
            if (!indicators.TryGetValue(NormalizeSymbol(symbol), out indicator) || indicator == null)
                return null;
            object current = ReadPublicMember(indicator, "CurrentValue");
            if (current == null)
                return null;
            try
            {
                return Convert.ToDecimal(current);
            }
            catch
            {
                return null;
            }
        }

        private static string NormalizePeriodName(string raw)
        {
            // Matriks bar olayları periyodu Türkçe raporlar ("1 DAKIKA",
            // "1 SAAT", "GÜNLÜK"); boşluklar da atılır ki "1 DAKIKA" ile
            // MIN1 aynı kanonik ada insin — aksi halde timeframeMismatch
            // her zaman true kalıyordu ve actualBarPeriodSeconds null'du.
            string value = (raw ?? string.Empty)
                .Trim()
                .ToUpperInvariant()
                .Replace("_", string.Empty)
                .Replace(" ", string.Empty);
            if (value == "MIN" || value == "1M" || value == "MIN1" || value == "1DAKIKA" || value == "DAKIKA") return "MIN1";
            if (value == "5M" || value == "MIN5" || value == "5DAKIKA") return "MIN5";
            if (value == "15M" || value == "MIN15" || value == "15DAKIKA") return "MIN15";
            if (value == "30M" || value == "MIN30" || value == "30DAKIKA") return "MIN30";
            if (value == "HOUR" || value == "1H" || value == "MIN60" || value == "60DAKIKA" || value == "1SAAT" || value == "SAAT" || value == "SAATLIK" || value == "SAATLİK") return "MIN60";
            if (value == "DAY" || value == "1D" || value == "DAY1" || value == "1GUN" || value == "1GÜN" || value == "GUNLUK" || value == "GÜNLÜK") return "DAY1";
            return value;
        }

        private static int PeriodSeconds(string period)
        {
            switch (NormalizePeriodName(period))
            {
                case "MIN1": return 60;
                case "MIN5": return 300;
                case "MIN15": return 900;
                case "MIN30": return 1800;
                case "MIN60": return 3600;
                case "DAY1": return 86400;
                default: return 0;
            }
        }

        private static bool PeriodsEquivalent(string left, string right)
        {
            return string.Equals(
                NormalizePeriodName(left),
                NormalizePeriodName(right),
                StringComparison.Ordinal);
        }

        private void ValidateAndLogMarketDataSemantics(
            string symbol,
            string requestedPeriod,
            string actualPeriod,
            decimal totalVol,
            decimal barVolume,
            decimal? volumeIndicatorValue,
            decimal? volumeTlIndicatorValue,
            string instrumentType)
        {
            if (totalVol < 0m || barVolume < 0m
                || (volumeIndicatorValue.HasValue && volumeIndicatorValue.Value < 0m)
                || (volumeTlIndicatorValue.HasValue && volumeTlIndicatorValue.Value < 0m))
            {
                LogMarketDataWarning(symbol, "VOLUME_SEMANTICS", "Negative volume/turnover value received");
            }
            if (!string.IsNullOrWhiteSpace(actualPeriod)
                && !PeriodsEquivalent(requestedPeriod, actualPeriod))
            {
                LogMarketDataWarning(symbol, "TIMEFRAME_MISMATCH",
                    "requested=" + requestedPeriod + " actual=" + actualPeriod);
            }
            if (!MarketDataDiagnosticsEnabled || !ShouldSampleDiagnostic(symbol))
                return;

            DateTime now = DateTime.UtcNow;
            DateTime previous;
            if (_marketDataDiagnosticUtcBySymbol.TryGetValue(symbol, out previous)
                && (now - previous).TotalSeconds < MarketDataWarningRateLimitSeconds)
                return;
            _marketDataDiagnosticUtcBySymbol[symbol] = now;
            SafeDebug("Market data semantic diagnostic symbol=" + symbol
                + " instrumentType=" + instrumentType
                + " requestedPeriod=" + requestedPeriod
                + " actualPeriod=" + actualPeriod
                + " totalVol=" + totalVol
                + " barVolume=" + barVolume
                + " volumeIndicator=" + (volumeIndicatorValue.HasValue ? volumeIndicatorValue.Value.ToString() : "UNAVAILABLE")
                + " volumeTlIndicator=" + (volumeTlIndicatorValue.HasValue ? volumeTlIndicatorValue.Value.ToString() : "UNAVAILABLE"));
        }

        private bool ShouldSampleDiagnostic(string symbol)
        {
            if (MarketDataDiagnosticSampleRatePct >= 100m)
                return true;
            if (MarketDataDiagnosticSampleRatePct <= 0m)
                return false;
            int hash = 17;
            foreach (char value in NormalizeSymbol(symbol))
                hash = unchecked(hash * 31 + value);
            decimal bucket = Math.Abs((long)hash) % 10000L / 100m;
            return bucket < MarketDataDiagnosticSampleRatePct;
        }

        private double? GetNativeRsi(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            try
            {
                if (_rsiBySymbol.TryGetValue(symbol, out var indicator) && indicator != null)
                    return Convert.ToDouble(indicator.CurrentValue);
            }
            catch (Exception ex)
            {
                SafeDebug("Native RSI unavailable symbol=" + symbol + " error=" + ex.Message);
            }
            return null;
        }

        private double? GetNativeEma20(string symbol)
        {
            return GetNativeMovValue(_ema20BySymbol, symbol, "EMA20");
        }

        private double? GetNativeEma50(string symbol)
        {
            return GetNativeMovValue(_ema50BySymbol, symbol, "EMA50");
        }

        private double? GetNativeMovValue(ConcurrentDictionary<string, MOV> indicators, string symbol, string name)
        {
            symbol = NormalizeSymbol(symbol);
            try
            {
                if (indicators.TryGetValue(symbol, out var indicator) && indicator != null)
                    return Convert.ToDouble(indicator.CurrentValue);
            }
            catch (Exception ex)
            {
                SafeDebug("Native " + name + " unavailable symbol=" + symbol + " error=" + ex.Message);
            }
            return null;
        }

        private double? GetNativeMacdLine(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            try
            {
                if (_macdBySymbol.TryGetValue(symbol, out var indicator) && indicator != null)
                    return Convert.ToDouble(indicator.CurrentValue);
            }
            catch (Exception ex)
            {
                SafeDebug("Native MACD unavailable symbol=" + symbol + " error=" + ex.Message);
            }
            return null;
        }

        private double? GetNativeMacdSignal(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            try
            {
                if (_macdBySymbol.TryGetValue(symbol, out var indicator)
                    && indicator != null
                    && indicator.MacdTrigger != null)
                {
                    return Convert.ToDouble(indicator.MacdTrigger.CurrentValue);
                }
            }
            catch (Exception ex)
            {
                SafeDebug("Native MACD signal unavailable symbol=" + symbol + " error=" + ex.Message);
            }
            return null;
        }

        private string ResolveIndicatorSource(double? rsi, double? ema20, double? ema50, double? macd, double? macdSignal)
        {
            if (rsi.HasValue && ema20.HasValue && ema50.HasValue && macd.HasValue && macdSignal.HasValue)
                return "MATRIX_NATIVE_OR_READY";
            if (rsi.HasValue || ema20.HasValue || ema50.HasValue || macd.HasValue || macdSignal.HasValue)
                return "PARTIAL";
            return "UNAVAILABLE";
        }

        // ── Position helpers ────────────────────────────────────────

        private decimal GetAccountAvailableQty(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            return _accountAvailableQtyBySymbol.TryGetValue(symbol, out var qty) ? qty : 0m;
        }

        private decimal GetTotalAccountQty(string symbol)
        {
            decimal qty;
            return _accountNetQtyBySymbol.TryGetValue(NormalizeSymbol(symbol), out qty) ? qty : 0m;
        }

        private decimal GetLockedLongTermQty(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            return LockedLongTermQty.TryGetValue(symbol, out var qty) ? qty : 0m;
        }

        private decimal GetBotOwnedQty(string symbol)
        {
            decimal qty;
            return BotOwnedQty.TryGetValue(NormalizeSymbol(symbol), out qty) ? Math.Max(0m, qty) : 0m;
        }

        private void LoadRealPositionsSnapshot()
        {
            try
            {
                DateTime snapshotStartedUtc = DateTime.UtcNow;
                var positions = GetRealPositions();
                if (positions == null)
                {
                    SafeDebug("GetRealPositions returned null.");
                    return;
                }

                // Some Matriks/demo-account builds keep PositionReceiveComplated
                // false even though GetRealPositions already contains the full
                // portfolio. A non-empty snapshot is authoritative; an empty
                // snapshot is accepted only after Matriks reports completion.
                if (!PositionReceiveComplated && positions.Count == 0)
                {
                    SafeDebug("Real position snapshot not ready yet; "
                        + "PositionReceiveComplated=false and GetRealPositions is empty.");
                    return;
                }

                var netSnapshot = new Dictionary<string, decimal>();
                var availableSnapshot = new Dictionary<string, decimal>();
                foreach (var item in positions)
                {
                    AlgoTraderPosition position = item.Value;
                    if (position == null) continue;
                    string symbol = NormalizeSymbol(position.Symbol);
                    if (symbol == "") continue;
                    DateTime newerEventUtc;
                    if (_lastPositionEventUtcBySymbol.TryGetValue(symbol, out newerEventUtc) && newerEventUtc > snapshotStartedUtc)
                        continue;
                    netSnapshot[symbol] = position.QtyNet;
                    availableSnapshot[symbol] = position.QtyAvailable;
                    if (position.QtyNet == 0m && position.QtyAvailable == 0m)
                    {
                        PositionMarketSnapshot removed;
                        _positionMarketBySymbol.TryRemove(symbol, out removed);
                    }
                    else
                    {
                        _positionMarketBySymbol[symbol] = BuildPositionMarketSnapshot(position, "MATRIX_POSITION_SNAPSHOT");
                    }
                    if (position.QtyNet != 0m || position.QtyAvailable != 0m)
                        EnsurePortfolioSymbolSubscribed(symbol);
                }
                // Publish complete snapshots with atomic reference swaps. Readers
                // can observe either the previous or the new valid snapshot,
                // never an intermediate Clear()/refill state.
                _accountNetQtyBySymbol = new ConcurrentDictionary<string, decimal>(netSnapshot);
                _accountAvailableQtyBySymbol = new ConcurrentDictionary<string, decimal>(availableSnapshot);
                foreach (string cachedSymbol in _positionMarketBySymbol.Keys.ToList())
                {
                    if (!netSnapshot.ContainsKey(cachedSymbol))
                    {
                        PositionMarketSnapshot removed;
                        _positionMarketBySymbol.TryRemove(cachedSymbol, out removed);
                    }
                }

                _realPositionsLoadedFromSnapshot = true;
                _lastPositionSyncUtc = DateTime.UtcNow;
                _positionSnapshotCompleteFlag = PositionReceiveComplated;
                _positionSnapshotNonEmpty = positions.Count > 0;
                _positionSnapshotGeneration++;
                _positionSnapshotConfidence = PositionReceiveComplated ? "HIGH" : (positions.Count > 0 ? "MEDIUM" : "LOW");
                SafeDebug("Real positions snapshot loaded count=" + positions.Count);
            }
            catch (Exception ex)
            {
                SafeDebug("GetRealPositions failed: " + ex.Message);
            }
        }

        private void UpdatePositionCache(AlgoTraderPosition position, string source)
        {
            if (position == null)
                return;

            string symbol = NormalizeSymbol(position.Symbol);
            if (string.IsNullOrWhiteSpace(symbol))
                return;

            if (position.QtyNet != 0m || position.QtyAvailable != 0m)
                EnsurePortfolioSymbolSubscribed(symbol);
            _accountNetQtyBySymbol[symbol] = position.QtyNet;
            _accountAvailableQtyBySymbol[symbol] = position.QtyAvailable;
            if (position.QtyNet == 0m && position.QtyAvailable == 0m)
            {
                PositionMarketSnapshot removed;
                _positionMarketBySymbol.TryRemove(symbol, out removed);
            }
            else
            {
                _positionMarketBySymbol[symbol] = BuildPositionMarketSnapshot(position, source);
            }
            _lastPositionEventUtcBySymbol[symbol] = DateTime.UtcNow;
            SafeDebug(source + " position symbol=" + symbol
                + " qtyAvailable=" + position.QtyAvailable
                + " qtyNet=" + position.QtyNet
                + " cachedAvailableQty=" + position.QtyAvailable);
        }

        private static PositionMarketSnapshot BuildPositionMarketSnapshot(AlgoTraderPosition position, string source)
        {
            return new PositionMarketSnapshot
            {
                QtyAvailable = position.QtyAvailable,
                QtyNet = position.QtyNet,
                AvgCost = Convert.ToDecimal(position.AvgCost),
                OpeningAveragePrice = Convert.ToDecimal(position.OpeningAveragePrice),
                Amount = Convert.ToDecimal(position.Amount),
                SettlementPx = Convert.ToDecimal(position.SettlementPx),
                Currency = Convert.ToString(ReadPublicMember(position, "Currency")),
                AccountId = MaskAccountId(Convert.ToString(ReadPublicMember(position, "AccountId"))),
                UpdatedAt = DateTime.UtcNow,
                Source = source
            };
        }

        /// <summary>
        /// A portfolio symbol may not be part of the configured market-data
        /// watchlist.  Manual SELL must still be able to obtain its price and
        /// pass the gateway allow-list, so subscribe it when a real position
        /// is discovered.
        /// </summary>
        private void EnsurePortfolioSymbolSubscribed(string symbol)
        {
            if (IsAllowedSymbol(symbol))
                return;

            lock (_symbolSubscriptionLock)
            {
                if (IsAllowedSymbol(symbol))
                    return;

                AddSymbol(symbol, SymbolPeriod.Min);
                if (IndicatorPeriod != SymbolPeriod.Min)
                    AddSymbol(symbol, IndicatorPeriod);
                AddSymbolMarketData(symbol);
                if (IsEquitySymbol(symbol))
                    AddSymbolMarketDepth(symbol);
                RegisterNewsSubscriptionsForSymbol(symbol);
                InitializeIndicators(symbol);

                AllowedSymbols = AllowedSymbols.Concat(new[] { symbol }).ToArray();
                _closeHistoryBySymbol.TryAdd(
                    BuildSeriesKey(symbol, IndicatorPeriod.ToString()),
                    new List<decimal>());
                SafeDebug("Portfolio symbol subscribed symbol=" + symbol);
            }
        }

        private void RegisterGlobalNewsKeywordSubscriptions()
        {
            foreach (string keyword in ParseCsvList(NewsKeywordsCsv))
            {
                lock (_newsSubscriptionLock)
                {
                    if (_newsKeywordSubscriptions.Any(x => string.Equals(x.Keyword, keyword, StringComparison.OrdinalIgnoreCase)))
                        continue;
                    AddNewsKeyword(keyword, NewsFiltersOnlyInHeaders, NewsFiltersExactMatch);
                    _newsKeywordSubscriptions.Add(new NewsKeywordSubscription
                    {
                        Keyword = keyword,
                        OnlyInHeaders = NewsFiltersOnlyInHeaders,
                        IsExactMatch = NewsFiltersExactMatch
                    });
                }
                SafeDebug("News keyword subscribed keyword=" + keyword
                    + " onlyInHeaders=" + NewsFiltersOnlyInHeaders
                    + " exactMatch=" + NewsFiltersExactMatch);
            }
        }

        private void RegisterNewsSubscriptionsForSymbol(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            if (string.IsNullOrWhiteSpace(symbol))
                return;

            lock (_newsSubscriptionLock)
            {
                if (!_newsTrackedSymbols.Contains(symbol))
                {
                    AddNewsSymbol(symbol);
                    _newsTrackedSymbols.Add(symbol);
                }

                foreach (NewsSymbolKeywordSubscription rule in ParseNewsSymbolKeywordRules(NewsSymbolKeywordRulesCsv)
                    .Where(x => string.Equals(x.Symbol, symbol, StringComparison.OrdinalIgnoreCase)))
                {
                    var effectiveRule = new NewsSymbolKeywordSubscription
                    {
                        Symbol = rule.Symbol,
                        Keywords = rule.Keywords,
                        OnlyInHeaders = NewsFiltersOnlyInHeaders,
                        IsExactMatch = NewsFiltersExactMatch
                    };
                    bool exists = _newsSymbolKeywordSubscriptions.Any(x =>
                        string.Equals(x.Symbol, effectiveRule.Symbol, StringComparison.OrdinalIgnoreCase)
                        && x.OnlyInHeaders == effectiveRule.OnlyInHeaders
                        && x.IsExactMatch == effectiveRule.IsExactMatch
                        && x.Keywords.SequenceEqual(effectiveRule.Keywords, StringComparer.OrdinalIgnoreCase));
                    if (exists)
                        continue;

                    AddNewsSymbolKeyword(symbol, effectiveRule.Keywords, effectiveRule.OnlyInHeaders, effectiveRule.IsExactMatch);
                    _newsSymbolKeywordSubscriptions.Add(effectiveRule);
                }
            }
        }

        private void LogTradeUserInfo()
        {
            try
            {
                var tradeUser = GetTradeUser();
                if (tradeUser == null)
                {
                    SafeDebug("GetTradeUser returned null.");
                    return;
                }

                _autoOrderEnabled = tradeUser.AutoOrder;
                _testAutoOrderEnabled = tradeUser.TestAutoOrder;
                SafeDebug("TradeUser accountId=" + MaskAccountId(Convert.ToString(tradeUser.AccountId))
                    + " autoOrder=" + _autoOrderEnabled
                    + " testAutoOrder=" + _testAutoOrderEnabled);
            }
            catch (Exception ex)
            {
                SafeDebug("GetTradeUser failed: " + ex.Message);
            }
        }

        // ── Market data access ──────────────────────────────────────

        private MarketQuoteSnapshot ReadMarketQuote(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            DateTime quoteReadUtc = DateTime.UtcNow;
            decimal rawLast = SafeMarketData(symbol, SymbolUpdateField.Last);
            decimal rawBid = SafeMarketData(symbol, SymbolUpdateField.Bid);
            decimal rawAsk = SafeMarketData(symbol, SymbolUpdateField.Ask);
            decimal rawTotalVol = IsEquitySymbol(symbol)
                ? SafeMarketData(symbol, SymbolUpdateField.TotalVol)
                : 0m;

            bool liveReliable = rawLast > 0m || rawBid > 0m || rawAsk > 0m;
            if (liveReliable)
            {
                DateTime lastTradeUtc;
                bool hasLastTradeTime = _lastTradeUtcBySymbol.TryGetValue(symbol, out lastTradeUtc);
                bool quoteFresh = hasLastTradeTime && (quoteReadUtc - lastTradeUtc).TotalSeconds <= MaxQuoteAgeSecondsForOrder;
                if (_lastValidQuoteBySymbol.TryGetValue(symbol, out var previous))
                {
                    if (rawLast <= 0m) rawLast = previous.Last;
                    if (rawTotalVol <= 0m) rawTotalVol = previous.TotalVol;
                }

                var live = new MarketQuoteSnapshot
                {
                    Last = rawLast,
                    Bid = rawBid,
                    Ask = rawAsk,
                    TotalVol = rawTotalVol,
                    Reliable = quoteFresh,
                    Source = "LIVE",
                    LastTradeUtc = hasLastTradeTime ? lastTradeUtc : DateTime.MinValue,
                    ReadUtc = quoteReadUtc,
                    TimestampSource = hasLastTradeTime ? "LAST_TICK_TIME" : "READ_TIME_ONLY"
                };
                _lastValidQuoteBySymbol[symbol] = live;
                if (rawLast > 0m)
                    _dailyRefPriceBySymbol.TryAdd(symbol, rawLast);
                return live;
            }

            if (_lastValidQuoteBySymbol.TryGetValue(symbol, out var cached)
                && cached.LastTradeUtc != DateTime.MinValue
                && (quoteReadUtc - cached.LastTradeUtc).TotalHours <= 8)
            {
                cached.Source = "LAST_VALID";
                cached.Reliable = false;
                cached.ReadUtc = quoteReadUtc;
                LogMarketDataWarning(symbol, "QUOTE", "Live quote is zero; using last valid quote");
                return cached;
            }

            LogMarketDataWarning(symbol, "QUOTE", "Live quote is zero and no last valid quote exists");
            return new MarketQuoteSnapshot
            {
                Last = 0m,
                Bid = 0m,
                Ask = 0m,
                TotalVol = 0m,
                Reliable = false,
                Source = "ZERO_UNAVAILABLE",
                LastTradeUtc = DateTime.MinValue,
                ReadUtc = quoteReadUtc,
                TimestampSource = "READ_TIME_ONLY"
            };
        }

        private OhlcvSnapshot ResolveOhlcvSnapshot(string symbol, decimal lastPrice, decimal volume)
        {
            symbol = NormalizeSymbol(symbol);
            if (_lastOhlcvBySymbol.TryGetValue(symbol, out var cached)
                && cached.Close > 0m
                && cached.ReceivedAtUtc != DateTime.MinValue
                && (DateTime.UtcNow - cached.ReceivedAtUtc).TotalHours <= 8)
            {
                return cached;
            }

            return new OhlcvSnapshot
            {
                Open = lastPrice,
                High = lastPrice,
                Low = lastPrice,
                Close = lastPrice,
                Volume = volume,
                Reliable = false,
                Source = "QUOTE_FALLBACK",
                ReceivedAtUtc = DateTime.UtcNow,
                BarEventUtc = DateTime.MinValue,
                BarTimestampSource = "UNAVAILABLE",
                BarTimeReliable = false
            };
        }

        private void UpdateOhlcvSnapshotFromBarData(BarDataEventArgs barData)
        {
            try
            {
                string symbol = ResolveBarEventSymbol(barData.SymbolId);
                if (string.IsNullOrWhiteSpace(symbol))
                    return;

                var subscribedBarData = GetBarData(symbol, IndicatorPeriod);
                if (subscribedBarData == null || subscribedBarData.PeriodInfo != barData.PeriodInfo)
                    return;

                // The event carries the current bar. Reading it directly avoids
                // GetBarData() selecting another symbol in a multi-symbol algo.
                decimal close = barData.BarData.Close;
                if (close <= 0m)
                {
                    LogMarketDataWarning(symbol, "BAR", "Bar close <= 0; OHLC cache not updated");
                    return;
                }

                decimal open = barData.BarData.Open;
                decimal high = barData.BarData.High;
                decimal low = barData.BarData.Low;
                decimal volume = barData.BarData.Volume;

                if (open <= 0m) open = close;
                if (high <= 0m) high = close;
                if (low <= 0m) low = close;

                DateTime receivedAtUtc = DateTime.UtcNow;
                DateTime barTimestamp = barData.BarData.Dtime;
                bool hasDtime = barTimestamp != DateTime.MinValue;
                DateTime lastTickTime = barData.LastTickTime;
                string actualBarPeriod = NormalizePeriodName(Convert.ToString(barData.PeriodInfo));
                string seriesKey = BuildSeriesKey(symbol, actualBarPeriod);
                int periodSeconds = PeriodSeconds(actualBarPeriod);
                DateTime officialFallbackTime = DateTime.MinValue;
                string timestampSource = hasDtime ? "BAR_DATA_DTIME" : "BAR_INDEX_FALLBACK";
                string observationSource = hasDtime ? "BAR_DATA_DTIME" : "BAR_INDEX_FALLBACK";
                if (!hasDtime && lastTickTime != DateTime.MinValue)
                {
                    officialFallbackTime = lastTickTime;
                    observationSource = "LAST_TICK_TIME";
                }
                else if (!hasDtime && TryGetLastUpdateForSymbol(symbol, out officialFallbackTime))
                {
                    observationSource = "LAST_UPDATE_FOR_SYMBOL";
                }
                bool barClosed = hasDtime
                    && periodSeconds > 0
                    && lastTickTime != DateTime.MinValue
                    && lastTickTime >= barTimestamp.AddSeconds(periodSeconds);

                string barKey = null;
                if (hasDtime)
                    barKey = "TIME:" + barTimestamp.Ticks;
                else if (barData.BarDataIndex >= 0)
                    barKey = "INDEX:" + barData.BarDataIndex;
                if (barKey == null)
                    LogMarketDataWarning(symbol, "BAR_TIME", "Bar timestamp and bar index unavailable; close history not updated");

                var snapshot = new OhlcvSnapshot
                {
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Volume = volume,
                    Reliable = open > 0m && high > 0m && low > 0m && close > 0m,
                    Source = "BAR_DATA_EVENT",
                    ReceivedAtUtc = receivedAtUtc,
                    BarEventUtc = hasDtime ? barTimestamp.ToUniversalTime() : DateTime.MinValue,
                    ResolvedTimestamp = hasDtime ? barTimestamp : officialFallbackTime,
                    BarTimestampSource = timestampSource,
                    BarObservationSource = observationSource,
                    BarTimeReliable = hasDtime,
                    ActualBarPeriod = actualBarPeriod,
                    ActualBarPeriodSeconds = periodSeconds > 0 ? (int?)periodSeconds : null,
                    BarPeriodSource = "BarDataEventArgs.PeriodInfo",
                    BarClosed = barClosed,
                    BarIsNew = barData.IsNewBar,
                    BarDataIndex = barData.BarDataIndex,
                    LastTickTime = lastTickTime
                };
                _lastOhlcvBySymbol[symbol] = snapshot;
                if (barKey != null)
                    UpdateBarHistory(seriesKey, snapshot, barKey, barData.BarDataIndex);
            }
            catch (Exception ex)
            {
                LogMarketDataWarning("UNKNOWN", "BAR", ex.Message);
            }
        }

        private string ResolveBarEventSymbol(int symbolId)
        {
            foreach (string symbolRaw in AllowedSymbols)
            {
                string symbol = NormalizeSymbol(symbolRaw);
                if (GetSymbolId(symbol) == symbolId)
                    return symbol;
            }
            return "";
        }

        private void LogMarketDataWarning(string symbol, string field, string message)
        {
            string key = NormalizeSymbol(symbol) + "|" + field;
            DateTime now = DateTime.UtcNow;
            if (_marketDataWarningUtcByKey.TryGetValue(key, out var last)
                && (now - last).TotalSeconds < MarketDataWarningRateLimitSeconds)
            {
                return;
            }

            _marketDataWarningUtcByKey[key] = now;
            SafeDebug("Market data warning symbol=" + NormalizeSymbol(symbol)
                + " field=" + field
                + " message=" + message);
        }

        private decimal SafeMarketData(string symbol, SymbolUpdateField field)
        {
            try
            {
                return GetMarketData(symbol, field);
            }
            catch (Exception ex)
            {
                LogMarketDataWarning(symbol, field.ToString(), ex.Message);
                return 0m;
            }
        }

        // ── Close history (thread-safe) ──────────────────────────────

        private static string BuildSeriesKey(string symbol, string actualPeriod)
        {
            return NormalizeSymbol(symbol) + "|" + NormalizePeriodName(actualPeriod);
        }

        private void UpdateBarHistory(string seriesKey, OhlcvSnapshot bar, string barKey, int barIndex)
        {
            if (bar.Close <= 0 || string.IsNullOrWhiteSpace(barKey))
                return;

            lock (_closeLock)
            {
                int previousIndex;
                bool indexReset = barIndex >= 0
                    && _lastBarIndexBySeries.TryGetValue(seriesKey, out previousIndex)
                    && barIndex < previousIndex;
                if (indexReset)
                {
                    _closeHistoryBySymbol[seriesKey] = new List<decimal>();
                    _ohlcvHistoryBySeries[seriesKey] = new List<OhlcvBarPoint>();
                    string removedKey;
                    _lastCloseBarKeyBySymbol.TryRemove(seriesKey, out removedKey);
                }
                if (barIndex >= 0)
                    _lastBarIndexBySeries[seriesKey] = barIndex;

                string previous;
                var list = _closeHistoryBySymbol.GetOrAdd(seriesKey, _ => new List<decimal>());
                var ohlcv = _ohlcvHistoryBySeries.GetOrAdd(seriesKey, _ => new List<OhlcvBarPoint>());
                var point = new OhlcvBarPoint
                {
                    Open = bar.Open,
                    High = bar.High,
                    Low = bar.Low,
                    Close = bar.Close,
                    Volume = bar.Volume,
                    Reliable = bar.Reliable,
                    Closed = bar.BarClosed
                };
                if (_lastCloseBarKeyBySymbol.TryGetValue(seriesKey, out previous)
                    && string.Equals(previous, barKey, StringComparison.Ordinal))
                {
                    if (list.Count > 0)
                        list[list.Count - 1] = bar.Close;
                    if (ohlcv.Count > 0)
                        ohlcv[ohlcv.Count - 1] = point;
                    return;
                }
                _lastCloseBarKeyBySymbol[seriesKey] = barKey;
                list.Add(bar.Close);
                ohlcv.Add(point);
                if (list.Count > MaxCloseHistory)
                    list.RemoveAt(0);
                if (ohlcv.Count > MaxCloseHistory)
                    ohlcv.RemoveAt(0);
            }
        }

        private List<decimal> GetCloseHistory(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            OhlcvSnapshot snapshot;
            string actualPeriod = _lastOhlcvBySymbol.TryGetValue(symbol, out snapshot)
                ? snapshot.ActualBarPeriod
                : NormalizePeriodName(IndicatorPeriod.ToString());
            string seriesKey = BuildSeriesKey(symbol, actualPeriod);

            lock (_closeLock)
            {
                return _closeHistoryBySymbol.GetOrAdd(seriesKey, _ => new List<decimal>());
            }
        }

        // ── Technical indicators ────────────────────────────────────

        private double? CalculateRsi(string symbol, int period)
        {
            var closes = new List<decimal>();
            lock (_closeLock)
            {
                closes = new List<decimal>(GetCloseHistory(symbol));
            }

            if (closes.Count <= period)
                return null;

            decimal gain = 0;
            decimal loss = 0;
            for (int i = closes.Count - period; i < closes.Count; i++)
            {
                decimal diff = closes[i] - closes[i - 1];
                if (diff >= 0)
                    gain += diff;
                else
                    loss += Math.Abs(diff);
            }

            if (loss == 0)
                return 100;

            decimal rs = gain / loss;
            return ToDouble(100 - (100 / (1 + rs)));
        }

        private double? CalculateEma(string symbol, int period)
        {
            var closes = new List<decimal>();
            lock (_closeLock)
            {
                closes = new List<decimal>(GetCloseHistory(symbol));
            }
            return CalculateEma(closes, period);
        }

        private double? CalculateEma(List<decimal> values, int period)
        {
            if (values.Count < period)
                return null;

            decimal multiplier = 2m / (period + 1);
            decimal ema = values.Take(period).Average();
            for (int i = period; i < values.Count; i++)
            {
                ema = ((values[i] - ema) * multiplier) + ema;
            }

            return ToDouble(ema);
        }

        private double? CalculateMacdLine(string symbol)
        {
            double? ema12 = CalculateEma(symbol, 12);
            double? ema26 = CalculateEma(symbol, 26);
            if (!ema12.HasValue || !ema26.HasValue)
                return null;
            return ema12.Value - ema26.Value;
        }

        private double? CalculateMacdSignal(string symbol)
        {
            var closes = new List<decimal>();
            lock (_closeLock)
            {
                closes = new List<decimal>(GetCloseHistory(symbol));
            }

            if (closes.Count < 35)
                return null;

            var macdValues = new List<decimal>();
            for (int end = 26; end <= closes.Count; end++)
            {
                var slice = closes.Take(end).ToList();
                double? ema12 = CalculateEma(slice, 12);
                double? ema26 = CalculateEma(slice, 26);
                if (ema12.HasValue && ema26.HasValue)
                    macdValues.Add(Convert.ToDecimal(ema12.Value - ema26.Value));
            }

            return CalculateEma(macdValues, 9);
        }

        private Dictionary<string, object> BuildTechnicalFeaturePayload(
            string symbol,
            decimal lastPrice,
            double? rsi,
            double? ema20,
            double? ema50,
            double? macd,
            double? macdSignal,
            string indicatorSource,
            decimal bid1Size,
            decimal maxBid1Size,
            decimal depthQueueDropPct,
            bool depthReliable)
        {
            var features = new Dictionary<string, object>();
            var consensus = CalculateIndicatorConsensus(lastPrice, rsi, ema20, ema50, macd, macdSignal);
            double? averageAbsoluteCloseChange = CalculateAverageAbsoluteCloseChange(symbol, 14);
            double? normalizedCloseChangePct = CalculateNormalizedCloseChangePct(symbol, 14);
            VolatilitySnapshot volatility = ResolveVolatility(symbol, lastPrice, 14);
            OhlcvSnapshot currentBar;
            string atrTimeframe = _lastOhlcvBySymbol.TryGetValue(NormalizeSymbol(symbol), out currentBar)
                ? currentBar.ActualBarPeriod
                : NormalizePeriodName(IndicatorPeriod.ToString());

            features["schemaVersion"] = "technical-features-v2";
            features["deprecatedFields"] = new[] { "marketRegime", "averageAbsoluteCloseChange", "normalizedCloseChangePct" };
            features["indicatorSource"] = indicatorSource;
            features["alphaTrendSignal"] = CalculateAlphaTrendProxySignal(rsi, ema20, ema50, macd, macdSignal);
            features["alphaTrendMode"] = "PROXY_EMA_MACD_RSI";
            features["indicatorBuyCount"] = consensus.BuyCount;
            features["indicatorSellCount"] = consensus.SellCount;
            features["indicatorNeutralCount"] = consensus.NeutralCount;
            features["indicatorConsensus"] = consensus.Signal;
            features["indicatorConsensusRatio"] = consensus.Ratio;
            AddIfNotNull(features, "averageAbsoluteCloseChange", averageAbsoluteCloseChange);
            AddIfNotNull(features, "normalizedCloseChangePct", normalizedCloseChangePct);
            features["closeChangeVolatilityProxy"] = normalizedCloseChangePct;
            features["atr"] = volatility.Atr;
            features["natr"] = volatility.Natr;
            features["atrPeriod"] = 14;
            features["atrTimeframe"] = atrTimeframe;
            features["volatilityMetricSource"] = volatility.Source;
            // v2: MOST + ADX native göstergelerden (Faz 3). Gösterge yoksa
            // alan hiç yazılmaz — Python tarafı None'ı fail-open işler.
            decimal? nativeAdx = ReadIndicatorCurrentValue(_adxIndicatorBySymbol, symbol);
            if (nativeAdx.HasValue && nativeAdx.Value >= 0m)
                features["adx"] = ToDouble(nativeAdx.Value);
            decimal? nativeMost = ReadIndicatorCurrentValue(_mostIndicatorBySymbol, symbol);
            if (nativeMost.HasValue && nativeMost.Value > 0m && lastPrice > 0m)
            {
                features["most"] = ToDouble(nativeMost.Value);
                features["mostSignal"] = lastPrice >= nativeMost.Value ? "LONG" : "SHORT";
            }
            features["depthReliable"] = depthReliable;
            if (depthReliable)
            {
                features["depthBid1Size"] = ToDouble(bid1Size);
                features["depthBid1MaxSize"] = ToDouble(maxBid1Size);
                features["depthQueueDropPct"] = ToDouble(depthQueueDropPct);
            }
            features["symbolTrendRegime"] = ClassifyMarketRegime(volatility.Natr, consensus);

            return features;
        }

        private VolatilitySnapshot ResolveVolatility(string symbol, decimal lastPrice, int period)
        {
            decimal? nativeAtr = ReadIndicatorCurrentValue(_atrIndicatorBySymbol, symbol);
            if (nativeAtr.HasValue && nativeAtr.Value > 0m && lastPrice > 0m)
            {
                return new VolatilitySnapshot
                {
                    Atr = ToDouble(nativeAtr.Value),
                    Natr = ToDouble(nativeAtr.Value / lastPrice * 100m),
                    Source = "MATRIX_NATIVE_ATR"
                };
            }

            OhlcvSnapshot current;
            string actualPeriod = _lastOhlcvBySymbol.TryGetValue(NormalizeSymbol(symbol), out current)
                ? current.ActualBarPeriod
                : NormalizePeriodName(IndicatorPeriod.ToString());
            string seriesKey = BuildSeriesKey(symbol, actualPeriod);
            List<OhlcvBarPoint> bars;
            lock (_closeLock)
            {
                List<OhlcvBarPoint> source;
                bars = _ohlcvHistoryBySeries.TryGetValue(seriesKey, out source)
                    ? new List<OhlcvBarPoint>(source)
                    : new List<OhlcvBarPoint>();
            }
            if (bars.Count <= period || lastPrice <= 0m)
                return new VolatilitySnapshot { Atr = null, Natr = null, Source = "UNAVAILABLE" };

            var trueRanges = new List<decimal>();
            int start = Math.Max(1, bars.Count - period);
            for (int i = start; i < bars.Count; i++)
            {
                decimal previousClose = bars[i - 1].Close;
                decimal range = Math.Max(
                    bars[i].High - bars[i].Low,
                    Math.Max(Math.Abs(bars[i].High - previousClose), Math.Abs(bars[i].Low - previousClose)));
                trueRanges.Add(range);
            }
            if (trueRanges.Count == 0)
                return new VolatilitySnapshot { Atr = null, Natr = null, Source = "UNAVAILABLE" };
            decimal atr = trueRanges.Average();
            return new VolatilitySnapshot
            {
                Atr = ToDouble(atr),
                Natr = ToDouble(atr / lastPrice * 100m),
                Source = "OHLC_TRUE_RANGE_SMA"
            };
        }

        private IndicatorConsensus CalculateIndicatorConsensus(
            decimal lastPrice,
            double? rsi,
            double? ema20,
            double? ema50,
            double? macd,
            double? macdSignal)
        {
            int buy = 0;
            int sell = 0;
            int neutral = 0;

            AddVote(ref buy, ref sell, ref neutral, rsi.HasValue && rsi.Value < 35, rsi.HasValue && rsi.Value > 70);
            AddVote(ref buy, ref sell, ref neutral, ema20.HasValue && lastPrice > ToDecimal(ema20.Value), ema20.HasValue && lastPrice < ToDecimal(ema20.Value));
            AddVote(ref buy, ref sell, ref neutral, ema20.HasValue && ema50.HasValue && ema20.Value > ema50.Value, ema20.HasValue && ema50.HasValue && ema20.Value < ema50.Value);
            AddVote(ref buy, ref sell, ref neutral, macd.HasValue && macdSignal.HasValue && macd.Value > macdSignal.Value, macd.HasValue && macdSignal.HasValue && macd.Value < macdSignal.Value);
            AddVote(ref buy, ref sell, ref neutral, ema50.HasValue && lastPrice > ToDecimal(ema50.Value), ema50.HasValue && lastPrice < ToDecimal(ema50.Value));

            int total = buy + sell + neutral;
            string signal = "NEUTRAL";
            int maxDirectional = Math.Max(buy, sell);
            if (buy >= 4)
                signal = "BUY";
            else if (sell >= 4)
                signal = "SELL";

            return new IndicatorConsensus
            {
                BuyCount = buy,
                SellCount = sell,
                NeutralCount = neutral,
                Signal = signal,
                Ratio = total > 0 ? ToDouble((decimal)maxDirectional / total) : 0
            };
        }

        private static void AddVote(ref int buy, ref int sell, ref int neutral, bool buyCondition, bool sellCondition)
        {
            if (buyCondition && !sellCondition)
                buy++;
            else if (sellCondition && !buyCondition)
                sell++;
            else
                neutral++;
        }

        private string CalculateAlphaTrendProxySignal(
            double? rsi,
            double? ema20,
            double? ema50,
            double? macd,
            double? macdSignal)
        {
            if (!rsi.HasValue || !ema20.HasValue || !ema50.HasValue || !macd.HasValue || !macdSignal.HasValue)
                return "NEUTRAL";

            bool trendUp = ema20.Value > ema50.Value;
            bool trendDown = ema20.Value < ema50.Value;
            bool momentumUp = macd.Value > macdSignal.Value && rsi.Value >= 40 && rsi.Value <= 75;
            bool momentumDown = macd.Value < macdSignal.Value && rsi.Value >= 25;

            if (trendUp && momentumUp)
                return "BUY";
            if (trendDown && momentumDown)
                return "SELL";
            return "NEUTRAL";
        }

        private double? CalculateAverageAbsoluteCloseChange(string symbol, int period)
        {
            var closes = new List<decimal>();
            lock (_closeLock)
            {
                closes = new List<decimal>(GetCloseHistory(symbol));
            }

            if (closes.Count <= period)
                return null;

            decimal totalRange = 0m;
            for (int i = closes.Count - period; i < closes.Count; i++)
            {
                totalRange += Math.Abs(closes[i] - closes[i - 1]);
            }

            return ToDouble(totalRange / period);
        }

        private double? CalculateNormalizedCloseChangePct(string symbol, int period)
        {
            double? closeChange = CalculateAverageAbsoluteCloseChange(symbol, period);
            if (!closeChange.HasValue)
                return null;

            decimal lastClose = 0m;
            lock (_closeLock)
            {
                var closes = GetCloseHistory(symbol);
                if (closes.Count > 0)
                    lastClose = closes[closes.Count - 1];
            }

            if (lastClose <= 0m)
                return null;

            return ToDouble(ToDecimal(closeChange.Value) / lastClose * 100m);
        }

        private string ClassifyMarketRegime(double? natr, IndicatorConsensus consensus)
        {
            if (natr.HasValue && natr.Value >= 8)
                return "HIGH_VOLATILITY";
            if (consensus.Signal == "BUY" || consensus.Signal == "SELL")
                return "TRENDING";
            if (natr.HasValue && natr.Value <= 2)
                return "RANGE_LOW_VOLATILITY";
            return "NEUTRAL";
        }

        // ── Daily cache management ──────────────────────────────────

        private void ResetDailyCachesIfNeeded()
        {
            if (_dailyCounterDate == DateTime.Today)
                return;

            lock (_dailyCounterLock)
            {
                if (_dailyCounterDate == DateTime.Today)
                    return; // double-checked

                _dailyCounterDate = DateTime.Today;
                _maxBid1SizeBySymbol.Clear();
                _dailyTradeCountBySymbol.Clear();
                _dailyRefPriceBySymbol.Clear();
                SafeDebug("Daily caches reset.");
            }
        }

        // ── Symbol helpers ──────────────────────────────────────────

        private bool IsAllowedSymbol(string symbol)
        {
            // Empty AllowedSymbols means "whole scanned universe" (allow all);
            // a non-empty set is an explicit whitelist. The BUY/SELL and
            // decline gates run separately in TrySendOrderAsync.
            if (AllowedSymbols == null || AllowedSymbols.Length == 0)
                return true;
            string normalized = NormalizeSymbol(symbol);
            return AllowedSymbols.Any(x => NormalizeSymbol(x) == normalized);
        }

        private bool IsIndexSymbol(string symbol)
        {
            return string.Equals(
                ResolveInstrumentType(symbol),
                "INDEX",
                StringComparison.Ordinal);
        }

        private bool TryGetLastUpdateForSymbol(string symbol, out DateTime timestamp)
        {
            timestamp = DateTime.MinValue;
            try
            {
                object currentValues = ReadPublicMember(this, "BarDataCurrentValues");
                if (currentValues == null)
                    return false;
                MethodInfo method = currentValues.GetType().GetMethod(
                    "GetLastUpdateForSymbol",
                    BindingFlags.Instance | BindingFlags.Public,
                    null,
                    new[] { typeof(string), typeof(SymbolPeriod) },
                    null);
                if (method == null)
                    return false;
                object value = method.Invoke(currentValues, new object[] { symbol, IndicatorPeriod });
                return TryConvertTimestamp(value, out timestamp);
            }
            catch
            {
                return false;
            }
        }

        private bool IsEquitySymbol(string symbol)
        {
            return string.Equals(
                ResolveInstrumentType(symbol),
                "EQUITY",
                StringComparison.Ordinal);
        }

        private string ResolveInstrumentType(string symbol)
        {
            string normalized = NormalizeSymbol(symbol);
            if (normalized == "")
                return "UNKNOWN";
            string configuredType;
            if (InstrumentTypes != null
                && InstrumentTypes.TryGetValue(normalized, out configuredType))
                return NormalizeInstrumentType(configuredType);
            if (normalized == NormalizeSymbol(MarketIndexSymbol))
                return "INDEX";

            // Compatibility fallback for old backends without instrumentTypes.
            // New server payloads classify the full subscription universe, so
            // normal production conversion is type-driven rather than keyed to
            // any individual security code.
            if (normalized.Length >= 4
                && normalized[0] == 'X'
                && normalized.All(char.IsLetterOrDigit))
                return "INDEX";
            return "EQUITY";
        }

        private static string NormalizeInstrumentType(string raw)
        {
            string value = (raw ?? string.Empty).Trim().ToUpperInvariant();
            if (value == "INDEX") return "INDEX";
            if (value == "FUTURE" || value == "FUTURES" || value == "VIOP" || value == "DERIVATIVE")
                return "DERIVATIVE";
            if (value == "EQUITY" || value == "STOCK" || value == "SHARE") return "EQUITY";
            return "UNKNOWN";
        }

        private static bool NewsMatchesKeyword(NewsSnapshot item, string keyword)
        {
            if (string.IsNullOrWhiteSpace(keyword))
                return true;

            string normalized = keyword.Trim();
            if (!string.IsNullOrWhiteSpace(item.Header)
                && item.Header.IndexOf(normalized, StringComparison.OrdinalIgnoreCase) >= 0)
                return true;
            if (item.Categories != null && item.Categories.Any(x => !string.IsNullOrWhiteSpace(x) && x.IndexOf(normalized, StringComparison.OrdinalIgnoreCase) >= 0))
                return true;
            if (item.Symbols != null && item.Symbols.Any(x => !string.IsNullOrWhiteSpace(x) && x.IndexOf(normalized, StringComparison.OrdinalIgnoreCase) >= 0))
                return true;
            if (item.Sources != null && item.Sources.Any(x => !string.IsNullOrWhiteSpace(x) && x.IndexOf(normalized, StringComparison.OrdinalIgnoreCase) >= 0))
                return true;
            if (item.MatchedFilters != null && item.MatchedFilters.Any(x => !string.IsNullOrWhiteSpace(x) && x.IndexOf(normalized, StringComparison.OrdinalIgnoreCase) >= 0))
                return true;
            return false;
        }

        private static string[] ParseSymbolsCsv(string csv)
        {
            return (csv ?? "")
                .Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(NormalizeSymbol)
                .Where(x => x != "")
                .Distinct()
                .ToArray();
        }

        private static string[] ParseCsvList(string raw)
        {
            return (raw ?? "")
                .Split(new[] { ',', ';', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(x => x.Trim())
                .Where(x => x != "")
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToArray();
        }

        private static List<NewsSymbolKeywordSubscription> ParseNewsSymbolKeywordRules(string raw)
        {
            var rules = new List<NewsSymbolKeywordSubscription>();
            foreach (string entry in (raw ?? "").Split(new[] { ';', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string part = entry.Trim();
                int separator = part.IndexOf('=');
                if (separator <= 0 || separator >= part.Length - 1)
                    continue;

                string symbol = NormalizeSymbol(part.Substring(0, separator));
                List<string> keywords = part.Substring(separator + 1)
                    .Split(new[] { '|', ',' }, StringSplitOptions.RemoveEmptyEntries)
                    .Select(x => x.Trim())
                    .Where(x => x != "")
                    .Distinct(StringComparer.OrdinalIgnoreCase)
                    .ToList();
                if (symbol == "" || keywords.Count == 0)
                    continue;

                rules.Add(new NewsSymbolKeywordSubscription
                {
                    Symbol = symbol,
                    Keywords = keywords,
                    OnlyInHeaders = true,
                    IsExactMatch = false
                });
            }
            return rules;
        }

        private static Dictionary<string, decimal> ParseLockedCsv(string csv)
        {
            var result = new Dictionary<string, decimal>();
            foreach (string pairRaw in (csv ?? "").Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string[] parts = pairRaw.Split(':');
                if (parts.Length != 2)
                    continue;

                string symbol = NormalizeSymbol(parts[0]);
                if (symbol == "")
                    continue;

                decimal qty;
                if (decimal.TryParse(parts[1].Trim(), out qty) && qty > 0m)
                {
                    result[symbol] = qty;
                }
            }
            return result;
        }

        private static string NormalizeSymbol(string symbol)
        {
            return (symbol ?? "").Trim().ToUpperInvariant();
        }

        private static string NormalizeAction(string action)
        {
            return (action ?? "WAIT").Trim().ToUpperInvariant();
        }

        // v2: NormalizeMode (PAPER/MANUAL/DEMO_LIVE/REAL_LIVE) kaldırıldı;
        // çalışma modu artık NormalizeSystemMode ile OBSERVE_ONLY/AUTO_TRADE.

        private static string NormalizeOrderSide(string side)
        {
            string value = (side ?? "").Trim().ToUpperInvariant();
            if (value.Contains("SELL"))
                return "SELL";
            if (value.Contains("BUY"))
                return "BUY";
            return value == "" ? "BUY" : value;
        }

        private static string NormalizeOrderStatus(object ordStatus)
        {
            if (ordStatus == null)
                return "UNKNOWN";

            if (ordStatus.Equals(OrdStatus.New))
                return "NEW";
            if (ordStatus.Equals(OrdStatus.PartiallyFilled))
                return "PARTIALLY_FILLED";
            if (ordStatus.Equals(OrdStatus.Filled))
                return "FILLED";
            if (ordStatus.Equals(OrdStatus.Canceled))
                return "CANCELED";
            if (ordStatus.Equals(OrdStatus.Rejected))
                return "REJECTED";
            if (ordStatus.Equals(OrdStatus.Expired))
                return "EXPIRED";

            return ordStatus.ToString().Trim().ToUpperInvariant();
        }

        private static string NormalizeTimeInForce(string value)
        {
            string normalized = (value ?? "Day").Trim().ToUpperInvariant();
            if (normalized == "GTC" || normalized == "GOODTILLCANCEL" || normalized == "GOOD_TILL_CANCEL")
                return "GOOD_TILL_CANCEL";
            return "DAY";
        }

        private static string NormalizeDataType(string dataType)
        {
            string value = (dataType ?? "DEPTH").Trim().ToUpperInvariant();
            if (value == "ORDER_FLOW")
                return "BROKER_FLOW";
            if (value == "")
                return "DEPTH";
            return value;
        }

        // ── Type conversion ─────────────────────────────────────────

        private static double ToDouble(decimal value)
        {
            return Convert.ToDouble(value);
        }

        private static decimal ToDecimal(double value)
        {
            return Convert.ToDecimal(value);
        }

        private static void AddIfNotNull(Dictionary<string, object> payload, string key, double? value)
        {
            if (value.HasValue)
            {
                payload[key] = value.Value;
            }
        }

        // ── HTTP protocol helpers (TradeAiHttpApiTest'ten birebir) ──

        private async Task<HttpRequest> ReadRequestAsync(NetworkStream stream, CancellationToken token)
        {
            var buffer = new byte[8192];
            var data = new List<byte>();
            int headerEnd = -1;

            while (!token.IsCancellationRequested && data.Count < MaxHttpHeaderBytes)
            {
                int read = await stream.ReadAsync(buffer, 0, buffer.Length, token);
                if (read <= 0)
                {
                    break;
                }

                for (int i = 0; i < read; i++)
                {
                    data.Add(buffer[i]);
                }

                headerEnd = FindHeaderEnd(data);
                if (headerEnd >= 0)
                {
                    break;
                }
            }

            if (headerEnd < 0)
            {
                return InvalidHttpRequest(data.Count >= MaxHttpHeaderBytes
                    ? "request headers too large" : "incomplete request headers");
            }

            string headerText = Encoding.UTF8.GetString(data.GetRange(0, headerEnd).ToArray());
            string[] lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.None);
            if (lines.Length == 0)
            {
                return null;
            }

            string[] requestLine = lines[0].Split(' ');
            if (requestLine.Length != 3 || !requestLine[2].StartsWith("HTTP/1.")) return InvalidHttpRequest("invalid request line");
            string method = requestLine[0].ToUpperInvariant();
            if (method != "GET" && method != "POST") return InvalidHttpRequest("unsupported HTTP method");

            var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 1; i < lines.Length; i++)
            {
                int idx = lines[i].IndexOf(':');
                if (idx <= 0)
                {
                    continue;
                }
                string key = lines[i].Substring(0, idx).Trim();
                string value = lines[i].Substring(idx + 1).Trim();
                headers[key] = value;
            }

            int contentLength = 0;
            if (headers.ContainsKey("Content-Length"))
            {
                if (!int.TryParse(headers["Content-Length"], out contentLength)
                    || contentLength < 0 || contentLength > MaxHttpBodyBytes)
                    return InvalidHttpRequest("invalid Content-Length");
            }
            string transferEncoding;
            if (headers.TryGetValue("Transfer-Encoding", out transferEncoding)
                && !string.IsNullOrWhiteSpace(transferEncoding))
                return InvalidHttpRequest("unsupported Transfer-Encoding; chunked requests are not accepted");

            int bodyStart = headerEnd + 4;
            while (data.Count - bodyStart < contentLength && !token.IsCancellationRequested)
            {
                int read = await stream.ReadAsync(buffer, 0, buffer.Length, token);
                if (read <= 0)
                {
                    break;
                }
                for (int i = 0; i < read; i++)
                {
                    data.Add(buffer[i]);
                }
            }
            if (data.Count - bodyStart < contentLength) return InvalidHttpRequest("incomplete request body");

            string body = "";
            if (contentLength > 0 && data.Count >= bodyStart)
            {
                int available = Math.Min(contentLength, data.Count - bodyStart);
                body = Encoding.UTF8.GetString(data.GetRange(bodyStart, available).ToArray());
            }

            string path = requestLine[1];
            var query = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            int queryIndex = path.IndexOf('?');
            if (queryIndex >= 0)
            {
                string queryText = path.Substring(queryIndex + 1);
                path = path.Substring(0, queryIndex);
                foreach (string pairRaw in queryText.Split('&'))
                {
                    int eq = pairRaw.IndexOf('=');
                    if (eq <= 0)
                    {
                        continue;
                    }
                    string key = Uri.UnescapeDataString(pairRaw.Substring(0, eq));
                    string value = Uri.UnescapeDataString(pairRaw.Substring(eq + 1));
                    query[key] = value;
                }
            }

            return new HttpRequest
            {
                Method = method,
                Path = path,
                Headers = headers,
                Query = query,
                Body = body
            };
        }

        private static string MaskAccountId(string accountId)
        {
            string value = (accountId ?? "").Trim();
            if (value.Length <= 4) return "****";
            return new string('*', Math.Min(8, value.Length - 4)) + value.Substring(value.Length - 4);
        }

        private static HttpRequest InvalidHttpRequest(string error)
        {
            return new HttpRequest { ParseError = error };
        }

        private static int FindHeaderEnd(List<byte> data)
        {
            for (int i = 3; i < data.Count; i++)
            {
                if (data[i - 3] == 13 && data[i - 2] == 10 && data[i - 1] == 13 && data[i] == 10)
                {
                    return i - 3;
                }
            }
            return -1;
        }

        private bool IsAuthorized(HttpRequest request)
        {
            if (!request.Headers.ContainsKey("Authorization"))
            {
                return false;
            }

            string expected = "Bearer " + (ApiToken ?? "");
            return string.Equals(request.Headers["Authorization"], expected, StringComparison.Ordinal);
        }

        private async Task WriteJsonAsync(NetworkStream stream, int statusCode, object payload)
        {
            string json = JsonConvert.SerializeObject(payload);
            byte[] body = Encoding.UTF8.GetBytes(json);
            string statusText = StatusText(statusCode);
            string header =
                "HTTP/1.1 " + statusCode + " " + statusText + "\r\n" +
                "Content-Type: application/json; charset=utf-8\r\n" +
                "Content-Length: " + body.Length + "\r\n" +
                "Connection: close\r\n" +
                "\r\n";

            byte[] headerBytes = Encoding.ASCII.GetBytes(header);
            await stream.WriteAsync(headerBytes, 0, headerBytes.Length);
            await stream.WriteAsync(body, 0, body.Length);
        }

        private static string StatusText(int statusCode)
        {
            if (statusCode == 200) return "OK";
            if (statusCode == 400) return "Bad Request";
            if (statusCode == 401) return "Unauthorized";
            if (statusCode == 404) return "Not Found";
            if (statusCode == 500) return "Internal Server Error";
            return "OK";
        }

        private void SafeDebug(string message)
        {
            try
            {
                Debug("[TradeAI Gateway] " + message);
            }
            catch
            {
            }
        }

        // ── Internal types ──────────────────────────────────────────

        private class HttpRequest
        {
            public string Method { get; set; }
            public string Path { get; set; }
            public Dictionary<string, string> Headers { get; set; }
            public Dictionary<string, string> Query { get; set; }
            public string Body { get; set; }
            public string ParseError { get; set; }

            public string GetQueryValue(string key)
            {
                if (Query == null)
                    return "";
                return Query.TryGetValue(key, out var value) ? value : "";
            }
        }

        private struct IndicatorConsensus
        {
            public int BuyCount { get; set; }
            public int SellCount { get; set; }
            public int NeutralCount { get; set; }
            public string Signal { get; set; }
            public double Ratio { get; set; }
        }

        private struct OhlcvBarPoint
        {
            public decimal Open { get; set; }
            public decimal High { get; set; }
            public decimal Low { get; set; }
            public decimal Close { get; set; }
            public decimal Volume { get; set; }
            public bool Reliable { get; set; }
            public bool Closed { get; set; }
        }

        private struct VolatilitySnapshot
        {
            public double? Atr { get; set; }
            public double? Natr { get; set; }
            public string Source { get; set; }
        }

        private struct MarketQuoteSnapshot
        {
            public decimal Last { get; set; }
            public decimal Bid { get; set; }
            public decimal Ask { get; set; }
            public decimal TotalVol { get; set; }
            public bool Reliable { get; set; }
            public string Source { get; set; }
            public DateTime LastTradeUtc { get; set; }
            public DateTime ReadUtc { get; set; }
            public string TimestampSource { get; set; }
        }

        private struct OhlcvSnapshot
        {
            public decimal Open { get; set; }
            public decimal High { get; set; }
            public decimal Low { get; set; }
            public decimal Close { get; set; }
            public decimal Volume { get; set; }
            public bool Reliable { get; set; }
            public string Source { get; set; }
            public DateTime ReceivedAtUtc { get; set; }
            public DateTime BarEventUtc { get; set; }
            public DateTime ResolvedTimestamp { get; set; }
            public string BarTimestampSource { get; set; }
            public string BarObservationSource { get; set; }
            public bool BarTimeReliable { get; set; }
            public string ActualBarPeriod { get; set; }
            public int? ActualBarPeriodSeconds { get; set; }
            public string BarPeriodSource { get; set; }
            public bool BarClosed { get; set; }
            public bool BarIsNew { get; set; }
            public int BarDataIndex { get; set; }
            public DateTime LastTickTime { get; set; }
        }

        private sealed class PositionMarketSnapshot
        {
            public decimal QtyAvailable { get; set; }
            public decimal QtyNet { get; set; }
            public decimal AvgCost { get; set; }
            public decimal OpeningAveragePrice { get; set; }
            public decimal Amount { get; set; }
            public decimal SettlementPx { get; set; }
            public string Currency { get; set; }
            public string AccountId { get; set; }
            public DateTime UpdatedAt { get; set; }
            public string Source { get; set; }
        }

        private struct MarketDataPayload
        {
            public string Symbol { get; set; }
            public string DataType { get; set; }
            public Dictionary<string, object> Payload { get; set; }
            public string Timestamp { get; set; }
        }

        private struct NewsSnapshot
        {
            public int NewsId { get; set; }
            public string Header { get; set; }
            public DateTime DateTime { get; set; }
            [JsonProperty("publishedAt")]
            public string PublishedAt
            {
                get { return DateTime == DateTime.MinValue ? null : DateTime.ToUniversalTime().ToString("o"); }
            }
            public List<string> Categories { get; set; }
            public List<string> Symbols { get; set; }
            public List<string> Sources { get; set; }
            public string FilterType { get; set; }
            public List<string> MatchedFilters { get; set; }
            public bool HasAttachments { get; set; }
            public bool HasDetail { get; set; }
            public int DailyNewsNo { get; set; }
        }

        private struct NewsKeywordSubscription
        {
            public string Keyword { get; set; }
            public bool OnlyInHeaders { get; set; }
            public bool IsExactMatch { get; set; }
        }

        private struct NewsSymbolKeywordSubscription
        {
            public string Symbol { get; set; }
            public List<string> Keywords { get; set; }
            public bool OnlyInHeaders { get; set; }
            public bool IsExactMatch { get; set; }
        }

        private struct OrderRequest
        {
            [JsonProperty("requestId")]
            public string RequestId { get; set; }

            [JsonProperty("symbol")]
            public string Symbol { get; set; }

            [JsonProperty("side")]
            public string Side { get; set; }

            [JsonProperty("qty")]
            public double Qty { get; set; }

            [JsonProperty("limitPrice")]
            public double LimitPrice { get; set; }

            [JsonProperty("mode")]
            public string Mode { get; set; }
        }

        private struct CancelOrderRequest
        {
            [JsonProperty("orderId")]
            public string OrderId { get; set; }
        }

        private sealed class GatewayOrderSnapshot
        {
            [JsonProperty("orderId")]
            public string OrderId { get; set; }
            [JsonProperty("requestId")]
            public string RequestId { get; set; }
            [JsonProperty("symbol")]
            public string Symbol { get; set; }
            [JsonProperty("side")]
            public string Side { get; set; }
            [JsonProperty("status")]
            public string Status { get; set; }
            [JsonProperty("qty")]
            public decimal Qty { get; set; }
            [JsonProperty("filledQty")]
            public decimal FilledQty { get; set; }
            [JsonProperty("price")]
            public decimal Price { get; set; }
            [JsonProperty("avgPrice")]
            public decimal AvgPrice { get; set; }
            [JsonProperty("updatedAt")]
            public DateTime UpdatedAt { get; set; }
        }

        private struct OrderResultRequest
        {
            [JsonProperty("requestId")]
            public string RequestId { get; set; }

            [JsonProperty("symbol")]
            public string Symbol { get; set; }

            [JsonProperty("action")]
            public string Action { get; set; }

            [JsonProperty("qty")]
            public double Qty { get; set; }

            [JsonProperty("price")]
            public double Price { get; set; }

            [JsonProperty("orderQty")]
            public double OrderQty { get; set; }

            [JsonProperty("filledQty")]
            public double FilledQty { get; set; }

            [JsonProperty("lastFillQty")]
            public double LastFillQty { get; set; }

            [JsonProperty("avgPrice")]
            public double AvgPrice { get; set; }

            [JsonProperty("limitPrice")]
            public double LimitPrice { get; set; }

            [JsonProperty("status")]
            public string Status { get; set; }

            [JsonProperty("matriksMessage")]
            public string MatriksMessage { get; set; }

            [JsonProperty("orderId", NullValueHandling = NullValueHandling.Ignore)]
            public string OrderId { get; set; }
        }

        private struct PendingOrderContext
        {
            public string RequestId { get; set; }
            public string Symbol { get; set; }
            public string Action { get; set; }
            public decimal Qty { get; set; }
            public decimal Price { get; set; }
        }

        private struct OrderExecutionResult
        {
            public bool Success { get; set; }
            public string OrderId { get; set; }
            public string Message { get; set; }
        }

        private sealed class DepthSnapshot
        {
            public List<DepthLevelSnapshot> Bids { get; set; }
            public List<DepthLevelSnapshot> Asks { get; set; }
            public DepthAnalysisSnapshot Analysis { get; set; }
        }

        private sealed class DepthLevelSnapshot
        {
            [JsonProperty("level")]
            public int Level { get; set; }
            [JsonProperty("price")]
            public decimal Price { get; set; }
            [JsonProperty("size")]
            public decimal Size { get; set; }
            [JsonProperty("orderCount")]
            public int OrderCount { get; set; }
        }

        private sealed class DepthBandSnapshot
        {
            [JsonProperty("totalBidSize")]
            public decimal TotalBidSize { get; set; }
            [JsonProperty("totalAskSize")]
            public decimal TotalAskSize { get; set; }
            [JsonProperty("bidAskRatio")]
            public decimal BidAskRatio { get; set; }
            [JsonProperty("imbalance")]
            public decimal Imbalance { get; set; }
            [JsonProperty("bidOrderCount")]
            public int BidOrderCount { get; set; }
            [JsonProperty("askOrderCount")]
            public int AskOrderCount { get; set; }
            [JsonProperty("averageBidOrderSize")]
            public decimal AverageBidOrderSize { get; set; }
            [JsonProperty("averageAskOrderSize")]
            public decimal AverageAskOrderSize { get; set; }
        }

        private sealed class DepthWallSnapshot
        {
            [JsonProperty("level")]
            public int Level { get; set; }
            [JsonProperty("price")]
            public decimal Price { get; set; }
            [JsonProperty("size")]
            public decimal Size { get; set; }
            [JsonProperty("orderCount")]
            public int OrderCount { get; set; }
            [JsonProperty("distancePct")]
            public decimal DistancePct { get; set; }
        }

        private sealed class DepthAnalysisSnapshot
        {
            [JsonProperty("available")]
            public bool Available { get; set; }
            [JsonProperty("levelsUsed")]
            public int LevelsUsed { get; set; }
            [JsonProperty("bestBid")]
            public decimal BestBid { get; set; }
            [JsonProperty("bestAsk")]
            public decimal BestAsk { get; set; }
            [JsonProperty("spread")]
            public decimal Spread { get; set; }
            [JsonProperty("spreadPct")]
            public decimal SpreadPct { get; set; }
            [JsonProperty("bidSizeTop1")]
            public decimal BidSizeTop1 { get; set; }
            [JsonProperty("askSizeTop1")]
            public decimal AskSizeTop1 { get; set; }
            [JsonProperty("top1")]
            public DepthBandSnapshot Top1 { get; set; }
            [JsonProperty("top3")]
            public DepthBandSnapshot Top3 { get; set; }
            [JsonProperty("top5")]
            public DepthBandSnapshot Top5 { get; set; }
            [JsonProperty("top10")]
            public DepthBandSnapshot Top10 { get; set; }
            [JsonProperty("top25")]
            public DepthBandSnapshot Top25 { get; set; }
            [JsonProperty("bidAskRatioTop5")]
            public decimal BidAskRatioTop5 { get; set; }
            [JsonProperty("bidAskRatioTop10")]
            public decimal BidAskRatioTop10 { get; set; }
            [JsonProperty("bidAskRatioTop25")]
            public decimal BidAskRatioTop25 { get; set; }
            [JsonProperty("imbalanceTop5")]
            public decimal ImbalanceTop5 { get; set; }
            [JsonProperty("imbalanceTop10")]
            public decimal ImbalanceTop10 { get; set; }
            [JsonProperty("imbalanceTop25")]
            public decimal ImbalanceTop25 { get; set; }
            [JsonProperty("weightedBidPrice")]
            public decimal WeightedBidPrice { get; set; }
            [JsonProperty("weightedAskPrice")]
            public decimal WeightedAskPrice { get; set; }
            [JsonProperty("bidConcentrationTop3Pct")]
            public decimal BidConcentrationTop3Pct { get; set; }
            [JsonProperty("askConcentrationTop3Pct")]
            public decimal AskConcentrationTop3Pct { get; set; }
            [JsonProperty("bidWallConcentrationRisk")]
            public bool BidWallConcentrationRisk { get; set; }
            [JsonProperty("askWallConcentrationRisk")]
            public bool AskWallConcentrationRisk { get; set; }
            [JsonProperty("largestBidWall")]
            public DepthWallSnapshot LargestBidWall { get; set; }
            [JsonProperty("largestAskWall")]
            public DepthWallSnapshot LargestAskWall { get; set; }
            [JsonProperty("nearestLargeBidWall")]
            public DepthWallSnapshot NearestLargeBidWall { get; set; }
            [JsonProperty("nearestLargeAskWall")]
            public DepthWallSnapshot NearestLargeAskWall { get; set; }
            public int BidWallCountTop5 { get; set; }
            public int AskWallCountTop5 { get; set; }
            public int BidWallCountTop10 { get; set; }
            public int AskWallCountTop10 { get; set; }
            [JsonProperty("buyPressureScore")]
            public decimal BuyPressureScore { get; set; }
            [JsonProperty("sellPressureScore")]
            public decimal SellPressureScore { get; set; }
            [JsonProperty("orderBookSignal")]
            public string OrderBookSignal { get; set; }
            [JsonProperty("depthTimestamp")]
            public string DepthTimestamp { get; set; }
            [JsonProperty("depthAgeSeconds")]
            public double DepthAgeSeconds { get; set; }
            [JsonProperty("depthReadUtc")]
            public string DepthReadUtc { get; set; }
            [JsonProperty("depthReadLatencySeconds")]
            public double DepthReadLatencySeconds { get; set; }
            [JsonProperty("depthTimestampSource")]
            public string DepthTimestampSource { get; set; }
            [JsonProperty("depthEventTimestampAvailable")]
            public bool DepthEventTimestampAvailable { get; set; }
            [JsonProperty("depthReliable")]
            public bool DepthReliable { get; set; }
        }

        private static bool TryConvertTimestamp(object value, out DateTime timestamp)
        {
            timestamp = DateTime.MinValue;
            if (value == null) return false;
            if (value is DateTime)
            {
                timestamp = (DateTime)value;
                return timestamp != DateTime.MinValue;
            }
            object dtime = ReadPublicMember(value, "Dtime");
            if (dtime != null && !ReferenceEquals(dtime, value))
                return TryConvertTimestamp(dtime, out timestamp);
            DateTime parsed;
            if (DateTime.TryParse(Convert.ToString(value), out parsed))
            {
                timestamp = parsed;
                return timestamp != DateTime.MinValue;
            }
            return false;
        }

        private sealed class GatewayConfigSnapshot
        {
            public readonly string RuntimeMode, ConfigVersion, ProfileCode;
            public readonly bool EnableDemoOrders, EnableRealOrders, RealLiveModeAllowed, RealLiveArmed, RequireDemoAccount, DemoAccountConfirmed, TradingKillSwitchActive, ForceSafeMode;
            public readonly string[] BuyAllowedSymbols, SellExitAllowedSymbols, DeclineSymbols;
            public GatewayConfigSnapshot(string runtimeMode, bool demo, bool real, bool realAllowed, bool armed, bool requireDemo, bool confirmed, bool kill, bool safe, string[] buy, string[] sell, string[] decline, string version, string profile)
            { RuntimeMode = runtimeMode; EnableDemoOrders = demo; EnableRealOrders = real; RealLiveModeAllowed = realAllowed; RealLiveArmed = armed; RequireDemoAccount = requireDemo; DemoAccountConfirmed = confirmed; TradingKillSwitchActive = kill; ForceSafeMode = safe; BuyAllowedSymbols = buy ?? new string[0]; SellExitAllowedSymbols = sell ?? new string[0]; DeclineSymbols = decline ?? new string[0]; ConfigVersion = version; ProfileCode = profile; }
            public static GatewayConfigSnapshot SafeDefault() { return new GatewayConfigSnapshot("PAPER", false, false, false, false, true, false, true, true, new string[0], new string[0], new string[0], "UNAVAILABLE", "UNAVAILABLE"); }
        }

        private sealed class IdempotencyEntry
        {
            public DateTime CreatedUtc { get; set; }
            public bool Accepted { get; set; }
            public string Status { get; set; }
            public string Message { get; set; }
        }

        private struct OrderResultEnvelope
        {
            public PendingOrderContext Context { get; set; }
            public string Status { get; set; }
            public string MatriksMessage { get; set; }
            public string OrderId { get; set; }
            public decimal OrderQty { get; set; }
            public decimal FilledQty { get; set; }
            public decimal LastFillQty { get; set; }
            public decimal AvgPrice { get; set; }
            public decimal LimitPrice { get; set; }
            public decimal Price { get; set; }
        }
    }
}
