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
| Usable free area | 67.9 m² reachable free, 27.1 m² obstacle at 5 cm resolution — 77.0 m² before the reachability pass |
| Can a robot drive it? | Yes — Spot reaches 4 of 4 spread-out goals autonomously, tracking the plan to 0.07–0.12 m |

The splat must be left out (`--no-splat`): `ParticleField3DGaussianSplat`
needs OpenUSD 26.03 and Isaac Sim 5.1 ships 24.05. See `../usd-export/README.md`
for the wider support table.

### CollisionAPI on the parent Xform is enough, but the approximation is stated

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

The exporter now authors `MeshCollisionAPI` with `approximation = none` on each
of those meshes anyway. Not because the behaviour changes — it does not, and
re-exporting moved `raw free` by only the 27 cells the `ceil` fix added — but
because that behaviour was coming from a schema default rather than from
anything we asked for, and the default is load-bearing: a convex hull would
swallow the doorways, the gaps under furniture and the holes the reconstruction
left, and the map would come back as one solid slab of floor. A test asserts it,
and also asserts the background is not a rigid body, since a triangle mesh can
only be static and PhysX quietly substitutes a hull otherwise.

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
| `--border` | `0.5` | metres of unknown margin around the grid |
| `--max-step` | `0.0` | a taller height change between neighbours becomes an obstacle; 0 keeps the map 2D |

### The clearance ray has to start from the ground under *that* cell

The second pass casts upward through the robot's own height to demote a cell
that has floor but no room above it. It used to start at `floor_top + 0.05`,
and `floor_top` is the highest vertex of the whole floor mesh — 0.371 m on
`marble-office`, against ground that is actually at 0.109 m along the route the
robot drives. Everything in the 0.26 m between them was invisible to both
passes: too low for the upward ray, and not the first thing the downward ray
met.

A 16 cm rise at (0.95, 6.21) sat in that band and was written to the map as
clear floor. `navigate.py` walked Spot into it and stopped, three runs running,
each time on a route the map said had 0.90 m of clearance and which the
controller tracked to 3 cm. **No amount of checking the map against itself
would have found this** — `check_navmap.py` and nav2 both agreed with it,
because they were reading the same wrong grid.

Starting the ray from the cell's own measured ground took `marble-office` from
14.1 m² of obstacle to 27.1 m², which for an office with furniture in it is the
more believable of the two numbers.

The offset is `--floor-tolerance`, which already means "this much above the
floor still counts as floor", so by definition nothing inside it obstructs. It
cannot be much smaller: the collision mesh is a shell with thickness, and this
floor's two sheets sit 10 cm apart, so a 5 cm offset makes every cell that saw
through a gap in the upper sheet collide with its own underside — measured, and
it turned 7091 obstacle cells into 38503.

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

`--border` is on by default because a grid that stops exactly at the collider's
bounding box has free cells in its last row, and both the inflation here and a
planner's own neighbourhood queries then run off the end of the array. Set it to
`0` to get the bare bounding box back.

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

## Checking the map the way a nav stack will read it (`check_navmap.py`)

```bash
scripts/usd-export/.venv/Scripts/python scripts/isaac-sim/check_navmap.py   public/worlds/marble-office/export/marble-office-navmap.yaml --robot-radius 0.25
```

Writing a `map_server` YAML is not the same as writing one a planner can use,
and the parts most likely to be wrong are the boring ones: which corner the
origin names, which way the image rows run, and whether the thresholds put our
three greys in the buckets we meant. A sign error in the row order is invisible
in the picture -- it just silently mirrors the room.

So this loads the pair by `map_server`'s own rules, rebuilds the `OccupancyGrid`
it would publish, cross-checks the cell counts against the JSON `build_navmap`
wrote, and plans an 8-connected route across it. Plain Python; `--ros`
additionally publishes through rclpy and reads the message back, and pins DDS to
a multicast-capable interface itself, so it no longer needs `nav2_check.sh` to
have set that up first (see the DDS section below for why WSL needs it).

It exits non-zero when the **obstacles alone** leave no route. The
obstacles-and-holes scenario prints as a note instead, because those are
different findings and only the first is about the map: the second is the
reconstruction's holes, which are always there, and which nav2 does not inflate
around anyway.

Measured on both worlds:

| | marble-office | marble-from-photo-7y19ti |
| --- | --- | --- |
| Grid | 268 x 297 @ 5 cm | 201 x 212 @ 5 cm |
| Free | 67.92 m2 | 16.82 m2 |
| Counts vs `build_navmap` | identical, cell for cell | identical |
| Planned route | 10.42 m, straight line 9.63 (detour x1.08) | 5.45 m, straight 5.41 (x1.01) |

Both grids include the 0.5 m unknown border, which is why they are 20 cells
wider than the collider's bounding box in each direction.

The route starts at the world origin, which lands in cell (-0.01, 0.02) -- the
origin convention survives the trip into ROS coordinates.

### Walls or holes: the inflation result worth knowing

With `--robot-radius` the check runs twice, growing the obstacles alone and then
growing the unknown cells too, because the difference says something different
about the world:

| marble-office, r=0.25 m | Passable | Route |
| --- | --- | --- |
| obstacles only | 51.51 m2 | 10.60 m, plans fine |
| obstacles and holes | 49.09 m2 | **no path -- not connected** |

So the walls are far enough apart for a Nova Carter-sized robot; what breaks
connectivity is the 76.7 m2 of cells with no floor under them. That is a
reconstruction result, not a planning one, and it would be easy to misread as
"the robot does not fit" if the two were inflated together -- which is what
`build_navmap.py --robot-radius` does, deliberately, since a hole is as bad to
drive into as a wall.

Only the first row can fail the run. Both used to, which meant `--robot-radius`
never came back green on real reconstruction data, and a genuine regression
would have looked exactly like the holes that are always there. The second row
now prints as a note. nav2 agrees with the split, for the reason two sections
down: it inflates lethal obstacles and leaves unknown cells alone.

### nav2 itself plans on it

```bash
# inside WSL
bash scripts/isaac-sim/nav2_check.sh /mnt/d/.../marble-office-navmap.yaml 3.79,9.02 0,0
```

Brings up `nav2_map_server` and `nav2_planner` (NavFn over a `nav2_costmap_2d`
global costmap at `robot_radius: 0.25`, `allow_unknown: false`), drives their
lifecycle transitions, and asks for a route through the real
`ComputePathToPose` action. Deliberately not a full bringup: no localisation,
no controller, no behaviour tree. Those need a robot and say nothing about
whether the map is usable.

| World | nav2 route | `check_navmap.py` A* |
| --- | --- | --- |
| `marble-office` | 407 poses, **10.88 m** (detour x1.14) | 10.60 m, obstacles-only |
| `marble-from-photo-7y19ti` | 203 poses, **5.36 m** (detour x1.04) | 5.29 m |

`map_server` republishes the grid unchanged: 268 x 297 @ 0.05 m, origin
(-5.735311692253731, -5.2518977547521235) -- every digit of what
`build_navmap.py` wrote.

**The negative control matters as much as the route.** Asked for a goal at
(6.50, -4.30), out in the no-floor region, nav2 refuses: *"failed to generate a
valid path"*, empty path, action aborted. It is respecting the occupied and
unknown cells rather than drawing a line.

**This also settles the walls-versus-holes question for a real stack.** nav2's
10.88 m matches the obstacles-only A* (10.60 m), not the obstacles-and-holes
scenario, which had no path at all. nav2's inflation layer grows lethal
obstacles; unknown cells are `NO_INFORMATION` and are not inflated around --
they simply cannot be routed through. So the disconnection `build_navmap.py
--robot-radius` reports is a property of that flag inflating both, and does not
mean nav2 would refuse the map.

### The ERROR storm, and what it was really saying

The planner used to log roughly 400 lines of `worldToMap failed: mx,my: 187,284,
size_x,size_y: 247,276` at ERROR level on every run -- which is what a broken
stack looks like from the log, and it was not the plan failing.

Row 284 is eight rows past the 276-row map, and the numbers say which code path
found it: `mx` sweeps while `my` stays put, so something is scanning a
rectangle. NavFn scans a square of `tolerance` -- 0.5 m, ten cells -- around the
goal, looking for a reachable substitute, and reports every cell of that square
that lies past the edge of the map this way. The goal sat within ten cells of
the top row, so a fifth of its search box was off the map.

`build_navmap.py --border` pads the grid with 0.5 m of unknown cells, and the
count is now **zero** on both worlds. That is not only cosmetic: with the search
no longer clipped, the returned path ends on the goal itself, `(3.29, 9.02)`,
instead of five centimetres short at `(3.29, 8.97)`.

The general lesson is the one this project keeps relearning: read what the error
actually says. `my` fixed while `mx` sweeps was enough to name the code path
without reading a line of nav2's source.

### The DDS problem this uncovered, and its fix

Cross-process ROS communication did not work in this WSL2 instance at all --
not for our nodes, and not for `demo_nodes_cpp talker` and `listener`, which
came up cleanly and never heard each other. `ros2 node list` was empty
throughout. Nothing logged an error.

The cause: **WSL2 in mirrored networking mode gives `lo` no MULTICAST flag**, so
DDS discovery over loopback is impossible. `ROS_LOCALHOST_ONLY=1` -- the usual
first advice -- forces discovery onto exactly that interface and makes it worse.
Raw multicast on the real NIC works fine, which is what made this confusing:
`ros2 multicast send`/`receive` pass.

`nav2_check.sh` finds a multicast-capable global interface, skipping `/32`
addresses so a tunnel interface is not chosen, and writes a FastDDS profile
whitelisting it with the builtin transports off. Talker and listener then hear
each other immediately.

Three things tried first, all wrong: `ROS_LOCALHOST_ONLY=1`, a FastDDS profile
pinned to 127.0.0.1 (the one interface that cannot work), and UDP-only with no
whitelist. Also checked and cleared: `/dev/shm` size and permissions, and the
`ros2` CLI daemon (`--no-daemon` changed nothing).

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

### All four robots, measured

| Robot | Policy dt | In `marble-office` | On NVIDIA's flat grid |
| --- | --- | --- | --- |
| `spot` (quadruped) | 0.002 / 500 Hz | 2.67 m, ends fallen | 4.39 m, upright |
| `anymal` (quadruped) | 0.005 / 200 Hz | 2.63 m, ends fallen at 0.24 m | not run |
| `h1` (humanoid biped) | 0.005 / 200 Hz | **2.33 m, upright throughout at 0.87 m** | not run |
| `go2` (quadruped) | 0.005 / 200 Hz | **0.01 m -- never stands** | 4.20 m, locomotes |

Walking forward for 10 s each; only Spot has been driven interactively.

Two things here matter more than the distances.

**The policy rates disagree.** Spot wants 500 Hz, the other three want 200 Hz.
A hardcoded 1/200 is right for three of the four, which is exactly why the Spot
failure was so hard to place -- copying a number from an example works until it
does not. Reading `_dt` off the loaded policy is the only version that is right
for every robot.

**Size tracks success, and the obvious explanation does not hold.** The two
small machines fail on the reconstructed floor and the three larger ones walk on
it: Go2 covers 4.20 m on flat ground and 0.01 m here, the same shape as the
wheeled robot's failure below. The tempting story is that small feet and wheels
catch on marching-cubes detail -- but a 1600-point height scan over 2 x 2 m of
that floor measured a range of 0.000 m. The correlation is real; the cause is
not. The humanoid holding station at 0.87 m for a whole run, while both
quadrupeds that walked ended up fallen, was not expected either.

## Driving the map, not just checking it (`navigate.py`)

```bash
D:\isaacsim-6.0.1\python.bat scripts/isaac-sim/navigate.py   public/worlds/marble-office/export/marble-office.usdz   --map public/worlds/marble-office/export/marble-office-navmap.yaml
```

Everything above this is open loop. `build_navmap.py` measures the room,
`check_navmap.py` shows a planner can cross the map, `nav2_check.sh` shows nav2's
planner can too, and `walk_robot.py` walks a robot around the same room under a
keyboard. Nothing joined the two halves, so "the map is usable" was an argument
rather than a demonstration.

    map -> A* -> string-pulled polyline -> pure pursuit -> [vx, vy, yaw] -> policy

The plan comes off the map on disk through the same `navmap` module
`check_navmap.py` uses, so the route being driven is the route that was checked.
The robot is the same policy-driven articulation `walk_robot.py` drives. Only
the controller between them is new, and it is deliberately plain — a lookahead
point on the path, proportional heading, and a gate that turns before it walks.
Anything cleverer would make it harder to tell a map problem from a controller
problem, which is the entire point of running this.

What it reports is not "it moved" but how far the robot strayed from the line it
was given, whether it arrived, and what it hit that the map did not have. A
robot that reaches the goal by bouncing off two walls has not navigated.

### Three things this found, none of which the checks could

**Chasing waypoints is not pure pursuit.** String-pulling leaves
`marble-office`'s route with four waypoints, so the steering target sat six
metres away, where half a metre of lateral offset is a four-degree heading
error. Spot tracked a slow rightward drift the whole length of the room and put
itself into a wall at 0.75 m off the line. Pursuing a point a fixed distance
ahead *on the line* instead took the mean tracking error from 0.225 m to
0.068 m and the maximum from 0.746 m to 0.313 m, on the same route.

**Planning nav2's way sends a robot along the edge of the holes.** nav2 inflates
lethal obstacles and leaves `NO_INFORMATION` alone, which is right for a real
robot in a real room: the floor is there, the map merely failed to see it. In
here the room *is* the reconstruction, and a cell with no floor in the map has
no floor in the collision mesh either. Planned nav2's way the route ran within
0.07 m of unmapped space; with the holes counted as obstacles and a clearance
preference in the cost, the tightest point on the route is 0.90 m.

| planner setting | tightest clearance | median |
| --- | --- | --- |
| holes passable, shortest route (nav2's rules) | 0.07 m | 1.13 m |
| holes passable, clearance preferred | 0.25 m | 1.30 m |
| holes are obstacles, shortest route | 0.35 m | 1.45 m |
| holes are obstacles, clearance preferred | **0.90 m** | 1.49 m |

**And the map was wrong about a step.** With the route fixed and tracked to
0.03 m, Spot still stopped dead — twice, in different places, both around
y ≈ 6.1. The map said free with 0.90 m of clearance. The collision mesh has a
16 cm rise at (0.95, 6.21), from a walking surface at 0.109 m to one at 0.266 m,
and `build_navmap.py` had written it as floor. See the next section: it was a
bug in the clearance check, and driving a robot on the map is what surfaced it.

### It drives, measured

Spot, `--robot-radius 0.35`, holes counted as obstacles, clearance preferred,
on maps built with `--max-step 0.10`. Four spread-out goals on `marble-office`
plus the default goal on `marble-from-photo-7y19ti`:

| goal | planned | drove | mean off-line | max | time |
| --- | --- | --- | --- | --- | --- |
| office (0.89, 7.97) | 8.10 m | 8.29 m (x1.02) | 0.080 m | 0.298 m | 13.2 s |
| office (3.49, −3.93) | 5.28 m | 5.02 m (x0.95) | 0.066 m | 0.180 m | 8.0 s |
| office (−2.01, −3.93) | 4.43 m | 4.50 m (x1.02) | 0.118 m | 0.303 m | 8.4 s |
| office (−0.81, 4.12) | 4.18 m | 4.44 m (x1.06) | 0.099 m | 0.289 m | 8.0 s |
| photo (−1.07, 2.82) | 3.50 m | 3.32 m (x0.95) | 0.056 m | 0.120 m | 7.6 s |

**Five of five arrived**, none needed a recovery, and all five finished standing
between 0.49 and 0.53 m above the floor — Spot's own standing height, so none of
them arrived by falling over on the way. Driven distance within 6% of planned
in every case: the robot is following the line, not finding its own way there.

The `x0.95` rows are shorter than the plan because arrival is a 0.35 m radius
around the goal and the last leg is not driven to the centimetre.

### Believing the robot over the map

No reconstruction is complete and no threshold is right everywhere, so a stack
that only trusts the map cannot finish. Re-planning on an unchanged map returns
the same route and the robot walks into the same thing — measured, six times in
one run before the recovery behaviour existed.

On a stall the robot writes off a disc of map just ahead of itself, reverses out
of it without turning (turning into what stopped you is how a stuck robot stays
stuck), and plans again on what is left. `--recoveries` bounds the attempts and
the report says how many cells it had to invent. This is nav2's local costmap
and its recovery behaviours in the smallest form that does the job.

**And it is tested, which took some doing.** Every successful run needs zero
recoveries, so nothing exercises the path that matters most — it was aspiration
rather than behaviour. `--obstacle X,Y[,SIZE]` drops a solid box into the scene
and tells the map nothing about it, which is the honest version of the case it
exists for: someone left a chair in the corridor after the survey.

```bash
... --goal=0.89,7.97 --obstacle 0.30,4.00,0.70
```

A 0.70 m box on a route with 1.40 m of room beside it:

```
 10.0s  at (0.22, 3.21)  5.00 m of path left  off-line 0.02 m
 13.3s  something the map does not have at (0.27, 3.55)
        -- backing off and going round: 6 waypoints, 5.22 m
 15.0s  at (0.24, 2.91)   <- reversing
 20.0s  at (-0.35, 3.41)  <- round the west side
 27.8s  ARRIVED at (0.77, 7.65), 0.35 m from the goal
```

Arrived, one recovery, 253 cells written off, driven 11.19 m against a planned
8.10 m — the x1.38 *is* the detour — and still standing at 0.523 m. The overlay
draws the original plan in blue, the re-plan in pale blue, what the robot found
out in yellow, and the route it drove in red.

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
grid gave in the same run.

`build_navmap.py` drops hits whose collision prim ends in `GroundPlane`. This
only started mattering once the plane was moved to the origin — before that it
floated 1.46 m up, above the cast origin, and was never hit.

Moving it also made `isaacsim.asset.gen.omap` start returning non-zero occupied
counts, having reported an all-free grid until then. Plausibly the misplaced
plane cut through the scan volume at chest height; not chased further, since the
raycast grid is the one that is checked.

## Known limitations

- No despeckle, and this one is a decision rather than an omission.
  `build_walkable.py` closes pinholes and drops specks before it floods. Doing
  the same here was measured and rejected: `marble-office` has 4 obstacle blobs
  of one or two cells and `marble-from-photo-7y19ti` has 6, and at 5 cm a
  single occupied cell is exactly the width of a chair leg. Nothing in the grid
  separates the two, and the mistakes are not symmetric — keeping a speck costs
  a slightly tighter map, removing a real leg costs a collision. Each run now
  reports the obstacle blob histogram instead, so the day specks stop being a
  handful is visible. The 67.9 m² here against that tool's 73.03 m² walkable is
  the two tools now disagreeing by 7%, from opposite directions: this one has
  less headroom (0.6 m against 1.6 m) but checks it from the local ground, and
  uses a 5 cm cell against that tool's 15 cm.
- The floor/obstacle split is a single height threshold, and it is looser than
  `--floor-tolerance` makes it sound: the reference is the *highest* vertex of
  the floor mesh, so on `marble-office` the band ends 0.49 m above a floor whose
  median is 4 mm. The clearance ray no longer shares that reference, which is
  what made it safe, but the classification still does, so the ground recorded
  under free cells still reaches 0.473 m. `--max-step` is what turns the
  difference between two such cells into an obstacle.
- **Nothing here localises.** `navigate.py` reads the robot's pose from the
  simulator, so this measures whether the *map* is drivable, not whether a robot
  could work out where it is on it. Adding AMCL or a lidar to close that half is
  a separate question, and its answer does not change any number above.
- Only Spot has been driven. Of the four shipped policies `walk_robot.py`
  measured H1 upright but slow, ANYmal and Spot walking, and Go2 never standing
  at all. `--robot` accepts the others and nothing in the controller is
  Spot-specific, but nothing has been measured either.

## Reachability and inflation, measured

On `marble-office` the grid before the fill has 74.4 m² of floor spread over
**150 disconnected regions** — but 149 of them come to 6.5 m² between them.
Marble still leaves very little unreachable scrap. The region count tripled when
the clearance ray was fixed, because furniture that used to read as open floor
now cuts the room up, which is what furniture does.

| World | Raw free | Regions | Dropped | Reachable | With `--robot-radius 0.25` |
| --- | --- | --- | --- | --- | --- |
| `marble-office` | 74.4 m² | 150 | 6.5 m² | 67.9 m² | 48.8 m² |
| `marble-from-photo-7y19ti` | 19.2 m² | 67 | 1.4 m² | 16.8 m² | 9.9 m² |

Two corrections moved these numbers, and both made them smaller. `--border`
took the last column from 61.6 m² to 59.9 m²: before the border, inflation's
shifts zero-filled at the array edge, which reads as "not blocked", so a free
cell on the boundary was never grown against what lay beyond it. Fixing the
clearance ray took the rest. Neither is lost accuracy — the robot's centre has
to stay a radius clear of the edge of what we mapped, and the room really does
have furniture in it.

Inflating by 0.25 m costs `marble-office` 19.1 m² and takes it from 150 regions
to 4: grown obstacles swallow the small scraps outright before the fill ever
looks at them.

**The fill keeps the right room.** Both worlds report the world origin — which
is `scene.json`'s `spawnPoint`, where the viewer drops a character — inside the
kept region. That is the check that the fill latched onto the room rather than
onto some other blob, and it costs nothing to run.

Running with `--no-reachability` gives back 29771 free cells, which is exactly
`raw free was 30805` minus the 1034 the step limit turned into obstacle — both
printed by the same run. The filters are additive and the identity is checkable
without trusting either of them.

Seeding it three ways used to land on the same region. Two still do — the
largest-region default and `--spawn 0,0` both keep 27168 cells — and the third
now does not, which is more informative than the agreement was. An off-map
`--spawn 100,100` falls back to the nearest free cell, and on a map with 150
regions instead of 34 that lands in an 11-cell pocket in a far corner; the fill
keeps only that. Nobody had to notice: the run reports `world origin reachable:
False`, which is the whole reason that check is printed.

### A grid map has no third dimension, and a robot finds out

The map says where there is floor, not how high it is, so a step reads as flat
and a planner routes straight over it. `build_navmap.py` records the ground
height under every cell it calls free, which turns the threshold from a constant
into something measurable:

| with `--max-step 0.10` | p50 | p95 | p99 | max | band ends at |
| --- | --- | --- | --- | --- | --- |
| `marble-office` | 0.004 | 0.052 | 0.148 | 0.473 | 0.491 |
| `marble-from-photo-7y19ti` | 0.007 | 0.007 | 0.062 | 0.286 | 0.287 |

The tail still reaches the band. `floor_top` is the *highest vertex of the floor
mesh* — one outlier sets it — and the walkable band is `floor_top +
--floor-tolerance`, which on `marble-office` is 0.49 m over a floor whose median
is 4 mm.

`--max-step` turns a height change taller than the robot can climb into an
obstacle, on both sides, so it is in the map rather than in this script and
every consumer of the PNG sees it. A ramp is unaffected by construction: its
height changes gradually, so no neighbouring pair exceeds the limit — the
discrimination a single height threshold cannot make. At 0.10 m it costs
`marble-office` 1034 cells (2.6 m²) and `marble-from-photo-7y19ti` 386 (1.0 m²).

Each run also reports what the limit costs the *fill*, whether or not it is on:

| step limit | `marble-office` | `marble-from-photo-7y19ti` |
| --- | --- | --- |
| 0.20 m | 27168 | 6727 |
| 0.10 m | 27168 | 6727 |
| 0.05 m | 26922 (−246) | 6709 (−18) |
| 0.03 m | 26709 (**−459**) | 6698 (−29) |

The cliff at 0.03 m is the reconstruction's own floor noise breaking
connectivity between neighbouring cells — a lower bound on what this data can
express, not a robot property.

The default is still 0, so the map stays a measurement of the room rather than
of one robot. `navigate.py` says so out loud when it loads a map built without
it, because that is the map that stopped Spot.
