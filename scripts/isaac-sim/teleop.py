"""Walk through a generated world in Isaac Sim, on the keyboard.

This is the navigation map with a person in it. Every step is decided by the
same `raycast_closest` query `build_navmap.py` grids the room with -- down to
find the floor, forward to find a wall -- so if you can walk somewhere here, the
map says free there, and the two cannot drift apart.

Deliberately kinematic. A capsule under PhysX would bring back the contact
behaviour that stops `drive_robot.py` driving straight over a marching-cubes
floor, which has nothing to do with whether the room is navigable. Raycasts see
the real triangles; contact resolution is the part that does not work yet.

    W/S      forward, back            LEFT/RIGHT, Q/E   turn
    A/D      strafe                   SHIFT             run
    R        back to the spawn        close the window  quit

The collision mesh is hidden so the Gaussian splat is what you see -- hiding a
prim does not disable its collider, so you still cannot walk through walls.

Usage:
    D:\\isaacsim-6.0.1\\python.bat scripts/isaac-sim/teleop.py <usdz>
"""
import argparse
import math
import os
import sys

sys.stdout.reconfigure(line_buffering=True)

parser = argparse.ArgumentParser()
parser.add_argument("usd", help="a .usdz exported with --up-axis z")
parser.add_argument("--eye-height", type=float, default=1.60, help="metres")
parser.add_argument("--speed", type=float, default=1.4, help="m/s, walking")
parser.add_argument("--body-radius", type=float, default=0.30,
                    help="how close to a wall you may get, metres")
parser.add_argument("--step-up", type=float, default=0.45,
                    help="the biggest drop or rise a step may cross, metres")
parser.add_argument("--show-collider", action="store_true",
                    help="look at the collision mesh instead of the splat")
args = parser.parse_args()

from isaacsim import SimulationApp

simulation_app = SimulationApp({"headless": False})

import carb
import carb.input
import isaacsim.core.experimental.utils.app as app_utils
import omni.appwindow
import omni.physx
import omni.usd
from isaacsim.core.experimental.objects import DomeLight
from isaacsim.core.experimental.utils import stage as stage_utils
from isaacsim.core.simulation_manager import SimulationManager
from isaacsim.core.utils.viewports import set_camera_view
from pxr import Usd, UsdGeom

DT = 1.0 / 60.0
TURN_RATE = 1.8      # rad/s
RUN_FACTOR = 2.5
CAST_HEIGHTS = (0.30, 1.20)   # shin and chest, so a low obstacle stops you too

usd_path = os.path.abspath(args.usd).replace("\\", "/")

# --- stage ---------------------------------------------------------------
stage_utils.set_stage_up_axis("Z")
stage_utils.set_stage_units(meters_per_unit=1.0)
stage_utils.add_reference_to_stage(usd_path=usd_path, path="/Environment")
app_utils.update_app(steps=150)

stage = omni.usd.get_context().get_stage()
collider = stage.GetPrimAtPath("/Environment/Background/Collider")
if not (collider and collider.IsValid()):
    raise SystemExit("no /Environment/Background/Collider -- nothing to walk on")

cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                          [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
rng = cache.ComputeWorldBound(collider).ComputeAlignedRange()
wmin, wmax = rng.GetMin(), rng.GetMax()
floor_prim = stage.GetPrimAtPath("/Environment/Background/Collider/floor")
floor_top = float(cache.ComputeWorldBound(floor_prim).ComputeAlignedRange().GetMax()[2])
print(f"room {wmax[0] - wmin[0]:.2f} x {wmax[1] - wmin[1]:.2f} m, floor top z={floor_top:.3f}")

splat = stage.GetPrimAtPath("/Environment/Background/Splat")
if splat and splat.IsValid() and not args.show_collider:
    UsdGeom.Imageable(collider).MakeInvisible()
    print("splat visible, collision mesh hidden (it still collides)")
else:
    if not splat or not splat.IsValid():
        print("no splat in this export -- showing the collision mesh")
    ceiling = stage.GetPrimAtPath("/Environment/Background/Collider/ceiling")
    if ceiling and ceiling.IsValid():
        UsdGeom.Imageable(ceiling).MakeInvisible()

DomeLight("/World/DomeLight").set_intensities(600)
SimulationManager.setup_simulation(dt=DT, device="cpu")
app_utils.play()
app_utils.update_app(steps=30)
sq = omni.physx.get_physx_scene_query_interface()


def floor_at(x, y, from_z):
    """Height of the reconstructed floor under (x, y), or None.

    `/World/GroundPlane` is skipped for the same reason `build_navmap.py` skips
    it: it is an infinite plane at z=0 put there so a spawned body has something
    under it, and treating it as floor would let you walk out of the building
    across ground the reconstruction never saw.
    """
    hit = sq.raycast_closest((float(x), float(y), float(from_z)), (0.0, 0.0, -1.0), 6.0)
    if not (hit and hit.get("hit")):
        return None
    if str(hit.get("collision", "")).endswith("GroundPlane"):
        return None
    return float(from_z) - float(hit["distance"])


def blocked(x, y, z, dx, dy, distance):
    """Is something in the way of a step from (x, y) along (dx, dy)?"""
    for height in CAST_HEIGHTS:
        hit = sq.raycast_closest((float(x), float(y), float(z + height)),
                                 (float(dx), float(dy), 0.0), float(distance))
        if hit and hit.get("hit") and not str(hit.get("collision", "")).endswith("GroundPlane"):
            return True
    return False


# --- where to start ------------------------------------------------------
# The world origin is `scene.json`'s spawnPoint and is inside the reachable
# region on both Marble worlds, so try it before hunting for anywhere at all.
cx, cy = float((wmin[0] + wmax[0]) / 2), float((wmin[1] + wmax[1]) / 2)
spawn = None
for px, py in [(0.0, 0.0), (cx, cy)]:
    z = floor_at(px, py, floor_top + 1.5)
    if z is not None and z <= floor_top + 0.12:
        spawn = (px, py, z)
        break
if spawn is None:
    for radius in (0.5, 1.0, 1.5, 2.0, 3.0):
        for dx, dy in ((radius, 0), (-radius, 0), (0, radius), (0, -radius)):
            z = floor_at(cx + dx, cy + dy, floor_top + 1.5)
            if z is not None and z <= floor_top + 0.12:
                spawn = (cx + dx, cy + dy, z)
                break
        if spawn:
            break
if spawn is None:
    raise SystemExit("found no floor to stand on")
print(f"spawn {tuple(round(v, 3) for v in spawn)}")

pos = list(spawn)
yaw = 0.0

# --- keyboard ------------------------------------------------------------
# NVIDIA's pattern, from standalone_examples/api/isaacsim.robot.policy.examples/
# anymal_standalone.py. A held-key *set* rather than their running vector sum:
# the window can lose focus mid-press and never deliver the release, and a sum
# then keeps walking on its own forever.
held = set()
WALK_KEYS = {"W", "S", "A", "D", "Q", "E", "LEFT", "RIGHT", "UP", "DOWN",
             "LEFT_SHIFT", "RIGHT_SHIFT", "R"}


def on_keyboard(event, *_args, **_kwargs):
    if event.type == carb.input.KeyboardEventType.KEY_PRESS:
        if event.input.name in WALK_KEYS:
            held.add(event.input.name)
    elif event.type == carb.input.KeyboardEventType.KEY_RELEASE:
        held.discard(event.input.name)
    return True


appwindow = omni.appwindow.get_default_app_window()
input_iface = carb.input.acquire_input_interface()
sub = input_iface.subscribe_to_keyboard_events(appwindow.get_keyboard(), on_keyboard)

print("\n  W/S forward, back     A/D strafe     LEFT/RIGHT or Q/E turn")
print("  SHIFT run             R respawn      close the window to quit\n")


def look():
    fx, fy = math.cos(yaw), math.sin(yaw)
    eye = [pos[0], pos[1], pos[2] + args.eye_height]
    set_camera_view(eye=eye,
                    target=[pos[0] + fx, pos[1] + fy, pos[2] + args.eye_height],
                    camera_prim_path="/OmniverseKit_Persp")


look()
frame = 0
while simulation_app.is_running():
    if "R" in held:
        pos, yaw = list(spawn), 0.0
        held.discard("R")

    turn = ("LEFT" in held or "Q" in held) - ("RIGHT" in held or "E" in held)
    yaw += turn * TURN_RATE * DT

    ahead = ("W" in held or "UP" in held) - ("S" in held or "DOWN" in held)
    side = ("D" in held) - ("A" in held)
    if ahead or side:
        speed = args.speed * (RUN_FACTOR if held & {"LEFT_SHIFT", "RIGHT_SHIFT"} else 1.0)
        fx, fy = math.cos(yaw), math.sin(yaw)
        # right-handed, z up: strafe right is forward x up = (fy, -fx)
        mx, my = fx * ahead + fy * side, fy * ahead - fx * side
        length = math.hypot(mx, my)
        if length > 1e-9:
            mx, my = mx / length, my / length
            travel = speed * DT
            if not blocked(pos[0], pos[1], pos[2], mx, my, travel + args.body_radius):
                nx, ny = pos[0] + mx * travel, pos[1] + my * travel
                # Never step onto nothing, and never off a ledge: the floor has
                # to be there, and within one step of where you already are.
                nz = floor_at(nx, ny, pos[2] + args.eye_height)
                if nz is not None and abs(nz - pos[2]) <= args.step_up:
                    pos = [nx, ny, nz]

    look()
    simulation_app.update()
    frame += 1
    if frame % 300 == 0 and (ahead or side or turn):
        print(f"  at ({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f})  yaw {math.degrees(yaw):6.1f}")

input_iface.unsubscribe_to_keyboard_events(appwindow.get_keyboard(), sub)
simulation_app.close()
