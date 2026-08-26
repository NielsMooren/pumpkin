#!/usr/bin/env python3
"""pmk - drive a live Pumpkin server over RCON.

Zero dependencies, zero build. Works the moment a Pumpkin server is up with
RCON enabled, so it is usable while the real Rust/azalea harness is still
compiling.

Design notes (from three rounds of model review + source recon of Pumpkin):
  * NEVER uses `@s`. RCON has no player identity, so `@s` silently resolves to
    nothing and the failure masquerades as a permission or serialisation bug.
    Every entity read takes an explicit player name.
  * Wall-clock is used ONLY as a failure timeout, never as a completion
    condition. `tick_barrier()` polls a real observable and fails on overshoot.
  * Health is never used as a completion probe - it is the behaviour under test.

Usage:
  ./pmk.py status
  ./pmk.py cmd "say hello"
  ./pmk.py data <PlayerName>            # full NBT
  ./pmk.py health <PlayerName>
  ./pmk.py effects <PlayerName>
  ./pmk.py tick query
  ./pmk.py freeze | unfreeze
  ./pmk.py step [n]
  ./pmk.py gametime
  ./pmk.py watch <PlayerName> [--interval 1.0]
  ./pmk.py poison-probe <PlayerName> [--steps 60]

Env:
  PMK_HOST (default 127.0.0.1)
  PMK_PORT (default 25575)
  PMK_PASS (default "test")
"""
import argparse
import os
import re
import socket
import struct
import sys
import time

HOST = os.environ.get("PMK_HOST", "127.0.0.1")
PORT = int(os.environ.get("PMK_PORT", "25575"))
PASSWORD = os.environ.get("PMK_PASS", "test")

LOGIN, COMMAND, RESP_VALUE = 3, 2, 0


class RconError(Exception):
    pass


class Rcon:
    """Minimal RCON client (Valve protocol, little-endian)."""

    def __init__(self, host=HOST, port=PORT, password=PASSWORD, timeout=10.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock = None
        self._rid = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        rid = self._send(LOGIN, self.password)
        pkt = self._recv()
        # Auth failure is signalled by request_id == -1
        if pkt["id"] == -1 or (pkt["id"] != rid and pkt["id"] == -1):
            raise RconError("RCON authentication failed (wrong password?)")
        return self

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _send(self, ptype, body):
        self._rid += 1
        payload = struct.pack("<ii", self._rid, ptype) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)
        return self._rid

    def _read_exactly(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed by server")
            buf += chunk
        return buf

    def _recv(self):
        (length,) = struct.unpack("<i", self._read_exactly(4))
        data = self._read_exactly(length)
        rid, ptype = struct.unpack("<ii", data[:8])
        body = data[8:].rstrip(b"\x00").decode("utf-8", "replace")
        return {"id": rid, "type": ptype, "body": body}

    def cmd(self, command):
        self._send(COMMAND, command)
        # Responses can be fragmented; read until the socket goes quiet.
        parts, deadline = [], time.time() + self.timeout
        while time.time() < deadline:
            try:
                pkt = self._recv()
            except socket.timeout:
                break
            parts.append(pkt["body"])
            self.sock.settimeout(0.25)
            try:
                self.sock.recv(0)
            except Exception:
                pass
            if len(parts[-1]) < 4000:
                break
        self.sock.settimeout(self.timeout)
        return "".join(parts).strip()


# ---------- ANSI / formatting helpers ----------

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clean(s):
    return ANSI.sub("", s).strip()


# ---------- higher-level probes ----------

def data_get(r, player, path=None):
    """Authoritative server-side read. Explicit target name - never @s.

    NOTE (verified live 2026-08-26): Pumpkin does NOT support NBT path
    arguments on `/data get entity` - `data get entity <sel> Health` returns
    "Incorrect argument for command". Only whole-entity dumps work, so `path`
    is applied client-side by grepping the dump.
    """
    out = clean(r.cmd(f"data get entity {player}"))
    if not path:
        return out
    # Dump looks like: `X has the following entity data: {a: 1, Health: 20f, ...}`
    m = re.search(rf"\b{re.escape(path)}:\s*([^,}}]+)", out)
    return m.group(1).strip() if m else ""


def parse_float(text):
    m = re.search(r"(-?\d+(?:\.\d+)?)(?:[fd])?\s*$", text.strip())
    if m:
        return float(m.group(1))
    m = re.search(r"(-?\d+(?:\.\d+)?)(?:[fd])?", text)
    return float(m.group(1)) if m else None


def health(r, player):
    return parse_float(data_get(r, player, "Health"))


def gametime(r):
    out = clean(r.cmd("time query gametime"))
    m = re.search(r"(\d+)", out)
    return int(m.group(1)) if m else None


def tick_barrier(r, n=1, timeout=10.0):
    """Advance exactly n ticks and CONFIRM arrival.

    `/tick step` may return "requested" rather than "done", so never trust the
    command response as a barrier. Poll a real observable (gametime) until it
    reaches before+n. Overshoot is a hard failure: it means the server was not
    actually frozen, which would silently invalidate every downstream timing
    assertion.
    """
    before = gametime(r)
    if before is None:
        raise RconError("cannot read gametime; is the world loaded?")
    r.cmd(f"tick step {n}")
    target, deadline = before + n, time.time() + timeout
    while time.time() < deadline:
        now = gametime(r)
        if now is None:
            continue
        if now == target:
            return {"before": before, "after": now, "advanced": n}
        if now > target:
            raise RconError(
                f"OVERSHOOT: gametime {before} -> {now}, expected {target}. "
                "Server is probably NOT frozen; timing assertions are invalid."
            )
        time.sleep(0.02)
    raise RconError(f"tick_barrier timed out: gametime stuck at {gametime(r)}, wanted {target}")


# ---------- commands ----------

def c_status(r, _a):
    print(f"connected  {r.host}:{r.port}")
    for label, cmd in (("list", "list"), ("tick", "tick query"), ("time", "time query gametime")):
        try:
            print(f"{label:9}  {clean(r.cmd(cmd))}")
        except Exception as e:
            print(f"{label:9}  ERROR {e}")


def c_cmd(r, a):
    print(clean(r.cmd(" ".join(a.command))) or "(empty response)")


def c_data(r, a):
    print(data_get(r, a.player, a.path))


def c_health(r, a):
    h = health(r, a.player)
    print("unknown" if h is None else h)


def c_effects(r, a):
    print(data_get(r, a.player, "active_effects") or "(none)")


def c_tick(r, a):
    print(clean(r.cmd("tick " + " ".join(a.args))))


def c_freeze(r, _a):
    print(clean(r.cmd("tick freeze")))


def c_unfreeze(r, _a):
    print(clean(r.cmd("tick unfreeze")))


def c_step(r, a):
    try:
        print(tick_barrier(r, a.n))
    except RconError as e:
        print(f"BARRIER FAILED: {e}", file=sys.stderr)
        sys.exit(1)


def c_gametime(r, _a):
    print(gametime(r))


def c_watch(r, a):
    print(f"{'time':>10} {'health':>7}  effects")
    try:
        while True:
            gt, h = gametime(r), health(r, a.player)
            eff = data_get(r, a.player, "active_effects") or "-"
            print(f"{gt if gt is not None else '?':>10} {h if h is not None else '?':>7}  {eff[:70]}")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print("\nstopped")


def c_poison_probe(r, a):
    """Characterise poison cadence under freeze - the experiment the panel agreed.

    Establishes a VERIFIED negative control first: regeneration off, then step
    ticks with no effect applied and confirm health does not move. Without that,
    natural regeneration can mask poison damage and make a wrong test pass.

    REQUIRES A CONNECTED PLAYER. `/effect` is players-only in Pumpkin
    (crates/pumpkin/src/command/commands/effect.rs:264 uses
    PlayersArgumentConsumer), and entities do not tick at all with nobody
    online - see README "Two blockers".
    """
    p = a.player
    print("== baseline setup")
    for cmd in (
        "gamerule naturalRegeneration false",
        "difficulty normal",
        f"effect clear {p}",
        f"gamemode survival {p}",
    ):
        print(f"   {cmd:45} -> {clean(r.cmd(cmd))[:60]}")

    print(clean(r.cmd("tick freeze")))
    print(f"   frozen. health={health(r, p)} gametime={gametime(r)}")

    print("\n== negative control (no effect, 20 ticks) - health MUST NOT move")
    h0 = health(r, p)
    for _ in range(20):
        tick_barrier(r, 1)
    h1 = health(r, p)
    if h0 != h1:
        print(f"   FAIL: health drifted {h0} -> {h1} with no effect applied.")
        print("   Something else is damaging/healing. Fix before trusting cadence data.")
        return
    print(f"   OK: health stable at {h1}")

    print(f"\n== applying poison, stepping {a.steps} ticks one at a time")
    print(clean(r.cmd(f"effect give {p} poison 60 0 true")))
    prev, events = health(r, p), []
    print(f"   t=0 health={prev}")
    for i in range(1, a.steps + 1):
        tick_barrier(r, 1)
        h = health(r, p)
        if h != prev:
            events.append((i, prev, h))
            print(f"   tick {i:>3}: {prev} -> {h}")
            prev = h

    print("\n== result")
    if len(events) < 2:
        print(f"   only {len(events)} damage event(s) in {a.steps} ticks.")
        print("   NOTE: if health changed at EVERY step, freeze is not gating")
        print("   player effect ticking (the known tick_players_and_network path).")
    else:
        gaps = [events[i][0] - events[i - 1][0] for i in range(1, len(events))]
        print(f"   damage events at ticks: {[e[0] for e in events]}")
        print(f"   intervals: {gaps}")
        print(f"   final health: {prev}  (poison must floor at 1.0, never kill)")
    print(clean(r.cmd("tick unfreeze")))


def c_entity(r, a):
    """Read a non-player entity. Pumpkin's selector support is minimal:
    `@e[limit=1]` works; `@e`, `@e[type=zombie]` do NOT (verified live)."""
    print(data_get(r, a.selector, a.path) or "(no match)")


def main():
    ap = argparse.ArgumentParser(description="Drive a live Pumpkin server over RCON")
    sub = ap.add_subparsers(dest="cmd_name", required=True)

    sub.add_parser("status").set_defaults(fn=c_status)
    p = sub.add_parser("cmd"); p.add_argument("command", nargs="+"); p.set_defaults(fn=c_cmd)
    p = sub.add_parser("data"); p.add_argument("player"); p.add_argument("path", nargs="?"); p.set_defaults(fn=c_data)
    p = sub.add_parser("entity", help="read a mob, e.g. entity '@e[limit=1]' Health")
    p.add_argument("selector"); p.add_argument("path", nargs="?"); p.set_defaults(fn=c_entity)
    p = sub.add_parser("health"); p.add_argument("player"); p.set_defaults(fn=c_health)
    p = sub.add_parser("effects"); p.add_argument("player"); p.set_defaults(fn=c_effects)
    p = sub.add_parser("tick"); p.add_argument("args", nargs="+"); p.set_defaults(fn=c_tick)
    sub.add_parser("freeze").set_defaults(fn=c_freeze)
    sub.add_parser("unfreeze").set_defaults(fn=c_unfreeze)
    p = sub.add_parser("step"); p.add_argument("n", nargs="?", type=int, default=1); p.set_defaults(fn=c_step)
    sub.add_parser("gametime").set_defaults(fn=c_gametime)
    p = sub.add_parser("watch"); p.add_argument("player"); p.add_argument("--interval", type=float, default=1.0); p.set_defaults(fn=c_watch)
    p = sub.add_parser("poison-probe"); p.add_argument("player"); p.add_argument("--steps", type=int, default=60); p.set_defaults(fn=c_poison_probe)

    a = ap.parse_args()
    try:
        with Rcon() as r:
            a.fn(r, a)
    except (RconError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        print(f"  (tried {HOST}:{PORT}; set PMK_HOST/PMK_PORT/PMK_PASS to override)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
