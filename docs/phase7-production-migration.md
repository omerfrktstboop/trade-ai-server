# Phase 7 production migration runbook

Run this procedure with `SCANNER_ALLOW_ORDERS=false` and the trading kill
switch active. Never run production schema changes while dispatch is open.

1. Back up PostgreSQL before migration:

   ```powershell
   pg_dump --format=custom --file trade_ai_pre_phase7.dump $env:DATABASE_URL
   ```

2. Verify the backup can be listed with `pg_restore --list` and copy it to a
   second storage location.
3. Detect duplicate ledger IDs before upgrade:

   ```sql
   SELECT request_id, count(*) FROM order_logs GROUP BY request_id HAVING count(*) > 1;
   ```

4. Apply and verify:

   ```powershell
   python -m alembic upgrade head
   python -m alembic current
   ```

5. Verify `/api/health/ready` reports migration `20260712_01`, then reload
   gateway config and reconcile active orders before reopening dispatch.

Staging must execute `upgrade head`, `downgrade base`, and `upgrade head`
against a restored production-like backup. Production rollback is
`python -m alembic downgrade base` only when no Phase 7 writer has started;
otherwise restore the verified backup instead of dropping lifecycle columns.
