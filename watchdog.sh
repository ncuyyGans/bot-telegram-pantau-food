#!/bin/bash
# Watchdog: pastikan bot pantau GrabFood selalu jalan. Dipanggil cron tiap 5 menit.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
if [ -f bot.pid ]; then
  PID=$(cat bot.pid)
  # Pola ketat: cocok "python3 bot.py" (boleh dengan path), bukan "otpbot.py"
  # (grep substring "bot.py" bisa false-positive bila PID dipakai ulang proses lain).
  if kill -0 "$PID" 2>/dev/null && ps -p "$PID" -o args= | grep -qE "(^|[[:space:]])python3[[:space:]]+([^[:space:]]*/)?bot\.py([[:space:]]|$)"; then
    exit 0
  fi
fi
echo "$(date '+%F %T') watchdog: starting bot" >> bot.log
nohup python3 bot.py >> bot.log 2>&1 &
echo $! > bot.pid
