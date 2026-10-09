"""Build a navigation map for a generated world inside Isaac Sim.

Loads the USD export, lets PhysX cook the collision meshes, and writes a 2D
occupancy grid plus the `map_server` YAML that goes with it.

Two routes to the same grid, and both are reported so they can be compared:

  omap     -- isaacsim.asset.gen.omap, the extension Isaac Sim ships for this
  raycast  -- PhysX scene queries, cell by cell

The raycast route is not a fallback that weakens the result: it *is* the physics
check, run one cell at a time, and it is what shows a robot could occupy a cell.

The raw grid then gets two passes that turn "there is floor here" into "a robot
could be here": obstacles grown by the robot's radius, and a flood fill that
drops anything with no route to the spawn. A margin of unknown cells is added
around the whole thing first, so that neither pass -- nor a planner later --
has to guess what lies just off the edge.

See README.md for what was measured and for the Vulkan crash this works around.
"""
import argparse
import json
import math
import os
import sys

sys.stdout.reconfigure(line_buffering=True)

parser = argparse.ArgumentParser()
parser.add_argument("usd", help="path to a .usdz exported with --no-splat --up-axis z")
parser.add_argument("--out", required=True, help="output directory")
parser.add_argument("--cell-size", type=float, default=0.05, help="grid resolution, metres")
parser.add_argument("--robot-height", type=float, default=0.60,
                    help="clearance a robot needs above the floor, metres")
parser.add_argument("--floor-tolerance", type=float, default=0.12,
                    help="how far above the floor a surface still counts as floor")
parser.add_argument("--floor-z", type=float,
                    help="height of the floor in stage units. Only needed when the "
                         "collider has no classified `floor` part, in which case "
                         "nothing in the export says where the floor is and the "
                         "run refuses rather than guessing")
parser.add_argument("--robot-radius", type=float, default=0.0,
                    help="grow obstacles by this radius in metres; 0 keeps the raw grid")
parser.add_argument("--spawn", metavar="X,Y",
                    help="world position to flood fill from; "
                         "default is the largest connected free region")
parser.add_argument("--no-reachability", action="store_true",
                    help="keep free cells that have no route to the spawn")
parser.add_argument("--max-step", type=float, default=0.10,
                    help="tallest step the robot can climb, metres. A bigger height "
                         "change between neighbouring cells becomes an obstacle, and "
                         "the flood fill will not cross it. 0 keeps the map purely 2D")
parser.add_argument("--probe", choices=("box", "ray"), default="box",
                    help="how to test a cell's column for clearance: a cell-wide "
                         "box, or a single ray up the cell's centre. Both are "
                         "always measured and reported; this picks which one the "
                         "map is written from")
parser.add_argument("--border", type=float, default=0.5,
                    help="metres of unknown margin around the grid; 0 stops at the "
                         "collider's bounding box")
parser.add_argument("--no-renderer", action="store_true",
                    help="use the renderer-free Kit experience instead of the stock one")
args = parser.parse_args()

from isaacsim import SimulationApp


def isaac_major() -> int:
    """Major version of the Isaac Sim this interpreter belongs to, 0 if unknown."""
    root = os.environ.get("ISAAC_PATH")
    if not root:
        return 0
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as fh:
            return int(fh.read().strip().split(".", 1)[0])
    except (OSError, ValueError):
        return 0


# Isaac Sim 5.1's experiences set `vulkan = true`, and on some machines the
# Vulkan backend faults inside rtx.scenedb during startup -- before any stage
# loads, so it looks like a broken asset and is not. Forcing D3D12 fixes it.
# Kit 110 (Isaac Sim 6.0) does not have the bug, and pinning the backend there
# would only take away the renderer's own choice. README.md has the evidence.
config = {"headless": True, "create_new_stage": True, "physics_gpu": 0,
          "extra_args": [] if isaac_major() >= 6 else ["--/app/vulkan=false"]}
experience = ""
if args.no_renderer:
    # The experience has to name its extension folders absolutely, and this file
    # lives outside the Isaac Sim tree. Resolve them from the ISAAC_PATH of the
    # interpreter actually running, so a second Isaac Sim install does not load
    # the first one's extensions.
    here = os.path.dirname(os.path.abspath(__file__))
    isaac_path = os.environ.get("ISAAC_PATH")
    if not isaac_path:
        raise SystemExit(
            "ISAAC_PATH is not set -- run this through <isaacsim>/python.bat, "
            "which sets it, rather than a bare python."
        )
    isaac_path = isaac_path.replace("\\", "/").rstrip("/")
    with open(os.path.join(here, "imageworld.omap.kit.in"), encoding="utf-8") as fh:
        template = fh.read()
    experience = os.path.join(here, "imageworld.omap.generated.kit")
    with open(experience, "w", encoding="utf-8") as fh:
        fh.write(template.replace("@ISAAC_PATH@", isaac_path))
    print(f"renderer-free experience -> {experience}  (ISAAC_PATH={isaac_path})")
    config["extra_args"] = []

simulation_app = SimulationApp(config, experience=experience)


def die(message):
    """Stop with a message and a non-zero exit code.

    `raise SystemExit(msg)` does not do that here. Once `SimulationApp` is up,
    Kit catches SystemExit, logs it as `[py stderr]`, shuts down cleanly and the
    process returns 0 -- so `pipeline.py` recorded a refused build as a success
    and went on to read the map file a previous run had left behind. The
    message was printed in full and changed nothing, which is the worst of both.
    """
    # The marker is for `pipeline.py`, which otherwise records a refusal as
    # `exit 1` and loses the sentence explaining it.
    print("REFUSED: " + message)
    sys.stdout.flush()
    # Straight out, without `simulation_app.close()`. The config here sets
    # `--/app/fastShutdown=True`, and under that Kit's close() terminates the
    # process itself, with status 0 -- so an `os._exit(1)` placed after it never
    # runs. Measured: the refusal printed in full and the caller saw success.
    os._exit(1)

import numpy as np
import omni.kit.app
import omni.physx
import omni.timeline
import omni.usd

from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

usd_path = os.path.abspath(args.usd).replace("\\", "/")
os.makedirs(args.out, exist_ok=True)
name = os.path.splitext(os.path.basename(args.usd))[0]
report = {"usd": usd_path, "cell_size": args.cell_size, "robot_height": args.robot_height}
try:
    with open(os.path.join(os.environ["ISAAC_PATH"], "VERSION"), encoding="utf-8") as fh:
        report["isaac_sim"] = fh.read().strip()
except (KeyError, OSError):
    report["isaac_sim"] = "unknown"
print(f"Isaac Sim {report['isaac_sim']}")


def step(n=1):
    for _ in range(n):
        omni.kit.app.get_app().update()


# --- load ---------------------------------------------------------------
# Reference the export into a fresh stage rather than opening it directly:
# that is how a simulation scene would consume it, and it leaves the file alone.
ctx = omni.usd.get_context()
ctx.new_stage()
step(2)
stage = ctx.get_stage()
UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
UsdGeom.SetStageMetersPerUnit(stage, 1.0)
UsdGeom.Xform.Define(stage, "/Environment").GetPrim().GetReferences().AddReference(usd_path)
scene = UsdPhysics.Scene.Define(stage, Sdf.Path("/physicsScene"))
scene.CreateGravityDirectionAttr().Set(Gf.Vec3f(0, 0, -1))
scene.CreateGravityMagnitudeAttr().Set(9.81)
step(20)

physx = omni.physx.get_physx_interface()
if args.no_renderer:
    # A minimal Kit app has no stage-update extension to attach the stage to
    # PhysX, and every query then fails with "No USD stage attached".
    # attach_stage lives on IPhysxSimulation, not on the PhysX interface.
    # Do NOT call it under the stock experience -- that one wires the stage up
    # itself, and re-attaching disturbs it.
    omni.physx.get_physx_simulation_interface().attach_stage(ctx.get_stage_id())
timeline = omni.timeline.get_timeline_interface()
timeline.play()
step(10)
sq = omni.physx.get_physx_scene_query_interface()

cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
# Bound the grid by the collision mesh, not by /Environment. Isaac Sim 6.0
# understands ParticleField3DGaussianSplat and so counts the splat's extent,
# which reaches past the room (12.38 x 13.84 x 2.94 m of collider against
# 15.95 x 16.04 x 3.38 m with the splat). 5.1 skipped the unknown prim and the
# two agreed by accident. The map is about where a robot can stand, so the
# collider is the right thing to measure.
collider_prim = stage.GetPrimAtPath("/Environment/Background/Collider")
if not (collider_prim and collider_prim.IsValid()):
    die("no /Environment/Background/Collider in this stage -- was it exported "
        "with --no-collider?")
world = cache.ComputeWorldBound(collider_prim).ComputeAlignedRange()
wmin, wmax = world.GetMin(), world.GetMax()
# Where the floor is decides the height every ray in this script starts from,
# so getting it wrong is not a small error -- it is every measurement.
#
# The old fallback for a collider with no classified `floor` part was the whole
# collider's bounding box, and `GetMax()[2]` of that is the CEILING. Measured
# across the eight worlds in this tree: three of them export a single
# unclassified `geometry_0` collider, and for those the fallback put `floor_top`
# at 0.94 to 0.97 of the room height -- 19.563 m in a 20.281 m world. Every
# downward ray then started 20 m above the floor and travelled 2.6 m, hitting
# nothing; the map came back with 0.02 m2 walkable and `build_navmap.py` exited
# 0. Nothing said the floor had been mistaken for the ceiling.
#
# So: take it from the collider's own floor part, or from `--floor-z`, or stop.
# The `else` below says why there is no third option.
floor_prim = stage.GetPrimAtPath("/Environment/Background/Collider/floor")
if args.floor_z is not None:
    floor_top = args.floor_z
    floor_source = "--floor-z"
elif floor_prim and floor_prim.IsValid():
    floor_top = float(cache.ComputeWorldBound(floor_prim).ComputeAlignedRange().GetMax()[2])
    floor_source = "the collider's floor part"
else:
    # Do not guess. Both obvious guesses were tried and measured, on the two
    # worlds here that export a single unclassified `geometry_0` collider:
    #
    #   the bounding box's top     -- the CEILING. 19.563 m in a 20.281 m world.
    #                                 Every downward ray then starts 20 m above
    #                                 the floor and travels 2.6 m, hitting
    #                                 nothing. 0.02 and 3.16 m2 walkable.
    #   the bounding box's bottom  -- below the floor, because the collider has
    #                                 stray geometry under it. The walkable band
    #                                 then excludes the real floor entirely.
    #                                 0.00 m2 on both.
    #
    # A third guess would be tuning against these two worlds. The honest answer
    # is that this export does not record where its floor is, and the map that
    # comes out of guessing is worse than no map -- the first guess shipped a
    # grid that looked plausible and was nonsense.
    die("this collider has no classified `floor` part, so there is nothing here "
        "that says where the floor is.\n"
        "  Its parts are: "
        + ", ".join(sorted(child.GetName() for child in collider_prim.GetChildren()))
        + "\n"
        "  A classified collider has floor/wall/ceiling/ramp/elevated, and "
        "backend/tools/build_collider.py <slug> is what produces one. Run that "
        "and re-export, or pass --floor-z <metres> if you already know where "
        "the floor is in this stage.")
print(f"world {wmax[0] - wmin[0]:.2f} x {wmax[1] - wmin[1]:.2f} x {wmax[2] - wmin[2]:.2f} m, "
      f"floor top z={floor_top:.3f} (from {floor_source})")
report["world_size_m"] = [round(float(wmax[i] - wmin[i]), 3) for i in range(3)]
report["floor_top_z"] = round(floor_top, 3)
report["floor_top_from"] = floor_source

# --- route 1: the shipped occupancy extension ---------------------------
print("\n--- omap ---")
try:
    from isaacsim.core.utils.extensions import enable_extension

    enable_extension("isaacsim.asset.gen.omap")
    step(10)
    from isaacsim.asset.gen.omap.bindings import _omap
    from isaacsim.asset.gen.omap.utils import update_location

    om = _omap.acquire_omap_interface()
    scan_z = floor_top + 0.30
    update_location(
        om,
        (0.0, 0.0, scan_z),
        (float(wmin[0]), float(wmin[1]), 0.0),
        (float(wmax[0]), float(wmax[1]), 0.0),
    )
    om.set_cell_size(args.cell_size)
    step(2)
    om.generate()
    step(2)
    buf = om.get_buffer()
    dims = om.get_dimensions()
    occ = sum(1 for b in buf if b == 1.0)
    free = sum(1 for b in buf if b == 0.0)
    print(f"  grid {dims[0]}x{dims[1]}, occupied={occ}, free={free}, cells={len(buf)}")
    report["omap"] = {"grid": [int(dims[0]), int(dims[1])], "occupied": occ,
                      "free": free, "cells": len(buf)}
except Exception as exc:  # noqa: BLE001 - report and carry on to the raycast route
    print(f"  unavailable: {exc!r}")
    report["omap"] = {"error": repr(exc)}

# --- route 2: PhysX raycasts, cell by cell ------------------------------
# Cast down from head height. A hit near floor level means the cell is standable;
# a hit higher up means something is in the way; no hit means there is no floor.
print("\n--- raycast grid ---")
x0, y0 = float(wmin[0]), float(wmin[1])
# Round the cell count up. Truncating drops the last partial cell, so a room
# 12.375 m wide got 247 cells and the final 2.5 cm was simply not in the map --
# small, but it is the edge of the room, which is where the walls are. Rounding
# up samples that strip instead; where the collider stops short of the last
# cell's centre the raycast finds nothing and the cell reads unknown, which is
# the truthful answer rather than a missing one.
nx = math.ceil((float(wmax[0]) - x0) / args.cell_size)
ny = math.ceil((float(wmax[1]) - y0) / args.cell_size)
cast_from = floor_top + args.robot_height

FREE, OCCUPIED, UNKNOWN = 0, 1, 2
grid = np.full((ny, nx), UNKNOWN, dtype=np.uint8)
# Where the downward ray actually landed, so the threshold can be checked rather
# than trusted. `floor_top` is the highest vertex of the floor mesh -- one
# outlier sets it -- and the band is `floor_top + tolerance` wide, which on
# marble-office is 0.49 m above a floor whose median height is 0.004 m. Whether
# that admits a real step is a question about this distribution, not about the
# constant.
height = np.full((ny, nx), np.nan, dtype=np.float32)

for iy in range(ny):
    wy = y0 + (iy + 0.5) * args.cell_size
    for ix in range(nx):
        wx = x0 + (ix + 0.5) * args.cell_size
        hit = sq.raycast_closest((wx, wy, cast_from), (0.0, 0.0, -1.0), args.robot_height + 2.0)
        if not (hit and hit.get("hit")):
            continue
        # /World/GroundPlane is an infinite collision plane sitting on z=0 so a
        # robot spawned at the origin has something under it. It is not floor
        # the reconstruction saw: counting it would mark every cell in the
        # bounding box walkable, including outside the building.
        if str(hit.get("collision", "")).endswith("GroundPlane"):
            continue
        surface_z = cast_from - float(hit["distance"])
        height[iy, ix] = surface_z
        grid[iy, ix] = FREE if surface_z <= floor_top + args.floor_tolerance else OCCUPIED
    if iy % 50 == 0:
        print(f"  row {iy}/{ny}")

# A cell with clear floor can still be blocked by something the base would hit:
# cast back up through the robot's own height and demote any cell that is not clear.
#
# From *this cell's* ground, not from a global reference. It used to start at
# `floor_top + 0.05`, and `floor_top` is the highest vertex of the whole floor
# mesh -- 0.371 m on marble-office, against ground that is actually at 0.109 m
# along the route. Everything in the 0.26 m between them was invisible to both
# passes: too low for the upward ray, and not the first thing the downward ray
# met. A 16 cm step at (0.95, 6.21) sat in that band, was written to the map as
# clear floor, and stopped Spot dead in three separate navigation runs.
#
# The offset is `--floor-tolerance`, which already means "this much above the
# floor still counts as floor" -- so by definition nothing inside it obstructs.
# It cannot be much smaller: the collision mesh is a shell with thickness, and
# this floor's two sheets sit 10 cm apart, so a 5 cm offset makes every cell
# that saw through a gap in the upper sheet collide with its own underside.
# Measured, not guessed -- at 5 cm the map went from 7091 obstacle cells to
# 38503, most of the room.
clearance_from = args.floor_tolerance
#
# The column gets tested two ways, because the obvious way is not the honest one.
#
# `raycast_closest` is a line through the cell's centre. A robot is a volume and
# a cell is 5 cm across, so anything standing inside the cell but off its centre
# line is invisible to the ray: the corner of a step, a chair leg between two
# centres, the lip of a kerb. That is not a hypothetical gap. It is the shape of
# the bug that stopped Spot at (0.95, 6.21) -- the ray went up through clear air
# beside the rise it needed to see.
#
# `overlap_box_any` tests the whole cell-wide column instead, which is the volume
# the cell actually stands for. The box strictly contains the ray's line, so it
# can only find more; `ray_only` in the report should be 0 and is worth watching
# in case it is not. Both are run every time and the difference is written to the
# JSON, because "the box is stricter here" is a measurement about this room, not
# a property of geometry, and it should not live in a comment.
IDENTITY = (0.0, 0.0, 0.0, 1.0)
half = args.cell_size / 2.0
column = args.robot_height          # the span the ray covers, matched exactly
by_ray = np.zeros(grid.shape, dtype=bool)
by_box = np.zeros(grid.shape, dtype=bool)
for iy in range(ny):
    wy = y0 + (iy + 0.5) * args.cell_size
    for ix in range(nx):
        if grid[iy, ix] != FREE:
            continue
        wx = x0 + (ix + 0.5) * args.cell_size
        base = float(height[iy, ix]) + clearance_from
        hit = sq.raycast_closest((wx, wy, base), (0.0, 0.0, 1.0), column)
        by_ray[iy, ix] = bool(hit and hit.get("hit"))
        by_box[iy, ix] = bool(sq.overlap_box_any(
            (half, half, column / 2.0), (wx, wy, base + column / 2.0), IDENTITY
        ))

ray_n, box_n = int(by_ray.sum()), int(by_box.sum())
both_n = int((by_ray & by_box).sum())
grid[by_box if args.probe == "box" else by_ray] = OCCUPIED
print(f"  clearance: ray blocks {ray_n} cells, cell-wide box blocks {box_n} "
      f"({box_n - both_n} the ray walked past, {ray_n - both_n} the box missed) "
      f"-- map written from the {args.probe}")
report["clearance_probe"] = {
    "used": args.probe, "ray_blocked": ray_n, "box_blocked": box_n,
    "both": both_n, "box_only": box_n - both_n, "ray_only": ray_n - both_n,
}

timeline.stop()

# --- an explicit edge ----------------------------------------------------
# The grid so far stops exactly at the collider's bounding box, and free cells
# run right up to the last row. Two things go wrong there, and both are quiet:
#
#   * The inflation below shifts the obstacle mask and zero-fills at the array
#     edge, which reads as "not blocked". A cell on the boundary is therefore
#     never grown against whatever is beyond it -- the one place we know
#     nothing about.
#   * nav2's NavFn searches a square of `tolerance` around the goal for a
#     reachable substitute. Where that square leaves the map it logs an ERROR
#     per cell: a goal near the top edge produced roughly 400 of them per run,
#     which is what a broken stack looks like from the log.
#
# A ring of unknown cells fixes both by saying the true thing: past the
# reconstruction there is no floor, and we are not guessing about it.
border_cells = int(round(args.border / args.cell_size))
if border_cells:
    grid = np.pad(grid, border_cells, mode="constant", constant_values=UNKNOWN)
    height = np.pad(height, border_cells, mode="constant", constant_values=np.nan)
    ny, nx = grid.shape
    x0 -= border_cells * args.cell_size
    y0 -= border_cells * args.cell_size
    print(f"  border: {border_cells} cells ({args.border} m) of unknown on every side "
          f"-> grid {nx}x{ny}, origin ({x0:.3f}, {y0:.3f})")
report["border"] = {"m": args.border, "cells": border_cells}

c2 = args.cell_size * args.cell_size
raw_free = int((grid == FREE).sum())


# --- from "there is floor" to "a robot could be here" --------------------
# Two passes, and the order between them is not free: inflation runs first,
# because growing an obstacle can close a doorway, and a region whose only way
# in is narrower than the robot is not reachable. Flooding first would keep it.


def shift(mask, dy, dx):
    """`mask` translated by (dy, dx), zero-filled at the edges -- no wrap."""
    out = np.zeros_like(mask)
    hh, ww = mask.shape[0] - abs(dy), mask.shape[1] - abs(dx)
    if hh <= 0 or ww <= 0:
        return out
    sy, sx, gy, gx = max(dy, 0), max(dx, 0), max(-dy, 0), max(-dx, 0)
    out[sy:sy + hh, sx:sx + ww] = mask[gy:gy + hh, gx:gx + ww]
    return out


STEPS_4 = ((1, 0), (-1, 0), (0, 1), (0, -1))


def climbable_steps(max_step):
    """Per-direction mask of transitions no taller than `max_step`.

    Without this the fill is purely two-dimensional, and it will happily walk
    from the floor onto a surface half a metre above it because the two cells
    are side by side in the picture. `marble-office` has free cells whose ground
    is 0.489 m up, so this is not hypothetical.

    A NaN on either side -- a cell the ray never hit -- compares False, which is
    what we want: those are not free cells anyway.
    """
    if not max_step:
        return None
    masks = {}
    for dy, dx in STEPS_4:
        came_from = np.full_like(height, np.inf)
        hh, ww = height.shape[0] - abs(dy), height.shape[1] - abs(dx)
        if hh > 0 and ww > 0:
            sy, sx, gy, gx = max(dy, 0), max(dx, 0), max(-dy, 0), max(-dx, 0)
            came_from[sy:sy + hh, sx:sx + ww] = height[gy:gy + hh, gx:gx + ww]
        with np.errstate(invalid="ignore"):
            masks[(dy, dx)] = np.abs(came_from - height) <= max_step
    return masks


def step_edges(max_step):
    """Cells on either side of a height change taller than `max_step`.

    A grid map has no third dimension, so a step reads as flat floor and a
    planner routes straight over it. That is not hypothetical here: Spot walked
    a route with 0.90 m of mapped clearance, tracking it to within 3 cm, and
    stopped dead against a 16 cm rise at (0.95, 6.21) that the map called free.
    The walkable band is `floor_top + tolerance` -- 0.49 m on marble-office --
    so a surface at 0.266 m is floor by that rule, and the surface it steps up
    from is at 0.109 m.

    Marking both sides puts the fact in the map rather than in this script, so
    nav2 and anything else reading the PNG sees it too. A ramp is unaffected by
    construction: its height changes gradually, so no neighbouring pair exceeds
    the limit, which is the discrimination a single height threshold cannot make.
    """
    edges = np.zeros(grid.shape, dtype=bool)
    if not max_step:
        return edges
    known = ~np.isnan(height)
    for dy, dx in STEPS_4:
        neighbour = np.full_like(height, np.nan)
        hh, ww = height.shape[0] - abs(dy), height.shape[1] - abs(dx)
        if hh <= 0 or ww <= 0:
            continue
        sy, sx, gy, gx = max(dy, 0), max(dx, 0), max(-dy, 0), max(-dx, 0)
        neighbour[sy:sy + hh, sx:sx + ww] = height[gy:gy + hh, gx:gx + ww]
        with np.errstate(invalid="ignore"):
            too_tall = np.abs(neighbour - height) > max_step
        # Both sides must have been measured. Next to a cell the ray never hit
        # there is no step to speak of -- that is a hole, and already unknown.
        both = known & ~np.isnan(neighbour)
        edges |= too_tall & both
        # ... and the other side of the same pair.
        back = np.zeros_like(edges)
        back[gy:gy + hh, gx:gx + ww] = (too_tall & both)[sy:sy + hh, sx:sx + ww]
        edges |= back
    return edges


def flood(passable, seed, climbable=None):
    """Cells of `passable` reachable from `seed`, 4-connected.

    Four rather than eight: a diagonal pinhole between two obstacle corners is
    not a gap a robot fits through, and eight-connectivity would call it one.
    """
    reach = seed & passable
    while True:
        grown = reach.copy()
        for direction in STEPS_4:
            candidate = shift(reach, *direction) & passable
            if climbable is not None:
                candidate &= climbable[direction]
            grown |= candidate
        if grown.sum() == reach.sum():
            return reach
        reach = grown


def blobs(mask, steps):
    """Connected components of a boolean mask, as lists of (iy, ix)."""
    seen = np.zeros(mask.shape, dtype=bool)
    height, width = mask.shape
    out = []
    for start in zip(*np.nonzero(mask)):
        if seen[start]:
            continue
        blob, stack = [], [start]
        seen[start] = True
        while stack:
            iy, ix = stack.pop()
            blob.append((iy, ix))
            for dy, dx in steps:
                ny_, nx_ = iy + dy, ix + dx
                if (0 <= ny_ < height and 0 <= nx_ < width
                        and mask[ny_, nx_] and not seen[ny_, nx_]):
                    seen[ny_, nx_] = True
                    stack.append((ny_, nx_))
        out.append(blob)
    return out


# Eight-connected here, the mirror of the four used for free space: a diagonal
# run of occupied cells is one wall, while a diagonal gap is not a doorway.
NEIGHBOURS_8 = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]

# Reported, and deliberately not acted on. It is tempting to delete the lone
# occupied cells as raycast noise, and at 0.05 m it is exactly the wrong move:
# one cell is 5 cm across, which is also the width of a chair leg. Nothing in
# the grid separates the two, and the two mistakes are not symmetric -- keeping
# a speck costs a slightly tighter map, removing a real leg costs a collision.
# What the histogram is for is noticing when specks stop being a handful.
report["max_step"] = args.max_step
if args.max_step > 0:
    edges = step_edges(args.max_step) & (grid == FREE)
    n_edges = int(edges.sum())
    grid[edges] = OCCUPIED
    print(f"  steps taller than {args.max_step} m: {n_edges} cells "
          f"({n_edges * c2:.1f} m2) become obstacle")
    report["step_edges"] = {"cells": n_edges, "m2": round(n_edges * c2, 2)}

obstacle_blobs = blobs(grid == OCCUPIED, NEIGHBOURS_8)
speck_sizes = sorted(len(b) for b in obstacle_blobs)
specks = sum(1 for n in speck_sizes if n <= 2)
print(f"  obstacles form {len(obstacle_blobs)} blobs, largest {speck_sizes[-1] if speck_sizes else 0}"
      f" cells, {specks} of 1-2 cells (kept: a speck and a chair leg look alike here)")


def ground_under_free():
    """Percentiles of the measured ground beneath whatever `grid` now calls free.

    A flat room puts every percentile within a centimetre or two of the floor; a
    tail reaching toward `floor_top + tolerance` means the threshold is admitting
    steps, and the p100 is the tallest thing a planner is being told it can drive
    onto.

    This is a function because *when* it runs changes what it means, and the two
    moments are worth keeping apart. Run before the filters it describes the raw
    physics; run after them it describes the map that ships. It used to run only
    at the first moment and be reported as if it were the second, so the JSON
    said marble-office had walkable ground 0.473 m up when the ledge in question
    had already been dropped for having no route to it.
    """
    z = height[grid == FREE]
    z = z[~np.isnan(z)]
    if not z.size:
        return None
    return {f"p{q}": round(float(np.percentile(z, q)), 4) for q in (0, 50, 95, 99, 100)}


raw_ground = ground_under_free()

report["obstacle_blobs"] = {
    "count": len(obstacle_blobs),
    "largest_cells": speck_sizes[-1] if speck_sizes else 0,
    "specks_1_2_cells": specks,
    "sizes": speck_sizes[:40],
}

report["robot_radius"] = args.robot_radius
if args.robot_radius > 0:
    # No-floor cells are grown too. A hole in the reconstruction is at least as
    # bad to drive into as a wall, and the robot's centre has to stay a radius
    # clear of both.
    radius = args.robot_radius / args.cell_size
    blocked = grid != FREE
    grown = blocked.copy()
    span = int(np.floor(radius))
    for dy in range(-span, span + 1):
        for dx in range(-span, span + 1):
            if (dx or dy) and dx * dx + dy * dy <= radius * radius:
                grown |= shift(blocked, dy, dx)
    added = grown & (grid == FREE)
    n_added = int(added.sum())
    grid[added] = OCCUPIED
    print(f"  inflated by {args.robot_radius} m ({radius:.1f} cells): "
          f"{n_added} cells ({n_added * c2:.1f} m2) become obstacle")
    report["inflation"] = {"radius_m": args.robot_radius,
                           "radius_cells": round(radius, 2),
                           "cells": n_added, "m2": round(n_added * c2, 2)}

if not args.no_reachability:
    free = grid == FREE
    climbable = climbable_steps(args.max_step)
    seed_cell, regions = None, None
    if args.spawn:
        sx, sy = (float(v) for v in args.spawn.split(","))
        ix, iy = int((sx - x0) / args.cell_size), int((sy - y0) / args.cell_size)
        if 0 <= iy < ny and 0 <= ix < nx and free[iy, ix]:
            seed_cell = (iy, ix)
        else:
            # A spawn can land a cell off the floor. Take the nearest free cell
            # rather than quietly falling back to "the biggest blob somewhere".
            cells = np.argwhere(free)
            if len(cells):
                seed_cell = tuple(
                    cells[int(np.argmin(((cells - np.array([iy, ix])) ** 2).sum(axis=1)))]
                )
                print(f"  spawn ({sx}, {sy}) is not free; "
                      f"seeded from the nearest free cell instead")
    if seed_cell is not None:
        seed = np.zeros_like(free)
        seed[seed_cell] = True
        reach = flood(free, seed, climbable)
    else:
        # With no spawn to go on, the largest connected region is the best
        # guess at "the room". Everything else is a scrap the reconstruction
        # left lying around.
        remaining, reach, regions = free.copy(), np.zeros_like(free), 0
        while remaining.any():
            seed = np.zeros_like(free)
            seed.flat[int(np.argmax(remaining))] = True
            component = flood(free, seed, climbable)
            regions += 1
            if component.sum() > reach.sum():
                reach = component
            remaining &= ~component
        if reach.any():
            seed_cell = tuple(int(v) for v in np.argwhere(reach)[0])
    # What a step limit would cost, measured rather than argued about. Run with
    # the default --max-step 0 the fill is two-dimensional, so a surface half a
    # metre up is connected to the floor beside it as if you could walk onto it.
    # These are the four numbers that say how much of the room is actually
    # reachable on foot, and they are printed whether or not the limit is on.
    if seed_cell is not None:
        origin_seed = np.zeros_like(free)
        origin_seed[seed_cell] = True
        sweep = {f"{candidate:.2f}": int(flood(free, origin_seed,
                                               climbable_steps(candidate)).sum())
                 for candidate in (0.03, 0.05, 0.10, 0.20)}
        print("  reachable if the robot can only climb: "
              + ", ".join(f"{limit} m -> {kept}" for limit, kept in sweep.items()))
        report["step_limit_sweep"] = sweep

    dropped = free & ~reach
    grid[dropped] = UNKNOWN
    # The viewer spawns a character at world (0, 0). If that cell survives the
    # fill, the region kept is the same room the viewer drops you into -- a
    # free cross-check that the fill did not pick some other blob.
    oy, ox = int((0.0 - y0) / args.cell_size), int((0.0 - x0) / args.cell_size)
    at_origin = bool(0 <= oy < ny and 0 <= ox < nx and reach[oy, ox])
    n_dropped = int(dropped.sum())
    print(f"  reachable: kept {int(reach.sum())} cells, dropped {n_dropped} "
          f"({n_dropped * c2:.1f} m2) with no route in"
          + (f", from {regions} regions" if regions else "")
          + f"; world origin reachable: {at_origin}")
    report["reachability"] = {
        "regions": regions,
        "kept": int(reach.sum()), "dropped": n_dropped,
        "dropped_m2": round(n_dropped * c2, 2),
        "seed_cell": [int(seed_cell[1]), int(seed_cell[0])] if seed_cell else None,
        "world_origin_reachable": at_origin,
    }
else:
    # Recorded either way, so a consumer of the JSON can tell an unfiltered map
    # from one that simply had nothing to drop.
    report["reachability"] = None

shipped_ground = ground_under_free()
if shipped_ground:
    print(f"  ground under the free cells: p50={shipped_ground['p50']:.3f} "
          f"p99={shipped_ground['p99']:.3f} max={shipped_ground['p100']:.3f} m "
          f"(floor {floor_top:.3f}, band ends {floor_top + args.floor_tolerance:.3f}"
          + (f"; before the filters the max was {raw_ground['p100']:.3f}"
             if raw_ground and raw_ground["p100"] > shipped_ground["p100"] + 5e-4 else "")
          + ")")
    report["walkable_ground_z"] = {
        "floor_top": round(floor_top, 4),
        "band_top": round(floor_top + args.floor_tolerance, 4),
        **shipped_ground,
        "before_filters": raw_ground,
    }

free_n = int((grid == FREE).sum())
occ_n = int((grid == OCCUPIED).sum())
unk_n = int((grid == UNKNOWN).sum())
# Split the unknown count, because the two halves mean opposite things, and
# split it by asking where the cells are rather than by arithmetic on the border
# width. `unk_n - ring_n` was the arithmetic, and it called everything outside
# the reconstructed shell a hole in the floor: on demo-office it reported 47.99
# m2 missing where the cells actually enclosed by the map come to 0.56 m2, an
# 85x overstatement. That is the number you would look at to decide whether a
# floor is complete enough to build a dataset on, so it has to mean what its
# name says.
#
# Flood the unknown from the outer edge. What the flood reaches is outside --
# the deliberate border ring and everything past the walls, neither of which is
# a defect. What it cannot reach is enclosed by mapped cells, and that is floor
# the reconstruction genuinely failed to produce.
edge = np.zeros(grid.shape, dtype=bool)
edge[0, :] = edge[-1, :] = True
edge[:, 0] = edge[:, -1] = True
unknown_mask = grid == UNKNOWN
outside = flood(unknown_mask, edge & unknown_mask)
holes_n = int((unknown_mask & ~outside).sum())
ring_n = unk_n - holes_n
print(f"  grid {nx}x{ny} @ {args.cell_size} m")
print(f"  free={free_n} ({free_n * c2:.1f} m2)  occupied={occ_n} ({occ_n * c2:.1f} m2)  "
      f"unmapped={unk_n} ({unk_n * c2:.1f} m2)   [raw free was {raw_free}]")
print(f"  of the unmapped, {holes_n} ({holes_n * c2:.1f} m2) is floor missing "
      f"from inside the map and {ring_n} ({ring_n * c2:.1f} m2) is outside it")

report["raycast"] = {
    "grid": [nx, ny],
    "cast_from_z": round(cast_from, 3),
    "free": free_n, "occupied": occ_n, "unknown": unk_n,
    "free_before_filters": raw_free,
    "free_m2": round(free_n * c2, 2),
    "occupied_m2": round(occ_n * c2, 2),
    "unknown_m2": round(unk_n * c2, 2),
    "unknown_holes": holes_n,
    "unknown_holes_m2": round(holes_n * c2, 2),
    # Renamed from `unknown_border`, which was true only while the number was
    # the border ring's area. It is now everything the flood reached from the
    # edge: the ring plus the space beyond the reconstructed walls.
    "unknown_outside": ring_n,
    "origin": [x0, y0, 0.0],
}

from PIL import Image

# Grey is "make no claim": no floor there, or floor with no route to it. Both
# are cells a planner must not route through, and neither is an obstacle it
# could usefully squeeze past.
img = np.full((ny, nx), 127, dtype=np.uint8)
img[grid == FREE] = 255
img[grid == OCCUPIED] = 0
# ROS map convention: origin is the bottom-left corner, image row 0 is the top.
png = os.path.join(args.out, f"{name}-navmap.png")
Image.fromarray(np.flipud(img)).save(png)
yaml_path = os.path.join(args.out, f"{name}-navmap.yaml")
with open(yaml_path, "w", encoding="utf-8") as fh:
    fh.write(f"image: {os.path.basename(png)}\n")
    fh.write(f"resolution: {args.cell_size}\n")
    fh.write(f"origin: [{x0}, {y0}, 0.0]\n")
    fh.write("negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
# Normalised, because otherwise the report records however the caller happened
# to spell `--out`: the same build run twice, once with a relative path and once
# absolute, produced two different JSON files and a diff that looked like the
# map had changed when only the command line had.
report["navmap_png"] = os.path.abspath(png).replace("\\", "/")
report["navmap_yaml"] = os.path.abspath(yaml_path).replace("\\", "/")
print(f"  wrote {png}\n  wrote {yaml_path}")

with open(os.path.join(args.out, f"{name}-navmap.json"), "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2)
print("\n[done]")

simulation_app.close()
