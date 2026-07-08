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
using Matriks.Lean.Algotrader.AlgoBase;
using Matriks.Lean.Algotrader.Models;
using Matriks.Lean.Algotrader.Trading;
using Matriks.Symbols;
using Matriks.Trader.Core;
using Matriks.Trader.Core.Fields;
using Matriks.Trader.Core.TraderModels;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Matriks.Lean.Algotrader
{
    using AgenticSignalRequest = System.Collections.Generic.Dictionary<string, object>;
    using AgenticSignalResponse = Newtonsoft.Json.Linq.JObject;
    using ContextStep = System.Collections.Generic.Dictionary<string, object>;
    using IndicatorConsensus = System.Tuple<int, int, int, string, double>;
    using MarketDataPayload = System.Collections.Generic.Dictionary<string, object>;
    using OrderExecutionResult = System.Tuple<bool, bool, string, string>;
    using PendingOrderContext = System.Tuple<string, string, string, decimal, decimal>;

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

        [Parameter("https://omermatriks.ddns.net")]
        public string ServerBaseUrl;

        [Parameter("BURAYA_TOKEN")]
        public string ApiToken;

        [Parameter("DEMO_LIVE")]
        public string Mode;

        [Parameter(true)]
        public bool EnableDemoOrders;

        [Parameter(false)]
        public bool EnableRealOrders;

        [Parameter(true)]
        public bool RequireDemoAccount;

        [Parameter(false)]
        public bool DemoAccountConfirmed;

        [Parameter(1000)]
        public decimal MaxOrderValueTl;

        [Parameter(10)]
        public decimal MaxQtyPerOrder;

        [Parameter(3)]
        public int MaxOrdersPerDay;

        [Parameter(1)]
        public int MaxOrdersPerSymbolPerDay;

        [Parameter(false)]
        public bool AllowMarketOrders;

        [Parameter(30)]
        public int ScanIntervalMinutes;

        [Parameter(15)]
        public int HttpTimeoutSeconds;

        [Parameter(3)]
        public int MaxFetchLoopPerSession;

        [Parameter("Day")]
        public string OrderTimeInForce;

        // ── Symbols ──────────────────────────────────────────────────

        public string[] AllowedSymbols =
        {
            "THYAO",
            "AKBNK",
            "SISE",
            "KCHOL",
            "TUPRS",
            "ANELE"
        };

        public Dictionary<string, decimal> LockedLongTermQty = new Dictionary<string, decimal>
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
        private readonly ConcurrentDictionary<string, List<decimal>> _closeHistoryBySymbol = new ConcurrentDictionary<string, List<decimal>>();
        private readonly ConcurrentDictionary<string, decimal> _maxBid1SizeBySymbol = new ConcurrentDictionary<string, decimal>();
        private readonly ConcurrentDictionary<string, PendingOrderContext> _pendingOrdersBySymbolSide = new ConcurrentDictionary<string, PendingOrderContext>();
        private readonly ConcurrentDictionary<string, PendingOrderContext> _pendingOrdersByOrderId = new ConcurrentDictionary<string, PendingOrderContext>();

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

        // ── Matriks lifecycle ───────────────────────────────────────

        public override void OnInit()
        {
            _http = new HttpClient
            {
                BaseAddress = new Uri(ServerBaseUrl.TrimEnd('/') + "/"),
                Timeout = TimeSpan.FromSeconds(Math.Max(10, HttpTimeoutSeconds))
            };
            _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", ApiToken);

            foreach (string symbol in AllowedSymbols)
            {
                string normalized = NormalizeSymbol(symbol);
                AddSymbol(normalized, SymbolPeriod.Min);
                AddSymbolMarketData(normalized);
                AddSymbolMarketDepth(normalized);

                _lastScanUtcBySymbol[normalized] = DateTime.MinValue;
                _inFlightBySymbol[normalized] = false;
                _dailyTradeCountBySymbol[normalized] = 0;
                _botPositionQtyBySymbol[normalized] = 0m;
                _closeHistoryBySymbol[normalized] = new List<decimal>();
            }

            SendOrderSequential(false);
            WorkWithPermanentSignal(false);
            SetTimerInterval(60);
            LogTradeUserInfo();
            LoadRealPositionsSnapshot();

            SafeDebug("Initialized symbols=" + string.Join(",", AllowedSymbols)
                + " mode=" + NormalizeMode(Mode)
                + " enableDemoOrders=" + EnableDemoOrders
                + " enableRealOrders=" + EnableRealOrders
                + " demoConfirmed=" + DemoAccountConfirmed
                + " scanIntervalMinutes=" + ScanIntervalMinutes
                + " timerSeconds=60"
                + " timeInForce=" + NormalizeTimeInForce(OrderTimeInForce)
                + " server=" + ServerBaseUrl);

            ScanDueSymbols();
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
            ScanDueSymbols();
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

                // Scan interval check
                DateTime lastScanUtc = _lastScanUtcBySymbol.TryGetValue(symbol, out var dt) ? dt : DateTime.MinValue;
                if (DateTime.UtcNow - lastScanUtc < TimeSpan.FromMinutes(Math.Max(1, ScanIntervalMinutes)))
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

            PendingOrderContext context = ResolvePendingOrderContext(orderId, symbol, side);
            if (context == null)
            {
                context = CreatePendingOrderContext(
                    "MATRiKS-" + (string.IsNullOrWhiteSpace(orderId) ? BuildRequestId(symbol) : orderId),
                    symbol,
                    side,
                    orderQty > 0 ? orderQty : filledQty,
                    avgPx > 0 ? avgPx : order.Price);
            }

            if (!string.IsNullOrWhiteSpace(orderId))
            {
                _pendingOrdersByOrderId[orderId] = context;
            }

            decimal reportQty = filledQty > 0 ? filledQty : (orderQty > 0 ? orderQty : PendingQty(context));
            decimal reportPrice = avgPx > 0 ? avgPx : (order.Price > 0 ? order.Price : PendingPrice(context));
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
                _pendingOrdersBySymbolSide.TryRemove(BuildSymbolSideKey(symbol, PendingAction(context)), out _);
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
                try { _http.Dispose(); } catch { }
            }
            SafeDebug("Stopped.");
        }

        // ── Agentic evaluate flow ────────────────────────────────────

        private async Task SendEvaluateAsync(string symbol)
        {
            string requestId = BuildRequestId(symbol);
            var request = BuildSignalRequest(
                requestId,
                null,
                symbol,
                BuildMarketData(symbol, "DEPTH"),
                new List<ContextStep>(),
                NormalizeMode(Mode));
            ApplyFlatCompatibilityFields(request);

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
                    SafeDebug("POST /api/signal/evaluate-agent requestId=" + GetRequestString(request, "requestId")
                        + " symbol=" + GetRequestString(request, "symbol")
                        + " sessionId=" + (GetRequestString(request, "sessionId") ?? "null")
                        + " attempt=" + (attempt + 1));

                    using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
                    using (var response = await _http.PostAsync("api/signal/evaluate-agent", content))
                    {
                        string body = await response.Content.ReadAsStringAsync();
                        if (!response.IsSuccessStatusCode)
                        {
                            throw new Exception("HTTP " + (int)response.StatusCode + ": " + body);
                        }

                        AgenticSignalResponse parsed = JObject.Parse(body);
                        if (string.IsNullOrWhiteSpace(GetResponseString(parsed, "action")))
                        {
                            throw new Exception("Empty response");
                        }

                        return parsed;
                    }
                }
                catch (Exception ex)
                {
                    SafeDebug("HTTP error requestId=" + GetRequestString(request, "requestId") + " attempt=" + (attempt + 1) + " error=" + ex.Message);
                    if (attempt >= 1)
                    {
                        return BuildWaitResponse(
                            GetRequestString(request, "requestId"),
                            GetRequestString(request, "sessionId"),
                            GetRequestString(request, "symbol"),
                            "HTTP error after retry: " + ex.Message);
                    }
                }
            }

            return BuildWaitResponse(
                GetRequestString(request, "requestId"),
                GetRequestString(request, "sessionId"),
                GetRequestString(request, "symbol"),
                "HTTP error");
        }

        private async Task HandleServerResponseAsync(
            string originalSymbol,
            AgenticSignalResponse response,
            AgenticSignalRequest previousRequest,
            int fetchLoopCount)
        {
            string action = NormalizeAction(GetResponseString(response, "action"));
            string targetSymbol = GetResponseTargetSymbol(response);
            string requiredDataType = GetResponseRequiredDataType(response);

            SafeDebug("Response action=" + action
                + " sessionId=" + (GetResponseString(response, "sessionId") ?? "null")
                + " targetSymbol=" + (targetSymbol ?? "null")
                + " dataType=" + (requiredDataType ?? "null")
                + " reason=" + GetResponseString(response, "reason"));

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
                    GetResponseString(response, "sessionId"),
                    GetRequestString(previousRequest, "requestId"),
                    previousRequest);

                AgenticSignalResponse nextResponse = await SendAgenticRequestAsync(nextRequest);
                await HandleServerResponseAsync(originalSymbol, nextResponse, nextRequest, fetchLoopCount + 1);
                return;
            }

            if (action == "WAIT")
            {
                SafeDebug("Final response action=WAIT reason=" + GetResponseString(response, "reason"));
                return;
            }

            if (action == "BUY" || action == "SELL")
            {
                await TrySendOrderAsync(response);
                return;
            }

            SafeDebug("Unknown response action=" + GetResponseString(response, "action") + ". Treated as WAIT.");
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
            var previousContextHistory = GetRequestContextHistory(previousRequest);
            if (previousContextHistory != null)
            {
                contextHistory.AddRange(previousContextHistory);
            }

            MarketDataPayload previousMarketData = GetRequestMarketData(previousRequest);
            Dictionary<string, object> previousPayload = GetMarketDataPayload(previousMarketData);
            if (previousPayload != null)
            {
                contextHistory.Add(BuildContextStep(
                    contextHistory.Count + 1,
                    GetMarketDataString(previousMarketData, "symbol"),
                    GetMarketDataString(previousMarketData, "dataType"),
                    previousPayload,
                    "Previous marketData"));
            }

            SafeDebug("Agentic fetch request rootSymbol=" + rootSymbol + " targetSymbol=" + targetSymbol + " dataType=" + requiredDataType);

            var request = BuildSignalRequest(
                rootRequestId,
                sessionId,
                NormalizeSymbol(rootSymbol),
                BuildMarketData(targetSymbol, requiredDataType),
                contextHistory,
                NormalizeMode(Mode));
            ApplyFlatCompatibilityFields(request);

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

            double? rsi = CalculateRsi(symbol, 14);
            double? ema20 = CalculateEma(symbol, 20);
            double? ema50 = CalculateEma(symbol, 50);
            double? macd = CalculateMacdLine(symbol);
            double? macdSignal = CalculateMacdSignal(symbol);
            var technicalFeatures = BuildTechnicalFeaturePayload(
                symbol,
                lastPrice,
                rsi,
                ema20,
                ema50,
                macd,
                macdSignal,
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

            var marketData = new MarketDataPayload();
            marketData["symbol"] = symbol;
            marketData["dataType"] = NormalizeDataType(dataType);
            marketData["payload"] = payload;
            marketData["timestamp"] = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz");
            return marketData;
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

            string action = NormalizeAction(GetResponseString(response, "action"));
            string symbol = NormalizeSymbol(GetResponseString(response, "symbol"));
            string mode = NormalizeMode(Mode);

            // Convert Qty/Price
            decimal qty = ToDecimal(GetResponseDouble(response, "qty"));
            double? responsePrice = GetResponseNullableDouble(response, "price");
            decimal price = responsePrice.HasValue ? ToDecimal(responsePrice.Value) : 0m;
            decimal orderValue = qty * price;

            SafeDebug("Final response action=" + action
                + " allowOrder=" + GetResponseBool(response, "allowOrder")
                + " orderType=" + GetResponseString(response, "orderType")
                + " price=" + (responsePrice.HasValue ? responsePrice.Value.ToString() : "null")
                + " qty=" + GetResponseDouble(response, "qty"));
            SafeDebug("Pre-trade checks started requestId=" + GetResponseString(response, "requestId"));

            // Gate 1: Action must be BUY or SELL
            if (action != "BUY" && action != "SELL")
            {
                await RejectOrderAsync(response, "unknown action=" + GetResponseString(response, "action"));
                return;
            }

            // Gate 2: Duplicate requestId (atomic check+add via ConcurrentDictionary)
            if (!_sentRequestIds.TryAdd(GetResponseString(response, "requestId"), null))
            {
                await RejectOrderAsync(response, "duplicate requestId");
                return;
            }

            // Gate 3: allowOrder must be true
            if (!GetResponseBool(response, "allowOrder"))
            {
                await RejectOrderAsync(response, "allowOrder=false");
                return;
            }

            // Gate 4: No confirmation required
            if (GetResponseBool(response, "requiresConfirmation"))
            {
                await RejectOrderAsync(response, "requiresConfirmation=true");
                return;
            }

            // Gate 5: Only LIMIT orders
            if (NormalizeOrderType(GetResponseString(response, "orderType")) != "LIMIT")
            {
                await RejectOrderAsync(response, "orderType is not LIMIT: " + GetResponseString(response, "orderType"));
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
                OrderExecutionResult execution = await SendLimitOrderAsync(GetResponseString(response, "requestId"), symbol, action, qty, price);
                if (ExecutionSuccess(execution))
                {
                    IncrementDailyTradeCount(symbol);
                    SafeDebug("Order SENT_PENDING symbol=" + symbol
                        + " side=" + action
                        + " qty=" + qty
                        + " price=" + price
                        + " message=" + ExecutionMessage(execution));
                    return;
                }

                await ReportOrderResultAsync(response, "REJECTED", ExecutionMessage(execution), ExecutionOrderId(execution));
            }
            catch (Exception ex)
            {
                SafeDebug("Order exception requestId=" + GetResponseString(response, "requestId") + " error=" + ex.Message);
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
                return CreateOrderExecutionResult(false, false, null, quantityError);
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
                return CreateOrderExecutionResult(false, false, null, "rounded limit price <= 0");
            }

            if (roundedPrice != limitPrice)
            {
                SafeDebug("Limit price rounded symbol=" + symbol + " original=" + limitPrice + " rounded=" + roundedPrice);
            }

            var pending = CreatePendingOrderContext(requestId, symbol, NormalizeAction(side), quantity, roundedPrice);
            _pendingOrdersBySymbolSide[BuildSymbolSideKey(symbol, PendingAction(pending))] = pending;

            SafeDebug("Sending real limit order: " + PendingAction(pending)
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
                _pendingOrdersBySymbolSide.TryRemove(BuildSymbolSideKey(symbol, PendingAction(pending)), out _);
                throw;
            }

            await Task.CompletedTask; // explicit async yield
            return CreateOrderExecutionResult(true, false, null, "Limit order SENT_PENDING; final status will be reported by OnOrderUpdate");
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
            double? responsePrice = GetResponseNullableDouble(response, "price");
            var payload = BuildOrderResultPayload(
                GetResponseString(response, "requestId"),
                NormalizeSymbol(GetResponseString(response, "symbol")),
                NormalizeAction(GetResponseString(response, "action")),
                GetResponseDouble(response, "qty"),
                responsePrice.HasValue ? responsePrice.Value : 0,
                status,
                matriksMessage,
                orderId);

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

                SafeDebug("Order result posted to server status=" + status + " requestId=" + GetResponseString(response, "requestId"));
            }
            catch (Exception ex)
            {
                SafeDebug("Order result post exception requestId=" + GetResponseString(response, "requestId") + " error=" + ex.Message);
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
            var payload = BuildOrderResultPayload(
                PendingRequestId(context),
                NormalizeSymbol(PendingSymbol(context)),
                NormalizeAction(PendingAction(context)),
                ToDouble(qty),
                ToDouble(price),
                status,
                matriksMessage,
                orderId);

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
                    + " requestId=" + PendingRequestId(context)
                    + " orderId=" + orderId);
            }
            catch (Exception ex)
            {
                SafeDebug("Order update post exception requestId=" + PendingRequestId(context) + " error=" + ex.Message);
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

        private void ApplyFlatCompatibilityFields(AgenticSignalRequest request)
        {
            MarketDataPayload marketData = GetRequestMarketData(request);
            Dictionary<string, object> payload = GetMarketDataPayload(marketData);
            if (payload == null)
                return;

            request["timeframe"] = SymbolPeriod.Min.ToString();
            request["lastPrice"] = GetPayloadDouble(payload, "lastPrice");
            request["open"] = GetPayloadDouble(payload, "open");
            request["high"] = GetPayloadDouble(payload, "high");
            request["low"] = GetPayloadDouble(payload, "low");
            request["volume"] = GetPayloadDouble(payload, "volume");
            request["rsi"] = GetPayloadNullableDouble(payload, "rsi");
            request["ema20"] = GetPayloadNullableDouble(payload, "ema20");
            request["ema50"] = GetPayloadNullableDouble(payload, "ema50");
            request["macd"] = GetPayloadNullableDouble(payload, "macd");
            request["macdSignal"] = GetPayloadNullableDouble(payload, "macdSignal");
            request["alphaTrendSignal"] = GetPayloadString(payload, "alphaTrendSignal");
            request["alphaTrendMode"] = GetPayloadString(payload, "alphaTrendMode");
            request["indicatorBuyCount"] = Convert.ToInt32(GetPayloadDouble(payload, "indicatorBuyCount"));
            request["indicatorSellCount"] = Convert.ToInt32(GetPayloadDouble(payload, "indicatorSellCount"));
            request["indicatorNeutralCount"] = Convert.ToInt32(GetPayloadDouble(payload, "indicatorNeutralCount"));
            request["indicatorConsensus"] = GetPayloadString(payload, "indicatorConsensus");
            request["indicatorConsensusRatio"] = GetPayloadNullableDouble(payload, "indicatorConsensusRatio");
            request["atr"] = GetPayloadNullableDouble(payload, "atr");
            request["natr"] = GetPayloadNullableDouble(payload, "natr");
            request["depthBid1Size"] = GetPayloadNullableDouble(payload, "depthBid1Size");
            request["depthBid1MaxSize"] = GetPayloadNullableDouble(payload, "depthBid1MaxSize");
            request["depthQueueDropPct"] = GetPayloadNullableDouble(payload, "depthQueueDropPct");
            request["marketRegime"] = GetPayloadString(payload, "marketRegime");
            request["botPositionQty"] = GetPayloadDouble(payload, "botPositionQty");
            request["totalAccountQty"] = GetPayloadDouble(payload, "totalAccountQty");
            request["lockedLongTermQty"] = GetPayloadDouble(payload, "lockedLongTermQty");
            request["dailyTradeCount"] = Convert.ToInt32(GetPayloadDouble(payload, "dailyTradeCount"));
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
            decimal bid1Size,
            decimal maxBid1Size,
            decimal depthQueueDropPct)
        {
            var features = new Dictionary<string, object>();
            var consensus = CalculateIndicatorConsensus(lastPrice, rsi, ema20, ema50, macd, macdSignal);
            double? atr = CalculateAtrFromClose(symbol, 14);
            double? natr = CalculateNatrPct(symbol, 14);

            features["schemaVersion"] = "technical-features-v1";
            features["alphaTrendSignal"] = CalculateAlphaTrendProxySignal(rsi, ema20, ema50, macd, macdSignal);
            features["alphaTrendMode"] = "PROXY_EMA_MACD_RSI";
            features["indicatorBuyCount"] = ConsensusBuyCount(consensus);
            features["indicatorSellCount"] = ConsensusSellCount(consensus);
            features["indicatorNeutralCount"] = ConsensusNeutralCount(consensus);
            features["indicatorConsensus"] = ConsensusSignal(consensus);
            features["indicatorConsensusRatio"] = ConsensusRatio(consensus);
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

            return Tuple.Create(buy, sell, neutral, signal, total > 0 ? ToDouble((decimal)maxDirectional / total) : 0);
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
            if (ConsensusSignal(consensus) == "BUY" || ConsensusSignal(consensus) == "SELL")
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

        private PendingOrderContext ResolvePendingOrderContext(string orderId, string symbol, string side)
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

        private static AgenticSignalRequest BuildSignalRequest(
            string requestId,
            string sessionId,
            string symbol,
            MarketDataPayload marketData,
            List<ContextStep> contextHistory,
            string mode)
        {
            var request = new AgenticSignalRequest();
            request["requestId"] = requestId;
            request["sessionId"] = sessionId;
            request["symbol"] = symbol;
            request["marketData"] = marketData;
            request["contextHistory"] = contextHistory;
            request["mode"] = mode;
            return request;
        }

        private static ContextStep BuildContextStep(
            int stepNo,
            string symbol,
            string dataType,
            Dictionary<string, object> payload,
            string reason)
        {
            var step = new ContextStep();
            step["stepNo"] = stepNo;
            step["symbol"] = symbol;
            step["dataType"] = dataType;
            step["payload"] = payload;
            step["reason"] = reason;
            return step;
        }

        private static Dictionary<string, object> BuildOrderResultPayload(
            string requestId,
            string symbol,
            string action,
            double qty,
            double price,
            string status,
            string matriksMessage,
            string orderId)
        {
            var payload = new Dictionary<string, object>();
            payload["requestId"] = requestId;
            payload["symbol"] = symbol;
            payload["action"] = action;
            payload["qty"] = qty;
            payload["price"] = price;
            payload["status"] = status;
            payload["matriksMessage"] = matriksMessage;
            payload["orderId"] = orderId;
            return payload;
        }

        private static string GetRequestString(AgenticSignalRequest request, string key)
        {
            if (request == null || !request.ContainsKey(key) || request[key] == null)
                return null;
            return Convert.ToString(request[key]);
        }

        private static MarketDataPayload GetRequestMarketData(AgenticSignalRequest request)
        {
            if (request == null || !request.ContainsKey("marketData"))
                return null;
            return request["marketData"] as MarketDataPayload;
        }

        private static List<ContextStep> GetRequestContextHistory(AgenticSignalRequest request)
        {
            if (request == null || !request.ContainsKey("contextHistory"))
                return null;
            return request["contextHistory"] as List<ContextStep>;
        }

        private static string GetMarketDataString(MarketDataPayload marketData, string key)
        {
            if (marketData == null || !marketData.ContainsKey(key) || marketData[key] == null)
                return null;
            return Convert.ToString(marketData[key]);
        }

        private static Dictionary<string, object> GetMarketDataPayload(MarketDataPayload marketData)
        {
            if (marketData == null || !marketData.ContainsKey("payload"))
                return null;
            return marketData["payload"] as Dictionary<string, object>;
        }

        private static AgenticSignalResponse BuildWaitResponse(string requestId, string sessionId, string symbol, string reason)
        {
            var response = new AgenticSignalResponse();
            response["requestId"] = requestId;
            response["sessionId"] = sessionId == null ? JValue.CreateNull() : new JValue(sessionId);
            response["symbol"] = symbol;
            response["action"] = "WAIT";
            response["allowOrder"] = false;
            response["requiresConfirmation"] = false;
            response["reason"] = reason;
            response["qty"] = 0;
            response["orderType"] = "NONE";
            response["confidenceScore"] = 0;
            response["riskScore"] = 0;
            return response;
        }

        private static string GetResponseString(AgenticSignalResponse response, string key)
        {
            if (response == null)
                return null;
            JToken token = response[key];
            if (token == null || token.Type == JTokenType.Null)
                return null;
            return token.Value<string>();
        }

        private static bool GetResponseBool(AgenticSignalResponse response, string key)
        {
            if (response == null)
                return false;
            JToken token = response[key];
            if (token == null || token.Type == JTokenType.Null)
                return false;
            return token.Value<bool>();
        }

        private static double GetResponseDouble(AgenticSignalResponse response, string key)
        {
            double? value = GetResponseNullableDouble(response, key);
            return value.HasValue ? value.Value : 0;
        }

        private static double? GetResponseNullableDouble(AgenticSignalResponse response, string key)
        {
            if (response == null)
                return null;
            JToken token = response[key];
            if (token == null || token.Type == JTokenType.Null)
                return null;
            return token.Value<double>();
        }

        private static string GetResponseTargetSymbol(AgenticSignalResponse response)
        {
            string targetSymbol = GetResponseString(response, "targetSymbol");
            if (!string.IsNullOrWhiteSpace(targetSymbol))
                return targetSymbol;

            JToken fetchData = response == null ? null : response["fetchData"];
            if (fetchData == null || fetchData.Type == JTokenType.Null)
                return null;
            return fetchData.Value<string>("targetSymbol");
        }

        private static string GetResponseRequiredDataType(AgenticSignalResponse response)
        {
            string requiredDataType = GetResponseString(response, "requiredDataType");
            if (!string.IsNullOrWhiteSpace(requiredDataType))
                return requiredDataType;

            JToken fetchData = response == null ? null : response["fetchData"];
            if (fetchData == null || fetchData.Type == JTokenType.Null)
                return null;
            return fetchData.Value<string>("dataType");
        }

        private static PendingOrderContext CreatePendingOrderContext(string requestId, string symbol, string action, decimal qty, decimal price)
        {
            return Tuple.Create(requestId, symbol, action, qty, price);
        }

        private static string PendingRequestId(PendingOrderContext context)
        {
            return context == null ? null : context.Item1;
        }

        private static string PendingSymbol(PendingOrderContext context)
        {
            return context == null ? null : context.Item2;
        }

        private static string PendingAction(PendingOrderContext context)
        {
            return context == null ? null : context.Item3;
        }

        private static decimal PendingQty(PendingOrderContext context)
        {
            return context == null ? 0m : context.Item4;
        }

        private static decimal PendingPrice(PendingOrderContext context)
        {
            return context == null ? 0m : context.Item5;
        }

        private static OrderExecutionResult CreateOrderExecutionResult(bool success, bool isSimulated, string orderId, string message)
        {
            return Tuple.Create(success, isSimulated, orderId, message);
        }

        private static bool ExecutionSuccess(OrderExecutionResult result)
        {
            return result != null && result.Item1;
        }

        private static string ExecutionOrderId(OrderExecutionResult result)
        {
            return result == null ? null : result.Item3;
        }

        private static string ExecutionMessage(OrderExecutionResult result)
        {
            return result == null ? null : result.Item4;
        }

        private static int ConsensusBuyCount(IndicatorConsensus consensus)
        {
            return consensus == null ? 0 : consensus.Item1;
        }

        private static int ConsensusSellCount(IndicatorConsensus consensus)
        {
            return consensus == null ? 0 : consensus.Item2;
        }

        private static int ConsensusNeutralCount(IndicatorConsensus consensus)
        {
            return consensus == null ? 0 : consensus.Item3;
        }

        private static string ConsensusSignal(IndicatorConsensus consensus)
        {
            return consensus == null ? "NEUTRAL" : consensus.Item4;
        }

        private static double ConsensusRatio(IndicatorConsensus consensus)
        {
            return consensus == null ? 0 : consensus.Item5;
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
    }
}
