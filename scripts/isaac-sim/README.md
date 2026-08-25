# Isaac Sim navigation maps

Turns a generated world into a 2D occupancy grid a navigation stack can plan
on, by loading the USD export into Isaac Sim and asking PhysX where the floor
is. Verified on 2026-08-22 against Isaac Sim **6.0.1** and **5.1.0**, which
produce identical grids.

## Which Isaac Sim to use

Prefer **6.0.1** (Kit 110, OpenUSD 25.11). It renders the Gaussian splat, and
it does not have the startup crash 5.1 has here. 5.1 still works for the
navigation map, which needs only the collision mesh.

Run the script with the interpreter of whichever install you mean:

```bash
D:\isaacsim-6.0.1\python.bat scripts/isaac-sim/build_navmap.py <usdz> --out <dir>
```

The script reads `ISAAC_PATH` to tell the versions apart, and applies the
`--/app/vulkan=false` workaround only below 6.0. The version it ran under is
recorded in the `-navmap.json` it writes.

## Seeing the splat (6.0 only)

Isaac Sim 6.0 resolves `ParticleField3DGaussianSplat`, which is what
`export_world_usd.py` already writes, so an export made *with* the splat renders
as-is — no 3DGRUT step, no NuRec container:

```bash
npm run export:usd -- marble-office --up-axis z
```

Hide `/World/Background/Collider` to actually see it. With the collision mesh
visible the frame comes back nearly black (image mean 7/255 against 94 with it
hidden) because the collider wraps the splat.

On 5.1 the same file opens without error, reports the splat prim as an unknown
type, and skips it — one unknown prim, everything else intact.

## What was established

Marble worlds import as a navigable collision scene with **no change to the
exporter**. The measurements below are from `marble-office`:

| Question | Answer |
| --- | --- |
| Does the export open in Isaac Sim? | Yes — Isaac Sim's OpenUSD 24.05 parses it with **0 unknown prim types** |
| Is it metrically correct? | Yes — floor hit 1.367 m below a probe placed 1.371 m above it |
| Does PhysX collide with it? | Yes — walls hit at 1.26 / 5.35 / 4.25 / 6.92 m in a 12.38 × 13.84 × 2.94 m room |
| Is the room enclosed? | Nearly — 2 to 5 of 72 horizontal directions escape, versus 8 of 24 for a SHARP world |
| Usable free area | 83.8 m² reachable free, 14.1 m² obstacle at 5 cm resolution — 87.3 m² before the reachability pass |

The splat must be left out (`--no-splat`): `ParticleField3DGaussianSplat`
needs OpenUSD 26.03 and Isaac Sim 5.1 ships 24.05. See `../usd-export/README.md`
for the wider support table.

### CollisionAPI on the parent Xform is enough

`export_world_usd.py` applies `UsdPhysics.CollisionAPI` once, to the
`/World/Background/Collider` Xform, rather than to each of the five classified
child meshes. That was the main thing worth checking, because a collider that
silently does not exist looks exactly like an empty room.

It works. The same scene was run three ways — as exported, with per-mesh
`CollisionAPI`, and with per-mesh `MeshCollisionAPI(approximation="none")` — and
all three produce identical raycast hits. The only difference is that per-mesh
colliders report the hit against `/Collider/floor` rather than `/Collider`,
which is useful if you want the surface class of what you hit and irrelevant
otherwise.

## Usage

First export the world for a Z-up physics consumer:

```bash
npm run export:usd -- marble-office --no-splat --up-axis z
```

Then build the map with Isaac Sim's own interpreter:

```bash
C:\isaacsim\python.bat scripts/isaac-sim/build_navmap.py public/worlds/marble-office/export/marble-office.usdz --out public/worlds/marble-office/export
```

Output is a `<slug>-navmap.png` plus a `<slug>-navmap.yaml` in the
`map_server` format (`image`, `resolution`, `origin`, `negate`,
`occupied_thresh`, `free_thresh`), so it drops straight into a ROS navigation
stack. White is free, black is obstacle, grey is "no floor here".

| Flag | Default | Meaning |
| --- | --- | --- |
| `--cell-size` | `0.05` | grid resolution in metres |
| `--robot-height` | `0.60` | clearance the robot needs above the floor |
| `--floor-tolerance` | `0.12` | how far above the floor still counts as floor |
| `--robot-radius` | `0.0` | grow obstacles by this radius before writing the map |
| `--spawn` | largest region | `X,Y` in world metres to flood fill from |
| `--no-reachability` | off | keep free cells with no route to the spawn |

### Two passes turn "there is floor" into "a robot could be here"

Inflation runs **before** the flood fill, and the order is not free: growing an
obstacle can close a doorway, and a region whose only way in is narrower than
the robot is not reachable. Flooding first would keep it.

The fill is 4-connected, so a diagonal pinhole between two obstacle corners is
not mistaken for a gap. Cells it drops go grey rather than white — the map makes
no claim about them instead of inviting a planner onto a scrap of floor with no
route in.

`--robot-radius` defaults to 0 because nav2 inflates in its own costmap layer
and would otherwise do it twice.

With no `--spawn`, the largest connected region is taken as "the room". Each run
also reports whether the world origin — where the viewer spawns a character —
survived the fill, which is a free check that the fill kept the right room.

## Isaac Sim crashes at startup here — use `--/app/vulkan=false`

Every Isaac Sim experience sets `vulkan = true` (`apps/isaacsim.exp.base.kit`
line 216, `isaacsim.exp.full.kit` line 155, and so on). On this machine the
Vulkan backend takes an access violation inside `rtx.scenedb.plugin.dll` during
startup, *before any stage loads* — a bare `SimulationApp({"headless": True})`
with no asset at all crashes identically, so a broken-looking world is not the
cause. Forcing the D3D12 backend fixes it:

```bash
C:\isaacsim\isaac-sim.bat --/app/vulkan=false
```

`isaac-sim.bat` passes its arguments straight through to `kit.exe`, and
`build_navmap.py` sets the flag by default.

Measured on an empty app: **3/3 survive with `--/app/vulkan=false`, 2/2 crash
with `--/app/vulkan=true`.** Checked separately against
`isaacsim.exp.full.kit`, the experience the GUI launcher uses — OK with the flag,
`rtx.scenedb` access violation without it — so this is the app launch, not just
the Python path. Nothing else moved the needle; the whole matrix was tried and
every other variant crashed:

| Variant | Result |
| --- | --- |
| `--/app/vulkan=false` | **OK** |
| baseline | crash |
| `--/renderer/asyncInit=false` | crash |
| `--/rtx/hydra/mdlMaterialWarmup=false` | crash |
| both of the above | crash |
| `--/renderer/activeGpu=0 --/renderer/multiGpu/enabled=false --/renderer/multiGpu/maxGpuCount=1` | crash |
| `--/rtx/aftermath/enabled=false` | crash |
| DLSS off | crash |
| clearing `%LOCALAPPDATA%\ov\cache\shaders` | crash |

Machine: RTX 5060 Ti (16 GB) plus an Intel iGPU, driver 610.88, Isaac Sim
5.1.0-rc.19, Kit 107.3.3. The driver is well past the 537.58 minimum Isaac Sim's
own compatibility check asks for, and that check lists no unsupported range, so
this is not a driver version the vendor flags.

`imageworld.omap.kit` remains as a second escape hatch (`--no-renderer`): a Kit
experience that never depends on `omni.hydra.rtx` at all, which starts in 1.3 s
instead of 45. It is no longer needed for the crash, but it is a fast way to run
physics-only work.

## Walking the map on the keyboard (`teleop.py`)

```bash
D:\isaacsim-6.0.1\python.bat scripts/isaac-sim/teleop.py public/worlds/marble-office/export/marble-office-with-splat.usdz
```

W/S forward and back, A/D strafe, arrow keys or Q/E to turn, SHIFT to run, R to
respawn. Close the window to quit.

This is the navigation map with a person in it. Every step is decided by the
same `raycast_closest` query `build_navmap.py` grids the room with — one cast
down to find the floor, two forward (shin and chest) to find a wall — so a place
you can walk to is a place the map calls free, and the two cannot drift apart.
It is the most direct check of the whole question there is: if the room is
navigable, you can walk it.

**Kinematic on purpose.** A PhysX capsule would drag in the contact behaviour
that stops `drive_robot.py` driving straight over a marching-cubes floor, and
that has nothing to do with whether the room is navigable. Raycasts hit the real
triangles; contact resolution is the part that does not work yet.

The same `GroundPlane` exclusion the map uses applies here, and for the same
reason — without it you walk straight out of the building on an infinite plane
the reconstruction never saw. The collision mesh is hidden so the splat is what
you see, which also demonstrates in the most direct way available that hiding a
prim does not disable its collider: the walls still stop you.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--eye-height` | `1.60` | camera height above the floor, metres |
| `--speed` | `1.4` | walking speed, m/s |
| `--body-radius` | `0.30` | how close to a wall you may get |
| `--step-up` | `0.45` | the biggest rise or drop a step may cross |
| `--show-collider` | off | look at the collision mesh instead of the splat |

## Walking a legged robot in the world (`walk_robot.py`)

```bash
D:\isaacsim-6.0.1\python.bat scripts/isaac-sim/walk_robot.py public/worlds/marble-office/export/marble-office-with-splat.usdz --robot spot
```

A real articulation under a learned locomotion policy: joints, foot contacts, a
centre of mass. W/S forward and back, A/D strafe, arrows or Q/E to turn. Isaac
Sim ships `spot`, `anymal`, `go2` and `h1`; only `spot` has been measured here.
`--auto N` walks forward for N seconds and writes a report instead of reading
keys, `--flat-ground` is the control run.

`torch` is a hard requirement (`PolicyController.load_policy` calls
`torch.jit.load`) and is not installed by default. The CPU build is enough:

```bash
D:\isaacsim-6.0.1\python.bat -m pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### Take the physics rate from the policy

Spot's env config declares `dt=0.002` and `decimation=10` — 500 Hz physics,
50 Hz control. The standalone examples hardcode 1/200 and the interactive one
uses 1/500, and that disagreement is the tell. At 1/200 the policy is close
enough to stumble forward under a firm command and too far off to stand:

| | Result |
| --- | --- |
| physics 1/200, zero command | collapses, z=0.196 |
| physics 1/200, 1.0 m/s forward | walks 1.2 m, then falls |
| the policy's own dt, zero command | **stands, z=0.462** |
| the policy's own dt, 1.0 m/s forward | 4.39 m on flat ground, upright throughout |

The script reads `_dt` off the loaded policy and sets the physics rate from it.

This one cost a long detour, because at 1/200 the system is marginal and
marginal systems manufacture convincing false leads — scene-setup order, the
moment the command is first assigned, a missing `post_reset()`. Each looked
causal, each reproduced or vanished once, none was it. What cut through was a
bisect against the vendor script (moving one assignment out of their loop
reproduced the failure in *their* file, to three decimals) and then reading the
policy's own config instead of another example.

**carb swallows exceptions raised in a physics callback.** A policy that is
running but cannot stand and a callback that died silently look identical — both
just lie at z=0.196. The callback now counts its own steps and reports its own
exceptions; without that, every downstream conclusion is a guess between two
indistinguishable states.

## Driving a wheeled robot in the world (`drive_robot.py`)

Puts a Nova Carter in the scene and drives it with a differential controller,
the way NVIDIA's Marble-to-Isaac-Sim writeup does. Isaac Sim 6.0 only — 6.0's
wheeled-robot API is `isaacsim.core.experimental`.

```bash
D:\isaacsim-6.0.1\python.bat scripts/isaac-sim/drive_robot.py public/worlds/marble-office/export/marble-office-with-splat.usdz --out /tmp/out --headless
```

**What works.** The robot loads with its full sensor suite, spawns above the
floor, falls under gravity and settles at exactly the measured floor height
(0.254 → 0.004 m), its wheel joints are found automatically, and the wheels
track commanded velocity precisely — the right wheel holds 6.98 rad/s against a
commanded 6.98. It moves: 0.48–0.76 m in the runs recorded here.

**What does not.** It will not drive in a straight line across the room. It
tips or ends up circling with one wheel off the ground. The same script on
NVIDIA's flat grid (`--flat-ground`) drives 2.41 m in 6 s at exactly the
commanded 0.40 m/s, dead level.

This was previously written down as contact behaviour between a small
differential-drive robot and a marching-cubes mesh floor. **That explanation is
retracted**: `walk_robot.py` puts a legged robot on the same floor, resolving
foot contacts against the same triangles, and it walks. Whatever is wrong is
specific to this wheeled setup. Unresolved, and the navigation map does not
depend on it.

Things ruled out along the way, each with a measurement rather than a guess:

| Suspected | Test | Result |
| --- | --- | --- |
| Physics not cooked yet | move probe after `reset()` | no change |
| Hiding the collider disables it | `--show-collider` | fails the same way |
| Mirrored wheel joints | drive with both sign conventions | wheel speeds identical |
| Floor too rough to drive on | 1600-point height scan over 2×2 m | range **0.000 m**, dead flat |
| The export's infinite `GroundPlane` | `--no-ground-plane` | still stuck, wheels then free-spin |
| Convex-hull collision | `--trimesh-collider` (`approximation="none"`) | worse — robot pushed up 0.22 m |

Two real bugs did come out of it, both from comparing against NVIDIA's own
examples rather than from more guessing:

* **The robot must be constructed while the timeline is stopped.** Built after
  `play()`, its root has no dynamics: it hangs in the air ignoring gravity while
  drifting sideways, wheels spinning freely. This is why the script plays once
  to probe the floor, stops, builds the robot, then plays again.
* **Nova Carter's wheel radius is 0.04295 m, not the ~0.14 m the wheel looks.**
  The articulation carries a gear ratio. Guessing from the geometry makes every
  command 3× off. Taken from
  `standalone_examples/api/isaacsim.sensors.experimental.rtx/lidar_robot_integration.py`.

**One gap this exposed in the exporter**, worth knowing even though forcing it
did not fix the driving: `export_world_usd.py` applies `CollisionAPI` to the
parent Xform and never states a `MeshCollisionAPI` approximation. Raycasts hit
the real triangles either way — which is why the navigation map is unaffected and
why an earlier A/B of the three collider variants found them identical. Contact
resolution is a different code path, and that A/B could not see it.

## Two smaller traps

**A minimal Kit app does not attach the stage to PhysX.** Under `--no-renderer`,
without `omni.physx.get_physx_simulation_interface().attach_stage(stage_id)`
every query fails with `No USD stage attached`. Note `attach_stage` lives on
`IPhysxSimulation`, not on the `PhysX` interface returned by
`get_physx_interface()`. Do *not* call it under the stock experience, which
wires the stage up itself.

**`isaacsim.asset.gen.omap` contradicts itself from a standalone script.** It
reports the right grid dimensions, but `get_buffer()` comes back all-free (every
value `0.0`) while `get_occupied_positions()` returns *every* cell and
`get_free_positions()` returns none — including cells outside the room, where
there is no geometry at all. Same result under the stock experience and the
minimal one, at five different scan heights, with bounds passed both absolute
and relative to the room centre. Meanwhile a direct PhysX raycast in the same
process finds the floor 1.367 m below a probe 1.371 m above it, so the colliders
are certainly there.

Unresolved — the extension wants something this standalone flow does not set up.
The map is therefore built from
`get_physx_scene_query_interface().raycast_closest` per cell, which is the same
query the extension makes, one cell at a time. Both routes are printed so the
discrepancy stays visible rather than quietly resolved in favour of whichever
answer looks nicer.

## The ground plane has to be excluded from the map

`/World/GroundPlane` is an infinite collision plane on z=0, there so a robot
spawned at the origin has something under it. A downward raycast therefore hits
it wherever the reconstruction has no floor — and if you count that as floor,
every cell in the bounding box comes back walkable, including the ground outside
the building. Measured on `marble-office`: 155.8 m² "free" and **zero** cells
marked no-floor, against the 87.3 m² free / 69.0 m² no-floor the raw raycast
grid gives.

`build_navmap.py` drops hits whose collision prim ends in `GroundPlane`. This
only started mattering once the plane was moved to the origin — before that it
floated 1.46 m up, above the cast origin, and was never hit.

Moving it also made `isaacsim.asset.gen.omap` start returning non-zero occupied
counts, having reported an all-free grid until then. Plausibly the misplaced
plane cut through the scan volume at chest height; not chased further, since the
raycast grid is the one that is checked.

## Known limitations

- No despeckle. `build_walkable.py` closes pinholes and drops specks before it
  floods; this does not, so a stray occupied cell inside the room stays a
  one-cell island. Its 83.8 m² against that tool's 73.03 m² walkable is now
  mostly headroom (0.6 m against 1.6 m) plus its coarser 15 cm cell and that
  despeckle pass.
- The floor/obstacle split is a single height threshold (12 cm above the floor
  plane), so a gentle ramp reads as floor along its whole length.

## Reachability and inflation, measured

On `marble-office` the raw grid has 87.3 m² of floor spread over **33
disconnected regions** — but 32 of them come to 3.5 m² between them. Marble
leaves very little unreachable scrap, which is a compliment to the
reconstruction and the reason this pass buys less here than it would on a SHARP
world.

| World | Raw free | Regions | Dropped | Reachable | With `--robot-radius 0.25` |
| --- | --- | --- | --- | --- | --- |
| `marble-office` | 87.3 m² | 33 | 3.5 m² | 83.8 m² | 61.6 m² |
| `marble-from-photo-7y19ti` | 20.4 m² | 17 | 0.1 m² | 20.3 m² | 11.7 m² |

Inflating by 0.25 m costs `marble-office` 24.6 m² and takes it from 33 regions
to 10: grown obstacles swallow the small scraps outright before the fill ever
looks at them.

**The fill keeps the right room.** Both worlds report the world origin — which
is `scene.json`'s `spawnPoint`, where the viewer drops a character — inside the
kept region. That is the check that the fill latched onto the room rather than
onto some other blob, and it costs nothing to run.

Running with `--no-reachability` gives back 34922 / 5644 / 27606 cells — the
same 87.3 / 14.1 / 69.0 m² measured before either pass existed, to the cell. The
filters are additive; nothing under them moved.

Seeding it three different ways lands on the same region, which is a stronger
statement than any one of them: the largest-region default, `--spawn 0,0`, and
an off-map `--spawn 100,100` that falls back to the nearest free cell all keep
33504 cells on `marble-office`.
