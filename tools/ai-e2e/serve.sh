#!/usr/bin/env bash
# Start the local Pumpkin test server.
#
# Server binds :25565 (Minecraft) and 127.0.0.1:25575 (RCON, loopback only).
# World data and config live in ./run/ so this never touches a real world.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${PUMPKIN_E2E_BIN:-$HOME/programming/Pumpkin/target/release/pumpkin}"
RUN_DIR="$HERE/run"

if [[ ! -x "$BIN" ]]; then
  echo "error: pumpkin binary not found or not executable: $BIN" >&2
  echo "  build it:  cd ~/programming/Pumpkin && cargo build --release" >&2
  echo "  or set PUMPKIN_E2E_BIN to an explicit path" >&2
  exit 1
fi

echo "binary : $BIN"
echo "rundir : $RUN_DIR"
echo
echo "Minecraft client -> localhost:25565   (online_mode=false)"
echo "RCON             -> 127.0.0.1:25575   (password: test)"
echo "Drive it with    -> $HERE/pmk.py status"
echo

# NOTE: do NOT probe the binary with `--version`. Pumpkin ignores unknown flags
# and boots a full server instead, which (a) blocks forever and (b) generates a
# world + config in whatever cwd it inherits. Verified the hard way.
cd "$RUN_DIR"
exec "$BIN"
