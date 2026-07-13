# Matriks Market Data Contract v2

`schemaVersion`: `technical-features-v2`

## Source map

| Field | Matriks source | Unit | Scope | Timestamp/reliability |
|---|---|---|---|---|
| `lastPrice` | `GetMarketData(symbol, Last)` | TRY/share | last trade | `lastTradeUtc` from `BarDataEventArgs.LastTickTime`; otherwise not fresh |
| `barOpen/High/Low/Close` | `BarDataEventArgs.BarData.Open/High/Low/Close` | TRY/share | actual bar | `barEventUtc` only from `BarData.Dtime` |
| `barVolume` | `BarDataEventArgs.BarData.Volume` | lots | actual bar | scoped by `actualBarPeriod` |
| `sessionTurnoverTl` | `GetMarketData(symbol, TotalVol)` | TRY | cumulative session | equity only; not bar volume |
| `volumeIndicatorValue` | verified `VolumeIndicator.CurrentValue` factory | lots | indicator period | optional |
| `volumeTlIndicatorValue` | verified `VolumeTLIndicator.CurrentValue` factory | TRY | indicator period | optional |
| `actualBarPeriod` | `BarDataEventArgs.PeriodInfo` | period | bar series | never inferred from requested timeframe |
| `barDataIndex` | `BarDataEventArgs.BarDataIndex` | index | bar series | dedup fallback when `Dtime` is default |
| `accountAvgCost` | `AlgoTraderPosition.AvgCost` | TRY/share | whole account position | never silently relabeled as bot cost |
| `botAvgCost` | backend order-fill ledger | TRY/share | bot-owned position | account-cost fallback only on exact full ownership |
| `accountQtyAvailable` | `AlgoTraderPosition.QtyAvailable` | lots | account position | hard SELL upper bound with bot ownership |
| `accountQtyNet` | `AlgoTraderPosition.QtyNet` | lots | account position | informational ownership comparison |
| `atr/natr` | verified native ATR or OHLC True Range SMA | TRY / percent | `atrTimeframe` | null when neither source is available |
| `closeChangeVolatilityProxy` | close history | percent | indicator period | explicitly not ATR/NATR |

Depth uses `GetMarketDepth`. In this gateway build no verified field-specific
depth event timestamp exists. Therefore `depthReadUtc` is a read time,
`depthEventTimestampAvailable=false`, `depthAgeSeconds=null`, and order-time
freshness stays fail-closed. XU100 skips depth and `TotalVol` with
`depthSkipReason=INDEX_SYMBOL_NOT_APPLICABLE`.

## Bar identity and reset

Primary identity is `symbol + actualBarPeriod + BarData.Dtime`. If `Dtime` is
default, `symbol + actualBarPeriod + BarDataIndex` is used and
`barTimestampSource=BAR_INDEX_FALLBACK`. Repeated ticks update the final bar;
an advancing index appends; a regressing index resets that series cache.

## Regimes and broker flow

- `macroMarketRegime`: XU100 price/EMA20/EMA50 market-wide filter.
- `symbolTrendRegime`: evaluated symbol trend/volatility classification.
- `smartMoneyAvailable=false`: institution names could not be classified;
  ratios remain null and `smartMoneyFlow=UNKNOWN`.
- `retrievedAt` is retrieval time. `asOf` and `dataAgeSeconds` remain null when
  Matriks does not provide market source time.

## Compatibility example

Legacy:

```json
{"timeframe":"1h","volume":1917814254.1,"marketRegime":"TRENDING"}
```

v2:

```json
{
  "requestedTimeframe":"MIN60",
  "actualBarPeriod":"MIN5",
  "timeframeMismatch":true,
  "barVolume":125000,
  "sessionTurnoverTl":1917814254.1,
  "macroMarketRegime":"DOWNTREND",
  "symbolTrendRegime":"TRENDING",
  "schemaVersion":"technical-features-v2"
}
```

## Validation status

Per delivery instruction, focused tests, full suite, Ruff and target Matriks IQ
compile were not run for this commit. Runtime comparison for KCHOL, THYAO,
AKBNK and XU100 therefore remains pending.

`DEMO_LIVE NO-GO` until target compile and runtime diagnostics pass.

`REAL_LIVE NO-GO`.
