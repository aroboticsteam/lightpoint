# Light Tracker

Moving heads follow a point picked on a map. Sits between the lighting board and the fixtures:

```
FLX S48 --Art-Net--> [light tracker] --Art-Net--> node(s)/fixtures
                          ^
                  web pages over a websocket
```

Every head not being followed is passed through untouched. A followed head has only its pan, fine pan, tilt, fine
tilt (and pan/tilt speed, where the mode has one) replaced; colour, dimmer, gobos etc. stay under the board's
control. "Full control" additionally forces the light on and white (dimmer, strobe open, colour mix reset). Each
head's control/reset channel is never written.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/python -m tracker --web-host 0.0.0.0     # http://<this-machine>:8080
```

## Docker

```bash
docker build -t lightpoint .
docker run --rm --network host -v "$PWD/config:/config:ro" lightpoint
```

- `--network host` is needed so the console's broadcast Art-Net discovery (ArtPoll) reaches the tracker. The
  tracker answers it with an ArtPollReply advertising `input.universe`, which is what lets a console in
  *Unicast* transmit mode (e.g. Zero 88 FLX S) find it and send ArtDmx straight to this machine.
- Environment: `TRACKER_CONFIG` (default `/config/fixtures.json`), `WEB_HOST` (`0.0.0.0`), `WEB_PORT` (`8080`).
- CI (`.github/workflows/docker.yml`) runs the tests, builds amd64 and arm64 natively (matrix, same scheme as vex-tm-tools) and publishes
  `ghcr.io/aroboticsteam/lightpoint` as one multi-arch manifest for amd64 and
  arm64 (and mirrors to `registry.kmnet.uk/lightpoint` when the `KMNET_REGISTRY_*` secrets are set).

## Using it

- **`/op/p1` ... `/op/p4`** (Person 1-4): each person's own page. At the top is a row with every light. Tap a light
  to take it (it starts following your target) and tap it again to let it go. Letting go returns the light to the board's *latest* values, even ones the board sent
  while you were holding it: pan/tilt ease there at the slew limit, everything else snaps back. If you close your
  page, your lights are released the same way after `disconnect_release_s` (default 15 s, so a reload keeps them;
  negative disables) and your target is cleared. Lights
  held by someone else are greyed out and say who has them. Click or drag on the plan to aim. Per light you can pick
  *Hold (board)*, *Follow* or *Full control*. There is no login.
- **Dynamic mode** (tick *Dynamic* on your page): pick the lights you might want, then only the *closest* N to your pointer
  (default 4; choose 1-8) follow it. As you move the pointer the set changes by itself, so a light further away drops out
  and a nearer one takes over. The next *ready* lights in line (default 1; 0-4) are pre-aimed at the pointer so they are
  in position when needed. Lights that follow are **on** (the profile's full-control values); lights that are pre-aiming, and
  every other light of yours, which points straight down, are **off** (dimmer to 0). With no target set, all of them point
  down and are off. A light has to be `hysteresis` closer (0.75 m, config `dynamic_hysteresis_m`)
  to take a place from one that already has it, so two almost equally near lights do not flicker in and out. Lights set
  to *Hold (board)* are not part of the pool. On the plan, parked lights are dimmed with a down arrow and ready lights
  have a dashed ring. *Min from back bar* (default 0; 0-4) keeps at least that many of the spots for lights tagged
  `"group": "back"` in the config, even when other lights are closer: the closest back lights take the places of the furthest
  front ones (never more than the spots, or than the back lights you hold). *Min from front bar* does the same for lights tagged
  `"group": "front"`; when both are set, back is filled first and front gets the spots that are left, and neither is
  taken from the other's minimum. Command: `{"type": "dynamic", "op": "p1", "on": true, "count": 4, "prepare": 1,
  "back_min": 1, "front_min": 1}` (any field optional).
- **`/`** overview: who holds what (an operator can reassign or free any light), "Release every head", and
  calibration.
- Everything is live: pages talk over `/ws`, send commands, and receive the full state whenever anything changes
  (checked 20x a second and straight after each command). Selections are not saved: after a restart every light is
  free.
- `POST /api/command` accepts the same JSON commands as the websocket (see `tracker/commands.py`), and
  `GET /api/state` returns the current state, for scripts.

## Configure: `config/fixtures.json`

That file is git-ignored (it holds your site's positions and calibration). Start with
`cp config/fixtures.example.json config/fixtures.json`; the tests use the example.


- `input` / `output`: Art-Net port-addresses are 0-based (a desk's "universe 1" is usually `0` here). The board sends
  to `input.universe`; the merged result goes to `output.host` on `output.universe`. Output defaults to
  `127.0.0.1`, so nothing reaches real fixtures until you change it. Input and output must differ in universe
  (or host) or the proxy would hear itself.
- `disconnect_release_s`: grace period before a closed operator page releases its lights.
- `operators`: `{id, name, fixtures}`. Add or rename people here; `fixtures` is an optional starting selection
  (empty by default). Optional `dynamic` (start in dynamic mode), `dynamic_count` (4), `dynamic_prepare` (1), `dynamic_back_min` (0) and `dynamic_front_min` (0).
- `dynamic_hysteresis_m` (default 0.75): see *Dynamic mode*.
- `fixtures`: `pos_magicq` is `[x, y = HEIGHT, z = depth]` as in the MagicQ patch, origin at the front-left corner.
  Optional per head: `group` (`"back"` / `"front"` mark the back / front bar for dynamic mode's *min from back / front bar*), `reach` (default 1; dynamic mode divides a light's distance to the pointer by it, so `1.35` makes it
  count as 1.35x closer and get picked more often), `pan_range`, `tilt_range`, `pan_invert`, `tilt_invert`, `max_slew_deg_s` (default 0 = no software cap: the head follows the pointer instantly and only its own motors limit the speed; set e.g. 60 to cap tracking speed). Handing a head back to the board always eases at 90 deg/s.
  `orientation` (3x3) is written by calibration.
- Profiles live in `tracker/fixtures.py`, from the manufacturer charts: Rogue R2X Wash 17ch, Azor S2 28CH Extended,
  Azor SP2 43CH Extended.

## Calibrate (do this before trusting the aim)

Until a head is calibrated it assumes it is hung from the truss with pan 0 facing into the hall, and will point the
wrong way. On the overview page, for each head: aim it from the board at a known floor mark, click that mark on the
plan, press **Record**; repeat for 3-4 spread-out marks, then **Solve**. The fit is stored in `fixtures.json`.
Pan/tilt **invert** and the **pan range** (540 vs 630 on the Azors) are not solved: set them to match the fixture.

## Not verified yet / known gaps

- Nothing has been run against real fixtures or the FLX S48. The Art-Net path is tested over loopback only.
- The proxy now answers ArtPoll with an ArtPollReply (one DMX output port on `input.universe`), so a console in
  Unicast mode can discover it. This is built from the Art-Net spec and tested over loopback only: whether the FLX S48
  accepts it, and the universe numbering offset (desk universe 1 may be Art-Net universe 0 or 1), are untested.
- Invert flags are copied from the MagicQ fixture schedule (P Invert on all Azors, T Invert on R1); whether MagicQ's
  sense matches `pan_invert`/`tilt_invert` here is untested. Pan range is assumed to be the profile default.
- "Full control" values (white, strobe open) come from the DMX charts but are untested on the fixtures.
- `hall` is 17.65 m wide (X) by 34.19 m deep (Y), assigned by fit (heads reach 19.2 m deep); swap if wrong.
- Calibration samples are held in memory until **Solve**; only the result is saved.
