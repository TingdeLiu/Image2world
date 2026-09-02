# Building an Isaac Sim navigation map from a generated world

A world that ImageWorld generates can be driven into Isaac Sim and used as a
navigable environment. The pipeline for it already exists; this is how to run it
and what the output is worth.

The usual mistake is to think of the world as one asset. It is two, and they do
different jobs:

| | What it is | Who consumes it |
| --- | --- | --- |
| **Gaussian splat** | `.spz` / `.ply`, ~500k–2M splats | the camera — RGB sensors, perception |
| **Collision mesh** | `<n>-world.glb`, 60k faces in five classified parts | PhysX — collision, raycasts, the map |

The splat is what the scene *looks* like. The collision mesh is what a robot can
*hit*. The navigation map comes entirely from the mesh; the splat contributes
nothing to it and can be left out. Keeping them separate is not a workaround —
it is the same split NVIDIA describes for Marble scenes, and it is why hiding the
collision mesh in the viewport still leaves a scene a robot can collide with.

## Which Isaac Sim

Use **6.0.1**. Two reasons, both measured:

| | 5.1.0 (Kit 107.3, OpenUSD 24.05) | 6.0.1 (Kit 110, OpenUSD 25.11) |
| --- | --- | --- |
| Startup | crashes in `rtx.scenedb`; needs `--/app/vulkan=false` | fine on stock settings |
| Renders the splat | no — `ParticleField3DGaussianSplat` is an unknown prim | **yes, natively** |
| Navigation map | works | works, identical grid |

The navigation map itself does not care — 5.1 and 6.0 produce bit-identical
grids — so 5.1 remains a usable fallback if 6.0 misbehaves. Everything else
prefers 6.0.

## Or all of it at once

```bash
python scripts/isaac-sim/pipeline.py marble-office
python scripts/isaac-sim/pipeline.py --all --json report.json
```

```
    export  ->  map  ->  check  ->  nav2  ->  drive
```

Every stage below already existed, and every stage was documented — as a list of
commands for a person to type in order. Nothing ran the list, and two things
grew in the gap:

* `nav2_check.sh` had been unrunnable on **every Windows checkout** for as long
  as `core.autocrlf` had been on, because a shell script with CRLF line endings
  is not a shell script. The step that proves the map works in a real ROS stack
  was broken, and nothing was red.
* Recorded numbers drifted from what the commands produce. A nav2 result in this
  file named a goal that two later map fixes turned into an unknown cell; the
  run had never been repeated, so nothing said so.

`pipeline.py` runs the stages, checks each one's output rather than only its
exit code, and prints a line per stage. It shells out rather than importing,
because the stages genuinely do not share an interpreter — the exporter is
Node, `build_navmap.py` and `navigate.py` need Isaac Sim's Python,
`check_navmap.py` runs anywhere, and nav2 lives in WSL. That boundary is where
this repository's bugs live, so the runner crosses it the same way a person
would.

It finds Isaac Sim itself, detects whether WSL can see the repo (and reports
nav2 as *skipped* rather than failing if not), and — the part that matters —
**computes nav2's goal from the map** rather than remembering one, using nav2's
own costmap rule. A remembered goal is exactly what went stale.

Running it turned up five things that had been true for a while and invisible,
because a list of commands nobody executes cannot fail. All five are written up
in `scripts/isaac-sim/README.md`; the shape of them is worth the summary:

| | What was wrong | Why nothing caught it |
| --- | --- | --- |
| `PYTHONHOME` leaked into Isaac Sim's Python | every `map` and `drive` stage died in `re` | only happens when a *program* launches the stage; a shell has no such variable |
| exit 0 ≠ a usable map | a 0.02 m² grid reported as success | nothing read the output, only the status |
| the floor fell back to the ceiling | three worlds silently mapped nothing | the fallback was plausible and never measured |
| a refusal that exits 0 | the message printed and changed nothing | Kit catches `SystemExit` and shuts down cleanly |
| a failed build got checked anyway | `map:XX check:ok` against an hour-old map | the check read a file it had not watched being written |

| Flag | Meaning |
| --- | --- |
| `--all` | every world with a `scene.json` under `public/worlds/` |
| `--skip export,drive` | leave stages out; names are `export map check nav2 drive` |
| `--isaac <path>` | Isaac Sim root, if it is not found automatically |
| `--robot <name>` | which policy drives the map (default `spot`) |
| `--json <file>` | the whole report, per world and per stage |

## The three commands

### 1. Export the world as USD

```bash
npm run export:usd -- marble-office --no-splat --up-axis z
```

`--up-axis z` because PhysX assumes gravity along −Z. `--no-splat` keeps the
file at ~1.7 MB and costs nothing if you only want the map. Drop `--no-splat`
(~33 MB at the 500k LOD) when you want the scene to look like the real room.

Output: `public/worlds/<slug>/export/<slug>.usdz`.

### 2. Build the map

```bash
D:\isaacsim-6.0.1\python.bat scripts/isaac-sim/build_navmap.py \
  public/worlds/marble-office/export/marble-office.usdz \
  --out public/worlds/marble-office/export
```

Output, beside the `.usdz`:

* `<slug>-navmap.png` — white free, black obstacle, grey "no claim": either no
  floor there, or floor with no route to it
* `<slug>-navmap.yaml` — `map_server` format (`image`, `resolution`, `origin`,
  `negate`, `occupied_thresh`, `free_thresh`), so it drops straight into a ROS
  navigation stack
* `<slug>-navmap.json` — the numbers, plus which Isaac Sim version produced them

| Flag | Default | Meaning |
| --- | --- | --- |
| `--cell-size` | `0.05` | grid resolution, metres |
| `--robot-height` | `0.60` | clearance the robot needs above the floor |
| `--floor-tolerance` | `0.12` | how far above the floor still counts as floor |
| `--floor-z` | (unset) | height of the floor, for a collider that has no classified `floor` part |
| `--robot-radius` | `0.0` | grow obstacles by this radius before writing the map |
| `--spawn` | largest region | `X,Y` in world metres to flood fill from |
| `--no-reachability` | off | keep free cells with no route to the spawn |
| `--border` | `0.5` | metres of unknown margin around the grid |
| `--max-step` | `0.10` | a taller height change between neighbours becomes an obstacle; 0 keeps the map 2D |
| `--probe` | `box` | test a cell's column for clearance with a cell-wide box, or with one ray up its centre |

`--robot-radius` is off by default because nav2 inflates in its own costmap
layer and would do it twice. Set it for a planner that does not, or to see how
much of the room a given robot actually fits in.

`--floor-z` exists for one situation and should otherwise never be needed. The
floor height sets the start height of every ray in the script, so getting it
wrong is not a small error — it is every measurement. A collider classified into
`floor / wall / ceiling / ramp / elevated` says where its floor is;
`backend/tools/build_collider.py` is what produces one. A collider that is a
single unclassified `geometry_0` mesh does not, and `build_navmap.py` now
**refuses** rather than guessing.

That refusal replaced a guess, and the guess was the bounding box — whose
`GetMax()[2]` is the *ceiling*. Three worlds in this tree export an unclassified
collider, and for all three the old code put the floor at 0.94 to 0.97 of the
room height: 19.563 m in a 20.281 m world. Every downward ray then started 20 m
above the floor, travelled 0.6 m, and hit nothing. The maps came back with 0.02
to 3.16 m² walkable and the build exited 0. Guessing the *bottom* of the
bounding box instead was measured too, and is worse — 0.00 m² on both, because
the collider has stray geometry below the floor. Two guesses, both wrong in
opposite directions, is the argument for not guessing a third time.

Finding the number by hand is not hard, and needs no Isaac Sim — a floor is a
spike in the collider's height histogram. On `multiview-meetingroom` the world-z
of the collider's points spans −0.273 to 4.389 m, and the densest 0.155 m band
holds 2190 of 23769 points at **0.038 to 0.193 m**. That band is the floor, so
`--floor-z 0.15`:

```python
from pxr import Usd, UsdGeom
import numpy as np
stage = Usd.Stage.Open("<world>.usdz")
mesh = next(p for p in stage.Traverse()
            if "/Collider/" in str(p.GetPath()) and p.IsA(UsdGeom.Mesh))
points = np.array(UsdGeom.Mesh(mesh).GetPointsAttr().Get())
to_world = np.array(UsdGeom.Xformable(mesh).ComputeLocalToWorldTransform(
    Usd.TimeCode.Default())).reshape(4, 4)
z = (np.c_[points, np.ones(len(points))] @ to_world)[:, 2]
counts, edges = np.histogram(z, bins=30)
print(edges[counts.argmax()], edges[counts.argmax() + 1])
```

Note the transform: `GetPointsAttr()` returns *local* coordinates, and on this
world they span 17 m against the 4.66 m the room actually is.

`--border` is on by default because a grid that stops exactly at the collider's
bounding box has free cells in its last row, and both the inflation here and a
planner's own neighbourhood queries then run off the end of the array. Set it to
`0` to get the bare bounding box back.

`--max-step` used to default to `0`, on the argument that a step height belongs
to a particular robot and the map should stay a measurement of the room. That
argument does not survive contact with the rest of the table: `--robot-height`
and `--floor-tolerance` are robot-shaped numbers with non-zero defaults too, so
`--max-step 0` was not neutrality, it was one robot parameter singled out to be
absent. What it bought was a map in which a 16 cm rise reads as flat floor —
`navigate.py` walked Spot into exactly that, three runs running, on a route the
map said was clear. Worse, both maps in this repo were built with `--max-step
0.10` while the recipe above and this table said `0.0`, so the documented
command did not rebuild the shipped map.

It now defaults to `0.10`, which costs `marble-office` 423 cells (1.1 m²) —
fewer than the 1034 it cost before, because the box-shaped clearance probe
described in `scripts/isaac-sim/README.md` already catches most of a step's
cells on its own. Pass `--max-step 0` for a purely two-dimensional measurement
of the room. The command under **2. Build the map** now reproduces the maps this
page quotes, which it did not while the default and the shipped maps disagreed.

### 3. (Optional) get a PLY for anything that wants one

Marble worlds carry only `.spz`. Isaac Sim's neural renderer and most 3DGS
tooling read PLY:

```bash
npm run assets:ply -- marble-office --lod 500k
```

Round-trip is field-identical except quaternions at 1.19e-7 (float32 epsilon).

## What the map is actually made of

For each cell, a ray is cast straight down from the robot's head height. A hit
at or below `floor_top + tolerance` is floor; a hit above it is an obstacle; no
hit means there is no floor there. Cells that pass are then re-cast upward
through the robot's own height and demoted if anything is in the way.

The raw grid is then put through two passes, and the order between them is not
free. **Obstacles are grown by `--robot-radius` first**, because growing an
obstacle can close a doorway, and a region whose only way in is narrower than
the robot is not reachable. **Then a flood fill** from the spawn — 4-connected,
so a diagonal pinhole between two obstacle corners does not count as a gap —
drops every free cell with no route in. Dropped cells go grey, not white: the
map makes no claim about them rather than inviting a planner into a scrap of
floor it can never reach. With no `--spawn` given, the largest connected region
is taken as "the room", and the run reports whether the world origin, where the
viewer spawns a character, survived the fill.

The downward cast is the same query `isaacsim.asset.gen.omap` makes — that
extension is tried first and its result is printed for comparison, but it
returns contradictory output from a standalone script, so the raycast grid is
the one used. See `scripts/isaac-sim/README.md`.

Two things the map deliberately excludes:

* **`/World/GroundPlane`.** The export carries an infinite collision plane at
  z=0 so a robot spawned at the origin has something under it. Counting it as
  floor marks every cell in the bounding box walkable, including the ground
  outside the building — measured 155.8 m² "free" and zero no-floor cells,
  against the 87.3 / 69.0 the raycast grid gave in the same run.
* **The splat's extent.** 6.0 understands the splat prim and so includes it in
  `/Environment`'s bounding box, which reaches past the room (15.95 × 16.04 m
  against the collider's 12.38 × 13.84). The grid is bounded by the collision
  mesh instead.

## Every world in the tree, one command

`pipeline.py --all`, on this machine, against every world under
`public/worlds/`:

| World | export | map | check | nav2 | drive |
|---|---|---|---|---|---|
| `demo-office` | ok | ok | ok | ok | ok |
| `family-splatfacto-integration-jpjwgt` | ok | **fail** | -- | -- | -- |
| `marble-from-photo-7y19ti` | ok | ok | ok | ok | ok |
| `marble-office` | ok | ok | ok | ok | ok |
| `multiview-meetingroom` | ok | **fail** | -- | -- | -- |
| `room1-cbti0t` | ok | **fail** | -- | -- | -- |
| `singleview-meetingroom` | ok | **fail** | -- | -- | -- |
| `sunlit-office-k7hil8` | ok | **fail** | -- | -- | -- |

**3 of 8 pass every stage** — `demo-office`, `marble-from-photo-7y19ti`, `marble-office` — which is the export, the
occupancy map, the `map_server` check, a nav2 plan and a robot driving that
plan, from one command.

The rest stop at the first stage that can tell they are not a room, and
say which:

| World | Stage | What it reported |
|---|---|---|
| `family-splatfacto-integration-jpjwgt` | map | refused - the collider has no classified `floor` part |
| `multiview-meetingroom` | map | refused - the collider has no classified `floor` part |
| `room1-cbti0t` | map | unusable map: nothing walkable at the world origin (2.49 m2 free) |
| `singleview-meetingroom` | map | refused - the collider has no classified `floor` part |
| `sunlit-office-k7hil8` | map | unusable map: nothing walkable at the world origin (4.11 m2 free) |

None of those is the pipeline failing. Three of them export a single
unclassified `geometry_0` collider, so nothing in the file says where the
floor is, and the build refuses rather than guessing. The other two
reconstructed a floor too broken to stand on: `room1-cbti0t` has no floor
under 88% of its own footprint, and `sunlit-office-k7hil8` splits 12.7 m²
of floor into 172 disconnected pieces of which the largest is 4.1 m² and
does not contain the spawn. The map says so now instead of producing a
grid that looks plausible and is nonsense.

## What you get, measured

Both worlds, Isaac Sim 6.0.1, 5 cm cells, 0.6 m clearance:

| World | Room | Free | Obstacle | No floor | Free for a 0.25 m robot |
| --- | --- | --- | --- | --- | --- |
| `marble-office` | 12.38 × 13.84 × 2.94 m | 65.6 m² | 30.6 m² | 75.6 m² | 47.3 m² |
| `marble-from-photo-7y19ti` | 9.04 × 9.60 × 2.64 m | 16.2 m² | 13.3 m² | 57.4 m² | 9.4 m² |

Free is down and obstacle up against the figures this table used to carry (67.9
/ 27.1 and 16.8 / 12.1). Nothing about the rooms changed: the clearance check
stopped being a single ray up each cell's centre line and became a box the width
of the cell, which is the volume the cell stands for. See **A cell is a column,
not a centre line** in `scripts/isaac-sim/README.md` — on `marble-office` the ray
walked past 2004 cells with something standing in them, 5.0 m² the map used to
call open floor.

"No floor" is the reconstruction's holes only. The grid also carries a 0.5 m
`--border` of unknown cells — 27.3 m² on `marble-office` — which the run reports
separately, because the two mean opposite things: one is a measurement of the
reconstruction, the other is a margin we chose.

The reachability pass is worth less than it sounds like and the reason is a
compliment to the collider: `marble-office`'s raw grid has 72.0 m² of floor in
**189 disconnected regions**, but 188 of them add up to only 5.3 m². Marble's
reconstruction leaves very little unreachable scrap. On a SHARP world the same
pass would matter far more.

The last column is where a robot's *centre* can be, once obstacles and holes are
grown by 0.25 m. It costs `marble-office` 18.3 m², and it breaks the single
connected room the fill left into 12 pieces — the one holding the spawn keeps
47.3 m² of the 47.5 m² that survive the growing, so inflation strands very
little here. It swallows the small scraps whole rather than disconnecting big
ones.

Sanity checks that the geometry survived the trip:

* **The flood fill keeps the room the viewer drops you into.** The world origin,
  which is `scene.json`'s `spawnPoint`, survives the fill on both worlds. Had the
  fill latched onto some other blob, this is where it would show.
* **`--no-reachability` reproduces the raw grid exactly** — 29771 free
  cells, which is the `raw free was 30805` the same run prints, to the cell. The
  two passes are the only thing that changed; the grid underneath them did not
  move.
* **Three ways of seeding it agree.** The largest-region default, an explicit
  `--spawn 0,0`, and a deliberately off-map `--spawn 100,100` that falls back to
  the nearest free cell **no longer agree**, and the disagreement is the point.
  The largest-region default and `--spawn 0,0` both keep 26246 cells. The
  off-map spawn now falls back into a four-cell pocket in a far corner and keeps
  only that
  — because the corrected map has 189 disconnected regions where it used to have
  34, so "the nearest free cell" is a much worse guess than it was.
  The run says so on its own: `world origin reachable: False`. That flag exists
  for exactly this, and it caught it without anyone looking.

* A probe placed 1.371 m above the floor raycasts to it at 1.367 m — 4 mm.
* Walls at 1.26 / 5.35 / 4.25 / 6.92 m from the room centre, consistent with the
  measured footprint.
* Rigid bodies dropped from 1.97 m rest at 0.10–0.15 m, on the floor.
* The map's origin (−5.235) matches `0-world-walkable.json`, the project's own
  walkable-area tool, exactly.
* That tool reports 73.03 m² walkable against this map's 65.6 m². They now
  disagree by 11%, from opposite directions: this one assumes less headroom
  (0.6 m against 1.6 m) but measures it from the ground under each cell rather
  than from a single floor plane, and uses a 5 cm cell against that tool's
  15 cm. Before the clearance ray was fixed this map read 83.8 m², which was
  the more flattering number and the wrong one.

**Marble worlds are near-sealed**, which is what makes them usable at all: 2 to 5
of 72 horizontal directions escape at body height, against 8 of 24 for a SHARP
world. A single-view reconstruction is an open shell and a planner will route
straight out through the missing wall.

## Seeing the room instead of the mesh

Export with the splat and open it in 6.0:

```bash
npm run export:usd -- marble-office --up-axis z
```

`export_world_usd.py` already writes `UsdVol.ParticleField3DGaussianSplat`,
which is the format NVIDIA's own tooling now recommends — 3DGRUT's guidance is
that the NuRec USDZ path is deprecating in favour of ParticleField. No
conversion step is needed. Isaac Sim 6.0 reports **zero unknown prim types** for
this file.

Hide `/World/Background/Collider` to actually see the splat. With the collision
mesh visible the frame comes back nearly black (image mean 7/255 against 94)
because the collider wraps the splat. Hiding a prim does not disable its
collider, so the scene stays physical.

On 5.1 the same file opens cleanly, reports the splat as one unknown prim, and
skips it.

## Walking it yourself

The most direct check that the room is navigable is to walk it:

```bash
D:\isaacsim-6.0.1\python.bat scripts/isaac-sim/teleop.py   public/worlds/marble-office/export/marble-office-with-splat.usdz
```

W/S forward and back, A/D strafe, arrows or Q/E to turn, SHIFT to run, R to
respawn. Click the viewport first so it has keyboard focus.

Every step is decided by the same `raycast_closest` query the map is built from
— one cast down for the floor, two forward (shin and chest) for a wall — so a
place you can walk to is a place the map calls free. It spawns at the world
origin, which lands at z=0.004, the same floor height a dropped rigid body
settles at.

It is kinematic on purpose. This is a probe of the map, not a robot: it answers
"does the free space the grid claims actually exist" without a dynamics model in
the way. For a robot that really walks, with joints and foot contacts, see the
next section. Because the collision mesh is hidden and the walls still stop you,
this also demonstrates directly that hiding a prim does not disable its
collider.

## Putting a robot in it

A legged robot walks the room under its own locomotion policy — joints, foot
contacts, a learned controller — and is driven from the keyboard:

```bash
D:\isaacsim-6.0.1\python.bat scripts/isaac-sim/walk_robot.py   public/worlds/marble-office/export/marble-office-with-splat.usdz --robot spot
```

W/S forward and back, A/D strafe, arrows or Q/E to turn. Isaac Sim ships four:
`spot`, `anymal`, `go2` (quadrupeds) and `h1` (a humanoid biped). Only `spot`
has been measured here. `torch` is required — the CPU build is enough, these
policies are small MLPs.

**Take the physics rate from the policy, not from an example.** Spot's env
config declares `dt=0.002` and `decimation=10`: 500 Hz physics, 50 Hz control.
The standalone examples hardcode 1/200 and the interactive one uses 1/500, which
is the tell that the number is not incidental. At 1/200 the policy is close
enough to stumble forward under a firm command and too far off to hold itself
up, which is a nasty failure to diagnose because an automated forward walk looks
like it works:

| | Result |
| --- | --- |
| physics 1/200, zero command | collapses, z=0.196 |
| physics 1/200, 1.0 m/s | walks 1.2 m, then falls |
| the policy's own dt, zero command | **stands, z=0.462** |
| the policy's own dt, 1.0 m/s | 4.39 m on flat ground, upright throughout |

`walk_robot.py` reads `_dt` off the loaded policy and sets the physics rate from
it, so this is handled for whichever robot you pick. It has to be: Spot wants
500 Hz and the other three want 200 Hz, so a hardcoded 1/200 is right for three
of the four -- which is precisely why the Spot failure was so hard to place.

All four, walking forward for 10 s in `marble-office`:

| Robot | Policy dt | In `marble-office` | On NVIDIA's flat grid |
| --- | --- | --- | --- |
| `spot` (quadruped) | 0.002 / 500 Hz | 2.67 m, ends fallen | 4.39 m, upright |
| `anymal` (quadruped) | 0.005 / 200 Hz | 2.63 m, ends fallen at 0.24 m | not run |
| `h1` (humanoid biped) | 0.005 / 200 Hz | **2.33 m, upright throughout at 0.87 m** | not run |
| `go2` (quadruped) | 0.005 / 200 Hz | **0.01 m -- never stands** | 4.20 m, locomotes |

Size tracks success, and the obvious explanation does not hold. The two small
machines fail on the reconstructed floor while the three larger ones walk on it
-- Go2 covers 4.20 m on flat ground and 0.01 m here, the same shape as the
wheeled robot's failure below. But a 1600-point height scan over 2 x 2 m of that
floor measured a range of 0.000 m, so "small feet catch on rough geometry" does
not survive the measurement. The correlation is real; the cause is not known.

**The wheeled robot is still unresolved.** `drive_robot.py` spawns a Nova Carter
and drives it with a differential controller. It loads, falls under gravity,
settles at exactly the measured floor height, tracks commanded wheel velocity
precisely, and moves — but it will not drive straight across the room, tipping
or circling instead, while the same script on NVIDIA's flat grid drives 2.41 m
in 6 s. That was previously put down to contact between a small
differential-drive robot and a marching-cubes mesh floor. **That explanation no
longer holds**: a legged robot resolving foot contacts against the same floor
walks on it. The cause is open again. Either way the navigation map is
unaffected, being built from raycasts. `scripts/isaac-sim/README.md` has the
table of what was ruled out.

## Driving it: the loop closed

```bash
D:\isaacsim-6.0.1\python.bat scripts/isaac-sim/navigate.py   public/worlds/marble-office/export/marble-office.usdz   --map public/worlds/marble-office/export/marble-office-navmap.yaml
```

Plans on the map with the same `navmap` module `check_navmap.py` uses, then
drives a legged robot along the route under its own locomotion policy and
reports how far it strayed from the line it was given.

**Five of five goals reached across both worlds**, tracking the plan to
0.056–0.118 m on average and never worse than 0.303 m, every run finishing
upright at Spot's own standing height. Driven distance within 6% of planned.

This is also what found the two things nothing else could: a 16 cm step the map
called floor, and that planning nav2's way — holes passable, shortest route —
sends the robot along the edge of the reconstruction's holes at 0.07 m. Checking
a map against itself cannot find either, because the check reads the same grid.
See `scripts/isaac-sim/README.md` for both.

## Checking the map before a planner sees it

```bash
scripts/usd-export/.venv/Scripts/python scripts/isaac-sim/check_navmap.py   public/worlds/marble-office/export/marble-office-navmap.yaml --robot-radius 0.25
```

Loads the YAML and image by `map_server`'s own rules, rebuilds the
`OccupancyGrid` it would publish, checks the cell counts against what
`build_navmap` recorded, and plans a route across it. Both worlds pass: counts
identical cell for cell, and a route from the world origin to the furthest
reachable cell (10.66 m on `marble-office`, a 1.09x detour on a 9.80 m straight
line). Published through rclpy, the grid comes back byte-identical.

One result is worth stating on its own. With a 0.25 m robot radius, growing only
the obstacles leaves 51.51 m2 passable and a route plans; also growing the
no-floor cells leaves 49.09 m2 and **no route at all**. The walls are far enough
apart for a robot that size -- what breaks the map is the 76.7 m2 of holes in
the reconstruction. Inflating the two together, which is what
`build_navmap.py --robot-radius` does, would read as "the robot does not fit".

### nav2 plans on it

```bash
bash scripts/isaac-sim/nav2_check.sh <map>.yaml 2.04,8.72 0,0   # inside WSL
```

The real stack, not a stand-in: `nav2_map_server` publishing the grid,
`nav2_planner` running NavFn over a `nav2_costmap_2d` global costmap at
`robot_radius: 0.25` and `allow_unknown: false`, asked through the
`ComputePathToPose` action. `marble-office` plans 384 poses over 9.75 m to
(2.04, 8.72); `marble-from-photo-7y19ti` 166 poses over 4.21 m to (-0.85, 3.65).
Both log zero `worldToMap failed`. `map_server` republishes the grid to the last
digit of the origin `build_navmap.py` wrote.

Asked for a goal out in the no-floor region, nav2 refuses with an empty path, so
it is reading the map rather than drawing lines.

The goals are not the ones this page used to quote, and `scripts/isaac-sim/README.md`
has the story: the old goal (3.79, 9.02) is an unknown cell on today's map, and
the 407-pose result recorded against it came from a grid two fixes out of date.

This also settles the walls-versus-holes question above for a real stack: nav2's
9.75 m sits just above the obstacles-only figure over the same goal (9.55 m),
not the obstacles-and-holes one that had no path. Its inflation layer grows lethal obstacles, while unknown cells are
`NO_INFORMATION` and are simply not routed through. `scripts/isaac-sim/README.md`
has the rest, including the WSL DDS problem that had to be solved first.

## Limitations

* **No despeckle.** `build_walkable.py` closes pinholes and drops specks before
  flooding; this does not, so a stray occupied cell inside the room stays a
  one-cell island of obstacle. It costs little here — 3.5 m² of scrap on
  `marble-office` — but it is the reason this map still reads larger than that
  tool's.
* **One height threshold** separates floor from obstacle, so a gentle ramp reads
  as floor along its whole length.
* **The exporter never states a mesh collision approximation.** It applies
  `CollisionAPI` to the parent Xform, which PhysX does inherit down to the child
  meshes — raycasts and therefore the map are unaffected. Contact resolution is
  a different code path and was not validated.
