#!/bin/bash
# Creates the separate database n8n uses for its own state.
# Runs once, on first postgres container init.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE ${N8N_DB_NAME:-n8n}'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${N8N_DB_NAME:-n8n}')\gexec
EOSQL
