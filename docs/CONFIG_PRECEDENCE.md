# Config precedence

There are four overlapping configuration systems in this codebase. This
document says which one wins for a given key, and lists the places that
still read a key through something other than the shared resolver for that
system.

## Panel-over-env keys (added after the original audit)

Per operator request, runtime behavior flags that used to be env-only are
now admin-panel keys where **a saved DB row permanently overrides the .env
value; no row → the live .env value applies** (so existing deployments and
test monkeypatching keep working until the panel writes a row):

| Panel key | .env fallback | Single entry point |
|---|---|---|
| `scannerAllowOrders` | `SCANNER_ALLOW_ORDERS` | `admin_config.get_scanner_allow_orders(session)` |
| `portfolioScanIntervalMinutes` | `PORTFOLIO_SCAN_INTERVAL_MINUTES` | `admin_config.get_portfolio_scan_interval_minutes(session)` |
| `scannerEnabled` | *(none — default `true`)* | `admin_config.is_scanner_runtime_enabled(session)` |

`scannerEnabled` is a runtime *pause* for an already-running scanner loop;
the env `SCANNER_ENABLED` still decides whether the background task starts
at process boot (a deployment definition, needs restart).

.env itself now holds only identity/connection definitions (API keys, DB
URL, gateway URL/token, Telegram) plus process-boot definitions and the
initial defaults for the keys above — see `.env.example`.

## The four layers

1. **`app/core/risk_config.py`** — the `risk_config` singleton (`RiskConfig`,
   a `pydantic-settings` class). Loaded once at import time from `RISK_*`
   env vars / `.env`, with hard-coded field defaults as the final fallback.
   Mostly supplies **default** values that `admin_config.CONFIG_DEFINITIONS`
   reference; `min_indicator_consensus_count` is read from it directly in
   `build_runtime_risk_config`.

2. **`app/services/admin_config.py`** — the DB-backed config store. Each
   key has a `ConfigDefinition` (default value, type and description).
   `SystemConfig` rows are created **lazily**:
   `get_admin_config_value(session, key)` returns the DB row's value if one
   exists, otherwise `CONFIG_DEFINITIONS[key].default` (fail-open to the
   default — a fresh database has *no* `SystemConfig` rows at all). Two
   composite readers sit on top of this:
   - `build_runtime_risk_config(session)` (`admin_config.py:795`) → a full
     `RiskConfig`-shaped object. Priority: **active `TradeProfile` field >
     per-field admin_config DB override > static env/code default.** Symbol
     lists, the trading cutoff time, and the timezone are *not* part of a
     trade profile, so those three stay purely admin_config-driven
     regardless of the active profile.
   - `is_kill_switch_enabled(session)` (`admin_config.py:780-785`) →
     `killSwitchEnabled OR tradingKillSwitchActive OR forceSafeMode`, each
     read via `get_admin_config_value`. This is the **only** correct way to
     ask "is trading halted?" — see "known inconsistencies" below.

3. **`app/services/effective_risk_config.py`** — a *separate* resolver,
   used only for position-sizing numeric limits (`EffectiveRiskConfig`,
   all `Decimal`). `resolve_effective_risk_config(session)` merges three
   layers and takes the **strictest** value on every field, so a looser
   profile or DB override can never relax a global safety boundary:
   - `EnvironmentRiskLimits.from_environment()` — reads `RISK_*` env vars
     **directly via `os.environ`**, independently of the `risk_config`
     singleton (same env-var names, separate read path).
   - `SystemRiskConfig` — reads a fixed subset of `SystemConfig` DB rows via
     the local `_SYSTEM_CONFIG_KEYS` mapping (bypasses
     `get_admin_config_value`, so a missing row falls back to the pydantic
     field default rather than `CONFIG_DEFINITIONS[key].default` — a
     second, independent default source for the same key; they agree today
     because both ultimately trace back to `risk_config.<field>`, but
     nothing enforces that).
   - active `TradeProfile` — `min()` for ceiling fields, `max()` for floor
     fields, `all()` for boolean permission fields.

4. **`app/services/trade_profile.py`** — the `TradeProfile` DB model +
   `get_active_profile(session)`. Deliberately does **not** import
   `admin_config` (admin_config imports trade_profile — importing back
   would cycle), so `activeTradeProfileCode` is read/written directly
   against `SystemConfig` (`trade_profile.py:314,618`) instead of through
   the `CONFIG_DEFINITIONS` machinery. Both layer 2 and layer 3 call
   `get_active_profile` independently. **Leave as-is** — this is a
   documented, deliberate exception, not an oversight.

## Which layer wins, by key

| Key | Resolved by | Notes |
|---|---|---|
| `disableTradingAfter` / `timezone` | admin_config only (`build_runtime_risk_config`) | No `EffectiveRiskConfig` field exists for either — never part of a trade profile or the sizing resolver. |
| `killSwitchEnabled`, `tradingKillSwitchActive`, `forceSafeMode` | admin_config only (`is_kill_switch_enabled`) | OR of all three. Env/profile play no role. |
| `allowedSymbols`, `buyAllowedSymbols`, `sellExitAllowedSymbols`, `declineSymbols`, `lockedLongTermSymbols`, `scanUniverseSymbols` | admin_config only | Empty string is a valid, meaningful value for the allow/deny lists (see `EMPTY_ALLOWED_CONFIG_KEYS`) — don't treat empty as "unset". |
| `tradingMode` (server-side evaluation override) vs `botMode` (value reported to the external gateway) | admin_config, two distinct keys | `get_trading_mode_override` returns `None` (not a default!) when no DB row exists — callers must handle that explicitly. `botMode` is separately downgraded per active profile by `gateway_config._effective_mode`. |
| Position-sizing limits (`sizingRiskPerTradePct`, `sizingMaxPositionValuePerSymbol`, `sizingMaxOrderValueTl`, stop-distance/slippage bounds, `sizingMinimumBuyConfidence`/`SellConfidence`, `sizingDailyOrderLimit`, ...) | **`resolve_effective_risk_config`** — strictest of env / admin_config / active profile | This is the single entry point T4 asks new sizing code to use. |
| `min_confidence_for_buy`/`sell`, `max_natr_for_buy`, depth/spread gates, `allow_real_live`, `allow_demo_live`, and everything else the active profile defines directly | `build_runtime_risk_config` — **profile value only**, no env/admin_config merge | Not in `EffectiveRiskConfig`'s field set, so there's no strictest-wins merge — the active profile is authoritative. |

## Known duplicate-read sites

### Confidence threshold: two resolvers, and they can genuinely disagree on the same BUY

`TradeProfile.min_confidence_for_buy`/`min_confidence_for_sell` feeds two
independent gates inside a single `RiskEngine.evaluate()` call
(`app/services/risk_engine.py`), both live in the BUY path:

- **Sizing gate** (`risk_engine.py:300-330`, calling
  `position_sizing.py:138`) uses `EffectiveRiskConfig.minimum_buy_confidence`
  — `max(env RISK_MINIMUM_BUY_CONFIDENCE, admin sizingMinimumBuyConfidence,
  profile.min_confidence_for_buy)`. Runs first; blocks the whole response
  (forces `WAIT`) on failure.
- **Step-7 confidence gate** (`risk_engine.py:358-366`) uses
  `RiskConfig.get_min_confidence(action)` — **profile value only**, via
  `build_runtime_risk_config`, plus a `+15` penalty applied **only here**
  when `market_regime == "HIGH_VOLATILITY"` (`risk_engine.py:196-200`).
  Runs after sizing already computed a qty; on failure it sets
  `allowOrder=False` but does **not** clear the already-computed
  `action`/`qty` — so the response can come back as `action=BUY, qty>0,
  allowOrder=False`, which is confusing to a caller reading the fields at
  face value without also checking `allowOrder`.

Two concrete ways they diverge:
1. **Regime-penalty asymmetry** — the HIGH_VOLATILITY `+15` never applies
   to the sizing gate's threshold, only step 7's.
2. **Admin-only override asymmetry** — raising `sizingMinimumBuyConfidence`
   in the admin panel raises the sizing threshold (so sizing blocks first,
   no inconsistent response) but has **zero effect** on step 7's threshold
   or its reason text — an operator can turn this knob and see no change
   in *why* a signal was rejected.
3. **SELL confidence is a dead knob** — `EffectiveRiskConfig
   .minimum_sell_confidence` / admin key `sizingMinimumSellConfidence` are
   computed and displayed in the admin panel, but there is no
   `PositionSizingService.calculate_sell_size` anywhere — nothing ever
   compares a SELL against this value. Only `RiskConfig
   .min_confidence_for_sell` (profile-only, step 7) gates SELL confidence
   in practice. The admin-panel knob does nothing.
4. **Replay bypasses the sizing gate entirely** — `replay.py:119`
   constructs `RiskEngine(config)` with no `effective_config`, so a
   replayed decision is judged purely against step-7/profile-only
   thresholds. A replayed BUY can be allowed when the original live
   evaluation (which did have `effective_config` set) would have been
   blocked by sizing, or vice versa.

**Left as-is** — unifying these two gates (e.g. applying the regime penalty
to both, or having step 7 read `effective_config.minimum_buy_confidence`
when available) changes which signals get how far through the pipeline and
what reason text they get, which is a real behavior change outside this
cleanup's scope. Flagging here in detail so it isn't mistaken for an
oversight, and so whoever picks it up next doesn't have to re-derive it.

### `accountReservationHandling`: raw SystemConfig query — fixed

`app/services/account_context.py`'s `get_account_reservation_handling` used
to run its own `select(SystemConfig).where(key == "accountReservationHandling")`
with a hardcoded `"UNKNOWN"` fallback, instead of calling
`get_admin_config_value`. The hardcoded fallback was byte-for-byte identical
to `CONFIG_DEFINITIONS["accountReservationHandling"].default`, and no
import cycle exists between `account_context.py` and `admin_config.py`, so
this was a pure behavior-preserving redirect to the single entry point.

### Kill switch: display-only reads, not yet consolidated

`app/routers/admin.py` (lines `1537, 1581, 1615, 2007, 2037, 2072, 2127`)
and `app/routers/gateway_config.py:191-193` read
`killSwitchEnabled`/`tradingKillSwitchActive`/`forceSafeMode` directly from
a pre-fetched `configs`/`values` dict (via `list_admin_configs`, not a raw
query) rather than calling `is_kill_switch_enabled`, to render admin-panel
status strips and the gateway config payload. These are **display paths,
not order-gating decisions** — the actual gate is `is_kill_switch_enabled`
inside `evaluator.with_runtime_controls`. `gateway_config.py`'s split into
two separate fields (`tradingKillSwitchActive`, `forceSafeMode`) looks
intentional — the Matriks gateway may need to distinguish the two states
operationally. Not touched in this pass: the admin dashboard can show
"Kill Switch: OFF" while trading is actually halted by
`tradingKillSwitchActive`/`forceSafeMode` alone, which is misleading to an
operator (low risk since it doesn't affect gating, but worth fixing for
DEMO_LIVE operator trust). `admin.py` is being split by domain in a
follow-up task (T5) — a more natural time to route these through
`is_kill_switch_enabled` for a consistent display.

### `SystemRiskConfig` bypasses `get_admin_config_value`

`effective_risk_config.py`'s `resolve_effective_risk_config` reads the
`SystemConfig` rows it needs directly rather than through
`get_admin_config_value`/`list_admin_configs`, so a missing row falls back
to the `SystemRiskConfig` pydantic field default rather than
`CONFIG_DEFINITIONS[key].default` — two independent default sources for the
same key that happen to agree today. Not consolidated:
`resolve_effective_risk_config` does its own min/max-across-layers merge,
so routing it through the single-key helper isn't a drop-in change. Worth
revisiting if the two default sets ever drift.

### Dead/placeholder code, noted but not touched

- `app/services/strategy.py:89` reads `config.max_position_value_per_symbol`
  off a raw `RiskConfig` inside `generate_dummy_decision`/`_suggest_qty`,
  which has no call sites anywhere in `app/` — dead code.
- `app/services/evaluator.py`'s static fallback (`_static_effective_config`
  /`_static_risk_engine`, used only when the DB-backed resolvers throw)
  independently re-derives defaults via `EnvironmentRiskLimits
  .from_environment()`, a blank `SystemRiskConfig()`, and
  `get_static_default_profile()` instead of calling
  `resolve_effective_risk_config`. Intentional — it's the fallback used
  precisely when the DB-backed path is unavailable — but it means this
  degraded mode silently ignores any admin-config DB overrides. Documenting
  this as an explicit degraded-mode behavior, not something to "fix."
