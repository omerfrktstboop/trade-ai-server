using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
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
    ///   - Mode kapıları: PAPER/MANUAL → red; DEMO_LIVE → EnableDemoOrders +
    ///     demo hesap onayı; REAL_LIVE → EnableRealOrders (+ RequireDemoAccount)
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

        // Server config gelene kadar bütün emir kapıları fail-closed.
        private bool EnableDemoOrders;
        private bool EnableRealOrders;
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

        private SymbolPeriod IndicatorPeriod = SymbolPeriod.Min5;

        // ── Symbols ──────────────────────────────────────────────────

        private string[] AllowedSymbols = new string[0];
        private Dictionary<string, decimal> LockedLongTermQty = new Dictionary<string, decimal>();

        // ── Constants ────────────────────────────────────────────────

        private const int MaxCloseHistory = 240;

        // ── HTTP server state ────────────────────────────────────────

        private TcpListener _listener;
        private CancellationTokenSource _cts;
        private Task _serverTask;
        private DateTime _startedAt;
        private int _requestCount;

        // ── Market data state (TradeAiAgenticBot'tan birebir) ────────

        private readonly ConcurrentDictionary<string, decimal> _botPositionQtyBySymbol = new ConcurrentDictionary<string, decimal>();
        private readonly ConcurrentDictionary<string, List<decimal>> _closeHistoryBySymbol = new ConcurrentDictionary<string, List<decimal>>();
        private readonly ConcurrentDictionary<string, MarketQuoteSnapshot> _lastValidQuoteBySymbol = new ConcurrentDictionary<string, MarketQuoteSnapshot>();
        private readonly ConcurrentDictionary<string, OhlcvSnapshot> _lastOhlcvBySymbol = new ConcurrentDictionary<string, OhlcvSnapshot>();
        private readonly ConcurrentDictionary<string, DateTime> _marketDataWarningUtcByKey = new ConcurrentDictionary<string, DateTime>();
        private readonly ConcurrentDictionary<string, decimal> _maxBid1SizeBySymbol = new ConcurrentDictionary<string, decimal>();
        private readonly ConcurrentDictionary<string, RSI> _rsiBySymbol = new ConcurrentDictionary<string, RSI>();
        private readonly ConcurrentDictionary<string, MOV> _ema20BySymbol = new ConcurrentDictionary<string, MOV>();
        private readonly ConcurrentDictionary<string, MOV> _ema50BySymbol = new ConcurrentDictionary<string, MOV>();
        private readonly ConcurrentDictionary<string, MACD> _macdBySymbol = new ConcurrentDictionary<string, MACD>();
        private readonly ConcurrentQueue<NewsSnapshot> _recentNews = new ConcurrentQueue<NewsSnapshot>();

        private readonly object _closeLock = new object();
        private readonly object _dailyCounterLock = new object();

        // ── Order path state (Phase 2, TradeAiAgenticBot'tan birebir) ─

        private HttpClient _http;
        private readonly JsonSerializerSettings _jsonSettings = new JsonSerializerSettings
        {
            NullValueHandling = NullValueHandling.Ignore
        };
        private readonly ConcurrentDictionary<string, object> _sentRequestIds = new ConcurrentDictionary<string, object>();
        private readonly ConcurrentDictionary<string, int> _dailyTradeCountBySymbol = new ConcurrentDictionary<string, int>();
        private readonly ConcurrentDictionary<string, PendingOrderContext> _pendingOrdersBySymbolSide = new ConcurrentDictionary<string, PendingOrderContext>();
        private readonly ConcurrentDictionary<string, PendingOrderContext> _pendingOrdersByOrderId = new ConcurrentDictionary<string, PendingOrderContext>();

        private DateTime _dailyCounterDate = DateTime.Today;
        private bool _realPositionsLoadedFromSnapshot;
        private bool? _autoOrderEnabled;
        private bool? _testAutoOrderEnabled;
        private bool _subscriptionsInitialized;
        private readonly object _symbolSubscriptionLock = new object();
        private readonly SemaphoreSlim _configFetchLock = new SemaphoreSlim(1, 1);

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
                AddSymbolMarketDepth(normalized);
                AddNewsSymbol(normalized);
                InitializeIndicators(normalized);

                _botPositionQtyBySymbol[normalized] = 0m;
                _closeHistoryBySymbol[normalized] = new List<decimal>();
            }
            _subscriptionsInitialized = true;

            SetTimerInterval(60);
            LogTradeUserInfo();
            LoadRealPositionsSnapshot();

            _cts = new CancellationTokenSource();
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
            UpdateOhlcvSnapshotFromBarData(barData);
            RefreshCloseHistoryFromMarketData();
        }

        public override void OnTimer()
        {
            ResetDailyCachesIfNeeded();
            RefreshCloseHistoryFromMarketData();
            if ((DateTime.UtcNow - _lastConfigFetchUtc).TotalSeconds >= 60)
                _ = FetchAndApplyServerConfigAsync();
            if (!_realPositionsLoadedFromSnapshot)
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

            decimal reportQty = filledQty > 0 ? filledQty : (orderQty > 0 ? orderQty : context.Qty);
            decimal reportPrice = avgPx > 0 ? avgPx : (order.Price > 0 ? order.Price : context.Price);
            string message = "Matriks order update status=" + status
                + " orderQty=" + orderQty
                + " filledQty=" + filledQty
                + " avgPx=" + avgPx;

            PendingOrderContext capturedContext = context;
            Task.Run(async () =>
            {
                await ReportOrderResultAsync(capturedContext, status, message, orderId, reportQty, reportPrice);
            });

            if (IsFinalOrderStatus(status))
            {
                if (!string.IsNullOrWhiteSpace(orderId))
                {
                    _pendingOrdersByOrderId.TryRemove(orderId, out _);
                }
                _pendingOrdersBySymbolSide.TryRemove(BuildSymbolSideKey(symbol, context.Action), out _);
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

            if (request.Method == "POST" && request.Path == "/config/reload")
            {
                bool loaded = await FetchAndApplyServerConfigAsync(true);
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
                        SafeDebug("Server config fetch failed HTTP " + (int)result.StatusCode + " body=" + body);
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
                    IndicatorPeriod = ParseSymbolPeriod(cfg.Value<string>("indicatorPeriod"));
                    if (_subscriptionsInitialized)
                    {
                        foreach (string symbol in symbols)
                            EnsurePortfolioSymbolSubscribed(symbol);
                    }
                    AllowedSymbols = symbols;

                    LockedLongTermQty = cfg["lockedLongTermQty"] != null
                        ? cfg["lockedLongTermQty"].ToObject<Dictionary<string, decimal>>()
                        : new Dictionary<string, decimal>();
                    RuntimeMode = NormalizeMode(cfg.Value<string>("mode"));
                    EnableDemoOrders = cfg.Value<bool?>("enableDemoOrders") ?? false;
                    EnableRealOrders = cfg.Value<bool?>("enableRealOrders") ?? false;
                    RequireDemoAccount = cfg.Value<bool?>("requireDemoAccount") ?? true;
                    DemoAccountConfirmed = cfg.Value<bool?>("demoAccountConfirmed") ?? false;
                    MaxOrderValueTl = cfg.Value<decimal?>("maxOrderValueTl") ?? 0m;
                    MaxQtyPerOrder = cfg.Value<decimal?>("maxQtyPerOrder") ?? 0m;
                    MaxOrdersPerDay = cfg.Value<int?>("maxOrdersPerDay") ?? 0;
                    MaxOrdersPerSymbolPerDay = cfg.Value<int?>("maxOrdersPerSymbolPerDay") ?? 0;
                    OrderTimeInForce = cfg.Value<string>("orderTimeInForce") ?? "Day";
                    ActiveProfileCode = cfg.Value<string>("profileCode") ?? "UNKNOWN";
                    _lastConfigFetchUtc = DateTime.UtcNow;

                    string configSignature = string.Join("|", new[]
                    {
                        ActiveProfileCode,
                        RuntimeMode,
                        string.Join(",", AllowedSymbols.OrderBy(x => x, StringComparer.Ordinal)),
                        EnableDemoOrders.ToString(),
                        EnableRealOrders.ToString(),
                        RequireDemoAccount.ToString(),
                        DemoAccountConfirmed.ToString(),
                        MaxOrderValueTl.ToString(),
                        MaxQtyPerOrder.ToString(),
                        MaxOrdersPerDay.ToString(),
                        MaxOrdersPerSymbolPerDay.ToString(),
                        OrderTimeInForce,
                        IndicatorPeriod.ToString(),
                    });
                    if (!string.Equals(_lastAppliedConfigSignature, configSignature, StringComparison.Ordinal))
                    {
                        _lastAppliedConfigSignature = configSignature;
                        SafeDebug("Server config applied profile=" + ActiveProfileCode
                            + " mode=" + RuntimeMode
                            + " symbols=" + string.Join(",", AllowedSymbols)
                            + " enableDemoOrders=" + EnableDemoOrders
                            + " enableRealOrders=" + EnableRealOrders
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
            var quoteAgeSeconds = new Dictionary<string, double?>();
            foreach (string symbolRaw in AllowedSymbols)
            {
                string symbol = NormalizeSymbol(symbolRaw);
                if (_lastValidQuoteBySymbol.TryGetValue(symbol, out var quote))
                {
                    quoteAgeSeconds[symbol] = Math.Round((DateTime.Now - quote.UpdatedAt).TotalSeconds, 1);
                }
                else
                {
                    quoteAgeSeconds[symbol] = null;
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
                profileCode = ActiveProfileCode,
                runtimeMode = RuntimeMode,
                positionsLoaded = _realPositionsLoadedFromSnapshot,
                autoOrderEnabled = _autoOrderEnabled,
                testAutoOrderEnabled = _testAutoOrderEnabled,
                quoteAgeSeconds = quoteAgeSeconds,
                orderLimits = new
                {
                    enableDemoOrders = EnableDemoOrders,
                    enableRealOrders = EnableRealOrders,
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

            MarketDataPayload data = BuildMarketData(symbol, "OHLCV");
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

            foreach (var kv in _botPositionQtyBySymbol)
            {
                seen.Add(kv.Key);
                entries.Add(new
                {
                    symbol = kv.Key,
                    botQty = ToDouble(kv.Value),
                    lockedLongTermQty = ToDouble(GetLockedLongTermQty(kv.Key)),
                    totalQty = ToDouble(kv.Value + GetLockedLongTermQty(kv.Key))
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
                    totalQty = ToDouble(kv.Value)
                });
            }

            await WriteJsonAsync(stream, 200, new
            {
                ok = true,
                positionsLoaded = _realPositionsLoadedFromSnapshot,
                positions = entries
            });
        }

        // ── Order path (Phase 2 — kilitler TradeAiAgenticBot'tan) ───

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
                var depth = GetMarketDepth(symbol);
                var bids = depth == null || depth.BidRows == null ? new List<object>()
                    : depth.BidRows.Take(levels).Select((row, index) => (object)new
                    { level = index + 1, price = ToDouble(row.Price), size = ToDouble(row.Size), orderCount = row.OrderCount }).ToList();
                var asks = depth == null || depth.AskRows == null ? new List<object>()
                    : depth.AskRows.Take(levels).Select((row, index) => (object)new
                    { level = index + 1, price = ToDouble(row.Price), size = ToDouble(row.Size), orderCount = row.OrderCount }).ToList();
                decimal totalBid = depth == null || depth.BidRows == null ? 0m : depth.BidRows.Take(levels).Sum(x => x.Size);
                decimal totalAsk = depth == null || depth.AskRows == null ? 0m : depth.AskRows.Take(levels).Sum(x => x.Size);
                decimal total = totalBid + totalAsk;
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true, symbol = symbol, levels = levels,
                    available = bids.Count > 0 || asks.Count > 0,
                    bids = bids, asks = asks,
                    analysis = new
                    {
                        totalBidSize = ToDouble(totalBid), totalAskSize = ToDouble(totalAsk),
                        imbalanceRatio = total > 0m ? ToDouble((totalBid - totalAsk) / total) : 0.0,
                        bidAskSizeRatio = totalAsk > 0m ? ToDouble(totalBid / totalAsk) : 0.0
                    },
                    timestamp = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz")
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
            try
            {
                var buyers = new List<object>();
                var sellers = new List<object>();
                for (int rank = 1; rank <= limit; rank++)
                {
                    var buyer = GetBestInstitution(symbol, TransactionDataField.Size, TransactionSide.Net, BestBuyerSellerOrder.NetBuyerLot, MoneyIncomePeriod.Daily, rank, true);
                    var seller = GetBestInstitution(symbol, TransactionDataField.Size, TransactionSide.Net, BestBuyerSellerOrder.NetSellerLot, MoneyIncomePeriod.Daily, rank, true);
                    if (buyer != null) buyers.Add(new { id = buyer.Id, name = buyer.Name, rank = buyer.Rank, value = ToDouble(buyer.Value) });
                    if (seller != null) sellers.Add(new { id = seller.Id, name = seller.Name, rank = seller.Rank, value = ToDouble(seller.Value) });
                }
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true, available = buyers.Count > 0 || sellers.Count > 0,
                    symbol = symbol, period = "DAILY", buyers = buyers, sellers = sellers,
                    requiresLicense = "AKDE/AKD for equities; VAKD for VIOP end-of-day data"
                });
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(stream, 200, new { ok = true, available = false, symbol = symbol, error = ex.Message, requiresLicense = "AKDE/AKD or VAKD" });
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

            ResetDailyCachesIfNeeded();

            string side = NormalizeAction(order.Side);
            string symbol = NormalizeSymbol(order.Symbol);
            string mode = NormalizeMode(order.Mode);
            decimal qty = ToDecimal(order.Qty);
            decimal price = ToDecimal(order.LimitPrice);
            decimal orderValue = qty * price;

            SafeDebug("Order request received requestId=" + order.RequestId
                + " symbol=" + symbol
                + " side=" + side
                + " qty=" + qty
                + " limitPrice=" + price
                + " mode=" + mode);

            // ── Güvenlik kapıları (sıra TradeAiAgenticBot.TrySendOrderAsync) ──

            string rejection = null;

            if (side != "BUY" && side != "SELL")
                rejection = "unknown side=" + order.Side;
            else if (!_sentRequestIds.TryAdd(order.RequestId, null))
                rejection = "duplicate requestId";
            else if (price <= 0m)
                rejection = "limitPrice is null or <= 0";
            else if (qty <= 0m)
                rejection = "qty <= 0";
            else if (!IsAllowedSymbol(symbol))
                rejection = "symbol not allowed: " + symbol;
            else if (!_realPositionsLoadedFromSnapshot)
                rejection = "real positions are not loaded yet";
            else if (orderValue > MaxOrderValueTl)
                rejection = "orderValue exceeds MaxOrderValueTl: " + orderValue;
            else if (qty > MaxQtyPerOrder)
                rejection = "qty exceeds MaxQtyPerOrder: " + qty;
            else if (GetTotalDailyOrderCount() >= MaxOrdersPerDay)
                rejection = "MaxOrdersPerDay reached: " + MaxOrdersPerDay;
            else if (GetDailyTradeCount(symbol) >= MaxOrdersPerSymbolPerDay)
                rejection = "MaxOrdersPerSymbolPerDay reached for " + symbol;
            else if (side == "SELL" && GetSellableQty(symbol) < qty)
                rejection = "SELL qty exceeds sellable position (bot="
                    + GetBotPositionQty(symbol)
                    + " locked=" + GetLockedLongTermQty(symbol) + ")";
            else
                rejection = CheckModeGates(mode);

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

            // ── Tüm kapılar geçildi — LIMIT emir gönder ──

            try
            {
                OrderExecutionResult execution = SendGatewayLimitOrder(order.RequestId, symbol, side, qty, price);
                if (execution.Success)
                {
                    IncrementDailyTradeCount(symbol);
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
                SafeDebug("Order exception requestId=" + order.RequestId + " error=" + ex.Message);
                await WriteJsonAsync(stream, 200, new
                {
                    ok = true,
                    accepted = false,
                    status = "ERROR",
                    requestId = order.RequestId,
                    symbol = symbol,
                    reason = ex.Message
                });
            }
        }

        /// <summary>
        /// Mode kapıları — server'ın bildirdiği mode'a göre son savunma hattı.
        /// null → geçti; aksi halde red gerekçesi.
        /// </summary>
        private string CheckModeGates(string mode)
        {
            if (mode == "PAPER")
                return "Mode=PAPER — orders are never sent in paper mode";

            if (mode == "MANUAL")
                return "Mode=MANUAL requires human confirmation";

            if (mode == "DEMO_LIVE")
            {
                if (!EnableDemoOrders)
                    return "EnableDemoOrders=false";
                if (!IsDemoAccount())
                    return "DEMO_LIVE blocked: demo account is not confirmed";
                return null;
            }

            if (mode == "REAL_LIVE")
            {
                if (!EnableRealOrders)
                    return "REAL_LIVE blocked: EnableRealOrders=false";
                if (RequireDemoAccount && !IsDemoAccount())
                    return "RequireDemoAccount=true and demo account is not confirmed";
                return null;
            }

            return "unsupported mode=" + mode;
        }

        /// <summary>
        /// SELL üst sınırı: bot pozisyonundan kilitli uzun vade lotlar
        /// düşülür — kilitli lotlar hiçbir koşulda satılamaz.
        /// </summary>
        private decimal GetSellableQty(string symbol)
        {
            decimal sellable = GetBotPositionQty(symbol) - GetLockedLongTermQty(symbol);
            return sellable > 0m ? sellable : 0m;
        }

        private OrderExecutionResult SendGatewayLimitOrder(string requestId, string symbol, string side, decimal qty, decimal limitPrice)
        {
            if (!TryConvertOrderQuantity(qty, out int quantity, out string quantityError))
            {
                return new OrderExecutionResult { Success = false, OrderId = null, Message = quantityError };
            }

            if (quantity != qty)
            {
                SafeDebug("Qty converted to int symbol=" + symbol + " original=" + qty + " quantity=" + quantity);
            }

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
            _pendingOrdersBySymbolSide[BuildSymbolSideKey(symbol, side)] = pending;

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
                _pendingOrdersBySymbolSide.TryRemove(BuildSymbolSideKey(symbol, side), out _);
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

            return null;
        }

        private async Task ReportOrderResultAsync(
            PendingOrderContext context,
            string status,
            string matriksMessage,
            string orderId,
            decimal qty,
            decimal price)
        {
            var payload = new OrderResultRequest
            {
                RequestId = context.RequestId,
                Symbol = NormalizeSymbol(context.Symbol),
                Action = NormalizeAction(context.Action),
                Qty = ToDouble(qty),
                Price = ToDouble(price),
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

        private bool IsDemoAccount()
        {
            if (!RequireDemoAccount)
                return true;

            if (!DemoAccountConfirmed)
                return false;

            if (_testAutoOrderEnabled.HasValue)
                return _testAutoOrderEnabled.Value;

            return true;
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
            return value == "FILLED" || value == "CANCELED" || value == "CANCELLED" || value == "REJECTED";
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

        private MarketDataPayload BuildMarketData(string symbolRaw, string dataType)
        {
            string symbol = NormalizeSymbol(symbolRaw);

            MarketQuoteSnapshot quote = ReadMarketQuote(symbol);
            decimal lastPrice = quote.Last;
            decimal bidPrice = quote.Bid;
            decimal askPrice = quote.Ask;
            decimal volume = quote.Volume;

            OhlcvSnapshot ohlc = ResolveOhlcvSnapshot(symbol, lastPrice, volume);
            if (lastPrice <= 0m && ohlc.Close > 0m)
            {
                lastPrice = ohlc.Close;
            }
            if (volume <= 0m && ohlc.Volume > 0m)
            {
                volume = ohlc.Volume;
            }
            decimal open = ohlc.Open;
            decimal high = ohlc.High;
            decimal low = ohlc.Low;
            bool ohlcReliable = ohlc.Reliable;

            UpdateCloseHistory(symbol, lastPrice);

            // Depth data
            decimal bestBid = 0m;
            decimal secondBid = 0m;
            decimal thirdBid = 0m;
            decimal bid1Size = 0m;
            decimal ask1Size = 0m;
            decimal maxBid1Size = 0m;
            decimal depthQueueDropPct = 0m;
            bool depthReliable = false;
            string depthSummary = "";
            try
            {
                var depth = GetMarketDepth(symbol);
                if (depth != null && depth.BidRows != null && depth.BidRows.Count >= 1)
                {
                    bestBid = depth.BidRows[0].Price;
                    bid1Size = depth.BidRows[0].Size;
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
                }
                if (depth != null && depth.BidRows != null && depth.BidRows.Count >= 2)
                {
                    secondBid = depth.BidRows[1].Price;
                }
                if (depth != null && depth.BidRows != null && depth.BidRows.Count >= 3)
                {
                    thirdBid = depth.BidRows[2].Price;
                }
                if (depth != null && depth.AskRows != null && depth.AskRows.Count >= 1)
                {
                    ask1Size = depth.AskRows[0].Size;
                }
                depthReliable = bestBid > 0m && bid1Size > 0m && ask1Size > 0m;
                if (!depthReliable)
                {
                    depthQueueDropPct = 0m;
                    LogMarketDataWarning(symbol, "DEPTH", "Depth unavailable or zero; depthReliable=false");
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

            double? rsi = GetNativeRsi(symbol) ?? CalculateRsi(symbol, 14);
            double? ema20 = GetNativeEma20(symbol) ?? CalculateEma(symbol, 20);
            double? ema50 = GetNativeEma50(symbol) ?? CalculateEma(symbol, 50);
            double? macd = GetNativeMacdLine(symbol) ?? CalculateMacdLine(symbol);
            double? macdSignal = GetNativeMacdSignal(symbol) ?? CalculateMacdSignal(symbol);
            string indicatorSource = ResolveIndicatorSource(rsi, ema20, ema50, macd, macdSignal);
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
            payload["lastPrice"] = ToDouble(lastPrice);
            payload["open"] = ToDouble(open);
            payload["high"] = ToDouble(high);
            payload["low"] = ToDouble(low);
            payload["ohlcReliable"] = ohlcReliable;
            payload["ohlcSource"] = ohlc.Source;
            payload["priceSource"] = quote.Source;
            payload["quoteReliable"] = quote.Reliable;
            payload["depthReliable"] = depthReliable;
            payload["volume"] = ToDouble(volume);
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
            payload["botPositionQty"] = ToDouble(GetBotPositionQty(symbol));
            payload["totalAccountQty"] = ToDouble(GetTotalAccountQty(symbol));
            payload["lockedLongTermQty"] = ToDouble(GetLockedLongTermQty(symbol));
            foreach (var item in technicalFeatures)
            {
                payload[item.Key] = item.Value;
            }
            payload["technicalFeatures"] = technicalFeatures;

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
                SafeDebug("Native indicators initialized symbol=" + symbol + " period=" + IndicatorPeriod);
            }
            catch (Exception ex)
            {
                SafeDebug("Native indicator init failed symbol=" + symbol + " error=" + ex.Message);
            }
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

        private decimal GetBotPositionQty(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            return _botPositionQtyBySymbol.TryGetValue(symbol, out var qty) ? qty : 0m;
        }

        private decimal GetTotalAccountQty(string symbol)
        {
            return GetBotPositionQty(symbol) + GetLockedLongTermQty(symbol);
        }

        private decimal GetLockedLongTermQty(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            return LockedLongTermQty.TryGetValue(symbol, out var qty) ? qty : 0m;
        }

        private void RefreshCloseHistoryFromMarketData()
        {
            foreach (string symbolRaw in AllowedSymbols)
            {
                string symbol = NormalizeSymbol(symbolRaw);
                if (!IsAllowedSymbol(symbol))
                    continue;

                MarketQuoteSnapshot quote = ReadMarketQuote(symbol);
                UpdateCloseHistory(symbol, quote.Last);
            }
        }

        private void LoadRealPositionsSnapshot()
        {
            try
            {
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

                foreach (var item in positions)
                {
                    UpdatePositionCache(item.Value, "GetRealPositions");
                }

                _realPositionsLoadedFromSnapshot = true;
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

            decimal qty = position.QtyAvailable != 0m ? position.QtyAvailable : position.QtyNet;
            if (qty != 0m)
                EnsurePortfolioSymbolSubscribed(symbol);
            _botPositionQtyBySymbol[symbol] = qty;
            SafeDebug(source + " position symbol=" + symbol
                + " qtyAvailable=" + position.QtyAvailable
                + " qtyNet=" + position.QtyNet
                + " cachedQty=" + qty);
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
                AddSymbolMarketDepth(symbol);
                AddNewsSymbol(symbol);
                InitializeIndicators(symbol);

                AllowedSymbols = AllowedSymbols.Concat(new[] { symbol }).ToArray();
                _closeHistoryBySymbol.TryAdd(symbol, new List<decimal>());
                SafeDebug("Portfolio symbol subscribed symbol=" + symbol);
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
                SafeDebug("TradeUser accountId=" + tradeUser.AccountId
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
            decimal rawLast = SafeMarketData(symbol, SymbolUpdateField.Last);
            decimal rawBid = SafeMarketData(symbol, SymbolUpdateField.Bid);
            decimal rawAsk = SafeMarketData(symbol, SymbolUpdateField.Ask);
            decimal rawVolume = SafeMarketData(symbol, SymbolUpdateField.TotalVol);

            bool liveReliable = rawLast > 0m || rawBid > 0m || rawAsk > 0m;
            if (liveReliable)
            {
                if (_lastValidQuoteBySymbol.TryGetValue(symbol, out var previous))
                {
                    if (rawLast <= 0m) rawLast = previous.Last;
                    if (rawVolume <= 0m) rawVolume = previous.Volume;
                }

                var live = new MarketQuoteSnapshot
                {
                    Last = rawLast,
                    Bid = rawBid,
                    Ask = rawAsk,
                    Volume = rawVolume,
                    Reliable = true,
                    Source = "LIVE",
                    UpdatedAt = DateTime.Now
                };
                _lastValidQuoteBySymbol[symbol] = live;
                return live;
            }

            if (_lastValidQuoteBySymbol.TryGetValue(symbol, out var cached)
                && (DateTime.Now - cached.UpdatedAt).TotalHours <= 8)
            {
                cached.Source = "LAST_VALID";
                cached.Reliable = true;
                LogMarketDataWarning(symbol, "QUOTE", "Live quote is zero; using last valid quote");
                return cached;
            }

            LogMarketDataWarning(symbol, "QUOTE", "Live quote is zero and no last valid quote exists");
            return new MarketQuoteSnapshot
            {
                Last = 0m,
                Bid = 0m,
                Ask = 0m,
                Volume = 0m,
                Reliable = false,
                Source = "ZERO_UNAVAILABLE",
                UpdatedAt = DateTime.Now
            };
        }

        private OhlcvSnapshot ResolveOhlcvSnapshot(string symbol, decimal lastPrice, decimal volume)
        {
            symbol = NormalizeSymbol(symbol);
            if (_lastOhlcvBySymbol.TryGetValue(symbol, out var cached)
                && cached.Close > 0m
                && (DateTime.Now - cached.UpdatedAt).TotalHours <= 8)
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
                UpdatedAt = DateTime.Now
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

                var snapshot = new OhlcvSnapshot
                {
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Volume = volume,
                    Reliable = open > 0m && high > 0m && low > 0m && close > 0m,
                    Source = "BAR",
                    UpdatedAt = DateTime.Now
                };
                _lastOhlcvBySymbol[symbol] = snapshot;
                UpdateCloseHistory(symbol, close);
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
                && (now - last).TotalSeconds < 60)
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

        private void UpdateCloseHistory(string symbol, decimal lastPrice)
        {
            if (lastPrice <= 0)
                return;

            symbol = NormalizeSymbol(symbol);

            lock (_closeLock)
            {
                var list = _closeHistoryBySymbol.GetOrAdd(symbol, _ => new List<decimal>());
                list.Add(lastPrice);
                if (list.Count > MaxCloseHistory)
                    list.RemoveAt(0);
            }
        }

        private List<decimal> GetCloseHistory(string symbol)
        {
            symbol = NormalizeSymbol(symbol);

            lock (_closeLock)
            {
                return _closeHistoryBySymbol.GetOrAdd(symbol, _ => new List<decimal>());
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
            double? atr = CalculateAtrFromClose(symbol, 14);
            double? natr = CalculateNatrPct(symbol, 14);

            features["schemaVersion"] = "technical-features-v1";
            features["indicatorSource"] = indicatorSource;
            features["alphaTrendSignal"] = CalculateAlphaTrendProxySignal(rsi, ema20, ema50, macd, macdSignal);
            features["alphaTrendMode"] = "PROXY_EMA_MACD_RSI";
            features["indicatorBuyCount"] = consensus.BuyCount;
            features["indicatorSellCount"] = consensus.SellCount;
            features["indicatorNeutralCount"] = consensus.NeutralCount;
            features["indicatorConsensus"] = consensus.Signal;
            features["indicatorConsensusRatio"] = consensus.Ratio;
            AddIfNotNull(features, "atr", atr);
            AddIfNotNull(features, "natr", natr);
            features["depthReliable"] = depthReliable;
            if (depthReliable)
            {
                features["depthBid1Size"] = ToDouble(bid1Size);
                features["depthBid1MaxSize"] = ToDouble(maxBid1Size);
                features["depthQueueDropPct"] = ToDouble(depthQueueDropPct);
            }
            features["marketRegime"] = ClassifyMarketRegime(natr, consensus);

            return features;
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

        private double? CalculateAtrFromClose(string symbol, int period)
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

        private double? CalculateNatrPct(string symbol, int period)
        {
            double? atr = CalculateAtrFromClose(symbol, period);
            if (!atr.HasValue)
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

            return ToDouble(ToDecimal(atr.Value) / lastClose * 100m);
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
                SafeDebug("Daily caches reset.");
            }
        }

        // ── Symbol helpers ──────────────────────────────────────────

        private bool IsAllowedSymbol(string symbol)
        {
            string normalized = NormalizeSymbol(symbol);
            return AllowedSymbols.Any(x => NormalizeSymbol(x) == normalized);
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

        private static string NormalizeMode(string mode)
        {
            string value = (mode ?? "PAPER").Trim().ToUpperInvariant();
            if (value != "PAPER"
                && value != "MANUAL"
                && value != "LIVE"
                && value != "DEMO_LIVE"
                && value != "REAL_LIVE")
            {
                return "PAPER";
            }
            return value;
        }

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

            while (!token.IsCancellationRequested && data.Count < 65536)
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
                return null;
            }

            string headerText = Encoding.UTF8.GetString(data.GetRange(0, headerEnd).ToArray());
            string[] lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.None);
            if (lines.Length == 0)
            {
                return null;
            }

            string[] requestLine = lines[0].Split(' ');
            if (requestLine.Length < 2)
            {
                return null;
            }

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
                int.TryParse(headers["Content-Length"], out contentLength);
            }

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
                Method = requestLine[0].ToUpperInvariant(),
                Path = path,
                Headers = headers,
                Query = query,
                Body = body
            };
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

        private struct MarketQuoteSnapshot
        {
            public decimal Last { get; set; }
            public decimal Bid { get; set; }
            public decimal Ask { get; set; }
            public decimal Volume { get; set; }
            public bool Reliable { get; set; }
            public string Source { get; set; }
            public DateTime UpdatedAt { get; set; }
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
            public DateTime UpdatedAt { get; set; }
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
            public List<string> Categories { get; set; }
            public List<string> Symbols { get; set; }
            public List<string> Sources { get; set; }
            public string FilterType { get; set; }
            public List<string> MatchedFilters { get; set; }
            public bool HasAttachments { get; set; }
            public bool HasDetail { get; set; }
            public int DailyNewsNo { get; set; }
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
    }
}
