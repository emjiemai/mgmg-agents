#!/usr/bin/env bash
# The local database copy on this computer (Ubuntu): PostgreSQL, the mgmg_mirror database, a 30-minute timer.
# Run once from the repo root:  bash scripts/db-mirror/install.sh   (asks for the sudo password once)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$REPO/.venv/bin/python"

# 1. PostgreSQL (Ubuntu 24.04: version 16, the same as Render's)
if ! command -v psql >/dev/null 2>&1; then
  sudo apt-get update -qq && sudo apt-get install -y postgresql
fi
# 2. A database role for this user (may create databases; not a superuser) and the copy's database
sudo -u postgres createuser --createdb "$USER" 2>/dev/null || true
createdb mgmg_mirror 2>/dev/null || true

# 3. The timer: every 30 minutes (:00 and :30); after sleep or a restart it runs as soon as the computer is up
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/mgmg-db-mirror.service" <<UNIT
[Unit]
Description=MGMG database copy (changes only)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO
ExecStart=$PY $REPO/scripts/db-mirror/mirror.py
Nice=10
TimeoutStartSec=15min
UNIT
cat > "$HOME/.config/systemd/user/mgmg-db-mirror.timer" <<UNIT
[Unit]
Description=MGMG database copy every 30 minutes

[Timer]
OnCalendar=*:00,30
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now mgmg-db-mirror.timer

# 4. The first copy now (the whole of the bot's own tables; later runs copy changes only)
"$PY" "$REPO/scripts/db-mirror/mirror.py"
echo "Done. Next runs: systemctl --user list-timers mgmg-db-mirror.timer"
