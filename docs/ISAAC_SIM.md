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
| `--robot-radius` | `0.0` | grow obstacles by this radius before writing the map |
| `--spawn` | largest region | `X,Y` in world metres to flood fill from |
| `--no-reachability` | off | keep free cells with no route to the spawn |

`--robot-radius` is off by default because nav2 inflates in its own costmap
layer and would do it twice. Set it for a planner that does not, or to see how
much of the room a given robot actually fits in.

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
  against the 87.3 / 69.0 the raycast grid gives before the reachability pass.
* **The splat's extent.** 6.0 understands the splat prim and so includes it in
  `/Environment`'s bounding box, which reaches past the room (15.95 × 16.04 m
  against the collider's 12.38 × 13.84). The grid is bounded by the collision
  mesh instead.

## What you get, measured

Both worlds, Isaac Sim 6.0.1, 5 cm cells, 0.6 m clearance:

| World | Room | Free | Obstacle | Unmapped | Free for a 0.25 m robot |
| --- | --- | --- | --- | --- | --- |
| `marble-office` | 12.38 × 13.84 × 2.94 m | 83.8 m² | 14.1 m² | 72.6 m² | 61.6 m² |
| `marble-from-photo-7y19ti` | 9.04 × 9.60 × 2.64 m | 20.3 m² | 10.0 m² | 55.7 m² | 11.7 m² |

The reachability pass is worth less than it sounds like and the reason is a
compliment to the collider: `marble-office`'s raw grid has 87.3 m² of floor in
**33 disconnected regions**, but 32 of them add up to only 3.5 m². Marble's
reconstruction leaves very little unreachable scrap. On a SHARP world the same
pass would matter far more.

The last column is where a robot's *centre* can be, once obstacles and holes are
grown by 0.25 m. It costs `marble-office` 24.6 m² and takes the region count
from 33 to 10, because inflation swallows the small scraps whole before the fill
ever sees them.

Sanity checks that the geometry survived the trip:

* **The flood fill keeps the room the viewer drops you into.** The world origin,
  which is `scene.json`'s `spawnPoint`, survives the fill on both worlds. Had the
  fill latched onto some other blob, this is where it would show.
* **`--no-reachability` reproduces the old numbers exactly** — 87.3 m² free,
  14.1 obstacle, 69.0 no-floor, to the cell. The two passes are the only thing
  that changed; the raycast grid underneath them did not move.
* **Three ways of seeding it agree.** The largest-region default, an explicit
  `--spawn 0,0`, and a deliberately off-map `--spawn 100,100` that falls back to
  the nearest free cell all keep the same 33504 cells on `marble-office`. The
  room is not an artefact of how the fill was started.

* A probe placed 1.371 m above the floor raycasts to it at 1.367 m — 4 mm.
* Walls at 1.26 / 5.35 / 4.25 / 6.92 m from the room centre, consistent with the
  measured footprint.
* Rigid bodies dropped from 1.97 m rest at 0.10–0.15 m, on the floor.
* The map's origin (−5.235) matches `0-world-walkable.json`, the project's own
  walkable-area tool, exactly.
* That tool reports 73.03 m² walkable against this map's 83.8 m². Both now
  flood-fill, so the remaining gap is headroom — it assumes 1.6 m against this
  map's 0.6 m — plus its coarser 15 cm cell and its despeckle pass. Before the
  reachability pass this map read 87.3 m², so the fill closed a quarter of the
  difference and the rest is accounted for.

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
it, so this is handled for whichever robot you pick.

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
