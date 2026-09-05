#!/usr/bin/env bash
# Local browser-gate runtime. Usage: forge-dev.sh [UI_ROOT] | forge-dev.sh stop
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="/tmp/forge-dev-${UID}-8070"
mkdir -p -- "$runtime_dir"
exec 9>"$runtime_dir/control.lock"
flock -n 9 || { echo 'Another forge-dev command is active.' >&2; exit 1; }
python_bin="$repo_root/.venv/bin/python"

stop_runtime() {
  "$python_bin" - "$runtime_dir/pids.json" <<'PY'
import json
from pathlib import Path
import os
import signal
import sys
import time
path = Path(sys.argv[1])
if not path.exists():
    print("Forge dev is already stopped.")
    raise SystemExit(0)
records = json.loads(path.read_text())
def alive(record):
    try:
        stat = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(')', 1)[1].split()
        return stat[0] != 'Z' and stat[19] == record['start']
    except FileNotFoundError:
        return False
# Stop the worker first so an active bake can restore its snapshot while API lives.
for name in ('worker', 'api'):
    record = records[name]
    if alive(record):
        os.kill(record['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 30
        while alive(record) and time.monotonic() < deadline:
            time.sleep(.1)
        if alive(record):
            raise SystemExit(f"{name} did not stop within 30s; retained PID record for retry.")
    print(f"Stopped {name} PID {record['pid']}")
path.unlink()
PY
}

if [[ "${1:-}" == stop ]]; then
  stop_runtime
  exit
fi
if [[ -f "$runtime_dir/pids.json" ]]; then
  echo "Forge dev has a retained PID record. Run $0 stop first." >&2
  exit 1
fi
export OPEN_SPRITE_UI_ROOT="${1:-/tmp/forge-dev-ui}"
export FORGE_ENABLED=1
export FORGE_ONCE=0
export FORGE_API=http://127.0.0.1:8070
export FORGE_SPIKE_ASSETS="${FORGE_SPIKE_ASSETS:-/home/alexk/debt-city-greybox-spike/apps/greybox/assets}"
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
# Refuse to launch alongside an existing listener; never stop somebody else's service.
"$python_bin" - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 8070))
PY
cd -- "$repo_root"
nohup "$python_bin" -m uvicorn open_sprite_pipeline.api:app --host 127.0.0.1 --port 8070 > "$runtime_dir/api.log" 2>&1 9>&- &
api_pid=$!
nohup "$python_bin" -m open_sprite_pipeline.forge_worker > "$runtime_dir/worker.log" 2>&1 9>&- &
worker_pid=$!
"$python_bin" - "$runtime_dir/pids.json" "$api_pid" "$worker_pid" <<'PY'
import json
from pathlib import Path
import sys
records = {}
for name, pid in zip(('api', 'worker'), sys.argv[2:]):
    stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    records[name] = {'pid': int(pid), 'start': stat[19]}
Path(sys.argv[1]).write_text(json.dumps(records))
PY
trap 'stop_runtime' ERR
"$python_bin" - "$api_pid" "$worker_pid" <<'PY'
from pathlib import Path
import json
import sys
import time
from urllib.request import ProxyHandler, build_opener
opener = build_opener(ProxyHandler({}))
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    for pid in sys.argv[1:]:
        path = Path(f'/proc/{pid}/stat')
        if not path.exists() or path.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            raise SystemExit(f'Forge process {pid} exited during startup.')
    try:
        with opener.open('http://127.0.0.1:8070/v1/forge/status', timeout=1) as response:
            if json.load(response)['enabled']:
                break
    except (OSError, ValueError):
        time.sleep(.2)
else:
    raise SystemExit('Forge API did not become ready within 20s.')
PY
trap - ERR
echo "API PID: $api_pid"
echo "Worker PID: $worker_pid"
echo "URL: http://127.0.0.1:8070"
echo "UI root: $OPEN_SPRITE_UI_ROOT"
echo "Logs: $runtime_dir/api.log $runtime_dir/worker.log"
echo "Stop: $0 stop"
