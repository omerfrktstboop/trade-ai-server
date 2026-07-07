// ═══════════════════════════════════════════════════════════════════════════════
// TradeAiAgenticBot.cs — Matriks IQ C# Agentic Protocol Client
// ═══════════════════════════════════════════════════════════════════════════════
// Connects to trade-ai-server's /api/signal/evaluate-agent endpoint.
//
// Protocol: AgenticSignalRequest → AgenticSignalResponse
// Multi-turn: FETCH_DATA ping-pong → final BUY/SELL/WAIT
//
// ════════ Configure these before use ════════
//   ServerBaseUrl  — e.g. "https://omermatriks.ddns.net"
//   ApiToken       — Bearer token from trade-ai-server
//   Mode           — "PAPER" | "LIVE" | "DEMO_LIVE"
//   EnableDemoOrders — true to place demo orders after DEMO_LIVE decisions
// ════════════════════════════════════════════════

using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace TradeAiAgentic
{
    // ─────────────────────────────────────────────────────────────────────────
    // Configuration
    // ─────────────────────────────────────────────────────────────────────────
    public static class BotConfig
    {
        // ── REQUIRED ────────────────────────────────────────────────────────
        public const string ServerBaseUrl      = "https://omermatriks.ddns.net";
        public const string ApiToken           = "your-bearer-token-here";

        // ── Trading mode ────────────────────────────────────────────────────
        public const string Mode               = "DEMO_LIVE";   // PAPER | LIVE | DEMO_LIVE
        public const bool   EnableDemoOrders   = true;          // place LIMIT orders
        public const bool   DemoAccountConfirmed = true;        // user acknowledged

        // ── Safety ──────────────────────────────────────────────────────────
        public const int    MaxFetchLoopPerSession = 3;         // bail out after N FETCH_DATA
        public const int    DuplicateWindowSeconds = 5;         // reject same requestId within window
    }

    // ─────────────────────────────────────────────────────────────────────────
    // JSON Models (camelCase serialization)
    // ─────────────────────────────────────────────────────────────────────────

    public class MarketDataPayload
    {
        [JsonPropertyName("symbol")]     public string Symbol { get; set; }
        [JsonPropertyName("dataType")]   public string DataType { get; set; }
        [JsonPropertyName("payload")]    public Dictionary<string, object> Payload { get; set; }
    }

    public class ContextStep
    {
        [JsonPropertyName("stepNo")]     public int StepNo { get; set; }
        [JsonPropertyName("symbol")]     public string Symbol { get; set; }
        [JsonPropertyName("dataType")]   public string DataType { get; set; }
        [JsonPropertyName("payload")]    public Dictionary<string, object> Payload { get; set; }
        [JsonPropertyName("reason")]     public string Reason { get; set; }
    }

    public class AgenticRequest
    {
        [JsonPropertyName("requestId")]   public string RequestId { get; set; }
        [JsonPropertyName("symbol")]      public string Symbol { get; set; }
        [JsonPropertyName("sessionId")]   public string SessionId { get; set; }
        [JsonPropertyName("mode")]        public string Mode { get; set; }
        [JsonPropertyName("marketData")]  public MarketDataPayload MarketData { get; set; }
        [JsonPropertyName("contextHistory")] public List<ContextStep> ContextHistory { get; set; }
    }

    public class AgenticResponse
    {
        [JsonPropertyName("requestId")]       public string RequestId { get; set; }
        [JsonPropertyName("sessionId")]        public string SessionId { get; set; }
        [JsonPropertyName("action")]          public string Action { get; set; }
        [JsonPropertyName("allowOrder")]      public bool AllowOrder { get; set; }
        [JsonPropertyName("requiresConfirmation")] public bool RequiresConfirmation { get; set; }
        [JsonPropertyName("reason")]          public string Reason { get; set; }

        // FETCH_DATA fields
        [JsonPropertyName("targetSymbol")]    public string TargetSymbol { get; set; }
        [JsonPropertyName("requiredDataType")] public string RequiredDataType { get; set; }

        // Order fields
        [JsonPropertyName("confidenceScore")] public double ConfidenceScore { get; set; }
        [JsonPropertyName("riskScore")]       public double RiskScore { get; set; }
        [JsonPropertyName("qty")]             public double Qty { get; set; }
        [JsonPropertyName("orderType")]       public string OrderType { get; set; }
        [JsonPropertyName("price")]           public double? Price { get; set; }
        [JsonPropertyName("entryRange")]      public EntryRange EntryRange { get; set; }
        [JsonPropertyName("stopLoss")]        public double? StopLoss { get; set; }
        [JsonPropertyName("targetPrice")]     public double? TargetPrice { get; set; }
    }

    public class EntryRange
    {
        [JsonPropertyName("min")] public double Min { get; set; }
        [JsonPropertyName("max")] public double Max { get; set; }
    }

    public class OrderResult
    {
        [JsonPropertyName("requestId")]   public string RequestId { get; set; }
        [JsonPropertyName("orderId")]     public string OrderId { get; set; }
        [JsonPropertyName("status")]      public string Status { get; set; }
        [JsonPropertyName("filledQty")]   public double FilledQty { get; set; }
        [JsonPropertyName("avgPrice")]    public double AvgPrice { get; set; }
        [JsonPropertyName("error")]       public string Error { get; set; }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Bot
    // ─────────────────────────────────────────────────────────────────────────

    public class TradeAiAgenticBot
    {
        private readonly HttpClient _http;
        private readonly JsonSerializerOptions _jsonOpts;

        // Duplicate requestId protection
        private readonly Dictionary<string, DateTime> _recentRequests = new();
        private readonly object _requestLock = new();

        // Session state for multi-turn
        private string _sessionId;
        private readonly List<ContextStep> _contextHistory = new();

        public TradeAiAgenticBot()
        {
            _http = new HttpClient { BaseAddress = new Uri(BotConfig.ServerBaseUrl) };
            _http.DefaultRequestHeaders.Add("Authorization", $"Bearer {BotConfig.ApiToken}");
            _http.Timeout = TimeSpan.FromSeconds(30);

            _jsonOpts = new JsonSerializerOptions
            {
                PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
                DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
            };
        }

        // ── Safety: duplicate requestId guard ──────────────────────────────

        private bool IsDuplicateRequest(string requestId)
        {
            lock (_requestLock)
            {
                // Clean old entries
                var cutoff = DateTime.UtcNow.AddSeconds(-BotConfig.DuplicateWindowSeconds);
                var stale = new List<string>();
                foreach (var kv in _recentRequests)
                    if (kv.Value < cutoff) stale.Add(kv.Key);
                foreach (var key in stale) _recentRequests.Remove(key);

                if (_recentRequests.TryGetValue(requestId, out var _))
                    return true; // duplicate

                _recentRequests[requestId] = DateTime.UtcNow;
                return false;
            }
        }

        // ── Safety validator ────────────────────────────────────────────────

        private (bool ok, string reason) ValidateSafety(AgenticResponse resp)
        {
            if (resp == null)
                return (false, "Null response");

            if (resp.Action == "BUY" || resp.Action == "SELL")
            {
                if (!BotConfig.EnableDemoOrders)
                    return (false, $"Order blocked: EnableDemoOrders=false (action={resp.Action})");
                if (!BotConfig.DemoAccountConfirmed)
                    return (false, $"Order blocked: DemoAccountConfirmed=false (action={resp.Action})");
            }

            if (resp.Qty <= 0 && (resp.Action == "BUY" || resp.Action == "SELL"))
                return (false, $"Invalid qty={resp.Qty} for action={resp.Action}");

            return (true, "OK");
        }

        // ── Main entry: evaluate a symbol ───────────────────────────────────

        public async Task EvaluateAsync(string symbol, Dictionary<string, object> marketData)
        {
            // Reset session state
            _sessionId = null;
            _contextHistory.Clear();

            string requestId = $"{symbol}-{DateTimeOffset.UtcNow.ToUnixTimeSeconds()}";

            // Safety: duplicate check
            if (IsDuplicateRequest(requestId))
            {
                Console.WriteLine($"[SAFETY] Duplicate requestId blocked: {requestId}");
                return;
            }

            int fetchLoopCount = 0;

            while (fetchLoopCount <= BotConfig.MaxFetchLoopPerSession)
            {
                var request = BuildRequest(requestId, symbol, marketData);

                var response = await SendEvaluateAsync(request);
                if (response == null)
                {
                    Console.WriteLine("[ERROR] No response from server");
                    return;
                }

                Console.WriteLine($"[{fetchLoopCount}] Action={response.Action} " +
                    $"Target={response.TargetSymbol} DataType={response.RequiredDataType}");

                switch (response.Action)
                {
                    case "FETCH_DATA":
                        fetchLoopCount++;
                        if (fetchLoopCount > BotConfig.MaxFetchLoopPerSession)
                        {
                            Console.WriteLine($"[SAFETY] Max fetch loop reached ({BotConfig.MaxFetchLoopPerSession})");
                            return;
                        }
                        // Server wants more data — handle it
                        var fetchResult = await HandleFetchData(response);
                        if (fetchResult == null)
                        {
                            Console.WriteLine("[ERROR] FETCH_DATA handler returned null");
                            return;
                        }
                        // Feed fetched data back as next request's marketData
                        marketData = fetchResult;
                        // Append step to context history
                        _contextHistory.Add(new ContextStep
                        {
                            StepNo = _contextHistory.Count + 1,
                            Symbol = response.TargetSymbol,
                            DataType = response.RequiredDataType,
                            Payload = marketData,
                            Reason = $"Fetched {response.RequiredDataType} for {response.TargetSymbol}",
                        });
                        _sessionId = response.SessionId;
                        requestId = $"{symbol}-f{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}";
                        continue; // loop back

                    case "BUY":
                    case "SELL":
                        // ── Final order decision ──────────────────────────
                        var (ok, reason) = ValidateSafety(response);
                        if (!ok)
                        {
                            Console.WriteLine($"[SAFETY] Blocked: {reason}");
                            return;
                        }

                        if (response.AllowOrder)
                        {
                            var orderResult = await SendLimitOrderAsync(response);
                            await PostOrderResultAsync(orderResult);
                            Console.WriteLine($"[ORDER] {response.Action} qty={response.Qty} " +
                                $"result={orderResult.Status} filled={orderResult.FilledQty}");
                        }
                        else
                        {
                            Console.WriteLine($"[INFO] {response.Action} signal but allowOrder=false. Reason: {response.Reason}");
                        }
                        _sessionId = null; // session complete
                        return;

                    case "WAIT":
                    default:
                        Console.WriteLine($"[WAIT] {response.Reason}");
                        _sessionId = null;
                        return;
                }
            }
        }

        // ── Build AgenticSignalRequest ──────────────────────────────────────

        private AgenticRequest BuildRequest(
            string requestId, string symbol, Dictionary<string, object> ohlcv)
        {
            return new AgenticRequest
            {
                RequestId = requestId,
                Symbol = symbol,
                SessionId = _sessionId,
                Mode = BotConfig.Mode,
                MarketData = new MarketDataPayload
                {
                    Symbol = symbol,
                    DataType = "OHLCV",
                    Payload = ohlcv,
                },
                ContextHistory = _contextHistory.Count > 0
                    ? new List<ContextStep>(_contextHistory)
                    : null,
            };
        }

        // ── HTTP POST to /api/signal/evaluate-agent ─────────────────────────

        public async Task<AgenticResponse> SendEvaluateAsync(AgenticRequest request)
        {
            try
            {
                var json = JsonSerializer.Serialize(request, _jsonOpts);
                var content = new StringContent(json, Encoding.UTF8, "application/json");

                var httpResp = await _http.PostAsync("/api/signal/evaluate-agent", content);

                if (!httpResp.IsSuccessStatusCode)
                {
                    var body = await httpResp.Content.ReadAsStringAsync();
                    Console.WriteLine($"[HTTP {httpResp.StatusCode}] {body}");
                    return null;
                }

                var respJson = await httpResp.Content.ReadAsStringAsync();
                return JsonSerializer.Deserialize<AgenticResponse>(respJson, _jsonOpts);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[EXCEPTION] SendEvaluateAsync: {ex.Message}");
                return null;
            }
        }

        // ── Handle FETCH_DATA: fetch the requested data from Matriks ────────

        /// <summary>
        /// Called when the server returns action=FETCH_DATA.
        /// Override this to fetch data from Matriks IQs API.
        /// Returns the payload to feed back to the server.
        /// </summary>
        public virtual async Task<Dictionary<string, object>> HandleFetchData(AgenticResponse response)
        {
            Console.WriteLine($"[FETCH_DATA] Need {response.RequiredDataType} for {response.TargetSymbol}");

            switch (response.RequiredDataType)
            {
                case "DEPTH":
                    return await FetchDepthAsync(response.TargetSymbol);
                case "OHLCV":
                    return await FetchOhlcvAsync(response.TargetSymbol);
                case "AKD":
                    return await FetchAkdAsync(response.TargetSymbol);
                case "TECHNICAL":
                    return await FetchTechnicalAsync(response.TargetSymbol);
                case "NEWS":
                    return await FetchNewsAsync(response.TargetSymbol);
                case "FUND":
                    return await FetchFundAsync(response.TargetSymbol);
                case "BROKER_FLOW":
                    return await FetchBrokerFlowAsync(response.TargetSymbol);
                default:
                    Console.WriteLine($"[WARN] Unknown dataType: {response.RequiredDataType}");
                    return null;
            }
        }

        // ── Stub data fetchers (implement with Matriks IQ API) ──────────────

        private async Task<Dictionary<string, object>> FetchDepthAsync(string symbol)
        {
            // TODO: Call Matriks IQ API to get depth/orderbook for symbol.
            // Example return:
            return await Task.FromResult(new Dictionary<string, object>
            {
                ["symbol"]      = symbol,
                ["bidPrice"]    = 299.5,
                ["askPrice"]    = 300.5,
                ["bidVolume"]   = 5000.0,
                ["askVolume"]   = 3000.0,
                ["bidDepth"]    = new[] {
                    new Dictionary<string, object> { ["price"] = 299.0, ["volume"] = 1000 },
                    new Dictionary<string, object> { ["price"] = 298.5, ["volume"] = 2000 },
                },
                ["askDepth"]    = new[] {
                    new Dictionary<string, object> { ["price"] = 301.0, ["volume"] = 1500 },
                    new Dictionary<string, object> { ["price"] = 301.5, ["volume"] = 800 },
                },
            });
        }

        private async Task<Dictionary<string, object>> FetchOhlcvAsync(string symbol)
        {
            // TODO: Call Matriks IQ OHLCV endpoint
            return await Task.FromResult(new Dictionary<string, object>
            {
                ["symbol"]      = symbol,
                ["timeframe"]   = "1h",
                ["open"]        = 298.0,
                ["high"]        = 305.0,
                ["low"]         = 296.0,
                ["close"]       = 300.0,
                ["volume"]      = 1_000_000.0,
            });
        }

        private async Task<Dictionary<string, object>> FetchAkdAsync(string symbol)
        {
            // TODO: Fetch Açığa Kısa Dönüşüm data
            return await Task.FromResult(new Dictionary<string, object>
            {
                ["symbol"]      = symbol,
                ["shortRatio"]  = 0.15,
                ["shortVolume"] = 10000.0,
            });
        }

        private async Task<Dictionary<string, object>> FetchTechnicalAsync(string symbol)
        {
            // TODO: Fetch RSI, MACD, EMA etc.
            return await Task.FromResult(new Dictionary<string, object>
            {
                ["symbol"]  = symbol,
                ["rsi"]     = 48.0,
                ["macd"]    = 0.15,
                ["macdSignal"] = 0.1,
                ["ema20"]   = 299.0,
                ["ema50"]   = 290.0,
            });
        }

        private async Task<Dictionary<string, object>> FetchNewsAsync(string symbol)
        {
            // TODO: Fetch KAP / news
            return await Task.FromResult(new Dictionary<string, object>
            {
                ["symbol"]  = symbol,
                ["news"]    = new object[0],
            });
        }

        private async Task<Dictionary<string, object>> FetchFundAsync(string symbol)
        {
            // TODO: Fetch fund distribution
            return await Task.FromResult(new Dictionary<string, object>
            {
                ["symbol"]  = symbol,
                ["funds"]   = new object[0],
            });
        }

        private async Task<Dictionary<string, object>> FetchBrokerFlowAsync(string symbol)
        {
            // TODO: Fetch broker transaction flow
            return await Task.FromResult(new Dictionary<string, object>
            {
                ["symbol"]      = symbol,
                ["brokerFlows"] = new object[0],
            });
        }

        // ── Send LIMIT order (stub/TODO) ────────────────────────────────────

        /// <summary>
        /// Place a LIMIT order via Matriks IQ.
        /// Only called when response.AllowOrder == true.
        /// </summary>
        public virtual async Task<OrderResult> SendLimitOrderAsync(AgenticResponse response)
        {
            // TODO: Call Matriks IQ SendOrder/PlaceOrder with:
            //   - symbol
            //   - side = response.Action (BUY/SELL)
            //   - qty = response.Qty
            //   - price = response.EntryRange?.Min (or midpoint)
            //   - stopLoss = response.StopLoss
            //   - targetPrice = response.TargetPrice
            //   - orderType = LIMIT

            if (!BotConfig.EnableDemoOrders)
            {
                return new OrderResult
                {
                    RequestId = response.RequestId,
                    Status = "BLOCKED",
                    Error = "Demo orders disabled in config",
                };
            }

            // Stub: simulate a filled order
            return await Task.FromResult(new OrderResult
            {
                RequestId  = response.RequestId,
                OrderId    = $"demo-{DateTimeOffset.UtcNow.ToUnixTimeSeconds()}",
                Status     = "DEMO_FILLED",
                FilledQty  = response.Qty,
                AvgPrice   = response.EntryRange != null
                    ? (response.EntryRange.Min + response.EntryRange.Max) / 2.0
                    : (response.Price ?? 0),
                Error      = null,
            });
        }

        // ── Post order result ───────────────────────────────────────────────

        /// <summary>
        /// Send order execution result back to the server or local log.
        /// </summary>
        public virtual async Task PostOrderResultAsync(OrderResult result)
        {
            // TODO: POST /api/orders/result with OrderResult body
            // or log to file for audit trail.

            Console.WriteLine($"[ORDER RESULT] id={result.OrderId} " +
                $"status={result.Status} filled={result.FilledQty} avg={result.AvgPrice}");

            if (result.Error != null)
                Console.WriteLine($"[ORDER ERROR] {result.Error}");

            await Task.CompletedTask;
        }

        // ── Dispose ─────────────────────────────────────────────────────────

        public void Dispose()
        {
            _http?.Dispose();
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Example usage (entry point for Matriks IQ integration)
    // ─────────────────────────────────────────────────────────────────────────

    public class Program
    {
        public static async Task Main(string[] args)
        {
            var bot = new TradeAiAgenticBot();

            try
            {
                // Example: evaluate ANELE with live OHLCV data
                var marketData = new Dictionary<string, object>
                {
                    ["symbol"]      = "ANELE",
                    ["timeframe"]   = "1h",
                    ["lastPrice"]   = 300.0,
                    ["open"]        = 298.0,
                    ["high"]        = 305.0,
                    ["low"]         = 296.0,
                    ["volume"]      = 1_000_000.0,
                    ["rsi"]         = 48.0,
                    ["rsi14"]       = 48.0,
                    ["ema20"]       = 299.0,
                    ["ema50"]       = 290.0,
                    ["macd"]        = 0.15,
                    ["macdSignal"]  = 0.1,
                    ["botPositionQty"]  = 0,
                    ["totalAccountQty"] = 0,
                    ["lockedLongTermQty"] = 0,
                    ["dailyTradeCount"] = 0,
                };

                await bot.EvaluateAsync("ANELE", marketData);
            }
            finally
            {
                bot.Dispose();
            }
        }
    }
}
