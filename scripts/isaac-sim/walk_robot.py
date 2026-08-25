"""Walk a legged robot through a generated world, under its own locomotion policy.

`teleop.py` moves a camera by raycast: it probes the navigation map and is not a
robot. This is a real articulation -- joints, foot contacts, a centre of mass,
and a learned policy closing the loop on top of 200 Hz physics. The keyboard
sets a velocity command; the policy decides where to put the feet.

Four robots ship with Isaac Sim, all as `*FlatTerrainPolicy` classes:

    anymal   ANYbotics ANYmal C, quadruped     spot   Boston Dynamics Spot
    go2      Unitree Go2, quadruped            h1     Unitree H1, humanoid biped

Controls (hold -- the command is a velocity, not a step):

    W/S  forward, back      A/D  strafe      LEFT/RIGHT or Q/E  turn
    close the window to quit

**Run physics at the rate the policy declares, not at a rate copied from an
example.** `load_policy` parses `_dt` and `_decimation` out of the policy's own
env config -- Spot's are 0.002 and 10, so 500 Hz physics and 50 Hz control. The
standalone examples hardcode 1/200 and the interactive one uses 1/500, which is
the tell that the number is not incidental. At 1/200 the policy is close enough
to stumble forward under a firm command, and far enough off that it cannot hold
itself up:

    physics 1/200, zero command      collapses, z=0.196
    physics 1/200, 1.0 m/s forward   walks, 1.2 m, then falls
    policy's own dt, zero command    stands, z=0.462
    policy's own dt, 1.0 m/s         4.39 m on flat ground, upright throughout

This took a long detour because at 1/200 the system is marginal, and marginal
systems produce convincing false leads: the scene-setup order, the moment the
command is first assigned, a missing `post_reset()` -- each looked causal, each
reproduced or vanished once, and none of them were it. Two things eventually
cut through. A collapse looks identical whether the policy is running or the
physics callback died silently in carb, so the callback now counts its own steps
and reports its own exceptions. And the answer came from the policy's config
rather than from another example.

`post_reset()` is kept below anyway -- the shipped interactive demo calls it and
it is the right thing to do -- but on its own it changed nothing, to three
decimal places.

`torch` is required -- `PolicyController.load_policy` calls `torch.jit.load`.
The CPU build is enough; these policies are small MLPs.

Usage:
    D:\\isaacsim-6.0.1\\python.bat scripts/isaac-sim/walk_robot.py <usdz> --robot spot
"""
import argparse
import json
import os
import sys

sys.stdout.reconfigure(line_buffering=True)

parser = argparse.ArgumentParser()
parser.add_argument("usd", help="a .usdz exported with --up-axis z")
parser.add_argument("--robot", default="spot", choices=("anymal", "spot", "go2", "h1"))
parser.add_argument("--out", help="directory for the report")
parser.add_argument("--headless", action="store_true")
parser.add_argument("--auto", type=float, default=0.0,
                    help="walk forward for N seconds and report, instead of reading keys")
parser.add_argument("--speed", type=float, default=1.0, help="commanded m/s at full stick")
parser.add_argument("--flat-ground", action="store_true",
                    help="control run: NVIDIA's flat grid instead of the world")
parser.add_argument("--settle", type=int, default=1,
                    help="frames before the robot is driven; 1 is the tested value")
parser.add_argument("--show-collider", action="store_true",
                    help="look at the collision mesh instead of the splat")
args = parser.parse_args()

from isaacsim import SimulationApp

simulation_app = SimulationApp({"headless": args.headless})

import carb
import carb.input
import numpy as np
import omni.appwindow
import omni.timeline
import omni.usd
from isaacsim.core.deprecation_manager import import_module
from isaacsim.core.experimental.utils.stage import define_prim
from isaacsim.core.rendering_manager import RenderingManager
from isaacsim.core.simulation_manager import SimulationManager
from isaacsim.core.simulation_manager.impl.isaac_events import IsaacEvents
from isaacsim.core.utils.viewports import set_camera_view
from isaacsim.robot.policy.examples.robots import (
    AnymalFlatTerrainPolicy,
    Go2FlatTerrainPolicy,
    H1FlatTerrainPolicy,
    SpotFlatTerrainPolicy,
)
from isaacsim.storage.native import get_assets_root_path
from pxr import Usd, UsdGeom

torch = import_module("torch")

# Spawn height of each base, from NVIDIA's own examples. These are drop heights,
# not standing heights -- Spot spawns at 0.8 and stands at about 0.48.
ROBOTS = {
    "anymal": (AnymalFlatTerrainPolicy, 0.70),
    "spot": (SpotFlatTerrainPolicy, 0.80),
    "go2": (Go2FlatTerrainPolicy, 0.50),
    "h1": (H1FlatTerrainPolicy, 1.05),
}
POLICY_CLASS, SPAWN_HEIGHT = ROBOTS[args.robot]
GRID = "/Isaac/Environments/Grid/default_environment.usd"
SETTLE_FRAMES = args.settle
# Where the travelled distance is measured from -- after it is up on its feet,
# which is not the same instant as when it starts being driven.
MEASURE_FROM = max(SETTLE_FRAMES, 30)

usd_path = os.path.abspath(args.usd).replace("\\", "/")
name = os.path.splitext(os.path.basename(args.usd))[0]
out_dir = args.out or os.path.dirname(usd_path)
os.makedirs(out_dir, exist_ok=True)
report = {"usd": usd_path, "robot": args.robot, "flat_ground": args.flat_ground}

physics_ready = False
base_command = None
steps_run = 0
callback_error = None


def on_physics_step(step_size, _context):
    """From `interactive/quadruped/quadruped_example.py`, the shipped demo that
    is actually driven by a keyboard.

    `post_reset()` is the line that matters. It calls
    `robot.reset_to_default_state()`, putting the joints in the pose
    `initialize()` registered as the policy's default. Without it the robot
    starts from whatever the USD happens to ship, which is out of distribution
    for the policy: given a strong forward command it can sometimes stumble its
    way out, which is why an automated forward walk looked like it worked, but
    left at rest it simply falls over. The standalone examples get away with
    omitting it because they never stop commanding motion.
    """
    global physics_ready, steps_run, callback_error
    try:
        if not robot.robot.is_physics_tensor_entity_valid():
            physics_ready = False
        if physics_ready:
            robot.forward(step_size, base_command)
            steps_run += 1
        else:
            physics_ready = True
            robot.initialize()
            robot.post_reset()
            print(f"policy ready (step {step_size:.5f}s)")
    except Exception as exc:  # noqa: BLE001
        # carb swallows anything raised here, and a policy that never runs looks
        # exactly like a robot that cannot stand -- both just lie at z=0.196.
        if callback_error is None:
            callback_error = repr(exc)
            print(f"!! physics callback raised: {exc!r}")


assets_root = get_assets_root_path()
if assets_root is None:
    raise SystemExit("no Isaac assets root -- is the machine offline?")

# --- environment, then physics scene, then robot -------------------------
ground = define_prim("/World/Ground", "Xform")
ground.GetReferences().AddReference(
    (assets_root + GRID) if args.flat_ground else usd_path
)

define_prim("/World/PhysicsScene", "PhysicsScene")
RenderingManager.set_dt(8.0 / 200.0)
SimulationManager.set_physics_sim_device("cpu")
SimulationManager.set_physics_dt(1.0 / 200.0)

stage = omni.usd.get_context().get_stage()
floor_z = 0.0
if args.flat_ground:
    print("control run: NVIDIA flat grid, floor at z=0")
else:
    collider = stage.GetPrimAtPath("/World/Ground/Background/Collider")
    if not (collider and collider.IsValid()):
        raise SystemExit("no collision mesh in this export -- nothing to walk on")
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                              [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    rng = cache.ComputeWorldBound(collider).ComputeAlignedRange()
    floor_prim = stage.GetPrimAtPath("/World/Ground/Background/Collider/floor")
    if floor_prim and floor_prim.IsValid():
        # The bbox *minimum*, not the maximum: the floor mesh spans a range
        # (0.004 to 0.371 on marble-office) and the low end is the ground the
        # robot stands on. Pure USD, no physics, so this can run before the
        # timeline ever plays -- which is what lets the robot be built while
        # stopped, the way the example does it.
        floor_z = float(cache.ComputeWorldBound(floor_prim).ComputeAlignedRange().GetMin()[2])
    print(f"room {rng.GetMax()[0] - rng.GetMin()[0]:.2f} x "
          f"{rng.GetMax()[1] - rng.GetMin()[1]:.2f} m, floor z={floor_z:.3f}")

    splat = stage.GetPrimAtPath("/World/Ground/Background/Splat")
    if splat and splat.IsValid() and not args.show_collider:
        UsdGeom.Imageable(collider).MakeInvisible()
        print("splat visible, collision mesh hidden (it still collides)")
    else:
        ceiling = stage.GetPrimAtPath("/World/Ground/Background/Collider/ceiling")
        if ceiling and ceiling.IsValid():
            UsdGeom.Imageable(ceiling).MakeInvisible()

spawn = [0.0, 0.0, floor_z + SPAWN_HEIGHT]
print(f"spawn {tuple(round(v, 3) for v in spawn)}  ({args.robot})")
report["spawn"] = [round(v, 3) for v in spawn]

robot = POLICY_CLASS(prim_path=f"/World/{args.robot}", position=spawn)

# Run physics at the rate the policy was trained at, rather than at a number
# copied from an example. `load_policy` parses `_dt` and `_decimation` out of the
# policy's own env config; the standalone examples hardcode 1/200 and the
# interactive one uses 1/500, which is a hint that the number is not incidental.
policy_dt = getattr(robot, "_dt", None)
decimation = getattr(robot, "_decimation", None)
print(f"policy env: dt={policy_dt}, decimation={decimation}, "
      f"render_interval={getattr(robot, 'render_interval', None)}")
if policy_dt:
    SimulationManager.set_physics_dt(policy_dt)
    RenderingManager.set_dt(policy_dt * (decimation or 4))

base_command = torch.zeros(3)

# --- keyboard ------------------------------------------------------------
# NVIDIA's subscription, from anymal_standalone.py. One change: a *set* of held
# keys rather than their running vector sum -- the window can lose focus
# mid-press and never deliver the release, and a sum then walks on by itself.
held = set()
KEYS = {"W", "S", "A", "D", "Q", "E", "LEFT", "RIGHT", "UP", "DOWN"}


def on_keyboard(event, *_a, **_kw):
    if event.type == carb.input.KeyboardEventType.KEY_PRESS:
        if event.input.name in KEYS:
            held.add(event.input.name)
    elif event.type == carb.input.KeyboardEventType.KEY_RELEASE:
        held.discard(event.input.name)
    return True


appwindow = omni.appwindow.get_default_app_window()
input_iface = carb.input.acquire_input_interface()
sub = input_iface.subscribe_to_keyboard_events(appwindow.get_keyboard(), on_keyboard)


def keyboard_command():
    """Velocity command [forward, left, yaw] from the held keys."""
    ahead = ("W" in held or "UP" in held) - ("S" in held or "DOWN" in held)
    side = ("A" in held) - ("D" in held)
    turn = ("LEFT" in held or "Q" in held) - ("RIGHT" in held or "E" in held)
    return torch.tensor([ahead * args.speed, side * args.speed, turn * 2.0])


SimulationManager.register_callback(on_physics_step, IsaacEvents.POST_PHYSICS_STEP)
omni.timeline.get_timeline_interface().play()
simulation_app.update()


def pose():
    return np.asarray(robot.robot.get_world_poses()[0]).reshape(-1)[:3].astype(float)


if args.auto:
    track, start = [], None
    for i in range(int(args.auto / (8.0 / 200.0))):
        simulation_app.update()
        if not SimulationManager.is_simulating():
            continue
        if i >= SETTLE_FRAMES:
            base_command = torch.tensor([args.speed, 0.0, 0.0])
        if i == MEASURE_FROM:
            start = pose()
        if start is not None and i % 25 == 0:
            track.append([round(float(v), 3) for v in pose()])
    final = pose()
    print(f"policy steps run: {steps_run}, callback error: {callback_error}")
    travelled = float(np.linalg.norm(final[:2] - start[:2]))
    print(f"\nstart {tuple(round(float(v), 3) for v in start)}")
    print(f"end   {tuple(round(float(v), 3) for v in final)}")
    print(f"travelled {travelled:.2f} m, standing "
          f"{final[2] - floor_z:.3f} m above the floor")
    print(f"track: {track}")
    report.update({
        "seconds": args.auto,
        "commanded_speed": args.speed,
        "start": [round(float(v), 3) for v in start],
        "end": [round(float(v), 3) for v in final],
        "travelled_m": round(travelled, 3),
        "standing_height_m": round(float(final[2] - floor_z), 3),
        "track": track,
    })
    out_json = os.path.join(out_dir, f"{name}-{args.robot}-walk.json")
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"wrote {out_json}")
else:
    print("\n  W/S forward, back     A/D strafe     LEFT/RIGHT or Q/E turn")
    print("  click the viewport first, then hold a key\n")
    frame = 0
    while simulation_app.is_running():
        simulation_app.update()
        frame += 1
        if SimulationManager.is_simulating() and frame >= SETTLE_FRAMES:
            base_command = keyboard_command()
        if frame % 4 == 0:
            # Chase camera, every fourth frame rather than every frame: the
            # viewport is the user's too, and taking the transform 60 times a
            # second makes it impossible to look anywhere else.
            p = pose()
            set_camera_view(eye=[p[0] - 2.6, p[1] - 2.6, p[2] + 1.5],
                            target=[p[0], p[1], p[2]],
                            camera_prim_path="/OmniverseKit_Persp")
        if frame % 120 == 0 and held:
            p = pose()
            print(f"  at ({p[0]:.2f}, {p[1]:.2f}, {p[2]:.2f})  keys {sorted(held)}")

input_iface.unsubscribe_to_keyboard_events(appwindow.get_keyboard(), sub)
simulation_app.close()
