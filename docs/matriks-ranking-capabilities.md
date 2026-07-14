# Matriks IQ market-ranking capability

The gateway deliberately does not invent a market-wide Matriks IQ ranking
method. The verified building blocks are:

- `AddSymbolMarketData(string)` / `GetMarketData(string, SymbolUpdateField)`:
  documented per-symbol surface-data subscription and retrieval.
- `SymbolUpdateField.WeekClose`: documented by Matriks' seven-session comparison;
  the gateway calculates `(Last - WeekClose) / WeekClose * 100`.
- `SymbolUpdateField.TotalVol`: the gateway's existing
  `CUMULATIVE_SESSION_TURNOVER_TL` contract field used for local turnover
  ordering. The public surface-data page documents the retrieval mechanism,
  but does not provide a field-by-field semantic catalogue; this is therefore
  not represented as a new native Matriks ranking API.

Official references:

- <https://iqyardim.matriksdata.com/docs/matriksiq-kullanim-kilavuzu/algotrader/yuzeysel-alan-verilerinin-kullanimi/>
- <https://iqyardim.matriksdata.com/docs/non-knowledgebase/ornek-stratejiler/fiyat-7-gun-ustu/>

Neither source documents an AlgoTrader API for BIST-wide weekly movers,
turnover leaders, or relative-volume leaders. In particular, no native
relative-volume ranking or historical baseline call was verified.

`GET /capabilities` now publishes `capabilities.marketRankings`; consumers
must consult it before treating a movers list as usable. Its current state is:

- `nativeMarketWide=false`: no claim of a BIST-wide native rank.
- `weeklyGainers` and `turnoverLeaders`: available only as
  `SUBSCRIBED_UNIVERSE_FALLBACK`, over configured, subscribed equity symbols.
- `relativeVolumeLeaders`: `UNAVAILABLE`; consumers must omit the signal,
  not infer it from turnover.

`GET /movers` returns the same `rankingCapabilities` block and a
`weeklyGainers` list only when `WeekClose` is present. Empty or unavailable
fields never become fabricated zeroes or leaders. This read-only discovery
metadata never changes `buyAllowedSymbols`, places orders, or expands market
data subscriptions.
