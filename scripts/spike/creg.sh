#!/usr/bin/env bash
# Phase 5 on concurrent_reg: the environment, then one bugpool command. The repository is never modified: bugpool works
# in throwaway worktrees and its burned-line registry lives outside the repo.
#
#   scripts/spike/creg.sh up                 start the private MySQL (127.0.0.1:3407, database concurrentreg_test)
#   scripts/spike/creg.sh session code|docs  a timed session on the sealed pool, under that condition
#   scripts/spike/creg.sh bugpool ARGS...    any bugpool command, with the test environment set
#
# CREG_HOME (default ~/.bugpool/concurrent_reg) holds: venv/ (Python 3.14, the repo's requirements + pytest-cov),
# mysql/ (a private MySQL 8.4 data dir, binary log off), burned.json, sealed/ (pool, selection: do not open).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
H="${CREG_HOME:-$HOME/.bugpool/concurrent_reg}"
REPO="${CREG_REPO:-$HOME/concurrent_reg}"
MYSQLD="${MYSQLD:-/opt/homebrew/opt/mysql@8.4/bin/mysqld}"
ABSEIL="${ABSEIL_LIB:-/opt/homebrew/Cellar/abseil/20260107.1/lib}"  # the bottle's mysqld links this abseil, not opt's
PORT=3407
SOCK="$H/s/m.sock"
export TEST_DATABASE_URL="mysql+pymysql://root@127.0.0.1:$PORT/concurrentreg_test"

up() {
  if "$H/venv/bin/python" - "$PORT" <<'PY' 2>/dev/null; then return 0; fi
import socket, sys
socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1).close()
PY
  mkdir -p "$H/s"
  [ -d "$H/mysql/mysql" ] || DYLD_LIBRARY_PATH="$ABSEIL" "$MYSQLD" --initialize-insecure --datadir="$H/mysql" --user="$(whoami)" 2>"$H/mysql-init.log"
  DYLD_LIBRARY_PATH="$ABSEIL" "$MYSQLD" --datadir="$H/mysql" --port=$PORT --bind-address=127.0.0.1 --socket="$SOCK" \
    --mysqlx=OFF --disable-log-bin --user="$(whoami)" --log-error="$H/mysql-run.log" --pid-file="$H/s/m.pid" \
    </dev/null >/dev/null 2>&1 &
  disown || true
  for _ in $(seq 1 60); do
    "$H/venv/bin/python" -c "import pymysql; c = pymysql.connect(host='127.0.0.1', port=$PORT, user='root'); \
c.cursor().execute('CREATE DATABASE IF NOT EXISTS concurrentreg_test'); c.close()" 2>/dev/null && return 0
    sleep 1
  done
  echo "MySQL did not start; see $H/mysql-run.log" >&2
  return 1
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up) up ;;
  session)
    cond="${1:?code or docs}"
    up
    cd "$HERE"
    exec "$HERE/.venv/bin/python" -m bugpool session --repo "$REPO" --pool "$H/sealed/pool.json" --condition "$cond" \
      --seed "$(cat "$H/sealed/seed_$cond.txt")" --python "$H/venv/bin/python" --registry "$H/burned.json" \
      --stranger "$H/sealed/stranger_$cond.json" --out "$HERE/reports/creg_session_$cond.json" ;;
  bugpool)
    up
    cd "$HERE"
    exec "${BUGPOOL_PY:-$HERE/.venv/bin/python}" -m bugpool "$@" ;;
  *) sed -n 2,12p "$0"; exit 2 ;;
esac
