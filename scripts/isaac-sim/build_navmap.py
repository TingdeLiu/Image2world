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
drops anything with no route to the spawn.

See README.md for what was measured and for the Vulkan crash this works around.
"""
import argparse
import json
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
parser.add_argument("--robot-radius", type=float, default=0.0,
                    help="grow obstacles by this radius in metres; 0 keeps the raw grid")
parser.add_argument("--spawn", metavar="X,Y",
                    help="world position to flood fill from; "
                         "default is the largest connected free region")
parser.add_argument("--no-reachability", action="store_true",
                    help="keep free cells that have no route to the spawn")
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
    raise SystemExit(
        "no /Environment/Background/Collider in this stage -- was it exported "
        "with --no-collider?"
    )
world = cache.ComputeWorldBound(collider_prim).ComputeAlignedRange()
wmin, wmax = world.GetMin(), world.GetMax()
floor_prim = stage.GetPrimAtPath("/Environment/Background/Collider/floor")
floor_range = cache.ComputeWorldBound(floor_prim).ComputeAlignedRange() if floor_prim else world
floor_top = float(floor_range.GetMax()[2])
print(f"world {wmax[0] - wmin[0]:.2f} x {wmax[1] - wmin[1]:.2f} x {wmax[2] - wmin[2]:.2f} m, "
      f"floor top z={floor_top:.3f}")
report["world_size_m"] = [round(float(wmax[i] - wmin[i]), 3) for i in range(3)]
report["floor_top_z"] = round(floor_top, 3)

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
nx = int((float(wmax[0]) - x0) / args.cell_size)
ny = int((float(wmax[1]) - y0) / args.cell_size)
cast_from = floor_top + args.robot_height

FREE, OCCUPIED, UNKNOWN = 0, 1, 2
grid = np.full((ny, nx), UNKNOWN, dtype=np.uint8)

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
        grid[iy, ix] = FREE if surface_z <= floor_top + args.floor_tolerance else OCCUPIED
    if iy % 50 == 0:
        print(f"  row {iy}/{ny}")

# A cell with clear floor can still be blocked by something the base would hit:
# cast back up through the robot's own height and demote any cell that is not clear.
for iy in range(ny):
    wy = y0 + (iy + 0.5) * args.cell_size
    for ix in range(nx):
        if grid[iy, ix] != FREE:
            continue
        wx = x0 + (ix + 0.5) * args.cell_size
        hit = sq.raycast_closest(
            (wx, wy, floor_top + 0.05), (0.0, 0.0, 1.0), args.robot_height
        )
        if hit and hit.get("hit"):
            grid[iy, ix] = OCCUPIED

timeline.stop()

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


def flood(passable, seed):
    """Cells of `passable` reachable from `seed`, 4-connected.

    Four rather than eight: a diagonal pinhole between two obstacle corners is
    not a gap a robot fits through, and eight-connectivity would call it one.
    """
    reach = seed & passable
    while True:
        grown = passable & (
            reach | shift(reach, 1, 0) | shift(reach, -1, 0)
            | shift(reach, 0, 1) | shift(reach, 0, -1)
        )
        if grown.sum() == reach.sum():
            return reach
        reach = grown


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
        reach = flood(free, seed)
    else:
        # With no spawn to go on, the largest connected region is the best
        # guess at "the room". Everything else is a scrap the reconstruction
        # left lying around.
        remaining, reach, regions = free.copy(), np.zeros_like(free), 0
        while remaining.any():
            seed = np.zeros_like(free)
            seed.flat[int(np.argmax(remaining))] = True
            component = flood(free, seed)
            regions += 1
            if component.sum() > reach.sum():
                reach = component
            remaining &= ~component
        if reach.any():
            seed_cell = tuple(int(v) for v in np.argwhere(reach)[0])
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

free_n = int((grid == FREE).sum())
occ_n = int((grid == OCCUPIED).sum())
unk_n = int((grid == UNKNOWN).sum())
print(f"  grid {nx}x{ny} @ {args.cell_size} m")
print(f"  free={free_n} ({free_n * c2:.1f} m2)  occupied={occ_n} ({occ_n * c2:.1f} m2)  "
      f"unmapped={unk_n} ({unk_n * c2:.1f} m2)   [raw free was {raw_free}]")

report["raycast"] = {
    "grid": [nx, ny],
    "cast_from_z": round(cast_from, 3),
    "free": free_n, "occupied": occ_n, "unknown": unk_n,
    "free_before_filters": raw_free,
    "free_m2": round(free_n * c2, 2),
    "occupied_m2": round(occ_n * c2, 2),
    "unknown_m2": round(unk_n * c2, 2),
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
report["navmap_png"] = png
report["navmap_yaml"] = yaml_path
print(f"  wrote {png}\n  wrote {yaml_path}")

with open(os.path.join(args.out, f"{name}-navmap.json"), "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2)
print("\n[done]")

simulation_app.close()
