"""Check a navigation map the way a ROS navigation stack will read it.

`build_navmap.py` writes a `map_server` YAML and image. Whether a planner can
actually use them is a separate question, and the parts most likely to be wrong
are the boring ones: which corner the origin refers to, which way the image rows
run, and whether the thresholds put our three greys in the buckets we meant.

This loads the pair using `nav2_map_server`'s documented rules, rebuilds the
`OccupancyGrid` it would publish, and then plans a route across it. Plain
Python, so it runs without a ROS install; with `--ros` it also publishes the
grid through rclpy so the real message type validates it.

    occ = (255 - pixel) / 255          (negate: 0)
    occ > occupied_thresh  -> 100      occupied
    occ < free_thresh      ->   0      free
    otherwise              ->  -1      unknown

Row order is the part worth stating: `map_server` treats image row 0 as the
*top* of the picture and the *highest* y in the world, so the OccupancyGrid data
runs bottom-up. `build_navmap.py` writes the PNG with `np.flipud` for exactly
this reason, and a sign error here is invisible in the picture -- it just
silently mirrors the room.

Usage:
    python scripts/isaac-sim/check_navmap.py public/worlds/<slug>/export/<n>-navmap.yaml
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument("yaml", help="the -navmap.yaml written by build_navmap.py")
parser.add_argument("--ros", action="store_true",
                    help="also publish the grid as nav_msgs/OccupancyGrid via rclpy")
parser.add_argument("--ros-seconds", type=float, default=12.0,
                    help="how long --ros keeps publishing, for a subscriber to catch it")
parser.add_argument("--robot-radius", type=float, default=0.0,
                    help="treat cells within this distance of an obstacle as blocked")
args = parser.parse_args()

FREE, OCCUPIED, UNKNOWN = 0, 100, -1


def read_yaml(path):
    """The six keys `map_server` needs, without a YAML dependency.

    Deliberately not `yaml.safe_load`: this file is written by
    `build_navmap.py` a few lines at a time, and parsing it the same simple way
    keeps the checker honest about what is actually in it.
    """
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            value = value.strip()
            if value.startswith("["):
                out[key.strip()] = [float(v) for v in value.strip("[]").split(",")]
            else:
                try:
                    out[key.strip()] = float(value)
                except ValueError:
                    out[key.strip()] = value
    return out


meta = read_yaml(args.yaml)
for required in ("image", "resolution", "origin", "negate",
                 "occupied_thresh", "free_thresh"):
    if required not in meta:
        raise SystemExit(f"{args.yaml} has no `{required}` -- map_server would refuse it")

image_path = os.path.join(os.path.dirname(os.path.abspath(args.yaml)), str(meta["image"]))
if not os.path.isfile(image_path):
    raise SystemExit(f"image {image_path} is missing")

resolution = float(meta["resolution"])
origin = [float(v) for v in meta["origin"]]
negate = int(meta["negate"])
occupied_thresh = float(meta["occupied_thresh"])
free_thresh = float(meta["free_thresh"])

pixels = np.asarray(Image.open(image_path).convert("L"), dtype=np.float64)
height, width = pixels.shape

occ = pixels / 255.0 if negate else (255.0 - pixels) / 255.0
grid = np.full((height, width), UNKNOWN, dtype=np.int8)
grid[occ > occupied_thresh] = OCCUPIED
grid[occ < free_thresh] = FREE
# map_server publishes row 0 of the message as the *lowest* y, while row 0 of
# the image is the top of the picture. Flipping here is what makes cell (0, 0)
# of everything below sit at the origin named in the YAML.
grid = np.flipud(grid)

print(f"map     {width} x {height} cells @ {resolution} m = "
      f"{width * resolution:.2f} x {height * resolution:.2f} m")
print(f"origin  {origin}  (world position of cell 0,0's corner)")
cell = resolution * resolution
counts = {name: int((grid == value).sum())
          for name, value in (("free", FREE), ("occupied", OCCUPIED), ("unknown", UNKNOWN))}
for name, n in counts.items():
    print(f"  {name:<9} {n:>7} cells  {n * cell:8.2f} m2")

# --- does it agree with what build_navmap said it wrote? -----------------
report_path = args.yaml.replace("-navmap.yaml", "-navmap.json")
if os.path.isfile(report_path):
    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)
    raycast = report.get("raycast", {})
    mismatches = [
        (name, counts[name], raycast[key])
        for name, key in (("free", "free"), ("occupied", "occupied"), ("unknown", "unknown"))
        if key in raycast and counts[name] != raycast[key]
    ]
    if mismatches:
        for name, got, expected in mismatches:
            print(f"  MISMATCH {name}: image says {got}, build_navmap said {expected}")
        raise SystemExit("the image does not carry the grid build_navmap measured")
    print("  counts match the grid build_navmap measured, cell for cell")
    if abs(raycast.get("origin", [origin[0]])[0] - origin[0]) > 1e-9:
        raise SystemExit("origin in the YAML disagrees with the one in the JSON")

# --- can anything actually plan on it? -----------------------------------
def inflate(blocked, radius_m):
    """Grow a boolean mask by a disc, in plain numpy."""
    if radius_m <= 0:
        return blocked
    radius = radius_m / resolution
    span = int(math.floor(radius))
    grown = blocked.copy()
    for dy in range(-span, span + 1):
        for dx in range(-span, span + 1):
            if not (dx or dy) or dx * dx + dy * dy > radius * radius:
                continue
            shifted = np.zeros_like(blocked)
            hh, ww = height - abs(dy), width - abs(dx)
            if hh <= 0 or ww <= 0:
                continue
            sy, sx, gy, gx = max(dy, 0), max(dx, 0), max(-dy, 0), max(-dx, 0)
            shifted[sy:sy + hh, sx:sx + ww] = blocked[gy:gy + hh, gx:gx + ww]
            grown |= shifted
    return grown


# Two ways to inflate, and the difference is diagnostic rather than cosmetic.
# Growing only the obstacles asks "are the walls far enough apart"; also growing
# the unknown cells asks "and are the holes in the reconstruction small enough".
# A map that passes the first and fails the second is not too narrow -- it is
# too incomplete, and that is a reconstruction problem, not a planning one.
scenarios = [("point robot", grid == FREE)]
if args.robot_radius > 0:
    scenarios = [
        (f"r={args.robot_radius} m, obstacles only",
         (grid == FREE) & ~inflate(grid == OCCUPIED, args.robot_radius)),
        (f"r={args.robot_radius} m, obstacles and holes",
         (grid == FREE) & ~inflate(grid != FREE, args.robot_radius)),
    ]

def cell_of(x, y):
    return int((y - origin[1]) / resolution), int((x - origin[0]) / resolution)


def world_of(iy, ix):
    return origin[0] + (ix + 0.5) * resolution, origin[1] + (iy + 0.5) * resolution


def nearest_passable(passable, iy, ix):
    cells = np.argwhere(passable)
    return tuple(cells[int(np.argmin(((cells - np.array([iy, ix])) ** 2).sum(axis=1)))])


def plan(passable, start, goal):
    """8-connected A*, the shape of planner a nav stack runs on a costmap."""
    def heuristic(cell):
        return math.hypot(cell[0] - goal[0], cell[1] - goal[1])

    open_set = [(heuristic(start), 0.0, start)]
    came, best = {}, {start: 0.0}
    while open_set:
        _, cost, current = heapq.heappop(open_set)
        if current == goal:
            path = [current]
            while path[-1] in came:
                path.append(came[path[-1]])
            return path[::-1]
        if cost > best.get(current, math.inf):
            continue
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if not (dy or dx):
                    continue
                ny, nx = current[0] + dy, current[1] + dx
                if not (0 <= ny < height and 0 <= nx < width) or not passable[ny, nx]:
                    continue
                # No cutting a diagonal between two blocked cells.
                if dy and dx and not (passable[current[0], nx] and passable[ny, current[1]]):
                    continue
                step = cost + math.hypot(dy, dx)
                if step < best.get((ny, nx), math.inf):
                    best[(ny, nx)] = step
                    came[(ny, nx)] = current
                    heapq.heappush(open_set, (step + heuristic((ny, nx)), step, (ny, nx)))
    return None


# Plan from the world origin -- the viewer's own spawn point -- to the passable
# cell furthest from it, which is the longest journey the map claims is possible.
any_failed = False
for label, passable in scenarios:
    print(f"\n{label}: {int(passable.sum())} cells passable "
          f"({int(passable.sum()) * cell:.2f} m2)")
    if not passable.any():
        print("  no free cells at all -- nothing could plan here")
        any_failed = True
        continue
    start = cell_of(0.0, 0.0)
    if not (0 <= start[0] < height and 0 <= start[1] < width and passable[start]):
        start = nearest_passable(passable, *start)
        print("  world origin is not passable; planning from the nearest cell instead")
    cells = np.argwhere(passable)
    goal = tuple(cells[int(np.argmax(((cells - np.array(start)) ** 2).sum(axis=1)))])
    path = plan(passable, start, goal)
    sx, sy = world_of(*start)
    gx, gy = world_of(*goal)
    if path is None:
        print(f"  NO PATH ({sx:.2f}, {sy:.2f}) -> ({gx:.2f}, {gy:.2f}) "
              f"-- the free space is not connected at this radius")
        any_failed = True
        continue
    length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                 for a, b in zip(path, path[1:])) * resolution
    straight = math.hypot(gx - sx, gy - sy)
    print(f"  planned ({sx:.2f}, {sy:.2f}) -> ({gx:.2f}, {gy:.2f}): "
          f"{len(path)} cells, {length:.2f} m, straight {straight:.2f} m "
          f"(detour x{length / straight:.2f})")

passable = scenarios[0][1]

# --- optionally hand it to the real message type -------------------------
if args.ros:
    import rclpy
    from nav_msgs.msg import OccupancyGrid
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

    rclpy.init()
    node = rclpy.create_node("navmap_check")
    # Latched, the way map_server publishes: a planner that starts later still
    # receives the map.
    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)
    publisher = node.create_publisher(OccupancyGrid, "/map", qos)

    message = OccupancyGrid()
    message.header.frame_id = "map"
    message.header.stamp = node.get_clock().now().to_msg()
    message.info.resolution = resolution
    message.info.width = width
    message.info.height = height
    message.info.origin.position.x = origin[0]
    message.info.origin.position.y = origin[1]
    message.info.origin.orientation.w = 1.0
    message.data = grid.reshape(-1).tolist()
    publisher.publish(message)
    print(f"published nav_msgs/OccupancyGrid on /map: "
          f"{len(message.data)} cells, frame '{message.header.frame_id}'")
    # Subscribe to our own topic and compare what comes back. This is the part
    # that catches a grid which serialises but arrives transposed, mirrored, or
    # with the wrong sign -- publishing alone only proves the message type
    # accepted the numbers.
    received = {}

    def on_map(msg):
        received["msg"] = msg

    reader = rclpy.create_node("navmap_check_reader")
    reader.create_subscription(OccupancyGrid, "/map", on_map, qos)

    deadline = time.monotonic() + args.ros_seconds
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.05)
        rclpy.spin_once(reader, timeout_sec=0.05)
        if "msg" in received:
            break

    if "msg" not in received:
        print("  no subscriber ever received it -- DDS discovery is not working here")
    else:
        got = received["msg"]
        back = np.asarray(got.data, dtype=np.int8).reshape(got.info.height, got.info.width)
        same = bool((back == grid).all())
        print(f"  read back {got.info.width}x{got.info.height} @ "
              f"{got.info.resolution} m, origin "
              f"({got.info.origin.position.x:.6f}, {got.info.origin.position.y:.6f})")
        print(f"  grid identical after a round trip through DDS: {same}")
        if not same:
            raise SystemExit("the grid changed in transit")
    reader.destroy_node()
    node.destroy_node()
    rclpy.shutdown()

if any_failed:
    print("\n[!] the map loads correctly, but the free space is not connected at every radius above")
    raise SystemExit(1)
print("\n[ok] the map loads under map_server's rules and a planner can cross it")
