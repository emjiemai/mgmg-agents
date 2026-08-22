-- One-time setup: a least-privilege Postgres role for the SAP gateway push
-- script. Run this ONCE against the real database, using an admin
-- connection (e.g. Render's own DATABASE_URL from the dashboard) --
-- never hand that admin connection string to anything outside our control.
--
-- This role can INSERT/UPDATE/SELECT on ar_aging_snapshots only -- nothing
-- else in the database. That's deliberate: the push script runs on a
-- machine we don't directly control, so it gets the smallest possible
-- blast radius if that machine or this credential is ever compromised.
--
-- Before running: replace REPLACE_WITH_A_STRONG_PASSWORD below with a real
-- generated password. Never commit the real password into this file or
-- anywhere in git -- generate it, paste it here only for this one run, and
-- put the actual value only in the connection string handed to the push
-- script's environment (never back into this repo).
--
-- Run with:
--   psql "$DATABASE_URL" -f scripts/sap-gateway-push/create_role.sql

CREATE ROLE sap_pusher WITH LOGIN PASSWORD 'REPLACE_WITH_A_STRONG_PASSWORD';

GRANT CONNECT ON DATABASE mgmg TO sap_pusher;
GRANT USAGE ON SCHEMA public TO sap_pusher;
GRANT SELECT, INSERT, UPDATE ON ar_aging_snapshots TO sap_pusher;
-- ar_aging_snapshots.id is BIGSERIAL -- the role needs USAGE on its
-- sequence to INSERT at all, not just the table itself.
GRANT USAGE, SELECT ON ar_aging_snapshots_id_seq TO sap_pusher;

-- Verify: this should show exactly one table (ar_aging_snapshots) and
-- nothing else.
-- SELECT table_name FROM information_schema.role_table_grants
-- WHERE grantee = 'sap_pusher';
