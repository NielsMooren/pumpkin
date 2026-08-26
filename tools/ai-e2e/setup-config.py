#!/usr/bin/env python3
"""Patch a Pumpkin-generated pumpkin.toml for local AI-driven testing.

Do NOT hand-write pumpkin.toml. Let the server generate it once, then patch it.
Hand-written configs get silently discarded, for two non-obvious reasons found
the hard way (2026-08-26):

  1. `online_mode` is NOT top-level - it lives under [networking.java]
     (and separately [networking.bedrock]). PumpkinConfig uses #[serde(flatten)]
     so there is no [basic]/[advanced] table, which makes it *look* like
     everything is top-level. It isn't.
  2. Bare top-level keys placed after any [table] header get absorbed into that
     table by TOML and then dropped by Pumpkin's deserializer. Silently.

Usage:
    ./setup-config.py run/pumpkin.toml
"""
import pathlib
import re
import sys

# (table, key, TOML-literal value)
SETTINGS = [
    ("networking.java", "online_mode", "false"),          # let offline clients/bots join
    ("networking.rcon", "enabled", "true"),
    ("networking.rcon", "address", '"127.0.0.1:25575"'),   # loopback only, never expose
    ("networking.rcon", "password", '"test"'),
    ("networking.rcon.logging", "commands", "false"),      # pmk.py is chatty
    ("networking.rcon.logging", "logged_successfully", "false"),
    ("networking.bedrock", "enabled", "false"),            # grabs :19132 and hits OIDC
]


def set_in_table(text, table, key, value):
    """Set key=value inside [table] only, without leaking into the next table."""
    m = re.search(rf"^\[{re.escape(table)}\]$", text, re.M)
    if not m:
        raise SystemExit(f"error: table [{table}] not found - is this a generated pumpkin.toml?")
    start = m.end()
    nxt = re.search(r"^\[", text[start:], re.M)
    end = start + (nxt.start() if nxt else len(text) - start)
    block = text[start:end]
    kre = re.compile(rf"^{re.escape(key)}\s*=.*$", re.M)
    if kre.search(block):
        block = kre.sub(f"{key} = {value}", block, count=1)
    else:
        block = block.rstrip("\n") + f"\n{key} = {value}\n"
    return text[:start] + block + text[end:]


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = pathlib.Path(sys.argv[1])
    if not path.exists():
        sys.exit(
            f"error: {path} does not exist.\n"
            "Start the server once to generate it (./serve.sh), stop it, then re-run this."
        )
    text = path.read_text()
    for table, key, value in SETTINGS:
        text = set_in_table(text, table, key, value)
    path.write_text(text)

    print(f"patched {path}")
    for table, key, value in SETTINGS:
        print(f"  [{table}] {key} = {value}")

    # Verify by re-parsing with a real TOML parser rather than trusting the regex.
    try:
        import tomllib
    except ModuleNotFoundError:
        print("\n(python <3.11: skipping verification - install 3.11+ to verify)")
        return
    cfg = tomllib.loads(path.read_text())
    net = cfg["networking"]
    checks = {
        "networking.java.online_mode is False": net["java"]["online_mode"] is False,
        "networking.rcon.enabled is True": net["rcon"]["enabled"] is True,
        "rcon bound to loopback": str(net["rcon"]["address"]).startswith("127.0.0.1"),
        "networking.bedrock.enabled is False": net["bedrock"]["enabled"] is False,
    }
    print()
    for name, ok in checks.items():
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    if not all(checks.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
