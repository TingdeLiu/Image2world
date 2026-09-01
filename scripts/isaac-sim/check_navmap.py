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
import json
import math
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The loading rules, the inflation and the A* live in `navmap`, because
# `navigate.py` drives the very map this checks. A second implementation of
# "which cells are passable" would quietly make these two different claims.
import navmap  # noqa: E402
from navmap import FREE, OCCUPIED, UNKNOWN  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("yaml", help="the -navmap.yaml written by build_navmap.py")
parser.add_argument("--ros", action="store_true",
                    help="also publish the grid as nav_msgs/OccupancyGrid via rclpy")
parser.add_argument("--ros-seconds", type=float, default=12.0,
                    help="how long --ros keeps publishing, for a subscriber to catch it")
parser.add_argument("--robot-radius", type=float, default=0.0,
                    help="treat cells within this distance of an obstacle as blocked")
args = parser.parse_args()

carte = navmap.load(args.yaml)
grid = carte.grid
height, width = carte.height, carte.width
resolution = carte.resolution
origin = [carte.origin[0], carte.origin[1], 0.0]

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
# Two ways to inflate, and the difference is diagnostic rather than cosmetic.
# Growing only the obstacles asks "are the walls far enough apart"; also growing
# the unknown cells asks "and are the holes in the reconstruction small enough".
# A map that passes the first and fails the second is not too narrow -- it is
# too incomplete, and that is a reconstruction problem, not a planning one.
#
# Hence the third field: only a scenario marked fatal can fail the run. Both
# used to, which meant --robot-radius could never come back green on real
# reconstruction data, and a genuine regression would have looked exactly like
# the holes that are always there. nav2 agrees with the split -- it inflates
# lethal obstacles and leaves NO_INFORMATION alone -- so the obstacles-only
# scenario is the one that predicts what its planner does.
scenarios = [("point robot", grid == FREE, True)]
if args.robot_radius > 0:
    scenarios = [
        (f"r={args.robot_radius} m, obstacles only",
         carte.passable(args.robot_radius), True),
        (f"r={args.robot_radius} m, obstacles and holes",
         carte.passable(args.robot_radius, avoid_unknown=True), False),
    ]

cell_of = carte.cell_of
world_of = carte.world_of
plan = navmap.plan


def nearest_passable(passable, iy, ix):
    return navmap.nearest_passable(passable, (iy, ix))


any_failed = False
holes_block_the_route = False
for label, passable, fatal in scenarios:
    print(f"\n{label}: {int(passable.sum())} cells passable "
          f"({int(passable.sum()) * cell:.2f} m2)")
    if not passable.any():
        print("  no free cells at all -- nothing could plan here")
        any_failed |= fatal
        holes_block_the_route |= not fatal
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
        any_failed |= fatal
        holes_block_the_route |= not fatal
        continue
    length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                 for a, b in zip(path, path[1:])) * resolution
    straight = math.hypot(gx - sx, gy - sy)
    print(f"  planned ({sx:.2f}, {sy:.2f}) -> ({gx:.2f}, {gy:.2f}): "
          f"{len(path)} cells, {length:.2f} m, straight {straight:.2f} m "
          f"(detour x{length / straight:.2f})")

passable = scenarios[0][1]

# --- optionally hand it to the real message type -------------------------
def pin_dds_to_a_multicast_interface():
    """Point FastDDS at an interface that can actually carry discovery.

    WSL2 in mirrored networking mode gives its `lo` no MULTICAST flag, so DDS
    discovery over loopback cannot work -- and ROS_LOCALHOST_ONLY=1, the usual
    advice, forces it there and makes it worse. Every node starts cleanly, logs
    nothing, and none of them see each other. `nav2_check.sh` already carries
    this workaround; without it here, `--ros` reports "no subscriber ever
    received it" and looks like a bug in the grid.

    A /32 is skipped: tunnel interfaces (Tailscale and friends) carry the
    MULTICAST flag too, and a participant that announces itself there is
    unreachable. Returns the address pinned, or None if nothing was changed.
    """
    if os.environ.get("FASTRTPS_DEFAULT_PROFILES_FILE"):
        return None  # someone already chose; do not second-guess them
    if not sys.platform.startswith("linux"):
        return None
    try:
        links = subprocess.run(["ip", "-o", "link", "show"], capture_output=True,
                               text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None

    address = None
    for line in links.splitlines():
        if "MULTICAST" not in line or ":" not in line:
            continue
        name = line.split(":")[1].strip().split("@")[0]
        try:
            addrs = subprocess.run(
                ["ip", "-o", "-4", "addr", "show", "dev", name, "scope", "global"],
                capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        for entry in addrs.split():
            if "/" in entry and entry.count(".") == 3 and not entry.endswith("/32"):
                address = entry.split("/")[0]
                break
        if address:
            break
    if not address:
        return None

    profile = os.path.join(tempfile.gettempdir(), "fastdds_navmap_check.xml")
    with open(profile, "w", encoding="utf-8") as fh:
        fh.write(
            "<?xml version='1.0' encoding='UTF-8' ?>"
            "<dds><profiles xmlns='http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles'>"
            "<transport_descriptors><transport_descriptor>"
            "<transport_id>udp_mcast</transport_id><type>UDPv4</type>"
            f"<interfaceWhiteList><address>{address}</address></interfaceWhiteList>"
            "</transport_descriptor></transport_descriptors>"
            "<participant profile_name='mcast_iface' is_default_profile='true'><rtps>"
            "<userTransports><transport_id>udp_mcast</transport_id></userTransports>"
            "<useBuiltinTransports>false</useBuiltinTransports>"
            "</rtps></participant></profiles></dds>"
        )
    os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"] = profile
    os.environ.pop("ROS_LOCALHOST_ONLY", None)
    return address


if args.ros:
    # Before rclpy: FastDDS reads the profile path when it builds a participant,
    # and rclpy.init() builds one.
    pinned = pin_dds_to_a_multicast_interface()
    if pinned:
        print(f"pinned DDS to {pinned} (loopback carries no MULTICAST here)")

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
    print("\n[!] the map loads, but the obstacles alone leave no route across it")
    raise SystemExit(1)
if holes_block_the_route:
    print()
    print("[note] treating the unmapped cells as obstacles too leaves no route. "
          "That is the reconstruction's holes, not the map's geometry: nav2 "
          "inflates lethal obstacles and leaves unknown space alone, so its "
          "planner still crosses this. Worth watching, not a failure.")
print("\n[ok] the map loads under map_server's rules and a planner can cross it")
