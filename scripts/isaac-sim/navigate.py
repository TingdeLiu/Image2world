"""Walk a legged robot to a goal, on the map built from the room it is standing in.

Everything before this was open loop. `build_navmap.py` measures the room,
`check_navmap.py` proves a planner can cross the map, `nav2_check.sh` proves
nav2's planner can too -- and `walk_robot.py` walks a robot around the same room
under a keyboard. Nothing joined the two halves, so "the map is usable" was
still an argument rather than a demonstration.

This closes it. The plan comes off the map on disk, through the same `navmap`
module `check_navmap.py` uses, so the route being driven is the route that was
checked. The robot is the same policy-driven articulation `walk_robot.py`
drives. The only new thing is the controller between them.

    map -> A* -> string-pulled polyline -> pure pursuit -> [vx, vy, yaw] -> policy

What it reports is the part that matters: not "it moved", but how far the robot
strayed from the line it was given, whether it arrived, and how the driven
distance compares with the planned one. A robot that reaches the goal by
bouncing off two walls has not navigated.

The controller is deliberately plain -- proportional heading, a lookahead point,
and a gate that turns before walking. A legged base that can strafe and spin in
place does not need anything cleverer, and anything cleverer would make it
harder to tell a map problem from a controller problem, which is the whole
point of running this. That mattered: the first run tracked badly enough to hit
a wall, and the fix was one line about which point to steer at rather than a
gain to tune.

Two defaults differ from what `check_navmap.py` reports, and both are about the
difference between a real room and a reconstruction of one:

  * Unknown cells are obstacles here. nav2 leaves NO_INFORMATION alone because
    a real floor exists whether or not the map saw it; in here the room *is*
    the reconstruction, and the robot's feet find nothing. `--allow-unknown`
    restores nav2's rule.
  * The planner pays for clearance, so the route runs down the middle of the
    space rather than skimming the inflation boundary, which is where the
    shortest route through an inflated map always goes.

And on a stall it does not simply re-plan -- that returns the same route on an
unchanged map, six times in a row, measured. It writes off the patch of map
ahead of it, backs out, and goes round.

Usage:
    D:\\isaacsim-6.0.1\\python.bat scripts/isaac-sim/navigate.py \\
        public/worlds/marble-office/export/marble-office-with-splat.usdz \\
        --map public/worlds/marble-office/export/marble-office-navmap.yaml \\
        --goal 3.3,9.0
"""
import argparse
import json
import math
import os
import re
import sys

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

parser = argparse.ArgumentParser()
parser.add_argument("usd", help="a .usdz exported with --up-axis z")
parser.add_argument("--map", required=True, help="the -navmap.yaml for this world")
parser.add_argument("--goal", metavar="X,Y",
                    help="world metres; default is the furthest reachable cell, "
                         "which is the longest journey the map claims exists")
parser.add_argument("--start", metavar="X,Y", default="0,0",
                    help="where to spawn; the viewer's own spawn is the origin")
parser.add_argument("--robot", default="spot", choices=("anymal", "spot", "go2", "h1"))
parser.add_argument("--robot-radius", type=float, default=0.35,
                    help="planning clearance; 0.35 is about Spot's half-width")
parser.add_argument("--allow-unknown", action="store_true",
                    help="plan through cells with no floor under them, the way "
                         "nav2 does. Right for a real robot in a real room; in "
                         "here the missing floor is genuinely missing")
parser.add_argument("--prefer-clearance", type=float, default=0.6,
                    help="metres of room the planner tries to keep on each side; "
                         "0 turns the preference off and takes the shortest route")
parser.add_argument("--clearance-weight", type=float, default=3.0,
                    help="how much dearer a cell with no clearance is")
parser.add_argument("--speed", type=float, default=0.8, help="max commanded m/s")
parser.add_argument("--lookahead", type=float, default=0.7,
                    help="pure-pursuit lookahead, metres")
parser.add_argument("--arrive", type=float, default=0.35,
                    help="how close to the goal counts as arrived, metres")
parser.add_argument("--timeout", type=float, default=180.0, help="seconds of sim time")
parser.add_argument("--recoveries", type=int, default=6,
                    help="how many times to back off and route around something the "
                         "map does not have, before giving up. 0 stops at the first stall")
parser.add_argument("--recovery-radius", type=float, default=0.45,
                    help="how much of the way ahead to write off after a stall, metres")
parser.add_argument("--headless", action="store_true")
parser.add_argument("--out", help="directory for the report and the overlay")
parser.add_argument("--show-collider", action="store_true",
                    help="look at the collision mesh instead of the splat")
parser.add_argument("--obstacle", action="append", metavar="X,Y[,SIZE[,HEIGHT]]",
                    default=[],
                    help="drop a solid box into the scene that the map does not "
                         "know about, to exercise the recovery behaviour. HEIGHT "
                         "defaults to SIZE, giving a cube; setting it lower makes "
                         "a wide low ledge, which is a step to climb rather than a "
                         "block to walk round. Repeatable")

# `--goal -2.01,-3.93` is a usage error to argparse: the value starts with a
# dash and is not a bare negative number, so it reads as an unknown flag and the
# run dies in a usage dump. Half of any room's coordinates are negative, so this
# is not an edge case -- pointing the robot at the left half of its own map was
# a syntax error, and the fix on offer was to know to write `--goal=`.
# Reattach the value instead, and only when it really is a coordinate.
POINT_FLAGS = ("--goal", "--start", "--obstacle")
POINT = re.compile(r"-?[0-9.]+(,-?[0-9.]+){1,3}$")
rejoined, skip = [], False
for i, token in enumerate(sys.argv[1:]):
    if skip:
        skip = False
        continue
    value = sys.argv[i + 2] if i + 2 < len(sys.argv) else None
    if token in POINT_FLAGS and value and value.startswith("-") and POINT.match(value):
        rejoined.append(f"{token}={value}")
        skip = True
    else:
        rejoined.append(token)
sys.argv[1:] = rejoined

args = parser.parse_args()

import numpy as np  # noqa: E402  -- before SimulationApp, which is fine and much faster

import navmap  # noqa: E402
import usdfloor  # noqa: E402

# --- plan first, and refuse early ----------------------------------------
# Starting Isaac Sim takes half a minute. If the goal is unreachable, or off the
# map, that should be said now rather than after the room has been loaded.
carte = navmap.load(args.map)
# Unknown cells count as obstacles unless asked otherwise, and that is the
# opposite of what `check_navmap.py` reports as the headline. Both are right for
# their question. nav2 leaves NO_INFORMATION alone because a real room has a
# floor the map merely failed to see; here the room *is* the reconstruction, and
# a cell with no floor in the map has no floor in the collision mesh either --
# the robot's feet find nothing. Planning nav2's way sent Spot down a corridor
# with 0.15 m of clearance, where it wedged and stopped dead.
# How the map was built decides what the robot can do with it, so say so here
# rather than leaving it to whoever reads two files. A map with no step limit
# describes a step as floor, and a flat-terrain policy walks into it and stops:
# that is not a controller failure, and it took three runs to stop reading it as
# one.
build = {}
build_json = args.map.replace("-navmap.yaml", "-navmap.json")
if os.path.isfile(build_json):
    with open(build_json, encoding="utf-8") as fh:
        build = json.load(fh)
    print(f"map built by Isaac Sim {build.get('isaac_sim', '?')}, "
          f"cell {build.get('cell_size')} m, robot height {build.get('robot_height')} m, "
          f"max step {build.get('max_step', 0)} m")
    if not build.get("max_step"):
        print("  !! this map was built without --max-step, so a step it calls floor "
              "can stop a robot on it")

avoid_unknown = not args.allow_unknown
passable = carte.passable(args.robot_radius, avoid_unknown=avoid_unknown)
print(f"map {carte.width} x {carte.height} @ {carte.resolution} m, "
      f"origin ({carte.origin[0]:.3f}, {carte.origin[1]:.3f})")
print(f"passable for r={args.robot_radius} m "
      f"({'holes count as obstacles' if avoid_unknown else 'holes allowed, nav2 style'}): "
      f"{int(passable.sum())} cells ({int(passable.sum()) * carte.resolution ** 2:.1f} m2)")

# Where the robot is dropped has to fit the whole body, not the planning disc.
# `--robot-radius` is about half Spot's *width*; its body is 1.1 m long, so a
# start 0.35 m from the edge of the map can put the rear feet over nothing. That
# is not hypothetical: `dinghao-2f`'s origin sits on the edge of a hole the
# scanner left where its operator stood, the nearest passable cell was 0.29 m
# from it, and Spot fell in backwards 1.2 s after landing. Half-lengths are the
# manufacturers' published body dimensions. Unknown cells always count here --
# whatever --allow-unknown says about routes, there is no floor to land on.
BODY_HALF_LENGTH = {"spot": 0.55, "anymal": 0.47, "go2": 0.35, "h1": 0.25}
spawn_room = max(args.robot_radius, BODY_HALF_LENGTH[args.robot])
standable = carte.passable(spawn_room, avoid_unknown=True) & passable
start_xy = tuple(float(v) for v in args.start.split(","))
start_cell = carte.cell_of(*start_xy)
if not (carte.inside(start_cell) and standable[start_cell]):
    moved = navmap.nearest_passable(standable, start_cell)
    if moved is None:
        raise SystemExit(f"nowhere on this map has {spawn_room} m of room to drop the robot")
    print(f"start {start_xy} does not leave {spawn_room} m of room for the body; "
          f"nearest that does is {tuple(round(v, 2) for v in carte.world_of(*moved))}")
    start_cell = moved

if args.goal:
    goal_xy = tuple(float(v) for v in args.goal.split(","))
    goal_cell = carte.cell_of(*goal_xy)
    if not (carte.inside(goal_cell) and passable[goal_cell]):
        raise SystemExit(f"goal {goal_xy} is not passable on this map")
# Shortest is not best. Through an inflated map the shortest route runs along
# the inflation boundary, so the robot is sent to skim every wall by exactly its
# own radius, with nothing left over for tracking error. This is nav2's
# inflation layer in two lines: prefer the middle, pay for the corners.
room = navmap.clearance(~passable, carte.resolution, cap_m=max(args.prefer_clearance, 0.05))
cost = (navmap.clearance_cost(room, args.prefer_clearance, args.clearance_weight)
        if args.prefer_clearance > 0 else None)

if not args.goal:
    # The furthest *reachable* cell, not the furthest passable one -- and one
    # with room around it. Taking the furthest passable cell means the default
    # goal can sit in a pocket with no route to it, or hard against the edge of
    # the passable region, which asks the robot to finish standing with no
    # clearance at all.
    goal_cell = navmap.furthest_reachable(passable, start_cell, room=room,
                                          min_clearance=args.prefer_clearance * 0.5)
    if goal_cell is None:
        raise SystemExit("nothing is reachable from the start at this radius")

route = navmap.plan(passable, start_cell, goal_cell, cost=cost)
if route is None:
    raise SystemExit("no route from start to goal at this radius")
waypoints = [carte.world_of(*cell) for cell in navmap.shortcut(route, passable)]
planned_m = navmap.path_length(waypoints)

# Clearance is measured against the map itself, not the inflated copy the
# planner used, so the number means "metres to the nearest thing that is not
# floor". The endpoints are excluded: the start is where the robot happens to
# be and the goal is a destination, and either can legitimately be tight
# without saying anything about the route between them.
real_room = navmap.clearance(carte.grid != navmap.FREE, carte.resolution, cap_m=1.5)
along = np.array([float(real_room[cell]) for cell in route])
margin = max(1, int(0.5 / carte.resolution))
inner = along[margin:-margin] if len(along) > 2 * margin else along
print(f"planned {len(route)} cells -> {len(waypoints)} waypoints, {planned_m:.2f} m")
print(f"  clearance along it: tightest {inner.min():.2f} m, median "
      f"{float(np.median(along)):.2f} m")
print(f"  {tuple(round(v, 2) for v in waypoints[0])} -> "
      f"{tuple(round(v, 2) for v in waypoints[-1])}")

from isaacsim import SimulationApp  # noqa: E402

simulation_app = SimulationApp({"headless": args.headless})

import omni.timeline  # noqa: E402
import omni.usd  # noqa: E402
from isaacsim.core.deprecation_manager import import_module  # noqa: E402
from isaacsim.core.experimental.utils.stage import define_prim  # noqa: E402
from isaacsim.core.rendering_manager import RenderingManager  # noqa: E402
from isaacsim.core.simulation_manager import SimulationManager  # noqa: E402
from isaacsim.core.simulation_manager.impl.isaac_events import IsaacEvents  # noqa: E402
from isaacsim.core.utils.viewports import set_camera_view  # noqa: E402
from isaacsim.robot.policy.examples.robots import (  # noqa: E402
    AnymalFlatTerrainPolicy,
    Go2FlatTerrainPolicy,
    H1FlatTerrainPolicy,
    SpotFlatTerrainPolicy,
)
from pxr import Gf, Usd, UsdGeom, UsdPhysics  # noqa: E402

torch = import_module("torch")

# Drop heights from NVIDIA's own examples -- Spot spawns at 0.8 and stands near
# 0.48. See walk_robot.py for why the physics rate has to come off the policy.
ROBOTS = {
    "anymal": (AnymalFlatTerrainPolicy, 0.70),
    "spot": (SpotFlatTerrainPolicy, 0.80),
    "go2": (Go2FlatTerrainPolicy, 0.50),
    "h1": (H1FlatTerrainPolicy, 1.05),
}
POLICY_CLASS, SPAWN_HEIGHT = ROBOTS[args.robot]

usd_path = os.path.abspath(args.usd).replace("\\", "/")
name = os.path.splitext(os.path.basename(args.usd))[0]
out_dir = args.out or os.path.dirname(usd_path)
os.makedirs(out_dir, exist_ok=True)

physics_ready = False
base_command = None
steps_run = 0
callback_error = None


def on_physics_step(step_size, _context):
    """The shipped interactive demo's callback, with its own instrumentation.

    carb swallows exceptions raised in here, and a policy that never runs looks
    exactly like a robot that cannot stand: both lie on the floor. Counting the
    steps and recording the first exception is what separates the two.
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
        if callback_error is None:
            callback_error = repr(exc)
            print(f"!! physics callback raised: {exc!r}")


# --- the room ------------------------------------------------------------
ground = define_prim("/World/Ground", "Xform")
ground.GetReferences().AddReference(usd_path)
define_prim("/World/PhysicsScene", "PhysicsScene")
RenderingManager.set_dt(8.0 / 200.0)
SimulationManager.set_physics_sim_device("cpu")
SimulationManager.set_physics_dt(1.0 / 200.0)

stage = omni.usd.get_context().get_stage()
collider = stage.GetPrimAtPath("/World/Ground/Background/Collider")
if not (collider and collider.IsValid()):
    raise SystemExit("no collision mesh in this export -- nothing to walk on")
cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                          [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
# The floor under the SPAWN, not the floor part's bbox minimum -- see
# usdfloor.py for the run where the difference put Spot on its belly. It also
# matters below: lying at 0.09 m read as 0.28 m above that minimum and passed
# the 0.25 m fall test, so the run reported six recoveries from an obstacle that
# was not there.
spawn_xy = carte.world_of(*start_cell)
floor_z, floor_note = usdfloor.spawn_floor(
    stage, "/World/Ground/Background/Collider/floor", *spawn_xy, cache)
if floor_note:
    print(f"  !! {floor_note}")
splat = stage.GetPrimAtPath("/World/Ground/Background/Splat")
if splat and splat.IsValid() and not args.show_collider:
    UsdGeom.Imageable(collider).MakeInvisible()
else:
    ceiling = stage.GetPrimAtPath("/World/Ground/Background/Collider/ceiling")
    if ceiling and ceiling.IsValid():
        UsdGeom.Imageable(ceiling).MakeInvisible()

spawn = [spawn_xy[0], spawn_xy[1], floor_z + SPAWN_HEIGHT]
print(f"floor z={floor_z:.3f}, spawn {tuple(round(v, 3) for v in spawn)} ({args.robot})")

# --- something the map does not know about -------------------------------
# The recovery path is the one part of this that no successful run exercises, so
# without a way to provoke it, it is aspiration rather than behaviour. A static
# box in the scene and nothing in the map is the honest version of the case it
# exists for: someone left a chair in the corridor after the survey.
for index, spec in enumerate(args.obstacle):
    numbers = [float(v) for v in spec.split(",")]
    ox, oy = numbers[0], numbers[1]
    side = numbers[2] if len(numbers) > 2 else 0.6
    # A cube is a block to walk round; a wide low box is a step to climb, and
    # those are different questions. `--max-step` wants the second one and there
    # was no way to ask it, so its default is inherited rather than measured.
    tall = numbers[3] if len(numbers) > 3 else side
    cube = UsdGeom.Cube.Define(stage, f"/World/Obstacle_{index}")
    cube.CreateSizeAttr(1.0)
    cube.AddTranslateOp().Set(Gf.Vec3d(ox, oy, floor_z + tall / 2.0))
    cube.AddScaleOp().Set(Gf.Vec3f(side, side, tall))
    # Collision but no rigid body: it is furniture, not a projectile.
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    print(f"dropped a {side:.2f} x {side:.2f} x {tall:.2f} m box at ({ox}, {oy}) "
          f"that the map does not have")

robot = POLICY_CLASS(prim_path=f"/World/{args.robot}", position=spawn)
policy_dt = getattr(robot, "_dt", None)
decimation = getattr(robot, "_decimation", None)
if policy_dt:
    SimulationManager.set_physics_dt(policy_dt)
    RenderingManager.set_dt(policy_dt * (decimation or 4))
render_dt = policy_dt * (decimation or 4) if policy_dt else 8.0 / 200.0
print(f"policy env: dt={policy_dt}, decimation={decimation}, frame={render_dt:.4f}s")

base_command = torch.zeros(3)
SimulationManager.register_callback(on_physics_step, IsaacEvents.POST_PHYSICS_STEP)
timeline = omni.timeline.get_timeline_interface()
timeline.play()
simulation_app.update()
print(f"timeline: {timeline.get_start_time():.2f}s to {timeline.get_end_time():.2f}s, "
      f"looping={timeline.is_looping()}")


# --- where the robot is --------------------------------------------------
def pose():
    """(x, y, z, yaw). Isaac hands back quaternions as (w, x, y, z)."""
    positions, orientations = robot.robot.get_world_poses()
    p = np.asarray(positions).reshape(-1)[:3].astype(float)
    w, x, y, z = np.asarray(orientations).reshape(-1)[:4].astype(float)
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return p[0], p[1], p[2], yaw


BACKUP_SECONDS = 1.2    # how long to walk away from whatever was in the way
SPACING = 0.10          # metres between points on the dense path


def densify(polyline, spacing=SPACING):
    """The polyline resampled at a fixed spacing.

    Aiming at "the next waypoint" is not pure pursuit and does not track a line.
    String-pulling leaves marble-office's route with four waypoints, so the
    target sat six metres away; a lateral offset of half a metre is a four
    degree heading error at that range, and the controller barely answered it.
    The robot tracked a slow rightward drift the whole way up the room and put
    itself into a wall. Pursuing a point a fixed distance ahead *on the line*
    makes the same offset a large error, because the target is near.
    """
    points = [polyline[0]]
    for a, b in zip(polyline, polyline[1:]):
        legs = max(1, int(math.ceil(math.dist(a, b) / spacing)))
        for i in range(1, legs + 1):
            t = i / legs
            points.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return points


class Pursuit:
    """Where we are on the path, and the point to steer at.

    `progress` only ever moves forward. Letting it search the whole path each
    step means a route that doubles back can capture the robot onto the wrong
    leg and send it round in circles.
    """

    WINDOW = 60         # points, so 6 m at the default spacing

    def __init__(self, polyline, lookahead):
        self.points = densify(polyline)
        self.lookahead = lookahead
        self.progress = 0

    def update(self, x, y):
        """(target point, cross-track error, metres of path left)."""
        best, best_distance = self.progress, float("inf")
        for i in range(self.progress, min(self.progress + self.WINDOW, len(self.points))):
            distance = math.hypot(self.points[i][0] - x, self.points[i][1] - y)
            if distance < best_distance:
                best_distance, best = distance, i
        self.progress = best

        ahead, travelled = best, 0.0
        while ahead < len(self.points) - 1 and travelled < self.lookahead:
            travelled += math.dist(self.points[ahead], self.points[ahead + 1])
            ahead += 1
        left = SPACING * (len(self.points) - 1 - best)
        return self.points[ahead], best_distance, left


# --- the controller ------------------------------------------------------
MAX_YAW = 1.2          # rad/s; the policies are trained around this magnitude
K_YAW = 1.8
K_V = 1.2
# Beyond this heading error the robot turns and does not walk. Without it a
# legged base leans into a command it cannot follow and drifts wide on corners.
TURN_FIRST = math.radians(50.0)


def command_towards(target, here, final_leg):
    """Body-frame [forward, left, yaw] that steers `here` towards `target`."""
    x, y, _z, yaw = here
    ex, ey = target[0] - x, target[1] - y
    forward = math.cos(yaw) * ex + math.sin(yaw) * ey
    left = -math.sin(yaw) * ex + math.cos(yaw) * ey
    heading_error = math.atan2(left, forward)

    turn = max(-MAX_YAW, min(MAX_YAW, K_YAW * heading_error))
    if abs(heading_error) > TURN_FIRST:
        return 0.0, 0.0, turn
    # Scale the walk down as the heading error grows, so the arc it traces stays
    # near the line rather than swinging outside it.
    gate = math.cos(heading_error)
    speed = min(args.speed, K_V * math.hypot(ex, ey))
    # Strafing is what lets it close the last few centimetres without spinning
    # on the spot, so it is allowed only on the final leg.
    lateral = max(-0.3, min(0.3, K_V * left)) if final_leg else 0.0
    return speed * gate, lateral, turn


goal_xy = waypoints[-1]
# Kept because `waypoints` is replaced by every recovery, and the picture is
# supposed to show the route the robot was originally given against the one it
# drove. Drawing the live list put the start marker wherever the last re-plan
# began, which reads as though the robot teleported.
first_plan = list(waypoints)
pursuit = Pursuit(waypoints, args.lookahead)
# What the map does not know, learned the hard way. No reconstruction is
# complete and no threshold is right everywhere, so a stack that only trusts the
# map cannot finish: re-planning on an unchanged map returns the same route, and
# the robot walks into the same thing. This is nav2's local costmap and its
# recovery behaviours, in the smallest form that does the job -- believe the
# robot over the map, write off the patch in front of it, and go round.
learned = np.zeros_like(passable)
recoveries = 0
backup_until = -1
track, cross_errors, events = [], [], []
frames = int(args.timeout / render_dt)
settle_frames = 60          # on its feet before it is asked to go anywhere
stall_window = int(4.0 / render_dt)
stall_reference = None
stall_frame = 0
stall_steps = 0
replans = 0  # kept only for the count of routes issued
arrived = False
fell = False
driven_m = 0.0
previous = None
sim_seconds = 0.0

print(f"\ndriving to {tuple(round(v, 2) for v in goal_xy)}, "
      f"timeout {args.timeout:.0f}s\n")

for frame in range(frames):
    simulation_app.update()
    if not SimulationManager.is_simulating():
        continue
    sim_seconds = frame * render_dt
    here = pose()

    if previous is not None:
        driven_m += math.hypot(here[0] - previous[0], here[1] - previous[1])
    previous = here

    if frame < settle_frames:
        base_command = torch.zeros(3)
        continue

    if frame < backup_until:
        # Reverse, without turning: whatever stopped us is directly ahead, and
        # turning into it is how a stuck robot stays stuck.
        base_command = torch.tensor([-0.4, 0.0, 0.0])
        continue

    if here[2] - floor_z < 0.25:
        fell = True
        events.append([round(sim_seconds, 1), "fell", [round(v, 2) for v in here[:3]]])
        print(f"  {sim_seconds:6.1f}s  FELL at ({here[0]:.2f}, {here[1]:.2f}), "
              f"base {here[2] - floor_z:.3f} m above the floor")
        break

    remaining = math.hypot(goal_xy[0] - here[0], goal_xy[1] - here[1])
    if remaining < args.arrive:
        arrived = True
        base_command = torch.zeros(3)
        events.append([round(sim_seconds, 1), "arrived", [round(v, 2) for v in here[:2]]])
        print(f"  {sim_seconds:6.1f}s  ARRIVED at ({here[0]:.2f}, {here[1]:.2f}), "
              f"{remaining:.2f} m from the goal")
        break

    target, offline, left = pursuit.update(here[0], here[1])
    forward, lateral, turn = command_towards(target, here, left < args.lookahead)
    base_command = torch.tensor([forward, lateral, turn])

    cross_errors.append(offline)
    if frame % 25 == 0:
        track.append([round(here[0], 3), round(here[1], 3), round(here[2], 3),
                      round(sim_seconds, 1)])
    if frame % 250 == 0:
        print(f"  {sim_seconds:6.1f}s  at ({here[0]:6.2f}, {here[1]:6.2f})  "
              f"{left:5.2f} m of path left  {remaining:5.2f} m to the goal  "
              f"off-line {offline:.2f} m")

    # --- stalled? --------------------------------------------------------
    # A wedged robot and a simulation that stopped stepping look identical from
    # out here: the pose simply stops changing. `steps_run` tells them apart,
    # and the first time this triggered it was the second one -- physics ran
    # 8658 steps and halted, at the same 17.3 s in two runs that took different
    # routes and stopped in different places.
    if stall_reference is None or frame - stall_frame > stall_window:
        if stall_reference is not None and steps_run == stall_steps:
            print(f"  {sim_seconds:6.1f}s  the SIMULATION stopped stepping after "
                  f"{steps_run} policy steps -- timeline at "
                  f"{timeline.get_current_time():.2f}s of "
                  f"{timeline.get_end_time():.2f}s, playing={timeline.is_playing()}")
            events.append([round(sim_seconds, 1), "simulation stopped", steps_run])
            break
        if (stall_reference is not None
                and math.hypot(here[0] - stall_reference[0],
                               here[1] - stall_reference[1]) < 0.15):
            if recoveries >= args.recoveries:
                events.append([round(sim_seconds, 1), "stalled",
                               [round(here[0], 2), round(here[1], 2)]])
                print(f"  {sim_seconds:6.1f}s  STALLED at "
                      f"({here[0]:.2f}, {here[1]:.2f}) after {recoveries} recoveries")
                break

            # Write off a disc just ahead of the robot -- not around it, or the
            # patch includes where it has to retreat to and there is no route
            # out of its own footprint.
            ahead = (here[0] + 0.35 * math.cos(here[3]),
                     here[1] + 0.35 * math.sin(here[3]))
            blot = carte.cell_of(*ahead)
            span = int(math.ceil(args.recovery_radius / carte.resolution))
            for dy in range(-span, span + 1):
                for dx in range(-span, span + 1):
                    if dy * dy + dx * dx > span * span:
                        continue
                    spot = (blot[0] + dy, blot[1] + dx)
                    if carte.inside(spot):
                        learned[spot] = True

            usable = passable & ~learned
            cell = carte.cell_of(here[0], here[1])
            if not (carte.inside(cell) and usable[cell]):
                cell = navmap.nearest_passable(usable, cell) or cell
            fresh = (navmap.plan(usable, cell, goal_cell, cost=cost)
                     if usable[goal_cell] else None)
            recoveries += 1
            if fresh is None:
                events.append([round(sim_seconds, 1), "no route around it", None])
                print(f"  {sim_seconds:6.1f}s  blocked at ({here[0]:.2f}, {here[1]:.2f}) "
                      f"and no route around it remains")
                break
            waypoints = [carte.world_of(*c) for c in navmap.shortcut(fresh, usable)]
            pursuit = Pursuit(waypoints, args.lookahead)
            backup_until = frame + int(BACKUP_SECONDS / render_dt)
            replans += 1
            events.append([round(sim_seconds, 1), "recovered",
                           [round(here[0], 2), round(here[1], 2)]])
            print(f"  {sim_seconds:6.1f}s  something the map does not have at "
                  f"({ahead[0]:.2f}, {ahead[1]:.2f}) -- backing off and going round: "
                  f"{len(waypoints)} waypoints, {navmap.path_length(waypoints):.2f} m")
        stall_reference = (here[0], here[1])
        stall_frame = frame
        stall_steps = steps_run

    if not args.headless and frame % 4 == 0:
        set_camera_view(eye=[here[0] - 3.0, here[1] - 3.0, here[2] + 2.0],
                        target=[here[0], here[1], here[2]],
                        camera_prim_path="/OmniverseKit_Persp")

base_command = torch.zeros(3)
final = pose()
gap = math.hypot(goal_xy[0] - final[0], goal_xy[1] - final[1])
errors = np.asarray(cross_errors) if cross_errors else np.zeros(1)

print(f"\npolicy steps run: {steps_run}, callback error: {callback_error}")
print(f"result:   {'ARRIVED' if arrived else ('FELL' if fell else 'did not arrive')}")
print(f"  goal    {tuple(round(v, 2) for v in goal_xy)}, ended "
      f"{tuple(round(float(v), 2) for v in final[:2])}, {gap:.2f} m away")
detour = driven_m / planned_m if planned_m else float("nan")
print(f"  planned {planned_m:.2f} m, drove {driven_m:.2f} m (x{detour:.2f})")
print(f"  off the planned line: mean {errors.mean():.3f} m, "
      f"p95 {np.percentile(errors, 95):.3f} m, max {errors.max():.3f} m")
print(f"  {sim_seconds:.1f} s of sim time, {recoveries} recoveries "
      f"({int(learned.sum())} cells the map did not have), "
      f"standing {final[2] - floor_z:.3f} m above the floor")

report = {
    "usd": usd_path,
    "map": os.path.abspath(args.map),
    "map_built_with": {key: build.get(key) for key in
                       ("isaac_sim", "cell_size", "robot_height", "floor_tolerance",
                        "max_step", "border")},
    "robot": args.robot,
    "robot_radius": args.robot_radius,
    "avoid_unknown": avoid_unknown,
    "prefer_clearance_m": args.prefer_clearance,
    "route_clearance_m": {"tightest": round(float(inner.min()), 3),
                          "median": round(float(np.median(along)), 3)},
    "speed": args.speed,
    "start": [round(float(v), 3) for v in spawn_xy],
    "goal": [round(float(v), 3) for v in goal_xy],
    "arrived": arrived,
    "fell": fell,
    "recoveries": recoveries,
    "learned_obstacle_cells": int(learned.sum()),
    "unmapped_obstacles": args.obstacle,
    "planned_m": round(planned_m, 3),
    "driven_m": round(driven_m, 3),
    "final_gap_m": round(gap, 3),
    "seconds": round(sim_seconds, 2),
    "cross_track_m": {
        "mean": round(float(errors.mean()), 4),
        "p95": round(float(np.percentile(errors, 95)), 4),
        "max": round(float(errors.max()), 4),
    },
    "standing_height_m": round(float(final[2] - floor_z), 3),
    "waypoints": [[round(v, 3) for v in w] for w in first_plan],
    "final_waypoints": ([[round(v, 3) for v in w] for w in waypoints]
                        if recoveries else None),
    "track": track,
    "events": events,
    "policy_steps": steps_run,
    "callback_error": callback_error,
}
out_json = os.path.join(out_dir, f"{name}-{args.robot}-navigate.json")
with open(out_json, "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2)
print(f"wrote {out_json}")

# --- the picture ---------------------------------------------------------
# The numbers say how far it strayed; the overlay says *where*, which is what
# tells a controller problem from a map problem.
try:
    from PIL import Image, ImageDraw

    picture = Image.open(os.path.join(os.path.dirname(os.path.abspath(args.map)),
                                      str(carte.meta["image"]))).convert("RGB")
    scale = 4
    picture = picture.resize((picture.width * scale, picture.height * scale), Image.NEAREST)
    draw = ImageDraw.Draw(picture)

    def to_pixels(x, y):
        ix = (x - carte.origin[0]) / carte.resolution
        iy = (y - carte.origin[1]) / carte.resolution
        # The PNG runs top-down while the grid runs bottom-up, so y flips back.
        return (ix * scale, (carte.height - iy) * scale)

    # What the robot found out, under everything else so the lines stay readable.
    for iy, ix in np.argwhere(learned):
        px, py = to_pixels(*carte.world_of(iy, ix))
        draw.rectangle([px, py - scale, px + scale, py], fill=(255, 214, 102))
    if len(first_plan) > 1:
        draw.line([to_pixels(*w) for w in first_plan], fill=(40, 110, 255), width=2 * scale)
    if recoveries and len(waypoints) > 1:
        draw.line([to_pixels(*w) for w in waypoints], fill=(150, 190, 255), width=scale)
    if len(track) > 1:
        draw.line([to_pixels(p[0], p[1]) for p in track], fill=(230, 60, 60), width=scale)
    for point, colour in ((spawn_xy, (0, 170, 0)), (goal_xy, (255, 140, 0))):
        px, py = to_pixels(*point)
        draw.ellipse([px - 3 * scale, py - 3 * scale, px + 3 * scale, py + 3 * scale],
                     outline=colour, width=scale)
    out_png = os.path.join(out_dir, f"{name}-{args.robot}-navigate.png")
    picture.save(out_png)
    print(f"wrote {out_png}  (blue: the plan, pale blue: re-planned, "
          f"red: driven, yellow: what the map did not have, "
          f"green: start, orange: goal)")
except Exception as exc:  # noqa: BLE001 - the run's result does not depend on the picture
    print(f"overlay not written: {exc!r}")

simulation_app.close()
raise SystemExit(0 if arrived else 1)
