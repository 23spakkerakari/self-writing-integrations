-- carto read-only login for PostgreSQL (spec 8.1.4, Appendix C).
-- Run as a superuser on the read replica (or the primary if no replica exists).
-- Replace: carto_reader password (use your secret manager), schema and table names.

CREATE ROLE carto_reader LOGIN PASSWORD 'REPLACE_WITH_GENERATED_PASSWORD' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE carto_reader SET default_transaction_read_only = on;
ALTER ROLE carto_reader SET statement_timeout = '30s';

GRANT CONNECT ON DATABASE wms TO carto_reader;            -- REPLACE database
GRANT USAGE ON SCHEMA public TO carto_reader;             -- REPLACE schema

-- Prefer a view that exposes only the columns carto needs (no free text, no PII columns).
CREATE VIEW carto_purchase_orders AS
  SELECT id, po_num, order_ref, status, warehouse_code, created_by, created_at, updated_at
  FROM purchase_orders;                                   -- REPLACE table and columns
GRANT SELECT ON carto_purchase_orders TO carto_reader;

-- Verify (carto's Test button runs the same checks and refuses write-capable logins):
--   SELECT has_table_privilege('carto_reader', 'purchase_orders', 'INSERT');  -- must be false
--   SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = 'carto_reader';
