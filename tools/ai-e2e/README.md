# pumpkin-e2e

AI-drivable test harness for [Pumpkin](https://github.com/Pumpkin-MC/Pumpkin),
a Minecraft server written in Rust.

The point: you (or an AI agent) can drive a **live** Pumpkin server, play the
game, and assert on real gameplay behaviour — while a real Minecraft client is
connected to the same server, watching it happen.

---

## Quick start

```bash
# 1. build the server (once, ~14 min)
cd ~/programming/Pumpkin
git submodule update --init --recursive   # REQUIRED - see pitfall below
cargo build --release

# 2. generate a config, then patch it
cd tools/ai-e2e
./serve.sh                     # writes run/pumpkin.toml, then Ctrl-C
./setup-config.py run/pumpkin.toml

# 3. start for real
./serve.sh

# 4. in another terminal
cd ~/programming/Pumpkin/tools/ai-e2e
./pmk.py status
```

Then connect a real Minecraft **Java 26.2** client to `localhost:25565`.
`online_mode = false`, so no Mojang auth is needed.

### Pitfall: the WIT submodule

Pumpkin vendors its plugin interface as a git submodule
(`crates/pumpkin-plugin-wit`, see `.gitmodules`). Without it,
`pumpkin-plugin-api` fails with **319 errors** including
`cannot find macro 'export' in this scope` — which looks like a broken
toolchain but is just a missing checkout. Pumpkin's own CI uses
`submodules: recursive` for this reason. `git clone --depth N` does **not**
fetch it.

Check with `git submodule status`: a leading `-` means not initialised.

---

## `pmk.py` — RCON control, zero dependencies

```bash
./pmk.py status                  # players, tick state, gametime
./pmk.py cmd "say hello"         # arbitrary command
./pmk.py health <Player>         # authoritative server-side health
./pmk.py effects <Player>        # active_effects NBT
./pmk.py data <Player> [path]    # any NBT field (grepped from the dump)
./pmk.py entity '@e[limit=1]' Health   # non-player entity
./pmk.py freeze / unfreeze       # /tick freeze
./pmk.py step [n]                # advance n ticks, CONFIRMED (see below)
./pmk.py gametime
./pmk.py watch <Player>          # live health/effects table
./pmk.py poison-probe <Player>   # full cadence experiment
```

Override the connection with `PMK_HOST` / `PMK_PORT` / `PMK_PASS`.

---

## Three design rules (learned the hard way)

These came out of three rounds of 4-model review plus source recon. They are
not style preferences — each one is a bug that would otherwise cost days.

**1. Never `@s`.** RCON has no player identity, so `/data get entity @s`
resolves to nothing. The failure is *silent* and masquerades as a permission,
serialisation, or entity-removal bug. Every read here takes an explicit player
name.

**2. Wall-clock is a failure timeout, never a completion condition.**
`/tick step N` may return "requested" rather than "done". So `tick_barrier()`
reads `gametime`, requests the step, then **polls until it reaches
`before + n`** — and treats *overshoot* as a hard error, because overshoot means
the server was never actually frozen and every timing assertion downstream is
worthless.

**3. Health is never the completion probe.** It is the behaviour under test.
Using it to detect "did the tick land" is circular.

---

## A connected client is REQUIRED

Two hard limits found live (details in SESSION-NOTES.md):

1. **`/effect` is players-only** — `commands/effect.rs:264` uses
   `PlayersArgumentConsumer`. You cannot poison a mob via command.
2. **Entities do not tick with nobody online** — a killed zombie stayed at
   `Health: 0f, DeathTime: 0s` for 15+ seconds while `gametime` advanced.
   Entity ticking needs loaded chunks, which need a player.

So `poison-probe` and any gameplay assertion need a Minecraft Java 26.2 client
(or an azalea bot) connected to `localhost:25565`. RCON-only commands
(`status`, `freeze`, `step`, `gametime`, `summon`, `damage`) work regardless.

---

## Known issue: freeze does not stop player effects

Verified in source (this is a real Pumpkin bug, not a harness limitation):

```
server/mod.rs:1086   tick_players_and_network()    <- runs even when FROZEN
server/mod.rs:1099     player_clone.tick(&server)  <- no freeze guard
player.rs:2526           self.living_entity.tick(...)
living.rs:2769             self.tick_effects()
```

`tick_worlds()` *is* freeze-gated, so **non-player** entity effects, block
ticks and the task scheduler all freeze correctly. But a **player's** effects
keep ticking while the server is frozen. Vanilla freezes them.

Consequences:
- `poison-probe` on a *player* may show damage every step → that is the bug,
  and the probe will tell you so.
- Deterministic effect tests should target a **summoned mob** until this is
  fixed, since that path is genuinely frozen.
- This is a decent first upstream PR: it is a vanilla-parity bug, not test
  scaffolding.

---

## Layout

```
pmk.py              RCON driver (stdlib only, works today)
serve.sh            starts the server against ./run/
setup-config.py     patches a GENERATED pumpkin.toml, then verifies it
SESSION-NOTES.md    full findings + how to resume (read this first)
run/                world data + generated config (gitignored)
```

`run/pumpkin.toml` is a *partial* config — Pumpkin merges it over defaults and
rewrites the file. Field layout verified against
`crates/pumpkin-config/src/lib.rs` (note `#[serde(flatten)]`: there is no
`[basic]`/`[advanced]` table).

---

## Verification status

`pmk.py` has ad-hoc verification only — **not** a test suite. 21 checks pass
against a fake in-process RCON server that validates Valve-protocol framing:
NBT float parsing, login/command round-trip, `tick_barrier()` exact-advance,
overshoot-is-fatal, stuck-times-out, config layout, and the no-`@s` rule.

Not yet verified: anything requiring a running Pumpkin server (real RCON auth,
actual `/tick freeze` semantics, `/data get entity` output shape). Those need
the build to finish.

---

## Next phase

`pmk.py` gives server-authoritative state. It cannot see what a *client*
believes — which is where desync bugs live. That needs
[azalea](https://github.com/azalea-rs/azalea) (Rust, supports MC 26.2 exactly,
the same version Pumpkin targets).

The valuable assertion is the **diff**: bot-believed state vs RCON-reported
state. Disagreement = desync, a bug class no unit test can reach.

When adding it, pin azalea to an exact commit SHA — not a branch — so a
harness run is reproducible.
