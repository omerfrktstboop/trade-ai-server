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

        // Atomic duplicate requestId check (ConcurrentDictionary.TryAdd = atomic)
        private readonly ConcurrentDictionary<string, object> _sentRequestIds = new ConcurrentDictionary<string, object>();

        // Lock objects for non-atomic compound operations
        private readonly object _inFlightLock = new object();
        private readonly object _closeLock = new object();
        private readonly object _dailyCounterLock = new object();

        private DateTime _dailyCounterDate = DateTime.Today;

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

            SafeDebug("Initialized symbols=" + string.Join(",", AllowedSymbols)
                + " mode=" + NormalizeMode(Mode)
                + " enableDemoOrders=" + EnableDemoOrders
                + " enableRealOrders=" + EnableRealOrders
                + " demoConfirmed=" + DemoAccountConfirmed
                + " scanIntervalMinutes=" + ScanIntervalMinutes
                + " server=" + ServerBaseUrl);
        }

        public override void OnDataUpdate(BarDataEventArgs barData)
        {
            ResetDailyCountersIfNeeded();

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
            var request = new AgenticSignalRequest
            {
                RequestId = requestId,
                SessionId = null,
                Symbol = symbol,
                MarketData = BuildMarketData(symbol, "DEPTH"),
                ContextHistory = new List<ContextStep>(),
                Mode = NormalizeMode(Mode)
            };
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

                        var parsed = JsonConvert.DeserializeObject<AgenticSignalResponse>(body);
                        if (parsed == null)
                        {
                            throw new Exception("Empty response");
                        }

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
            if (previousRequest != null)
            {
                if (previousRequest.ContextHistory != null)
                    contextHistory.AddRange(previousRequest.ContextHistory);

                if (previousRequest.MarketData != null)
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
            string depthSummary = "";
            try
            {
                var depth = GetMarketDepth(symbol);
                if (depth != null && depth.BidRows != null && depth.BidRows.Count >= 3)
                {
                    bestBid = depth.BidRows[0].Price;
                    secondBid = depth.BidRows[1].Price;
                    thirdBid = depth.BidRows[2].Price;
                    depthSummary = "bestBid=" + bestBid + ";secondBid=" + secondBid + ";thirdBid=" + thirdBid;
                }
            }
            catch (Exception ex)
            {
                depthSummary = "depth unavailable: " + ex.Message;
            }

            var payload = new Dictionary<string, object>();
            payload["lastPrice"] = ToDouble(lastPrice);
            payload["open"] = ToDouble(open);
            payload["high"] = ToDouble(high);
            payload["low"] = ToDouble(low);
            payload["ohlcReliable"] = ohlcReliable;
            payload["volume"] = ToDouble(volume);
            payload["rsi"] = CalculateRsi(symbol, 14);
            payload["ema20"] = CalculateEma(symbol, 20);
            payload["ema50"] = CalculateEma(symbol, 50);
            payload["macd"] = CalculateMacdLine(symbol);
            payload["macdSignal"] = CalculateMacdSignal(symbol);
            payload["bidPrice"] = ToDouble(bidPrice);
            payload["askPrice"] = ToDouble(askPrice);
            payload["bidVolume"] = 0;
            payload["askVolume"] = 0;
            payload["bestBid"] = ToDouble(bestBid);
            payload["secondBid"] = ToDouble(secondBid);
            payload["thirdBid"] = ToDouble(thirdBid);
            payload["depthSummary"] = depthSummary;
            payload["botPositionQty"] = ToDouble(GetBotPositionQty(symbol));
            payload["totalAccountQty"] = ToDouble(GetTotalAccountQty(symbol));
            payload["lockedLongTermQty"] = ToDouble(GetLockedLongTermQty(symbol));
            payload["dailyTradeCount"] = GetDailyTradeCount(symbol);

            return new MarketDataPayload
            {
                Symbol = symbol,
                DataType = NormalizeDataType(dataType),
                Payload = payload,
                Timestamp = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz")
            };
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

        // ── Order sending ────────────────────────────────────────────

        private async Task TrySendOrderAsync(AgenticSignalResponse response)
        {
            // ── Pre-trade validation gates ──

            if (response == null)
            {
                SafeDebug("Order blocked: response is null");
                return;
            }

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
                OrderExecutionResult execution = await SendLimitOrderAsync(symbol, action, qty, price);
                string status = execution.Success
                    ? (execution.IsSimulated ? "SIMULATED" : "SENT")
                    : "REJECTED";

                if (execution.Success)
                {
                    IncrementDailyTradeCount(symbol);
                    UpdateSimulatedPosition(symbol, action, qty);
                    SafeDebug("Order sent symbol=" + symbol
                        + " side=" + action
                        + " qty=" + qty
                        + " price=" + price
                        + " orderId=" + execution.OrderId
                        + " status=" + status);
                }

                await ReportOrderResultAsync(response, status, execution.Message, execution.OrderId);
            }
            catch (Exception ex)
            {
                SafeDebug("Order exception requestId=" + response.RequestId + " error=" + ex.Message);
                await ReportOrderResultAsync(response, "ERROR", ex.Message, null);
            }
        }

        /// <summary>
        /// Send a real limit order via Matriks IQ demo/sandbox account.
        /// Uses MatriksAlgo.SendLimitOrder(string symbol, decimal qty, OrderSide side, decimal price).
        /// 
        /// TODO: Verify the exact SendLimitOrder method signature against your Matriks SDK version.
        /// If the SDK uses a different overload (e.g., with OrderType enum, or returns a different type),
        /// adjust the call accordingly. The order IS sent to the Matriks demo infrastructure;
        /// it is NOT simulated/faked.
        /// </summary>
        private async Task<OrderExecutionResult> SendLimitOrderAsync(string symbol, string side, decimal qty, decimal limitPrice)
        {
            SafeDebug("Sending real limit order: " + side + " " + symbol + " qty=" + qty + " price=" + limitPrice);

            OrderSide orderSide = NormalizeAction(side) == "BUY" ? OrderSide.Buy : OrderSide.Sell;

            // Real Matriks IQ SDK call — sends to demo/sandbox account in DEMO_LIVE mode
            // Returns the order ID assigned by Matriks
            string orderId = SendLimitOrder(symbol, qty, orderSide, limitPrice);

            await Task.CompletedTask; // explicit async yield
            return new OrderExecutionResult
            {
                Success = true,
                IsSimulated = false,
                OrderId = orderId,
                Message = "Limit order sent to Matriks demo account"
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

        private bool IsDemoAccount()
        {
            if (!RequireDemoAccount)
                return true;
            return DemoAccountConfirmed;
        }

        // ── Compatibility layer (flat fields for legacy clients) ────

        private void ApplyFlatCompatibilityFields(AgenticSignalRequest request)
        {
            if (request == null || request.MarketData == null || request.MarketData.Payload == null)
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

        // ── Daily counter management ─────────────────────────────────

        private void IncrementDailyTradeCount(string symbol)
        {
            symbol = NormalizeSymbol(symbol);
            _dailyTradeCountBySymbol.AddOrUpdate(symbol, 1, (_, existing) => existing + 1);
        }

        private void UpdateSimulatedPosition(string symbol, string action, decimal qty)
        {
            symbol = NormalizeSymbol(symbol);
            if (NormalizeAction(action) == "BUY")
            {
                _botPositionQtyBySymbol.AddOrUpdate(symbol, qty, (_, existing) => existing + qty);
            }
            else if (NormalizeAction(action) == "SELL")
            {
                _botPositionQtyBySymbol.AddOrUpdate(symbol, 0m, (_, existing) => Math.Max(0m, existing - qty));
            }
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

        private static string NormalizeOrderType(string orderType)
        {
            return (orderType ?? "NONE").Trim().ToUpperInvariant();
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

    // ═════════════════════════════════════════════════════════════════
    // JSON DTOs — match trade-ai-server Pydantic models (camelCase JSON)
    // ═════════════════════════════════════════════════════════════════

    public class AgenticSignalRequest
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

        [JsonProperty("botPositionQty")]
        public double BotPositionQty { get; set; }

        [JsonProperty("totalAccountQty")]
        public double TotalAccountQty { get; set; }

        [JsonProperty("lockedLongTermQty")]
        public double LockedLongTermQty { get; set; }

        [JsonProperty("dailyTradeCount")]
        public int DailyTradeCount { get; set; }
    }

    public class MarketDataPayload
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

    public class ContextStep
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

    public class AgenticSignalResponse
    {
        [JsonProperty("requestId")]
        public string RequestId { get; set; }

        [JsonProperty("sessionId")]
        public string SessionId { get; set; }

        [JsonProperty("symbol")]
        public string Symbol { get; set; }

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
        public FetchData FetchData { get; set; }

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
        public EntryRange EntryRange { get; set; }

        [JsonProperty("stopLoss")]
        public double? StopLoss { get; set; }

        [JsonProperty("targetPrice")]
        public double? TargetPrice { get; set; }

        public string GetTargetSymbol()
        {
            if (!string.IsNullOrWhiteSpace(TargetSymbol))
                return TargetSymbol;
            return FetchData != null ? FetchData.TargetSymbol : null;
        }

        public string GetRequiredDataType()
        {
            if (!string.IsNullOrWhiteSpace(RequiredDataType))
                return RequiredDataType;
            return FetchData != null ? FetchData.DataType : null;
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
                Qty = 0,
                OrderType = "NONE",
                ConfidenceScore = 0,
                RiskScore = 0
            };
        }
    }

    public class FetchData
    {
        [JsonProperty("targetSymbol")]
        public string TargetSymbol { get; set; }

        [JsonProperty("dataType")]
        public string DataType { get; set; }

        [JsonProperty("reason")]
        public string Reason { get; set; }
    }

    public class EntryRange
    {
        [JsonProperty("min")]
        public double Min { get; set; }

        [JsonProperty("max")]
        public double Max { get; set; }
    }

    public class OrderResultRequest
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

    public class OrderExecutionResult
    {
        public bool Success { get; set; }
        public bool IsSimulated { get; set; }
        public string OrderId { get; set; }
        public string Message { get; set; }
    }
}
