# Phase 8 test and CI validation report

Date: 2026-07-12 (Europe/Istanbul)

## Measured baseline

- Initial `pytest -q`: timed out after 123.9 seconds on Windows while also
  emitting an httpx/Starlette access violation. It produced no valid pass/fail
  total and was not counted as a successful run.
- Completed pre-fix run: 573 passed, 4 failed in 368 seconds.
- Initial `ruff check .`: 89 errors.

## Final local result

- `ruff check .`: all checks passed.
- `pytest -q`: 590 passed, 1 skipped in 348.61 seconds, exit code 0.
- The skipped test is the PostgreSQL migration integration test because this
  workstation has no `TEST_POSTGRES_URL`. It is mandatory in the PostgreSQL CI
  job.
- Windows still emits an access-violation diagnostic from httpx/Starlette
  after pytest's successful summary. This is an environment diagnostic, not a
  C# compile result and not hidden from the report.

## CI and database isolation

- GitHub Actions runs Ruff and the complete test suite on Python 3.12 with an
  isolated SQLite test database.
- A PostgreSQL 16 job runs upgrade, duplicate merge, unique constraint,
  concurrent upsert, downgrade, and re-upgrade assertions.
- Test bootstrap rejects inherited non-test/production database URLs before
  importing application settings. Only test-named SQLite databases or
  loopback/test-named PostgreSQL databases are accepted.

## Automated coverage

- Order ledger: atomic duplicate reservation, concurrent same-request upsert,
  fingerprint mismatch, `SEND_UNKNOWN` persistence/no resend, pending
  symbol+side block, fractional and non-finite values, rounded-price notional.
- Callback lifecycle: DB outage returns 503, duplicate callbacks update one
  row, monotonic partial/final progression, final-state regression rejection.
- Config: atomic batch rollback, kill switch and REAL_LIVE gates, scoped auth
  tokens, stale config rejection.
- Positions: bot/account ownership split, locked quantity limits, incomplete
  snapshot behavior, cumulative fill monotonicity and stale-position rejection.
- Market data: stale quote/depth, crossed/zero-size book, decision age, price
  drift, closed session, NaN, fractional quantity, stale position/config.
- KAP/AKD/AI: dated and undated KAP handling, active risk beyond latest ten,
  AKD unavailable, cache fingerprint/invalidation, SSRF and untrusted-content
  prompt protections.
- Migration: SQLite upgrade/downgrade locally; PostgreSQL upgrade, duplicate
  cleanup, uniqueness, rollback and concurrent upsert in CI.

## Source-contract tests are not compile tests

Tests that inspect `matriks/TradeAiGateway.cs` only verify source contracts.
They do not prove that the target Matriks IQ version compiles or runs the code.
No C# compile success is claimed by this report.

## Open P0 validation gaps

- Gateway durable callback outbox restart is not verified. Current gateway
  health maps `callbackOutboxBacklog` to the in-memory callback queue; no durable
  disk-backed outbox implementation was found.
- Gateway restart rehydration, daily counter restore against the real gateway,
  socket-loss exchange deduplication and backend-down fill delivery still need
  target-environment fault injection.
- Conflicting final callback currently rejects state regression, but an
  operational audit alarm must be verified in deployment.
- Real Matriks IQ compile and all DEMO_LIVE acceptance sessions remain pending.

## Required Matriks IQ / DEMO_LIVE run

With REAL_LIVE flags disabled, perform and record: target IQ compile, gateway
start, `/health`, `/positions`, `/snapshot`, DEMO BUY, partial fill, cancel,
rejection, backend-down fill, gateway restart, backend restart, account switch,
demo-to-real account replacement and kill-switch drill. Then run the network
loss, duplicate request and open-order restart drills over several complete
sessions and reconcile exchange, ledger and callback/outbox records.

## Release decision

DEMO_LIVE automated development may continue with REAL_LIVE disabled. REAL_LIVE
is **NO-GO** until the open P0 items above, PostgreSQL CI job, target Matriks IQ
compile and multi-session DEMO_LIVE fault drills all pass with recorded
evidence.
