using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading.Tasks;
using Matriks.Data.Symbol;
using Matriks.Engines;
using Matriks.Enumeration;
using Matriks.Indicators;
using Matriks.Lean.Algotrader.AlgoBase;
using Matriks.Lean.Algotrader.Models;
using Matriks.Lean.Algotrader.Trading;
using Matriks.Symbols;
using Matriks.Trader.Core;
using Matriks.Trader.Core.Fields;
using Matriks.Trader.Core.TraderModels;
using Newtonsoft.Json;

namespace Matriks.Lean.Algotrader
{
    /// <summary>
    /// TradeAI Agentic Bot — Matriks IQ Algo.
    /// Agentic sinyal protokolü ile server'dan karar alır,
    /// FETCH_DATA ping-pong yapar, demo limit emir gönderir,
    /// sonucu /api/order-result ile raporlar.
    ///
    /// Kullanım:
    /// 1. PAPER modda test: Mode = "PAPER"
    /// 2. MANUAL mod: Mode = "MANUAL"
    /// 3. DEMO_LIVE mod (demo hesap emir): Mode = "DEMO_LIVE", EnableDemoOrders=true, DemoAccountConfirmed=true
    /// 4. REAL_LIVE default kapalı: EnableRealOrders=false
    /// MARKET emir ASLA gönderilmez — sadece LIMIT.
    /// </summary>
    public class TradeAiAgenticBot : MatriksAlgo
    {
        // ── Parameters ──────────────────────────────────────────────

        [Parameter("https://aaa.ddns.net")]
        public string ServerBaseUrl;

        [Parameter("BURAYA_TOKEN")]
        public string ApiToken;

        private string Mode = "PAPER";
        private bool EnableDemoOrders = false;
        private bool EnableRealOrders = false;
        private bool RequireDemoAccount = true;
        private bool DemoAccountConfirmed = false;
        private decimal MaxOrderValueTl = 1000m;
        private decimal MaxQtyPerOrder = 1m;
        private int MaxOrdersPerDay = 1;
        private int MaxOrdersPerSymbolPerDay = 1;
        private bool AllowMarketOrders = false;
        private int ScanIntervalMinutes = 30;
        private int HttpTimeoutSeconds = 15;
        private int MaxFetchLoopPerSession = 3;
        private string OrderTimeInForce = "Day";
        private SymbolPeriod IndicatorPeriod = SymbolPeriod.Min5;
        private string _configVersion = "";
        private string _configHash = "";

        // ── Symbols ──────────────────────────────────────────────────

        private string[] AllowedSymbols =
        {
            "THYAO",
            "AKBNK",
            "SISE",
            "KCHOL",
            "TUPRS",
            "ANELE"
        };

        private Dictionary<string, decimal> LockedLongTermQty = new Dictionary<string, decimal>
        {
            { "THYAO", 100m },
            { "ASELS", 50m }
        };

        // ── Constants ────────────────────────────────────────────────

        private const int MaxCloseHistory = 240;

        // ── Thread-safe state ────────────────────────────────────────

        private HttpClient _http;
        private readonly JsonSerializerSettings _jsonSettings = new JsonSerializerSettings
        {
            NullValueHandling = NullValueHandling.Ignore
        };

        // ConcurrentDictionary ile thread-safe state
        private readonly ConcurrentDictionary<string, DateTime> _lastScanUtcBySymbol = new ConcurrentDictionary<string, DateTime>();
        private readonly ConcurrentDictionary<string, bool> _inFlightBySymbol = new ConcurrentDictionary<string, bool>();
        private readonly ConcurrentDictionary<string, int> _dailyTradeCountBySymbol = new ConcurrentDictionary<string, int>();
        private readonly ConcurrentDictionary<string, decimal> _botPositionQtyBySymbol = new ConcurrentDictionary<string, decimal>();
        private readonly ConcurrentDictionary<string, bool> _pendingOverrideSymbols = new ConcurrentDictionary<string, bool>();
        private readonly ConcurrentDictionary<string, List<decimal>> _closeHistoryBySymbol = new ConcurrentDictionary<string, List<decimal>>();
        private readonly ConcurrentDictionary<string, decimal> _maxBid1SizeBySymbol = new ConcurrentDictionary<string, decimal>();
        private readonly ConcurrentDictionary<string, PendingOrderContext> _pendingOrdersBySymbolSide = new ConcurrentDictionary<string, PendingOrderContext>();
        private readonly ConcurrentDictionary<string, PendingOrderContext> _pendingOrdersByOrderId = new ConcurrentDictionary<string, PendingOrderContext>();
        private readonly ConcurrentDictionary<string, RSI> _rsiBySymbol = new ConcurrentDictionary<string, RSI>();
        private readonly ConcurrentDictionary<string, MOV> _ema20BySymbol = new ConcurrentDictionary<string, MOV>();
        private readonly ConcurrentDictionary<string, MOV> _ema50BySymbol = new ConcurrentDictionary<string, MOV>();
        private readonly ConcurrentDictionary<string, MACD> _macdBySymbol = new ConcurrentDictionary<string, MACD>();

        // Atomic duplicate requestId check (ConcurrentDictionary.TryAdd = atomic)
        private readonly ConcurrentDictionary<string, object> _sentRequestIds = new ConcurrentDictionary<string, object>();

        // Lock objects for non-atomic compound operations
        private readonly object _inFlightLock = new object();
        private readonly object _closeLock = new object();
        private readonly object _dailyCounterLock = new object();

        private DateTime _dailyCounterDate = DateTime.Today;
        private bool _realPositionsLoadedFromSnapshot;
        private bool? _autoOrderEnabled;
        private bool? _testAutoOrderEnabled;
        private bool _subscriptionsInitialized;

        // ── Matriks lifecycle ───────────────────────────────────────

        public override void OnInit()
        {
            _http = new HttpClient
            {
                BaseAddress = new Uri(ServerBaseUrl.TrimEnd('/') + "/"),
                Timeout = TimeSpan.FromSeconds(Math.Max(10, HttpTimeoutSeconds))
            };
            _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", ApiToken);

            if (!FetchBotConfigFromServer(true))
            {
                ApplySafeFallbackConfig();
            }

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
                InitializeIndicators(normalized);

                _lastScanUtcBySymbol[normalized] = DateTime.MinValue;
                _inFlightBySymbol[normalized] = false;
                _dailyTradeCountBySymbol[normalized] = 0;
                _botPositionQtyBySymbol[normalized] = 0m;
                _closeHistoryBySymbol[normalized] = new List<decimal>();
            }
            _subscriptionsInitialized = true;

            SendOrderSequential(false);
            WorkWithPermanentSignal(false);
            SetTimerInterval(60);
            LogTradeUserInfo();
            LoadRealPositionsSnapshot();
            Task.Run(async () =>
            {
                try
                {
                    await SyncPositionsToServerAsync();
                }
                catch (Exception ex)
                {
                    SafeDebug("Initial position sync error: " + ex.Message);
                }
            });

            SafeDebug("Initialized symbols=" + string.Join(",", AllowedSymbols)
                + " mode=" + NormalizeMode(Mode)
                + " enableDemoOrders=" + EnableDemoOrders
                + " enableRealOrders=" + EnableRealOrders
                + " demoConfirmed=" + DemoAccountConfirmed
                + " scanIntervalMinutes=" + ScanIntervalMinutes
                + " timerSeconds=60"
                + " indicatorPeriod=" + IndicatorPeriod
                + " timeInForce=" + NormalizeTimeInForce(OrderTimeInForce)
                + " server=" + ServerBaseUrl);

            ScanDueSymbols();
        }

        /// <summary>
        /// Fetch the admin-managed runtime config from the server.
        /// Blocking on startup because OnInit is not async; runtime refreshes are
        /// also intentionally simple so config drift never blocks order handling.
        /// </summary>
        private bool FetchBotConfigFromServer(bool startup)
        {
            try
            {
                using (var response = _http.GetAsync("api/bot/config").GetAwaiter().GetResult())
                {
                    string body = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                    if (!response.IsSuccessStatusCode)
                    {
                        SafeDebug("Bot config fetch failed: HTTP " + (int)response.StatusCode + ": " + body);
                        return false;
                    }

                    BotRuntimeConfigResponse parsed = JsonConvert.DeserializeObject<BotRuntimeConfigResponse>(body);
                    if (parsed.AllowedSymbols == null || parsed.AllowedSymbols.Length == 0)
                    {
                        SafeDebug("Bot config fetch returned empty allowedSymbols.");
                        return false;
                    }

                    ApplyBotRuntimeConfig(parsed, startup);
                    return true;
                }
            }
            catch (Exception ex)
            {
                SafeDebug("Bot config fetch error: " + ex.Message);
                return false;
            }
        }

        private void ApplyBotRuntimeConfig(BotRuntimeConfigResponse config, bool startup)
        {
            string[] incomingSymbols = NormalizeSymbols(config.AllowedSymbols);
            if (incomingSymbols.Length == 0)
            {
                SafeDebug("Bot config ignored because allowedSymbols is empty.");
                return;
            }

            if (startup || !_subscriptionsInitialized)
            {
                AllowedSymbols = incomingSymbols;
            }
            else if (!SameSymbols(AllowedSymbols, incomingSymbols))
            {
                SafeDebug("AllowedSymbols changed; restart required to resubscribe symbols. server="
                    + string.Join(",", incomingSymbols)
                    + " local="
                    + string.Join(",", AllowedSymbols));
            }

            Mode = NormalizeMode(config.Mode);
            EnableDemoOrders = config.EnableDemoOrders;
            EnableRealOrders = config.EnableRealOrders;
            RequireDemoAccount = config.RequireDemoAccount;
            DemoAccountConfirmed = config.DemoAccountConfirmed;
            MaxOrderValueTl = PositiveDecimal(config.MaxOrderValueTl, 500m);
            MaxQtyPerOrder = PositiveDecimal(config.MaxQtyPerOrder, 1m);
            MaxOrdersPerDay = PositiveInt(config.MaxOrdersPerDay, 1);
            MaxOrdersPerSymbolPerDay = PositiveInt(config.MaxOrdersPerSymbolPerDay, 1);
            AllowMarketOrders = false;
            if (config.AllowMarketOrders)
            {
                SafeDebug("Server returned allowMarketOrders=true; forced to false because MARKET orders are disabled.");
            }

            ScanIntervalMinutes = PositiveInt(config.ScanIntervalMinutes, 30);
            HttpTimeoutSeconds = Math.Max(10, PositiveInt(config.HttpTimeoutSeconds, 15));
            MaxFetchLoopPerSession = Math.Max(0, config.MaxFetchLoopPerSession);
            OrderTimeInForce = NormalizeTimeInForce(config.OrderTimeInForce);

            SymbolPeriod nextIndicatorPeriod = ParseSymbolPeriod(config.IndicatorPeriod);
            if (startup || !_subscriptionsInitialized)
            {
                IndicatorPeriod = nextIndicatorPeriod;
            }
            else if (!IndicatorPeriod.Equals(nextIndicatorPeriod))
            {
                SafeDebug("IndicatorPeriod changed; restart required to resubscribe indicators. server="
                    + nextIndicatorPeriod
                    + " local="
                    + IndicatorPeriod);
            }

            LockedLongTermQty = NormalizeLockedQty(config.LockedLongTermQty);
            _configVersion = config.ConfigVersion ?? "";
            _configHash = config.ConfigHash ?? "";
            if (_http != null)
            {
                _http.Timeout = TimeSpan.FromSeconds(HttpTimeoutSeconds);
            }

            string profileCode = config.ActiveTradeProfile.Code ?? "?";
            string profileRiskLevel = config.ActiveTradeProfile.RiskLevel ?? "?";
            SafeDebug("Config loaded profile=" + profileCode
                + " riskLevel=" + profileRiskLevel
                + " hash=" + (_configHash == "" ? "null" : _configHash));
            SafeDebug("Bot config loaded version=" + (_configVersion == "" ? "null" : _configVersion)
                + " hash=" + (_configHash == "" ? "null" : _configHash)
                + " mode=" + Mode
                + " symbols=" + string.Join(",", AllowedSymbols)
                + " scanIntervalMinutes=" + ScanIntervalMinutes
                + " maxFetchLoopPerSession=" + MaxFetchLoopPerSession
                + " indicatorPeriod=" + IndicatorPeriod
                + " timeInForce=" + OrderTimeInForce);
        }

        private void ApplySafeFallbackConfig()
        {
            Mode = "PAPER";
            EnableDemoOrders = false;
            EnableRealOrders = false;
            DemoAccountConfirmed = false;
            MaxOrderValueTl = 500m;
            MaxQtyPerOrder = 1m;
            MaxOrdersPerDay = 1;
            MaxOrdersPerSymbolPerDay = 1;
            AllowMarketOrders = false;
            _configVersion = "";
            _configHash = "";
            SafeDebug("Bot config unavailable; safe PAPER fallback active. symbols=" + string.Join(",", AllowedSymbols));
        }

        private void RefreshConfigIfChanged(AgenticSignalResponse response)
        {
            if (string.IsNullOrWhiteSpace(response.ConfigHash))
                return;

            if (string.Equals(response.ConfigHash, _configHash, StringComparison.OrdinalIgnoreCase))
                return;

            SafeDebug("Config hash changed server=" + response.ConfigHash
                + " local=" + (string.IsNullOrWhiteSpace(_configHash) ? "null" : _configHash)
                + "; fetching bot config");
            FetchBotConfigFromServer(false);
        }

        public override void OnDataUpdate(BarDataEventArgs barData)
        {
            ResetDailyCountersIfNeeded();
            RefreshCloseHistoryFromMarketData();
        }

        public override void OnTimer()
        {
            ResetDailyCountersIfNeeded();
            RefreshCloseHistoryFromMarketData();
            if (!_realPositionsLoadedFromSnapshot)
            {
                LoadRealPositionsSnapshot();
            }
            Task.Run(async () =>
            {
                try
                {
                    await SyncPositionsToServerAsync();
                }
                catch (Exception ex)
                {
                    SafeDebug("Position sync error: " + ex.Message);
                }
            });
            Task.Run(async () =>
            {
                try
                {
                    await RefreshPendingOverridesAsync();
                }
                catch (Exception ex)
                {
                    SafeDebug("Pending overrides refresh error: " + ex.Message);
                }
            });
            ScanDueSymbols();
        }

        /// <summary>
        /// Refreshes the cached set of symbols with a pending admin test
        /// override. Fire-and-forget, called once per OnTimer tick — so the
        /// cache used by ScanDueSymbols() may lag by up to one tick (~60s),
        /// which is an acceptable tradeoff for not blocking the timer thread
        /// on a network call every tick.
        /// </summary>
        private async Task RefreshPendingOverridesAsync()
        {
            try
            {
                using (var response = await _http.GetAsync("api/bot/pending-overrides"))
                {
                    string body = await response.Content.ReadAsStringAsync();
                    if (!response.IsSuccessStatusCode)
                    {
                        SafeDebug("Pending overrides fetch failed: HTTP " + (int)response.StatusCode + ": " + body);
                        return;
                    }

                    PendingOverridesResponse parsed = JsonConvert.DeserializeObject<PendingOverridesResponse>(body);
                    var fresh = new HashSet<string>();
                    if (parsed.Symbols != null)
                    {
                        foreach (string s in parsed.Symbols)
                        {
                            fresh.Add(NormalizeSymbol(s));
                        }
                    }

                    foreach (string existing in _pendingOverrideSymbols.Keys)
                    {
                        if (!fresh.Contains(existing))
                        {
                            _pendingOverrideSymbols.TryRemove(existing, out _);
                        }
                    }
                    foreach (string s in fresh)
                    {
                        _pendingOverrideSymbols[s] = true;
                    }
                }
            }
            catch (Exception ex)
            {
                SafeDebug("Pending overrides fetch error: " + ex.Message);
            }
        }

        private void ScanDueSymbols()
        {
            foreach (string symbolRaw in AllowedSymbols)
            {
                string symbol = NormalizeSymbol(symbolRaw);

                if (!IsAllowedSymbol(symbol))
                    continue;

                // Thread-safe in-flight check
                bool canProceed = false;
                lock (_inFlightLock)
                {
                    if (!_inFlightBySymbol.TryGetValue(symbol, out bool inFlight) || !inFlight)
                    {
                        _inFlightBySymbol[symbol] = true;
                        canProceed = true;
                    }
                }

                if (!canProceed)
                    continue;

                // Scan interval check — bypassed when a pending admin test
                // override is waiting for this symbol, so Force SELL/BUY
                // takes effect on the next tick instead of waiting out the
                // full ScanIntervalMinutes window.
                bool hasPendingOverride = _pendingOverrideSymbols.ContainsKey(symbol);
                DateTime lastScanUtc = _lastScanUtcBySymbol.TryGetValue(symbol, out var dt) ? dt : DateTime.MinValue;
                if (!hasPendingOverride && DateTime.UtcNow - lastScanUtc < TimeSpan.FromMinutes(Math.Max(1, ScanIntervalMinutes)))
                {
                    _inFlightBySymbol[symbol] = false;
                    continue;
                }

                _lastScanUtcBySymbol[symbol] = DateTime.UtcNow;

                // Fire-and-forget async evaluation — in-flight flag cleared in finally
                string capturedSymbol = symbol;
                Task.Run(async () =>
                {
                    try
                    {
                        await SendEvaluateAsync(capturedSymbol);
                    }
                    catch (Exception ex)
                    {
                        SafeDebug("Unhandled scan error symbol=" + capturedSymbol + " error=" + ex.Message);
                    }
                    finally
                    {
                        _inFlightBySymbol[capturedSymbol] = false;
                    }
                });
            }
        }

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
                + " avgPx=" + avgPx
                + " ordStatus=" + order.OrdStatus);

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
                    RequestId = "MATRiKS-" + (string.IsNullOrWhiteSpace(orderId) ? BuildRequestId(symbol) : orderId),
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

            Task.Run(async () =>
            {
                await ReportOrderResultAsync(context, status, message, orderId, reportQty, reportPrice);
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

        public override void OnRealPositionUpdate(AlgoTraderPosition position)
        {
            UpdatePositionCache(position, "OnRealPositionUpdate");
        }

        public override void OnStopped()
        {
            if (_http != null)
            {
                try { _http.Dispose(); }
                catch (Exception ex) { SafeDebug("Dispose error: " + ex.Message); }
            }
            SafeDebug("Stopped.");
        }

        // ── Agentic evaluate flow ────────────────────────────────────

        private async Task SendEvaluateAsync(string symbol)
        {
            string requestId = BuildRequestId(symbol);
            var request = new AgenticSignalRequest
            {
                RequestId = requestId,
                SessionId = null,
                Symbol = symbol,
                MarketData = BuildMarketData(symbol, "DEPTH"),
                ContextHistory = new List<ContextStep>(),
                Mode = NormalizeMode(Mode)
            };
            ApplyFlatCompatibilityFields(ref request);

            SafeDebug("Sending evaluate request symbol=" + symbol + " requestId=" + requestId);

            AgenticSignalResponse response = await SendAgenticRequestAsync(request);
            await HandleServerResponseAsync(symbol, response, request, 0);
        }

        private async Task<AgenticSignalResponse> SendAgenticRequestAsync(AgenticSignalRequest request)
        {
            for (int attempt = 0; attempt <= 1; attempt++)
            {
                try
                {
                    string json = JsonConvert.SerializeObject(request, _jsonSettings);
                    SafeDebug("POST /api/signal/evaluate-agent requestId=" + request.RequestId
                        + " symbol=" + request.Symbol
                        + " sessionId=" + (request.SessionId ?? "null")
                        + " attempt=" + (attempt + 1));

                    using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
                    using (var response = await _http.PostAsync("api/signal/evaluate-agent", content))
                    {
                        string body = await response.Content.ReadAsStringAsync();
                        if (!response.IsSuccessStatusCode)
                        {
                            throw new Exception("HTTP " + (int)response.StatusCode + ": " + body);
                        }

                        AgenticSignalResponse parsed = JsonConvert.DeserializeObject<AgenticSignalResponse>(body);
                        if (string.IsNullOrWhiteSpace(parsed.Action))
                        {
                            throw new Exception("Empty response");
                        }

                        RefreshConfigIfChanged(parsed);
                        return parsed;
                    }
                }
                catch (Exception ex)
                {
                    SafeDebug("HTTP error requestId=" + request.RequestId + " attempt=" + (attempt + 1) + " error=" + ex.Message);
                    if (attempt >= 1)
                    {
                        return AgenticSignalResponse.Wait(
                            request.RequestId,
                            request.SessionId,
                            request.Symbol,
                            "HTTP error after retry: " + ex.Message);
                    }
                }
            }

            return AgenticSignalResponse.Wait(request.RequestId, request.SessionId, request.Symbol, "HTTP error");
        }

        private async Task HandleServerResponseAsync(
            string originalSymbol,
            AgenticSignalResponse response,
            AgenticSignalRequest previousRequest,
            int fetchLoopCount)
        {
            string action = NormalizeAction(response.Action);
            string targetSymbol = response.GetTargetSymbol();
            string requiredDataType = response.GetRequiredDataType();

            SafeDebug("Response action=" + action
                + " sessionId=" + (response.SessionId ?? "null")
                + " targetSymbol=" + (targetSymbol ?? "null")
                + " dataType=" + (requiredDataType ?? "null")
                + " reason=" + response.Reason);

            if (action == "FETCH_DATA")
            {
                if (fetchLoopCount >= MaxFetchLoopPerSession)
                {
                    SafeDebug("FETCH_DATA loop stopped. symbol=" + originalSymbol + " maxFetchLoopPerSession=" + MaxFetchLoopPerSession);
                    return;
                }

                if (string.IsNullOrWhiteSpace(targetSymbol))
                {
                    targetSymbol = originalSymbol;
                }
                if (string.IsNullOrWhiteSpace(requiredDataType))
                {
                    requiredDataType = "DEPTH";
                }

                SafeDebug("Fetching requested data target=" + targetSymbol + " dataType=" + requiredDataType + " root=" + originalSymbol);
                AgenticSignalRequest nextRequest = await FetchRequestedDataAsync(
                    originalSymbol,
                    targetSymbol,
                    requiredDataType,
                    response.SessionId,
                    previousRequest.RequestId,
                    previousRequest);

                AgenticSignalResponse nextResponse = await SendAgenticRequestAsync(nextRequest);
                await HandleServerResponseAsync(originalSymbol, nextResponse, nextRequest, fetchLoopCount + 1);
                return;
            }

            if (action == "WAIT")
            {
                SafeDebug("Final response action=WAIT reason=" + response.Reason);
                return;
            }

            if (action == "BUY" || action == "SELL")
            {
                await TrySendOrderAsync(response);
                return;
            }

            SafeDebug("Unknown response action=" + response.Action + ". Treated as WAIT.");
        }

        // ── FETCH_DATA handler ──────────────────────────────────────

        private async Task<AgenticSignalRequest> FetchRequestedDataAsync(
            string rootSymbol,
            string targetSymbol,
            string requiredDataType,
            string sessionId,
            string rootRequestId,
            AgenticSignalRequest previousRequest)
        {
            var contextHistory = new List<ContextStep>();
            if (previousRequest.ContextHistory != null)
            {
                contextHistory.AddRange(previousRequest.ContextHistory);
            }

            if (previousRequest.MarketData.Payload != null)
            {
                contextHistory.Add(new ContextStep
                {
                    StepNo = contextHistory.Count + 1,
                    Symbol = previousRequest.MarketData.Symbol,
                    DataType = previousRequest.MarketData.DataType,
                    Payload = previousRequest.MarketData.Payload,
                    Reason = "Previous marketData"
                });
            }

            SafeDebug("Agentic fetch request rootSymbol=" + rootSymbol + " targetSymbol=" + targetSymbol + " dataType=" + requiredDataType);

            var request = new AgenticSignalRequest
            {
                RequestId = rootRequestId,
                SessionId = sessionId,
                Symbol = NormalizeSymbol(rootSymbol),
                MarketData = BuildMarketData(targetSymbol, requiredDataType),
                ContextHistory = contextHistory,
                Mode = NormalizeMode(Mode)
            };
            ApplyFlatCompatibilityFields(ref request);

            await Task.CompletedTask; // explicit async signal
            return request;
        }

        // ── Market data builder (real Matriks data) ──────────────────

        private MarketDataPayload BuildMarketData(string symbolRaw, string dataType)
        {
            string symbol = NormalizeSymbol(symbolRaw);

            // Real Matriks market data via GetMarketData
            decimal lastPrice = SafeMarketData(symbol, SymbolUpdateField.Last);
            decimal bidPrice = SafeMarketData(symbol, SymbolUpdateField.Bid);
            decimal askPrice = SafeMarketData(symbol, SymbolUpdateField.Ask);
            decimal volume = SafeMarketData(symbol, SymbolUpdateField.TotalVol);

            // Attempt to get real OHLC from bar data if available
            // Note: Matriks OnDataUpdate provides BarDataEventArgs with OHLCV on each bar close.
            // For intra-bar snapshots we use lastPrice as the best available reference.
            // If the Matriks SDK provides a way to query the latest completed bar, use it here.
            // TODO: Replace open/high/low with real bar data via GetBarData or equivalent SDK call.
            //       For now, flag ohlcReliable=false since these are approximate.
            decimal open = lastPrice;
            decimal high = lastPrice;
            decimal low = lastPrice;
            bool ohlcReliable = false;

            // Update close history for indicator calculations
            UpdateCloseHistory(symbol, lastPrice);

            // Depth data
            decimal bestBid = 0m;
            decimal secondBid = 0m;
            decimal thirdBid = 0m;
            decimal bid1Size = 0m;
            decimal ask1Size = 0m;
            decimal maxBid1Size = 0m;
            decimal depthQueueDropPct = 0m;
            string depthSummary = "";
            try
            {
                var depth = GetMarketDepth(symbol);
                if (depth != null && depth.BidRows != null && depth.BidRows.Count >= 1)
                {
                    bestBid = depth.BidRows[0].Price;
                    bid1Size = depth.BidRows[0].Size;
                    maxBid1Size = _maxBid1SizeBySymbol.AddOrUpdate(
                        symbol,
                        bid1Size,
                        (_, existing) => bid1Size > existing ? bid1Size : existing);

                    if (maxBid1Size > 0m)
                    {
                        depthQueueDropPct = Math.Max(0m, (maxBid1Size - bid1Size) / maxBid1Size * 100m);
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

                depthSummary = "bestBid=" + bestBid
                    + ";secondBid=" + secondBid
                    + ";thirdBid=" + thirdBid
                    + ";bid1Size=" + bid1Size
                    + ";maxBid1Size=" + maxBid1Size
                    + ";depthQueueDropPct=" + depthQueueDropPct;
            }
            catch (Exception ex)
            {
                depthSummary = "depth unavailable: " + ex.Message;
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
                depthQueueDropPct);

            var payload = new Dictionary<string, object>();
            payload["lastPrice"] = ToDouble(lastPrice);
            payload["open"] = ToDouble(open);
            payload["high"] = ToDouble(high);
            payload["low"] = ToDouble(low);
            payload["ohlcReliable"] = ohlcReliable;
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
            payload["dailyTradeCount"] = GetDailyTradeCount(symbol);
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

        private int GetDailyTradeCount(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            return _dailyTradeCountBySymbol.TryGetValue(symbol, out var count) ? count : 0;
        }

        private int GetTotalDailyOrderCount()
        {
            return _dailyTradeCountBySymbol.Values.Sum();
        }

        private void RefreshCloseHistoryFromMarketData()
        {
            foreach (string symbolRaw in AllowedSymbols)
            {
                string symbol = NormalizeSymbol(symbolRaw);
                if (!IsAllowedSymbol(symbol))
                    continue;

                decimal lastPrice = SafeMarketData(symbol, SymbolUpdateField.Last);
                UpdateCloseHistory(symbol, lastPrice);
            }
        }

        private void LoadRealPositionsSnapshot()
        {
            try
            {
                if (!PositionReceiveComplated)
                {
                    SafeDebug("Real position snapshot not ready yet; waiting for OnRealPositionUpdate.");
                    return;
                }

                var positions = GetRealPositions();
                if (positions == null)
                {
                    SafeDebug("GetRealPositions returned null.");
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
            _botPositionQtyBySymbol[symbol] = qty;
            SafeDebug(source + " position symbol=" + symbol
                + " qtyAvailable=" + position.QtyAvailable
                + " qtyNet=" + position.QtyNet
                + " cachedQty=" + qty);
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
                    + " autoOrder=" + tradeUser.AutoOrder
                    + " testAutoOrder=" + tradeUser.TestAutoOrder);
            }
            catch (Exception ex)
            {
                _autoOrderEnabled = null;
                _testAutoOrderEnabled = null;
                SafeDebug("GetTradeUser failed; DemoAccountConfirmed gate remains authoritative. error=" + ex.Message);
            }
        }

        // ── Order sending ────────────────────────────────────────────

        private async Task TrySendOrderAsync(AgenticSignalResponse response)
        {
            // ── Pre-trade validation gates ──

            string action = NormalizeAction(response.Action);
            string symbol = NormalizeSymbol(response.Symbol);
            string mode = NormalizeMode(Mode);

            // Convert Qty/Price
            decimal qty = ToDecimal(response.Qty);
            decimal price = response.Price.HasValue ? ToDecimal(response.Price.Value) : 0m;
            decimal orderValue = qty * price;

            SafeDebug("Final response action=" + action
                + " allowOrder=" + response.AllowOrder
                + " orderType=" + response.OrderType
                + " price=" + (response.Price.HasValue ? response.Price.Value.ToString() : "null")
                + " qty=" + response.Qty);
            SafeDebug("Pre-trade checks started requestId=" + response.RequestId);

            // Gate 1: Action must be BUY or SELL
            if (action != "BUY" && action != "SELL")
            {
                await RejectOrderAsync(response, "unknown action=" + response.Action);
                return;
            }

            // Gate 2: Duplicate requestId (atomic check+add via ConcurrentDictionary)
            if (!_sentRequestIds.TryAdd(response.RequestId, null))
            {
                await RejectOrderAsync(response, "duplicate requestId");
                return;
            }

            // Gate 3: allowOrder must be true
            if (!response.AllowOrder)
            {
                await RejectOrderAsync(response, "allowOrder=false");
                return;
            }

            // Gate 4: No confirmation required
            if (response.RequiresConfirmation)
            {
                await RejectOrderAsync(response, "requiresConfirmation=true");
                return;
            }

            // Gate 5: Only LIMIT orders
            if (NormalizeOrderType(response.OrderType) != "LIMIT")
            {
                await RejectOrderAsync(response, "orderType is not LIMIT: " + response.OrderType);
                return;
            }

            // Gate 6: MARKET orders NEVER allowed
            if (AllowMarketOrders)
            {
                await RejectOrderAsync(response, "AllowMarketOrders=true is not permitted; MARKET orders are never allowed");
                return;
            }

            // Gate 7: Price must be valid
            if (price <= 0)
            {
                await RejectOrderAsync(response, "price is null or <= 0");
                return;
            }

            // Gate 8: Qty must be positive
            if (qty <= 0)
            {
                await RejectOrderAsync(response, "qty <= 0");
                return;
            }

            // Gate 9: Symbol must be allowed
            if (!IsAllowedSymbol(symbol))
            {
                await RejectOrderAsync(response, "symbol not allowed: " + symbol);
                return;
            }

            // Gate 10: Max order value
            if (orderValue > MaxOrderValueTl)
            {
                await RejectOrderAsync(response, "orderValue exceeds MaxOrderValueTl: " + orderValue);
                return;
            }

            // Gate 11: Max qty per order
            if (qty > MaxQtyPerOrder)
            {
                await RejectOrderAsync(response, "qty exceeds MaxQtyPerOrder: " + qty);
                return;
            }

            // Gate 12: Max daily orders
            if (GetTotalDailyOrderCount() >= MaxOrdersPerDay)
            {
                await RejectOrderAsync(response, "MaxOrdersPerDay reached: " + MaxOrdersPerDay);
                return;
            }

            // Gate 13: Max per-symbol daily orders
            if (GetDailyTradeCount(symbol) >= MaxOrdersPerSymbolPerDay)
            {
                await RejectOrderAsync(response, "MaxOrdersPerSymbolPerDay reached for " + symbol);
                return;
            }

            // Gate 14: SELL requires sufficient bot position
            if (action == "SELL" && GetBotPositionQty(symbol) < qty)
            {
                await RejectOrderAsync(response, "SELL botPositionQty insufficient: " + GetBotPositionQty(symbol));
                return;
            }

            // Gate 15-17: Mode checks
            if (mode == "PAPER")
            {
                await RejectOrderAsync(response, "Mode=PAPER");
                return;
            }

            if (mode == "MANUAL")
            {
                await RejectOrderAsync(response, "Mode=MANUAL requires human confirmation");
                return;
            }

            if (mode == "DEMO_LIVE")
            {
                if (!EnableDemoOrders)
                {
                    await RejectOrderAsync(response, "EnableDemoOrders=false");
                    return;
                }
                if (!IsDemoAccount())
                {
                    await RejectOrderAsync(response, "DEMO_LIVE blocked. DemoAccountConfirmed=false");
                    return;
                }
            }
            else if (mode == "REAL_LIVE")
            {
                if (!EnableRealOrders)
                {
                    await RejectOrderAsync(response, "REAL_LIVE blocked. EnableRealOrders=false");
                    return;
                }
                if (RequireDemoAccount && !IsDemoAccount())
                {
                    await RejectOrderAsync(response, "RequireDemoAccount=true and demo account is not confirmed");
                    return;
                }
            }
            else
            {
                await RejectOrderAsync(response, "unsupported Mode=" + mode);
                return;
            }

            // ── All gates passed — send order ──

            try
            {
                OrderExecutionResult execution = await SendLimitOrderAsync(response.RequestId, symbol, action, qty, price);
                if (execution.Success)
                {
                    IncrementDailyTradeCount(symbol);
                    SafeDebug("Order SENT_PENDING symbol=" + symbol
                        + " side=" + action
                        + " qty=" + qty
                        + " price=" + price
                        + " message=" + execution.Message);
                    return;
                }

                await ReportOrderResultAsync(response, "REJECTED", execution.Message, execution.OrderId);
            }
            catch (Exception ex)
            {
                SafeDebug("Order exception requestId=" + response.RequestId + " error=" + ex.Message);
                await ReportOrderResultAsync(response, "ERROR", ex.Message, null);
            }
        }

        /// <summary>
        /// Send a real limit order via Matriks IQ demo/sandbox account.
        /// Uses MatriksAlgo.SendLimitOrder(string, int, OrderSide, decimal, TimeInForce, ChartIcon).
        /// </summary>
        private async Task<OrderExecutionResult> SendLimitOrderAsync(string requestId, string symbol, string side, decimal qty, decimal limitPrice)
        {
            if (!TryConvertOrderQuantity(qty, out int quantity, out string quantityError))
            {
                return new OrderExecutionResult
                {
                    Success = false,
                    IsSimulated = false,
                    OrderId = null,
                    Message = quantityError
                };
            }

            if (quantity != qty)
            {
                SafeDebug("Qty converted to int symbol=" + symbol + " original=" + qty + " quantity=" + quantity);
            }

            OrderSide orderSide = NormalizeAction(side) == "BUY" ? OrderSide.Buy : OrderSide.Sell;
            ChartIcon chartIcon = orderSide == OrderSide.Buy ? ChartIcon.Buy : ChartIcon.Sell;
            TimeInForce timeInForce = ResolveTimeInForce();
            decimal roundedPrice = RoundPriceStepBistViop(symbol, limitPrice);
            if (roundedPrice <= 0m)
            {
                return new OrderExecutionResult
                {
                    Success = false,
                    IsSimulated = false,
                    OrderId = null,
                    Message = "rounded limit price <= 0"
                };
            }

            if (roundedPrice != limitPrice)
            {
                SafeDebug("Limit price rounded symbol=" + symbol + " original=" + limitPrice + " rounded=" + roundedPrice);
            }

            var pending = new PendingOrderContext
            {
                RequestId = requestId,
                Symbol = symbol,
                Action = NormalizeAction(side),
                Qty = quantity,
                Price = roundedPrice
            };
            _pendingOrdersBySymbolSide[BuildSymbolSideKey(symbol, pending.Action)] = pending;

            SafeDebug("Sending real limit order: " + pending.Action
                + " " + symbol
                + " qty=" + quantity
                + " price=" + roundedPrice
                + " timeInForce=" + NormalizeTimeInForce(OrderTimeInForce)
                + " chartIcon=" + chartIcon);

            // Real Matriks IQ SDK call — sends to demo/sandbox account in DEMO_LIVE mode
            try
            {
                SendLimitOrder(symbol, quantity, orderSide, roundedPrice, timeInForce, chartIcon);
            }
            catch
            {
                _pendingOrdersBySymbolSide.TryRemove(BuildSymbolSideKey(symbol, pending.Action), out _);
                throw;
            }

            await Task.CompletedTask; // explicit async yield
            return new OrderExecutionResult
            {
                Success = true,
                IsSimulated = false,
                OrderId = null,
                Message = "Limit order SENT_PENDING; final status will be reported by OnOrderUpdate"
            };
        }

        private async Task RejectOrderAsync(AgenticSignalResponse response, string reason)
        {
            SafeDebug("Order blocked: " + reason);
            await ReportOrderResultAsync(response, "REJECTED", reason, null);
        }

        /// <summary>
        /// POST /api/order-result — reports order outcome back to the server.
        /// </summary>
        private async Task ReportOrderResultAsync(
            AgenticSignalResponse response,
            string status,
            string matriksMessage,
            string orderId)
        {
            var payload = new OrderResultRequest
            {
                RequestId = response.RequestId,
                Symbol = NormalizeSymbol(response.Symbol),
                Action = NormalizeAction(response.Action),
                Qty = response.Qty,
                Price = response.Price.HasValue ? response.Price.Value : 0,
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

                SafeDebug("Order result posted to server status=" + status + " requestId=" + response.RequestId);
            }
            catch (Exception ex)
            {
                SafeDebug("Order result post exception requestId=" + response.RequestId + " error=" + ex.Message);
            }
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
                        SafeDebug("Order update post failed HTTP " + (int)result.StatusCode + " body=" + body);
                        return;
                    }
                }

                SafeDebug("Order update posted to server status=" + status
                    + " requestId=" + context.RequestId
                    + " orderId=" + orderId);
            }
            catch (Exception ex)
            {
                SafeDebug("Order update post exception requestId=" + context.RequestId + " error=" + ex.Message);
            }
        }

        /// <summary>
        /// Reports the bot's full known position snapshot (not limited to
        /// AllowedSymbols — GetRealPositions/OnRealPositionUpdate populate
        /// _botPositionQtyBySymbol for every symbol Matriks reports) so the
        /// admin panel's Positions page reflects the real portfolio.
        /// </summary>
        private async Task SyncPositionsToServerAsync()
        {
            var positions = _botPositionQtyBySymbol
                .Select(kv => new PositionSyncEntry { Symbol = kv.Key, Qty = kv.Value })
                .ToList();

            if (positions.Count == 0)
                return;

            var payload = new PositionSyncRequest { Positions = positions };

            try
            {
                string json = JsonConvert.SerializeObject(payload, _jsonSettings);
                using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
                using (var result = await _http.PostAsync("api/bot/positions/sync", content))
                {
                    string body = await result.Content.ReadAsStringAsync();
                    if (!result.IsSuccessStatusCode)
                    {
                        SafeDebug("Position sync post failed HTTP " + (int)result.StatusCode + " body=" + body);
                        return;
                    }
                }

                SafeDebug("Position sync posted to server count=" + positions.Count);
            }
            catch (Exception ex)
            {
                SafeDebug("Position sync post exception: " + ex.Message);
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

        // ── Compatibility layer (flat fields for legacy clients) ────

        private void ApplyFlatCompatibilityFields(ref AgenticSignalRequest request)
        {
            if (request.MarketData.Payload == null)
                return;

            var payload = request.MarketData.Payload;
            request.Timeframe = SymbolPeriod.Min.ToString();
            request.LastPrice = GetPayloadDouble(payload, "lastPrice");
            request.Open = GetPayloadDouble(payload, "open");
            request.High = GetPayloadDouble(payload, "high");
            request.Low = GetPayloadDouble(payload, "low");
            request.Volume = GetPayloadDouble(payload, "volume");
            request.Rsi = GetPayloadNullableDouble(payload, "rsi");
            request.Ema20 = GetPayloadNullableDouble(payload, "ema20");
            request.Ema50 = GetPayloadNullableDouble(payload, "ema50");
            request.Macd = GetPayloadNullableDouble(payload, "macd");
            request.MacdSignal = GetPayloadNullableDouble(payload, "macdSignal");
            request.AlphaTrendSignal = GetPayloadString(payload, "alphaTrendSignal");
            request.AlphaTrendMode = GetPayloadString(payload, "alphaTrendMode");
            request.IndicatorBuyCount = Convert.ToInt32(GetPayloadDouble(payload, "indicatorBuyCount"));
            request.IndicatorSellCount = Convert.ToInt32(GetPayloadDouble(payload, "indicatorSellCount"));
            request.IndicatorNeutralCount = Convert.ToInt32(GetPayloadDouble(payload, "indicatorNeutralCount"));
            request.IndicatorConsensus = GetPayloadString(payload, "indicatorConsensus");
            request.IndicatorConsensusRatio = GetPayloadNullableDouble(payload, "indicatorConsensusRatio");
            request.Atr = GetPayloadNullableDouble(payload, "atr");
            request.Natr = GetPayloadNullableDouble(payload, "natr");
            request.DepthBid1Size = GetPayloadNullableDouble(payload, "depthBid1Size");
            request.DepthBid1MaxSize = GetPayloadNullableDouble(payload, "depthBid1MaxSize");
            request.DepthQueueDropPct = GetPayloadNullableDouble(payload, "depthQueueDropPct");
            request.MarketRegime = GetPayloadString(payload, "marketRegime");
            request.BotPositionQty = GetPayloadDouble(payload, "botPositionQty");
            request.TotalAccountQty = GetPayloadDouble(payload, "totalAccountQty");
            request.LockedLongTermQty = GetPayloadDouble(payload, "lockedLongTermQty");
            request.DailyTradeCount = Convert.ToInt32(GetPayloadDouble(payload, "dailyTradeCount"));
        }

        // ── Market data access ──────────────────────────────────────

        private decimal SafeMarketData(string symbol, SymbolUpdateField field)
        {
            try
            {
                return GetMarketData(symbol, field);
            }
            catch
            {
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
            decimal depthQueueDropPct)
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
            features["depthBid1Size"] = ToDouble(bid1Size);
            features["depthBid1MaxSize"] = ToDouble(maxBid1Size);
            features["depthQueueDropPct"] = ToDouble(depthQueueDropPct);
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

        // ── Daily counter management ─────────────────────────────────

        private void IncrementDailyTradeCount(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            _dailyTradeCountBySymbol.AddOrUpdate(symbol, 1, (_, existing) => existing + 1);
        }

        private void ResetDailyCountersIfNeeded()
        {
            if (_dailyCounterDate == DateTime.Today)
                return;

            lock (_dailyCounterLock)
            {
                if (_dailyCounterDate == DateTime.Today)
                    return; // double-checked

                _dailyCounterDate = DateTime.Today;
                _dailyTradeCountBySymbol.Clear();
                _maxBid1SizeBySymbol.Clear();
                foreach (string symbol in AllowedSymbols)
                {
                    _dailyTradeCountBySymbol[NormalizeSymbol(symbol)] = 0;
                }
                SafeDebug("Daily trade counters reset.");
            }
        }

        // ── Symbol helpers ──────────────────────────────────────────

        private bool IsAllowedSymbol(string symbol)
        {
            string normalized = NormalizeSymbol(symbol);
            return AllowedSymbols.Any(x => NormalizeSymbol(x) == normalized);
        }

        private static string BuildRequestId(string symbol)
        {
            return NormalizeSymbol(symbol) + "-" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
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

            return null;
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

        // ── Normalization helpers ────────────────────────────────────

        private static string[] NormalizeSymbols(string[] symbols)
        {
            if (symbols == null)
                return new string[0];

            return symbols
                .Select(NormalizeSymbol)
                .Where(x => !string.IsNullOrWhiteSpace(x))
                .Distinct()
                .ToArray();
        }

        private static bool SameSymbols(string[] left, string[] right)
        {
            string[] normalizedLeft = NormalizeSymbols(left).OrderBy(x => x).ToArray();
            string[] normalizedRight = NormalizeSymbols(right).OrderBy(x => x).ToArray();
            return normalizedLeft.SequenceEqual(normalizedRight);
        }

        private static Dictionary<string, decimal> NormalizeLockedQty(Dictionary<string, decimal> lockedQty)
        {
            var result = new Dictionary<string, decimal>();
            if (lockedQty == null)
                return result;

            foreach (var item in lockedQty)
            {
                string symbol = NormalizeSymbol(item.Key);
                if (string.IsNullOrWhiteSpace(symbol))
                    continue;

                result[symbol] = item.Value < 0m ? 0m : item.Value;
            }
            return result;
        }

        private static decimal PositiveDecimal(decimal value, decimal fallback)
        {
            return value > 0m ? value : fallback;
        }

        private static int PositiveInt(int value, int fallback)
        {
            return value > 0 ? value : fallback;
        }

        private static SymbolPeriod ParseSymbolPeriod(string value)
        {
            string normalized = (value ?? "Min5").Trim();
            if (normalized == "")
                normalized = "Min5";

            object parsed;
            if (Enum.TryParse(typeof(SymbolPeriod), normalized, true, out parsed))
                return (SymbolPeriod)parsed;

            return SymbolPeriod.Min5;
        }

        private static string NormalizeSymbol(string symbol)
        {
            return (symbol ?? "").Trim().ToUpperInvariant();
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

        private static string NormalizeAction(string action)
        {
            return (action ?? "WAIT").Trim().ToUpperInvariant();
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

        private static string NormalizeOrderType(string orderType)
        {
            return (orderType ?? "NONE").Trim().ToUpperInvariant();
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

        private static void AddIfNotNull(Dictionary<string, object> payload, string key, double? value)
        {
            if (value.HasValue)
            {
                payload[key] = value.Value;
            }
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

        private static decimal ToDecimal(double value)
        {
            return Convert.ToDecimal(value);
        }

        private static double GetPayloadDouble(Dictionary<string, object> payload, string key)
        {
            double? value = GetPayloadNullableDouble(payload, key);
            return value.HasValue ? value.Value : 0;
        }

        private static double? GetPayloadNullableDouble(Dictionary<string, object> payload, string key)
        {
            if (payload == null || !payload.ContainsKey(key) || payload[key] == null)
                return null;
            return Convert.ToDouble(payload[key]);
        }

        private static string GetPayloadString(Dictionary<string, object> payload, string key)
        {
            if (payload == null || !payload.ContainsKey(key) || payload[key] == null)
                return null;
            return Convert.ToString(payload[key]);
        }

        // ── Debug output ────────────────────────────────────────────

        private void SafeDebug(string message)
        {
            try
            {
                Debug("[TradeAI] " + message);
            }
            catch
            {
                // Debug should never break trading flow.
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

    // ═════════════════════════════════════════════════════════════════
    // JSON DTOs — match trade-ai-server Pydantic models (camelCase JSON)
    // ═════════════════════════════════════════════════════════════════

    private struct AgenticSignalRequest
    {
        [JsonProperty("requestId")]
        public string RequestId { get; set; }

        [JsonProperty("sessionId", NullValueHandling = NullValueHandling.Ignore)]
        public string SessionId { get; set; }

        [JsonProperty("symbol")]
        public string Symbol { get; set; }

        [JsonProperty("marketData")]
        public MarketDataPayload MarketData { get; set; }

        [JsonProperty("contextHistory")]
        public List<ContextStep> ContextHistory { get; set; }

        [JsonProperty("mode")]
        public string Mode { get; set; }

        // ── Flat compatibility fields ──

        [JsonProperty("timeframe")]
        public string Timeframe { get; set; }

        [JsonProperty("lastPrice")]
        public double LastPrice { get; set; }

        [JsonProperty("open")]
        public double Open { get; set; }

        [JsonProperty("high")]
        public double High { get; set; }

        [JsonProperty("low")]
        public double Low { get; set; }

        [JsonProperty("volume")]
        public double Volume { get; set; }

        [JsonProperty("rsi", NullValueHandling = NullValueHandling.Ignore)]
        public double? Rsi { get; set; }

        [JsonProperty("ema20", NullValueHandling = NullValueHandling.Ignore)]
        public double? Ema20 { get; set; }

        [JsonProperty("ema50", NullValueHandling = NullValueHandling.Ignore)]
        public double? Ema50 { get; set; }

        [JsonProperty("macd", NullValueHandling = NullValueHandling.Ignore)]
        public double? Macd { get; set; }

        [JsonProperty("macdSignal", NullValueHandling = NullValueHandling.Ignore)]
        public double? MacdSignal { get; set; }

        [JsonProperty("alphaTrendSignal", NullValueHandling = NullValueHandling.Ignore)]
        public string AlphaTrendSignal { get; set; }

        [JsonProperty("alphaTrendMode", NullValueHandling = NullValueHandling.Ignore)]
        public string AlphaTrendMode { get; set; }

        [JsonProperty("indicatorBuyCount")]
        public int IndicatorBuyCount { get; set; }

        [JsonProperty("indicatorSellCount")]
        public int IndicatorSellCount { get; set; }

        [JsonProperty("indicatorNeutralCount")]
        public int IndicatorNeutralCount { get; set; }

        [JsonProperty("indicatorConsensus", NullValueHandling = NullValueHandling.Ignore)]
        public string IndicatorConsensus { get; set; }

        [JsonProperty("indicatorConsensusRatio", NullValueHandling = NullValueHandling.Ignore)]
        public double? IndicatorConsensusRatio { get; set; }

        [JsonProperty("atr", NullValueHandling = NullValueHandling.Ignore)]
        public double? Atr { get; set; }

        [JsonProperty("natr", NullValueHandling = NullValueHandling.Ignore)]
        public double? Natr { get; set; }

        [JsonProperty("depthBid1Size", NullValueHandling = NullValueHandling.Ignore)]
        public double? DepthBid1Size { get; set; }

        [JsonProperty("depthBid1MaxSize", NullValueHandling = NullValueHandling.Ignore)]
        public double? DepthBid1MaxSize { get; set; }

        [JsonProperty("depthQueueDropPct", NullValueHandling = NullValueHandling.Ignore)]
        public double? DepthQueueDropPct { get; set; }

        [JsonProperty("marketRegime", NullValueHandling = NullValueHandling.Ignore)]
        public string MarketRegime { get; set; }

        [JsonProperty("botPositionQty")]
        public double BotPositionQty { get; set; }

        [JsonProperty("totalAccountQty")]
        public double TotalAccountQty { get; set; }

        [JsonProperty("lockedLongTermQty")]
        public double LockedLongTermQty { get; set; }

        [JsonProperty("dailyTradeCount")]
        public int DailyTradeCount { get; set; }
    }

    private struct MarketDataPayload
    {
        [JsonProperty("symbol")]
        public string Symbol { get; set; }

        [JsonProperty("dataType")]
        public string DataType { get; set; }

        [JsonProperty("payload")]
        public Dictionary<string, object> Payload { get; set; }

        [JsonProperty("timestamp")]
        public string Timestamp { get; set; }
    }

    private struct ContextStep
    {
        [JsonProperty("stepNo")]
        public int StepNo { get; set; }

        [JsonProperty("symbol")]
        public string Symbol { get; set; }

        [JsonProperty("dataType")]
        public string DataType { get; set; }

        [JsonProperty("payload")]
        public Dictionary<string, object> Payload { get; set; }

        [JsonProperty("reason")]
        public string Reason { get; set; }
    }

    private struct BotRuntimeConfigResponse
    {
        [JsonProperty("configVersion")]
        public string ConfigVersion { get; set; }

        [JsonProperty("configHash")]
        public string ConfigHash { get; set; }

        [JsonProperty("activeTradeProfile")]
        public ActiveTradeProfileInfo ActiveTradeProfile { get; set; }

        [JsonProperty("mode")]
        public string Mode { get; set; }

        [JsonProperty("enableDemoOrders")]
        public bool EnableDemoOrders { get; set; }

        [JsonProperty("enableRealOrders")]
        public bool EnableRealOrders { get; set; }

        [JsonProperty("requireDemoAccount")]
        public bool RequireDemoAccount { get; set; }

        [JsonProperty("demoAccountConfirmed")]
        public bool DemoAccountConfirmed { get; set; }

        [JsonProperty("maxOrderValueTl")]
        public decimal MaxOrderValueTl { get; set; }

        [JsonProperty("maxQtyPerOrder")]
        public decimal MaxQtyPerOrder { get; set; }

        [JsonProperty("maxOrdersPerDay")]
        public int MaxOrdersPerDay { get; set; }

        [JsonProperty("maxOrdersPerSymbolPerDay")]
        public int MaxOrdersPerSymbolPerDay { get; set; }

        [JsonProperty("allowMarketOrders")]
        public bool AllowMarketOrders { get; set; }

        [JsonProperty("scanIntervalMinutes")]
        public int ScanIntervalMinutes { get; set; }

        [JsonProperty("httpTimeoutSeconds")]
        public int HttpTimeoutSeconds { get; set; }

        [JsonProperty("maxFetchLoopPerSession")]
        public int MaxFetchLoopPerSession { get; set; }

        [JsonProperty("orderTimeInForce")]
        public string OrderTimeInForce { get; set; }

        [JsonProperty("indicatorPeriod")]
        public string IndicatorPeriod { get; set; }

        [JsonProperty("allowedSymbols")]
        public string[] AllowedSymbols { get; set; }

        [JsonProperty("lockedLongTermQty")]
        public Dictionary<string, decimal> LockedLongTermQty { get; set; }
    }

    private struct ActiveTradeProfileInfo
    {
        [JsonProperty("code")]
        public string Code { get; set; }

        [JsonProperty("name")]
        public string Name { get; set; }

        [JsonProperty("riskLevel")]
        public string RiskLevel { get; set; }
    }

    private struct AgenticSignalResponse
    {
        [JsonProperty("requestId")]
        public string RequestId { get; set; }

        [JsonProperty("sessionId")]
        public string SessionId { get; set; }

        [JsonProperty("symbol")]
        public string Symbol { get; set; }

        [JsonProperty("configVersion")]
        public string ConfigVersion { get; set; }

        [JsonProperty("configHash")]
        public string ConfigHash { get; set; }

        [JsonProperty("action")]
        public string Action { get; set; }

        [JsonProperty("allowOrder")]
        public bool AllowOrder { get; set; }

        [JsonProperty("requiresConfirmation")]
        public bool RequiresConfirmation { get; set; }

        [JsonProperty("reason")]
        public string Reason { get; set; }

        [JsonProperty("targetSymbol")]
        public string TargetSymbol { get; set; }

        [JsonProperty("requiredDataType")]
        public string RequiredDataType { get; set; }

        [JsonProperty("fetchData")]
        public FetchData? FetchData { get; set; }

        [JsonProperty("confidenceScore")]
        public double ConfidenceScore { get; set; }

        [JsonProperty("riskScore")]
        public double RiskScore { get; set; }

        [JsonProperty("qty")]
        public double Qty { get; set; }

        [JsonProperty("orderType")]
        public string OrderType { get; set; }

        [JsonProperty("price")]
        public double? Price { get; set; }

        [JsonProperty("entryRange")]
        public EntryRange? EntryRange { get; set; }

        [JsonProperty("stopLoss")]
        public double? StopLoss { get; set; }

        [JsonProperty("targetPrice")]
        public double? TargetPrice { get; set; }

        public string GetTargetSymbol()
        {
            if (!string.IsNullOrWhiteSpace(TargetSymbol))
                return TargetSymbol;
            return FetchData.HasValue ? FetchData.Value.TargetSymbol : null;
        }

        public string GetRequiredDataType()
        {
            if (!string.IsNullOrWhiteSpace(RequiredDataType))
                return RequiredDataType;
            return FetchData.HasValue ? FetchData.Value.DataType : null;
        }

        public static AgenticSignalResponse Wait(string requestId, string sessionId, string symbol, string reason)
        {
            return new AgenticSignalResponse
            {
                RequestId = requestId,
                SessionId = sessionId,
                Symbol = symbol,
                Action = "WAIT",
                AllowOrder = false,
                RequiresConfirmation = false,
                Reason = reason,
                ConfigVersion = null,
                ConfigHash = null,
                Qty = 0,
                OrderType = "NONE",
                ConfidenceScore = 0,
                RiskScore = 0
            };
        }
    }

    private struct FetchData
    {
        [JsonProperty("targetSymbol")]
        public string TargetSymbol { get; set; }

        [JsonProperty("dataType")]
        public string DataType { get; set; }

        [JsonProperty("reason")]
        public string Reason { get; set; }
    }

    private struct EntryRange
    {
        [JsonProperty("min")]
        public double Min { get; set; }

        [JsonProperty("max")]
        public double Max { get; set; }
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

    private struct PendingOverridesResponse
    {
        [JsonProperty("symbols")]
        public string[] Symbols { get; set; }
    }

    private struct PositionSyncEntry
    {
        [JsonProperty("symbol")]
        public string Symbol { get; set; }

        [JsonProperty("qty")]
        public decimal Qty { get; set; }
    }

    private struct PositionSyncRequest
    {
        [JsonProperty("positions")]
        public List<PositionSyncEntry> Positions { get; set; }
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
        public bool IsSimulated { get; set; }
        public string OrderId { get; set; }
        public string Message { get; set; }
    }
    }
}
