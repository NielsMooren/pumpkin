# AI-Driven E2E Testing for Pumpkin — Session Notes, 2026-08-26

Everything here is **work-in-progress, parked**. Company policy blocked the
Minecraft client mid-session, and (see "Two blockers") a connected client turns
out to be a hard requirement for the interesting tests. This branch stores the
working pieces plus everything learned, so it can be picked up cold.

**Nothing here has been pushed upstream. No PRs opened. No issues commented on.**

---

## What this is

A harness so an AI agent can drive a **live** Pumpkin server, play the game, and
assert on real gameplay behaviour — the thing Pumpkin's test suite structurally
cannot do today (4 unit test files, nothing that connects a client).

## What actually works, verified live

`tools/ai-e2e/pmk.py` — RCON driver, Python stdlib only, no build step.
Confirmed against a real server built from this branch:

```
$ ./pmk.py status
connected  127.0.0.1:25575
list       There are 0 of a max of 1000 players online:
tick       The game is running normally
time       Target tick rate: 20.0 per second. Average time per tick: 0.10ms

$ ./pmk.py freeze          -> The game is frozen
$ ./pmk.py step 1          -> {'before': 583, 'after': 584, 'advanced': 1}
$ ./pmk.py step 10         -> {'before': 584, 'after': 594, 'advanced': 10}
$ ./pmk.py unfreeze        -> +41 ticks in 2 real seconds
```

Also verified: `summon`, `damage`, `kill`, and whole-entity NBT reads.

### The headline result

**`/tick step N` gives a real completion barrier.** Three separate 4-model
review panels and a lot of source reading could not settle this. Measured
answer: exact advance, no overshoot, and `gametime` genuinely froze at 594
across 3 real seconds. **Deterministic tick-stepped testing on Pumpkin works.**

---

## Two blockers (why this is parked)

### 1. `/effect` is players-only

`crates/pumpkin/src/command/commands/effect.rs:264` uses
`PlayersArgumentConsumer`. You **cannot** apply an effect to a mob via command.

This killed the plan three review panels had converged on. The idea was to test
effects on a summoned mob to sidestep a player-freeze bug (below) — it doesn't
survive contact with the code.

### 2. Entities do not tick with no player online

A killed zombie sat at `Health: 0f, DeathTime: 0s` for **15+ seconds** while
`gametime` advanced normally (6126 → 6166). The removal logic at
`crates/pumpkin/src/entity/living.rs:2896-2916` is correct; it simply never
runs. Entity ticking happens inside `world.tick()` and needs loaded/ticking
chunks, which need a player.

**So a connected client (or headless bot) is a hard dependency — not for
watching, but because the world is inert without one.**

---

## Findings about Pumpkin (all source- or live-verified)

### Freeze does NOT gate player effect ticking — likely a vanilla-parity bug

Unconditional call chain, no freeze guard anywhere:

```
server/mod.rs:1086   tick_players_and_network()    <- runs even when FROZEN
server/mod.rs:1099     player_clone.tick(&server)
player.rs:2354         Player::tick
player.rs:2526           self.living_entity.tick(...)
living.rs:2698           LivingEntity::tick        <- no freeze check
living.rs:2769             self.tick_effects()
```

`tick_worlds()` *is* freeze-gated (`server/mod.rs:1075`), so non-player entity
effects, block ticks and the task scheduler all freeze correctly. But a
**player's** effects keep ticking while frozen. Vanilla freezes them.

This is probably a good first upstream PR: a real behaviour bug, not test
scaffolding.

### Effect tick phase: apply BEFORE decrement

`living.rs:1788-1815`, per effect: `duration == 0` → remove; else evaluate
`should_apply_effect_tick(current_duration, amplifier)`; **then**
`duration -= 1`. Also `duration == -1` means infinite and substitutes
`entity.age`.

This matters because it's the tick-25-vs-26 boundary that would otherwise get
tuned until tests went green (curve-fitting).

### Poison cannot kill; wither can

- `entity/effect/poison.rs` — gated `if current_health > 1.0`, damage clamped
  `(current_health - 1.0).min(1.0)`. Floors at 1 HP. Matches vanilla.
- `entity/effect/wither.rs` — same directory, no health gate, flat
  `damage(1.0, DamageType::WITHER)`. Kills. Also matches vanilla.
- Tick rates: poison `25 >> amplifier.min(4)`, wither `40 >> amplifier.min(4)`.

Two effects, deliberately different lethality, zero test coverage between them.

### Command/selector support is thinner than expected

| | |
|---|---|
| `@e[limit=1]` | works |
| `@e`, `@e[type=zombie]`, `@e[type=minecraft:zombie]` | "Incorrect argument for command" |
| `data get entity <sel> Health` (NBT path arg) | NOT supported — whole-entity dumps only |
| `/tick` permission | `PermissionLvl::Three` (`commands/tick.rs:370`), RCON clears it |

`pmk.py` works around the path limitation by grepping the dump.

### Mobs have working i-frames

`damage 4` → "Target is invulnerable to the given damage type" during hurt
cooldown; `damage 100` → `Health: 0f`. I nearly filed a bug that wasn't one.

### Port 0 is already half-supported

`lib.rs:280` explicitly comments on `port = 0` and captures
`listener.local_addr()`. **But** `main.rs:125-135` logs the *configured*
address, and `net/rcon/mod.rs:41` logs `config.address` without ever calling
`local_addr()`. So with `port = 0` the game port is discoverable-in-principle
and RCON is undiscoverable. A tiny "publish the actually-bound addresses"
patch is the natural first upstream contribution.

### Build pitfall: the WIT submodule

`git clone --depth N` skips `crates/pumpkin-plugin-wit`, and
`pumpkin-plugin-api` then fails with **319 errors** including
`cannot find macro 'export' in this scope`. Looks like a broken toolchain; it's
a missing checkout. Pumpkin's CI uses `submodules: recursive`.

```bash
git submodule update --init --recursive   # leading '-' in `git submodule status` = missing
```

Release build takes ~14 min on an M-series Mac.

### Config pitfall: don't hand-write pumpkin.toml

Two silent failure modes, both hit:
1. `online_mode` is **not** top-level — it's under `[networking.java]`.
   `PumpkinConfig` uses `#[serde(flatten)]`, so there's no `[basic]`/`[advanced]`
   table, which makes everything *look* top-level.
2. Bare keys after any `[table]` header get absorbed into that table and then
   dropped.

`tools/ai-e2e/setup-config.py` patches a **generated** config instead, and
re-parses with `tomllib` to prove the settings landed.

---

## Three design rules (each one is a bug that costs days)

1. **Never `@s`.** RCON has no player identity, so `/data get entity @s`
   resolves to nothing — and the failure is *silent*, masquerading as a
   permission, serialisation or entity-removal bug.
2. **Wall-clock is a failure timeout, never a completion condition.**
   `tick_barrier()` reads `gametime`, requests the step, polls until
   `before + n`, and treats **overshoot as fatal** — overshoot means the server
   wasn't frozen and every downstream timing assertion is worthless.
3. **Health is never the completion probe.** It's the behaviour under test.

---

## Files

```
tools/ai-e2e/pmk.py             RCON driver (stdlib only)
tools/ai-e2e/serve.sh           starts the server against ./run/
tools/ai-e2e/setup-config.py    patches a generated pumpkin.toml + verifies
tools/ai-e2e/README.md          quick start
tools/ai-e2e/SESSION-NOTES.md   this file
```

Design docs and the full multi-model review transcripts live outside the repo at
`~/programming/.hermes/plans/` (plans v1/v2/v3) and
`~/programming/.hermes/plans/pumpkin-panel/` (consensus + 56KB transcripts).

---

## Picking this up again

```bash
cd ~/programming/Pumpkin
git checkout wip/ai-e2e-test-harness
git submodule update --init --recursive
cargo build --release                      # ~14 min

cd tools/ai-e2e
./serve.sh                                 # generates run/pumpkin.toml, then Ctrl-C
./setup-config.py run/pumpkin.toml         # patch + verify
./serve.sh                                 # start for real
./pmk.py status                            # in another terminal
```

Then **connect a Minecraft Java 26.2 client to `localhost:25565`**
(`online_mode = false`, no Mojang auth). Everything unlocks: entities tick,
`/effect` works on you, real gameplay assertions become possible.

### Next steps, in order

1. **Connect a client** and re-run `./pmk.py poison-probe <YourName>`. It
   establishes a verified negative control (regeneration off, health stable
   across 20 stepped ticks) before applying poison, then records the damage
   cadence tick by tick. If health changes on *every* step, that confirms the
   player-freeze bug above.
2. **Confirm the poison floor**: health must stop at exactly 1.0 and survive
   further eligible ticks. Then wither, which must kill.
3. **Issue #3039** (tool durability resets on drop/pickup) — a good first real
   target. Deterministic, no tick timing, visible in the client *and* readable
   server-side. I traced `drop_item` (player.rs:4321), `on_player_collision`
   (item.rs:508), `can_stack_add_more`, `copy_with_count` (item_stack:540) and
   `write/read_item_stack` (:698/:720) and they all look correct — which is
   exactly why it needs live reproduction.
4. **Add azalea** for the client-belief side. Rust, supports MC 26.2 exactly
   (same as Pumpkin), offline mode, loopback only — so no launcher, no
   Microsoft account, no corporate-policy surface. Pin an exact commit SHA, not
   a branch. The valuable assertion is the **diff**: bot-believed state vs
   RCON-reported state. Disagreement = desync, a bug class no unit test reaches.
5. **Upstream, small and separate**: (a) publish actually-bound addresses,
   (b) gate player effect ticking on freeze. `CONTRIBUTING.md` fast-tracks agent
   PRs if you append `🤖🤖🤖` to the title.

### Verification status, honestly

`pmk.py` has **ad-hoc verification only, not a test suite.** 21 checks passed
against a fake in-process RCON server validating Valve-protocol framing
(NUL-NUL termination), `tick_barrier` overshoot-is-fatal and stuck-times-out,
config layout, and the no-`@s` rule. The live results at the top of this file
are the real proof. Not verified: anything needing a connected player.
