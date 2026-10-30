# Local database copy (IT computer) — changes only, every 30 minutes

`mirror.py` keeps a copy of the bot's database in a local PostgreSQL (`mgmg_mirror`) on this computer. Whenever
the computer is awake it runs at :00 and :30; after sleep or a restart it runs as soon as it's up.

**Changes only.** Render's plan has no real replication, and the tables have no "changed at" column, so:
1. one quick query asks Postgres which tables had inserts / updates / deletes since the last run — the rest
   are skipped without being read (every table is still fully checked once a day);
2. a changed table is compared by checksums — whole table, then 64 slices by primary key; only the slices
   that differ are copied again (`COPY`), so new, changed and deleted rows all arrive;
3. the audit log (`agent_actions`, insert-only) just gets its new rows by id.

A quiet run costs production one 0.3-second query; production is only read (READ ONLY session).

**What is in the copy:** the bot's own tables — the same list IT may open on `/db` for good
(`db_viewer.TECHNICAL_TABLES`, the Director's order of 07.10.2026 and the Boss's approval of 17.10). SAP, 1C,
Billz, Verifix and what is built from their figures keep their **structure only** (no rows). The full copy of
everything is the encrypted backup to the owner's key (`scripts/backup/`). Keep this computer's disk encrypted
and locked: the copy holds employees' names, reports and chats.

## Install (once)

```bash
bash scripts/db-mirror/install.sh
```
It installs PostgreSQL 16 (asks the sudo password once), makes the `mgmg_mirror` database, the timer
`mgmg-db-mirror.timer`, and runs the first copy. Production's address comes from `.env` (`DATABASE_URL`);
another local database: `MIRROR_DATABASE_URL`.

## Use

```bash
psql mgmg_mirror                                   # look at the copy
.venv/bin/python scripts/db-mirror/mirror.py        # sync now
.venv/bin/python scripts/db-mirror/mirror.py --check   # what would change, nothing written
systemctl --user list-timers mgmg-db-mirror.timer   # next run
journalctl --user -u mgmg-db-mirror -n 20           # last runs
psql mgmg_mirror -c "SELECT * FROM _mirror_runs ORDER BY id DESC LIMIT 5"
```
Stop: `systemctl --user disable --now mgmg-db-mirror.timer`. Remove the copy: `dropdb mgmg_mirror`.
