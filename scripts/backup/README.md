# Database backups — a copy outside Render (IT-5)

The owner's IT instruction (08.10.2026) asks for the **3-2-1 rule**: three
copies, on two kinds of storage, one away from the rest — and a restore test
that proves a copy really comes back. For the bot's database (`mgmg-db`):

| Copy | Where | Kept |
| ---- | ---- | ---- |
| 1 | the live database on Render | — |
| 2 | Render's own point-in-time recovery (Render → mgmg-db → Recovery) | 3 days (Hobby workspace) or 7 (Pro) |
| 3 | **an office computer**: `pull-backup.ps1` fetches an encrypted copy every day, and copies it to a second disk | 30 days, then the first copy of each month for 12 months |

**Encrypted to the owner's key.** The server makes the backup
(`pg_dump`, the whole database) and encrypts it with GnuPG to the owner's
**public** key. The private key never leaves the owner's password vault:
the files on the office computer and the USB disk can't be read by anyone
without it — IT included (the Director's order of 07.10.2026). Each file has
a manifest next to it (`.json`): time, size, SHA-256 and each table's row
count — counts only.

## 1. The owner makes the backup key (once, ~10 minutes)

On the owner's computer (or with him), install **Gpg4win**
(gpg4win.org), open **Kleopatra**:

1. File → New OpenPGP Key Pair… → name `MGMG backup`, his e-mail →
   **Protect the generated key with a passphrase** → a long passphrase.
2. Put the passphrase **and** a backup of the private key (right-click the key →
   Backup Secret Keys…, a `.asc` file) into the password vault. Lose both and
   no backup can ever be opened.
3. Right-click the key → **Export…** → save `mgmg-backup-public.asc`. This
   public part is not secret.

## 2. Render (once)

In Render → Environment Groups → `mgmg-shared`, add:

| Setting | Value |
| ---- | ---- |
| `BACKUP_SECRET` | a long random string (e.g. 40 letters and digits from the password manager's generator) |
| `BACKUP_PUBLIC_KEY` | the whole text of `mgmg-backup-public.asc`, from `-----BEGIN PGP PUBLIC KEY BLOCK-----` to `-----END PGP PUBLIC KEY BLOCK-----` |

Render redeploys `mgmg-api`. Until both are set, `/backup/…` answers
"not found" and IT's technical report says "созланмаган".

## 3. The office computer (once)

A computer that is on during the day, with a second disk or a USB disk
that stays plugged in.

1. Copy `pull-backup.ps1` and `install-backup-task.ps1` to `C:\mgmg-backup-script\`.
2. Open `pull-backup.ps1` in Notepad and fill in the top:
   `$MgmgApiHost` (`mgmg-api-eeky.onrender.com`), `$BackupSecret`
   (`BACKUP_SECRET` from Render), `$Folder` (e.g. `D:\mgmg-backup`) and
   `$SecondFolder` (the other disk, e.g. `E:\mgmg-backup`).
3. Check it (makes nothing):
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\mgmg-backup-script\pull-backup.ps1 -Check
   ```
   It says whether the server is set up (key, `pg_dump`, `gpg`) and whether
   both folders can be written to.
4. Run it once by hand — it ends with `All done`:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\mgmg-backup-script\pull-backup.ps1
   ```
5. Schedule it — PowerShell **Run as administrator**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\mgmg-backup-script\install-backup-task.ps1
   ```
   Task "MGMG database backup": every day at 13:00 (or as soon as the
   computer is on after that), as SYSTEM. `LastTaskResult` 0 = good,
   1 = see `pull-backup.log`, 2 = settings not filled in.

Every morning IT's technical report (Admin Bot, 08:00 and `/texnik`) shows
the last copy the office fetched — ❌ if there's none for more than a day.

## 4. Restore test (by 24.10, then once a month)

The instruction's acceptance: "тиклаш синови ўтди". With the owner (his key
opens the file):

1. On a Windows computer: Gpg4win with the owner's private key imported
   (Kleopatra → Import → the backup `.asc` from the vault), and
   **PostgreSQL 16** (postgresql.org → Windows installer; remember the
   `postgres` password).
2. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File restore-test.ps1 -Folder D:\mgmg-backup
   ```
   It takes the newest copy, checks its SHA-256, decrypts it (gpg asks the
   owner's passphrase), restores it into an empty test database
   `mgmg_restore_test`, compares every table's row count with the manifest,
   then **deletes the test database and the decrypted file**.
3. The report is `D:\mgmg-backup\restore-test-<date>.txt`: PASSED or FAILED,
   tables and rows compared — counts only. Print it, both sign it: that's the
   «тиклаш синови далолатномаси».

## Restoring for real (the database was lost)

1. First choice: Render → mgmg-db → **Recovery** → Restore Database to a
   time (the last 3–7 days) — Render's own copy is newer than the office's.
2. If Render's copy is gone: create a new PostgreSQL 16 database (Render or
   a server), decrypt the newest office copy with the owner's key
   (`gpg --output mgmg.dump --decrypt mgmg-db-….dump.gpg`), then
   `pg_restore --no-owner --no-privileges --dbname "<new database URL>" mgmg.dump`,
   point `DATABASE_URL` in Render at the new database, redeploy. Delete
   `mgmg.dump` afterwards.

## How it works (for whoever maintains it)

- `integrations/backup/dump.py`: `pg_dump --format=custom` piped into
  `gpg --recipient-file <owner's public key> --encrypt` — plain processes,
  no shell, no keyring on the server. The file waits in the server's temp
  folder for one hour or until it's fetched once, then it's deleted. One
  backup per 5 minutes at most. Row counts are read in a READ ONLY
  transaction. Every backup and fetch is in `agent_actions` (agent
  `backup`, sizes only).
- `integrations/api/app.py`: `GET /backup/{secret}` (status, for `-Check`),
  `POST /backup/{secret}` (make one; answers the manifest), `GET
  /backup/{secret}/{file}` (the file, once). Wrong secret or not set up =
  404.
- The image (`Dockerfile`) has `gnupg` and `postgresql-client-16` from
  PostgreSQL's own apt repository — `pg_dump` must be at least the
  database's version (16).
- The PowerShell scripts are plain ASCII on purpose: Windows PowerShell 5.1
  misreads UTF-8 files without a BOM.
- Tests: `scripts/selfcheck.py`, `test_backup` — a real encrypt → fetch →
  decrypt round trip with a throw-away key when `gpg` is installed.
