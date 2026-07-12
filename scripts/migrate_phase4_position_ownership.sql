-- Apply with SCANNER_ALLOW_ORDERS=false.
-- Existing invalid rows must be corrected explicitly; the migration refuses
-- to hide them by silently clamping quantities.
BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM locked_positions WHERE qty < 0 OR qty = 'NaN'::float8) THEN
    RAISE EXCEPTION 'locked_positions contains invalid quantity values';
  END IF;
END $$;

ALTER TABLE locked_positions
  DROP CONSTRAINT IF EXISTS ck_locked_positions_qty_nonnegative;
ALTER TABLE locked_positions
  ADD CONSTRAINT ck_locked_positions_qty_nonnegative
  CHECK (qty >= 0 AND qty <> 'Infinity'::float8 AND qty <> '-Infinity'::float8 AND qty <> 'NaN'::float8);

COMMIT;
