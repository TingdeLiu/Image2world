#!/usr/bin/env bash
# Bring up just enough of nav2 to plan on a generated world's navigation map,
# ask it for a route, and tear it all down again.
#
#   map_server  ->  /map  ->  global_costmap  ->  planner_server (NavFn)
#
# Run inside WSL:
#   bash scripts/isaac-sim/nav2_check.sh <map.yaml> <goal_x,goal_y> [start_x,start_y]

MAP_YAML="${1:?usage: nav2_check.sh <map.yaml> <goal_x,goal_y> [start_x,start_y]}"
GOAL="${2:?need a goal as x,y}"
START="${3:-0,0}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# WSL2 in mirrored networking mode gives its `lo` no MULTICAST flag, so DDS
# discovery cannot work over loopback -- and ROS_LOCALHOST_ONLY=1, the usual
# advice, forces it there and makes things worse. Without this block every node
# below starts cleanly, logs no error, and none of them can see each other:
# `ros2 node list` comes back empty even for ROS's own talker/listener pair.
#
# The address is found rather than hardcoded. A /32 is skipped because a tunnel
# interface (Tailscale and friends) carries the MULTICAST flag too, and
# participants that announce themselves there are unreachable.
unset ROS_LOCALHOST_ONLY
MCAST_IP=""
for iface in $(ip -o link show | grep MULTICAST | cut -d: -f2 | tr -d ' '); do
  addr="$(ip -o -4 addr show dev "$iface" scope global 2>/dev/null | awk '{print $4}' | head -1)"
  case "$addr" in
    ""|*/32) continue ;;
  esac
  MCAST_IP="${addr%%/*}"
  break
done

if [ -n "$MCAST_IP" ]; then
  cat > /tmp/fastdds_wsl.xml <<XML
<?xml version="1.0" encoding="UTF-8" ?>
<dds>
  <profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">
    <transport_descriptors>
      <transport_descriptor>
        <transport_id>udp_mcast</transport_id>
        <type>UDPv4</type>
        <interfaceWhiteList><address>${MCAST_IP}</address></interfaceWhiteList>
      </transport_descriptor>
    </transport_descriptors>
    <participant profile_name="wsl_multicast_iface" is_default_profile="true">
      <rtps>
        <userTransports><transport_id>udp_mcast</transport_id></userTransports>
        <useBuiltinTransports>false</useBuiltinTransports>
      </rtps>
    </participant>
  </profiles>
</dds>
XML
  export FASTRTPS_DEFAULT_PROFILES_FILE=/tmp/fastdds_wsl.xml
  echo "=== DDS pinned to ${MCAST_IP} (lo carries no MULTICAST under WSL mirrored mode)"
else
  echo "=== no multicast-capable interface found; discovery may not work"
fi

source /opt/ros/humble/setup.bash

PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null
}
trap cleanup EXIT

echo "=== map: $MAP_YAML"

# The costmap needs a TF chain from its global frame to the robot frame. There
# is no robot here, so publish it statically -- the planner only needs the
# frames to resolve, not to move.
ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 map odom > /dev/null 2>&1 &
PIDS+=($!)
ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 odom base_link > /dev/null 2>&1 &
PIDS+=($!)

ros2 run nav2_map_server map_server --ros-args \
  --params-file "$HERE/nav2_params.yaml" \
  -p yaml_filename:="$MAP_YAML" > /tmp/nav2_map_server.log 2>&1 &
PIDS+=($!)

ros2 run nav2_planner planner_server --ros-args \
  --params-file "$HERE/nav2_params.yaml" > /tmp/nav2_planner.log 2>&1 &
PIDS+=($!)

sleep 5

# Both are managed lifecycle nodes: they do nothing at all until told to
# configure and activate, which is the most common reason a nav2 stack looks
# started and answers nothing.
for node in map_server planner_server; do
  for transition in configure activate; do
    echo "--- $node: $transition"
    if ! timeout 25 ros2 lifecycle set "/$node" "$transition"; then
      echo "!! $node failed to $transition"
      tail -20 "/tmp/nav2_$node.log"
      exit 1
    fi
  done
done

sleep 3

echo
echo "=== /map as map_server published it"
timeout 15 ros2 topic echo /map --once --field info

echo
echo "=== asking nav2 to plan"
# --goal= rather than --goal, because a negative coordinate looks like a flag.
python3 "$HERE/nav2_plan.py" --start="$START" --goal="$GOAL"
STATUS=$?

echo
echo "=== planner_server log tail"
tail -4 /tmp/nav2_planner.log
exit $STATUS
