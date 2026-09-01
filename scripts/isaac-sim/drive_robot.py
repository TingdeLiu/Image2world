"""Drive a Nova Carter through a generated world in Isaac Sim 6.0.

The point is to show the world holds up as a place to actually simulate in, not
just as geometry that loads: a real articulated robot spawns on the floor, is
driven by a differential controller, and moves through the room.

Two things about the ordering matter, and both cost time to find:

* The robot has to be constructed while the timeline is **stopped**, the way
  `standalone_examples/api/isaacsim.robot.wheeled_robots.examples/jetbot_differential_move.py`
  does it. Building the Articulation after `play()` leaves its root without
  dynamics: the robot then hangs in the air ignoring gravity, its wheels spin
  freely, and it drifts sideways. Finding the spawn point needs physics running,
  so this plays once to probe the floor, stops, builds the robot, plays again.
* 6.0's wheeled-robot API is `isaacsim.core.experimental`, which the deprecated
  `isaacsim.core.api` World/Scene registry cannot hold (adding one raises
  `'WheeledRobot' object has no attribute 'name'`).

Usage:
    D:\\isaacsim-6.0.1\\python.bat scripts/isaac-sim/drive_robot.py <usdz> --out <dir>
"""
import argparse
import json
import os
import sys

sys.stdout.reconfigure(line_buffering=True)

parser = argparse.ArgumentParser()
parser.add_argument("usd", help="a .usdz exported with --up-axis z")
parser.add_argument("--out", required=True)
parser.add_argument("--seconds", type=float, default=12.0)
parser.add_argument("--speed", type=float, default=0.4, help="m/s")
parser.add_argument("--turn", type=float, default=0.0, help="rad/s")
parser.add_argument("--show-collider", action="store_true",
                    help="leave the collision mesh visible instead of the splat")
parser.add_argument("--headless", action="store_true")
parser.add_argument("--trimesh-collider", action="store_true",
                    help="force approximation='none' on the collider meshes, so PhysX "
                         "contacts the real triangles instead of a convex hull")
parser.add_argument("--flat-ground", action="store_true",
                    help="control run: drive on NVIDIA's flat grid instead of the world, "
                         "to tell a driving bug from a floor problem")
args = parser.parse_args()

from isaacsim import SimulationApp

simulation_app = SimulationApp({"headless": args.headless})

import isaacsim.core.experimental.utils.app as app_utils
import numpy as np
import omni.physx
import omni.usd
from isaacsim.core.experimental.objects import DomeLight
from isaacsim.core.experimental.utils import stage as stage_utils
from isaacsim.core.simulation_manager import SimulationManager
from isaacsim.core.utils.viewports import set_camera_view
from isaacsim.robot.experimental.wheeled_robots.controllers import DifferentialController
from isaacsim.robot.experimental.wheeled_robots.robots import WheeledRobot
from isaacsim.storage.native import get_assets_root_path
from pxr import Usd, UsdGeom, UsdPhysics

CARTER = "/Isaac/Robots/NVIDIA/NovaCarter/nova_carter.usd"
GRID = "/Isaac/Environments/Grid/default_environment.usd"
# From NVIDIA's own Nova Carter example
# (standalone_examples/api/isaacsim.sensors.experimental.rtx/lidar_robot_integration.py).
# The radius is far smaller than the wheel looks -- the articulation carries a
# gear ratio -- so guessing it from the geometry gives commands 3x off.
WHEEL_RADIUS = 0.04295
WHEEL_BASE = 0.4132
DT = 1.0 / 60.0
SPAWN_CLEARANCE = 0.25

os.makedirs(args.out, exist_ok=True)
usd_path = os.path.abspath(args.usd).replace("\\", "/")
name = os.path.splitext(os.path.basename(args.usd))[0]
report = {"usd": usd_path, "flat_ground": args.flat_ground}

assets_root = get_assets_root_path()
if assets_root is None:
    raise SystemExit("no Isaac assets root -- is the machine offline?")

# --- stage ---------------------------------------------------------------
stage_utils.set_stage_up_axis("Z")
stage_utils.set_stage_units(meters_per_unit=1.0)
stage_utils.add_reference_to_stage(
    usd_path=(assets_root + GRID) if args.flat_ground else usd_path, path="/Environment"
)
stage_utils.add_reference_to_stage(usd_path=assets_root + CARTER, path="/World/NovaCarter")
app_utils.update_app(steps=150)

stage = omni.usd.get_context().get_stage()
cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                          [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])

if args.flat_ground:
    floor_top, cx, cy = 0.0, 0.0, 0.0
    print("control run: NVIDIA flat grid, floor at z=0")
else:
    collider = stage.GetPrimAtPath("/Environment/Background/Collider")
    if not (collider and collider.IsValid()):
        raise SystemExit("no collision mesh in this export -- nothing to drive on")
    rng = cache.ComputeWorldBound(collider).ComputeAlignedRange()
    wmin, wmax = rng.GetMin(), rng.GetMax()
    floor_prim = stage.GetPrimAtPath("/Environment/Background/Collider/floor")
    floor_top = float(cache.ComputeWorldBound(floor_prim).ComputeAlignedRange().GetMax()[2])
    cx, cy = float((wmin[0] + wmax[0]) / 2), float((wmin[1] + wmax[1]) / 2)
    print(f"room {wmax[0]-wmin[0]:.2f} x {wmax[1]-wmin[1]:.2f} m, floor top z={floor_top:.3f}")

    splat = stage.GetPrimAtPath("/Environment/Background/Splat")
    if (splat and splat.IsValid() and splat.GetPrimTypeInfo().GetSchemaType()
            and not args.show_collider):
        UsdGeom.Imageable(collider).MakeInvisible()
        report["splat_rendered"] = True
        print("splat visible, collision mesh hidden")
    else:
        ceiling = stage.GetPrimAtPath("/Environment/Background/Collider/ceiling")
        if ceiling and ceiling.IsValid():
            UsdGeom.Imageable(ceiling).MakeInvisible()

if args.trimesh_collider and not args.flat_ground:
    # The exporter applies CollisionAPI to the parent Xform and never states an
    # approximation. Raycasts hit the real triangles either way, so this is
    # invisible to the navigation map -- but contact resolution is not: a convex
    # hull of the whole floor is a dome the robot would sit on and slide off.
    forced = []
    for prim in stage.Traverse():
        if not prim.GetPath().pathString.startswith("/Environment/Background/Collider"):
            continue
        if not prim.IsA(UsdGeom.Mesh):
            continue
        UsdPhysics.CollisionAPI.Apply(prim)
        UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr().Set("none")
        forced.append(prim.GetName())
    print(f"forced triangle-mesh collision on: {forced}")
    report["trimesh_collider"] = forced

DomeLight("/World/DomeLight").set_intensities(600)

# Wheel joints come off the stage rather than a hardcoded list, so this works
# for other differential-drive robots too. A USD query -- no physics needed.
wheel_joints = [
    p.GetName()
    for p in stage.Traverse()
    if p.GetPath().pathString.startswith("/World/NovaCarter")
    and p.IsA(UsdPhysics.RevoluteJoint)
    and "wheel" in p.GetName().lower()
]
if len(wheel_joints) < 2:
    raise SystemExit(f"expected two wheel joints, found {wheel_joints}")
wheel_joints = sorted(wheel_joints, key=lambda n: ("right" in n.lower(), n))[:2]
print(f"wheel joints: {wheel_joints}")
report["wheel_joints"] = wheel_joints

SimulationManager.setup_simulation(dt=DT, device="cpu")
SimulationManager.get_physics_scenes()[0].set_enabled_gpu_dynamics(False)

# --- phase 1: play only long enough to find the floor, then stop ----------
spawn = (0.0, 0.0, SPAWN_CLEARANCE)
if not args.flat_ground:
    app_utils.play()
    app_utils.update_app(steps=30)
    sq = omni.physx.get_physx_scene_query_interface()
    cast_from = floor_top + 1.0
    found = None
    # One-sided test: `floor_top` is the top of the floor mesh's bounding box,
    # not the ground height at a point (this floor spans 0.004..0.371 m).
    for radius in (0.0, 0.5, 1.0, 1.5, 2.0):
        for dx, dy in ((0, 0), (radius, 0), (-radius, 0), (0, radius), (0, -radius)):
            px, py = cx + dx, cy + dy
            hit = sq.raycast_closest((px, py, cast_from), (0.0, 0.0, -1.0), 3.0)
            if not (hit and hit.get("hit")):
                continue
            if str(hit.get("collision", "")).endswith("GroundPlane"):
                continue
            surface = cast_from - float(hit["distance"])
            if surface <= floor_top + 0.12:
                found = (float(px), float(py), float(surface) + SPAWN_CLEARANCE)
                break
        if found:
            break
    if found is None:
        raise SystemExit("found no floor to spawn on")
    spawn = found
    app_utils.stop()
    app_utils.update_app(steps=30)

print(f"spawn {tuple(round(v, 3) for v in spawn)}")
report["spawn"] = list(spawn)

# --- phase 2: build the robot while stopped, then run --------------------
robot = WheeledRobot(
    paths="/World/NovaCarter",
    wheel_dof_names=wheel_joints,
    positions=[spawn[0], spawn[1], spawn[2]],
)
controller = DifferentialController(
    wheel_radius=WHEEL_RADIUS, wheel_base=WHEEL_BASE,
    max_linear_speed=1.0, max_angular_speed=1.5,
)
app_utils.update_app(steps=30)

app_utils.play()
app_utils.update_app(steps=30)

# apply_wheel_actions() writes velocity targets, which a joint only follows when
# its implicit PD controller is in velocity mode (stiffness 0, damping default).
# Nova Carter's USD does not ship that way.
wheel_indices = robot.get_dof_indices(wheel_joints).numpy().tolist()
robot.switch_dof_control_mode("velocity", dof_indices=wheel_indices)
app_utils.update_app(steps=10)
report["wheel_dof_indices"] = wheel_indices


def pose():
    pos, _ = robot.get_world_poses()
    return np.asarray(pos).reshape(-1)[:3].astype(float)


def wheel_speeds():
    v = np.asarray(robot.get_dof_velocities(dof_indices=wheel_indices)).reshape(-1)
    return [round(float(x), 3) for x in v[:2]]


set_camera_view(eye=[spawn[0] - 3.5, spawn[1] - 3.5, floor_top + 2.2],
                target=[spawn[0], spawn[1], floor_top + 0.4],
                camera_prim_path="/OmniverseKit_Persp")

# Let it drop onto its wheels before commanding anything. A robot that loses no
# height here is not being simulated as a free body.
for _ in range(180):
    simulation_app.update()
settled = pose()
print(f"settled at {tuple(round(float(v), 3) for v in settled)} "
      f"({settled[2] - spawn[2]:+.3f} m from spawn)")
report["settled"] = [round(float(v), 3) for v in settled]

from omni.kit.viewport.utility import capture_viewport_to_file, get_active_viewport

vp = get_active_viewport()
shot_start = os.path.join(args.out, f"{name}-carter-start.png")
capture_viewport_to_file(vp, file_path=shot_start)
app_utils.update_app(steps=90)

# Ramp the speed up. Commanding the full wheel velocity to a stationary
# two-wheel robot is a torque impulse: Nova Carter does a wheelie and ends up
# balanced on its nose, wheels spinning, 0.5 m higher than it started.
RAMP_S = 2.0

start = pose()
track, wheels = [], []
steps = int(args.seconds / DT)
for i in range(steps):
    ramp = min(1.0, (i * DT) / RAMP_S)
    left, right = controller.forward([args.speed * ramp, args.turn * ramp])
    robot.apply_wheel_actions(np.array([left, right]))
    simulation_app.update()
    if i % 60 == 0:
        track.append([round(float(v), 3) for v in pose()])
        wheels.append(wheel_speeds())

final = pose()
travelled = float(np.linalg.norm(final[:2] - start[:2]))
drop = float(final[2] - start[2])

shot_end = os.path.join(args.out, f"{name}-carter-end.png")
capture_viewport_to_file(vp, file_path=shot_end)
app_utils.update_app(steps=90)
app_utils.stop()

print(f"\nstart  {tuple(round(float(v), 3) for v in start)}")
print(f"end    {tuple(round(float(v), 3) for v in final)}")
print(f"travelled {travelled:.2f} m in {args.seconds:.0f} s, height change {drop:+.3f} m")
print(f"track: {track}")
print(f"wheel rad/s: {wheels}")

report.update({
    "seconds": args.seconds,
    "commanded_speed": args.speed,
    "start": [round(float(v), 3) for v in start],
    "end": [round(float(v), 3) for v in final],
    "travelled_m": round(travelled, 3),
    "height_change_m": round(drop, 3),
    "track": track,
    "wheel_speeds": wheels,
    "shots": [shot_start, shot_end],
})
out_json = os.path.join(args.out, f"{name}-carter.json")
with open(out_json, "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2)
print(f"wrote {out_json}")

if not args.headless:
    print("window is open -- close it to end")
    while simulation_app.is_running():
        simulation_app.update()

simulation_app.close()
