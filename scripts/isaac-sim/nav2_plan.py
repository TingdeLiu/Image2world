"""Ask nav2's planner for a route across a generated world's navigation map.

`check_navmap.py` proves the map loads and that *a* planner can cross it, using
its own A*. This asks the real one: nav2's `planner_server`, running NavFn over
a `nav2_costmap_2d` global costmap fed by `nav2_map_server`.

The start pose is supplied rather than looked up, so no localisation stack is
needed -- `use_start` exists for exactly this.

Usage (inside the ROS environment, with the stack already up):
    python3 scripts/isaac-sim/nav2_plan.py --start 0,0 --goal 3.79,9.02
"""
import argparse
import math
import sys

import rclpy
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import ComputePathToPose
from rclpy.action import ActionClient

parser = argparse.ArgumentParser()
parser.add_argument("--start", default="0,0", help="x,y in map metres")
parser.add_argument("--goal", required=True, help="x,y in map metres")
parser.add_argument("--planner", default="GridBased")
parser.add_argument("--timeout", type=float, default=30.0)
args = parser.parse_args()


def pose(node, text):
    x, y = (float(v) for v in text.split(","))
    message = PoseStamped()
    message.header.frame_id = "map"
    message.header.stamp = node.get_clock().now().to_msg()
    message.pose.position.x = x
    message.pose.position.y = y
    message.pose.orientation.w = 1.0
    return message


rclpy.init()
node = rclpy.create_node("nav2_plan_probe")
client = ActionClient(node, ComputePathToPose, "compute_path_to_pose")

print(f"waiting for planner_server's compute_path_to_pose action ...")
if not client.wait_for_server(timeout_sec=args.timeout):
    print("planner_server never advertised the action -- is it activated?")
    sys.exit(1)

goal = ComputePathToPose.Goal()
goal.start = pose(node, args.start)
goal.goal = pose(node, args.goal)
goal.planner_id = args.planner
goal.use_start = True

print(f"planning {args.start} -> {args.goal} with '{args.planner}'")
send = client.send_goal_async(goal)
rclpy.spin_until_future_complete(node, send, timeout_sec=args.timeout)
handle = send.result()
if handle is None or not handle.accepted:
    print("planner rejected the goal")
    sys.exit(1)

result_future = handle.get_result_async()
rclpy.spin_until_future_complete(node, result_future, timeout_sec=args.timeout)
outcome = result_future.result()
if outcome is None:
    print("planner never returned a result")
    sys.exit(1)

path = outcome.result.path
if not path.poses:
    print("nav2 returned an EMPTY path -- it could not route through this map")
    sys.exit(1)

points = [(p.pose.position.x, p.pose.position.y) for p in path.poses]
length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
straight = math.dist(points[0], points[-1])
print(f"nav2 planned {len(points)} poses, {length:.2f} m "
      f"(straight line {straight:.2f} m, detour x{length / straight:.2f})")
print(f"  first {points[0][0]:.2f}, {points[0][1]:.2f}   "
      f"last {points[-1][0]:.2f}, {points[-1][1]:.2f}")

node.destroy_node()
rclpy.shutdown()
