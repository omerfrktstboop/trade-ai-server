using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Matriks.Lean.Algotrader.AlgoBase;
using Newtonsoft.Json;

namespace Matriks.Lean.Algotrader
{
    /// <summary>
    /// Minimal local HTTP receiver test for Matriks IQ.
    ///
    /// This is intentionally separate from TradeAiAgenticBot and never sends
    /// orders. It only proves that an external process can call into a running
    /// Matriks algo over localhost.
    ///
    /// Test examples:
    ///   curl http://127.0.0.1:8787/ping
    ///   curl -H "Authorization: Bearer test-token" http://127.0.0.1:8787/status
    ///   curl -X POST -H "Authorization: Bearer test-token" -H "Content-Type: application/json" ^
    ///        -d "{\"symbol\":\"THYAO\",\"action\":\"PING\"}" http://127.0.0.1:8787/signal-test
    /// </summary>
    public class TradeAiHttpApiTest : MatriksAlgo
    {
        [Parameter(8787)]
        public int Port;

        [Parameter("test-token")]
        public string ApiToken;

        [Parameter(true)]
        public bool RequireToken;

        private TcpListener _listener;
        private CancellationTokenSource _cts;
        private Task _serverTask;
        private DateTime _startedAt;
        private int _requestCount;
        private string _lastRequestPath = "";
        private string _lastRequestBody = "";

        public override void OnInit()
        {
            _startedAt = DateTime.Now;
            _cts = new CancellationTokenSource();

            try
            {
                _listener = new TcpListener(IPAddress.Loopback, Port);
                _listener.Start();
                _serverTask = Task.Run(() => AcceptLoopAsync(_cts.Token));
                SafeDebug("HTTP API test listener started url=http://127.0.0.1:" + Port + "/ requireToken=" + RequireToken);
            }
            catch (Exception ex)
            {
                SafeDebug("HTTP API test listener failed: " + ex.Message);
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
                SafeDebug("HTTP API test listener stopped.");
            }
            catch (Exception ex)
            {
                SafeDebug("HTTP API test stop error: " + ex.Message);
            }
        }

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

                _requestCount++;
                _lastRequestPath = request.Path;
                _lastRequestBody = request.Body ?? "";

                SafeDebug("HTTP request method=" + request.Method
                    + " path=" + request.Path
                    + " bodyLength=" + _lastRequestBody.Length);

                if (request.Method == "GET" && request.Path == "/ping")
                {
                    await WriteJsonAsync(stream, 200, new
                    {
                        ok = true,
                        message = "pong",
                        server = "TradeAiHttpApiTest",
                        now = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz")
                    });
                    return;
                }

                if (RequireToken && !IsAuthorized(request))
                {
                    await WriteJsonAsync(stream, 401, new { ok = false, error = "unauthorized" });
                    return;
                }

                if (request.Method == "GET" && request.Path == "/status")
                {
                    await WriteJsonAsync(stream, 200, new
                    {
                        ok = true,
                        startedAt = _startedAt.ToString("yyyy-MM-ddTHH:mm:sszzz"),
                        requestCount = _requestCount,
                        lastRequestPath = _lastRequestPath,
                        lastRequestBody = _lastRequestBody
                    });
                    return;
                }

                if (request.Method == "POST" && request.Path == "/signal-test")
                {
                    await WriteJsonAsync(stream, 200, new
                    {
                        ok = true,
                        received = true,
                        body = request.Body,
                        note = "No order was sent; this endpoint only receives and echoes test data."
                    });
                    return;
                }

                await WriteJsonAsync(stream, 404, new { ok = false, error = "not found", path = request.Path });
            }
        }

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
            int queryIndex = path.IndexOf('?');
            if (queryIndex >= 0)
            {
                path = path.Substring(0, queryIndex);
            }

            return new HttpRequest
            {
                Method = requestLine[0].ToUpperInvariant(),
                Path = path,
                Headers = headers,
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
            return "OK";
        }

        private void SafeDebug(string message)
        {
            try
            {
                Debug("[TradeAI HTTP Test] " + message);
            }
            catch
            {
            }
        }

        private class HttpRequest
        {
            public string Method { get; set; }
            public string Path { get; set; }
            public Dictionary<string, string> Headers { get; set; }
            public string Body { get; set; }
        }
    }
}
