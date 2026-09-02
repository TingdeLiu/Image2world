"""Reading a `map_server` map, and planning on it, in one place.

`check_navmap.py` had this inline, and `navigate.py` needs exactly the same
thing -- but the point of `navigate.py` is that the robot drives the map we
checked, so a second implementation of "which cells are passable" would quietly
turn that into a different claim. One module, both callers.

Deliberately dependency-light: numpy and Pillow, no ROS and no yaml. It has to
import under Isaac Sim's interpreter, under WSL's ROS python, and under a bare
system python, and those three have very little in common.

The loading rules are `nav2_map_server`'s own:

    occ = (255 - pixel) / 255          (negate: 0)
    occ > occupied_thresh  -> 100      occupied
    occ < free_thresh      ->   0      free
    otherwise              ->  -1      unknown

Row order is the part worth stating: `map_server` treats image row 0 as the
*top* of the picture and the *highest* y in the world, so the OccupancyGrid data
runs bottom-up. `build_navmap.py` writes the PNG with `np.flipud` for exactly
this reason, and a sign error here is invisible in the picture -- it just
silently mirrors the room.
"""
from __future__ import annotations

import heapq
import math
import os

import numpy as np

FREE, OCCUPIED, UNKNOWN = 0, 100, -1

REQUIRED_KEYS = ("image", "resolution", "origin", "negate",
                 "occupied_thresh", "free_thresh")


def read_yaml(path):
    """The six keys `map_server` needs, without a YAML dependency.

    Deliberately not `yaml.safe_load`: this file is written by
    `build_navmap.py` a few lines at a time, and parsing it the same simple way
    keeps the reader honest about what is actually in it.
    """
    # A missing file gets the same treatment as a missing key or a missing
    # image: a sentence, not a traceback. `pipeline.py` prints the tail of a
    # failed stage, and a stack trace there says "this crashed" where the useful
    # thing to say is "that world has no map yet".
    if not os.path.isfile(path):
        raise SystemExit(f"{path} does not exist -- has build_navmap.py been run?")
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


class NavMap:
    """An occupancy grid with its world placement, indexed row 0 = lowest y."""

    def __init__(self, grid, resolution, origin, meta=None):
        self.grid = grid
        self.resolution = float(resolution)
        self.origin = (float(origin[0]), float(origin[1]))
        self.meta = meta or {}
        self.height, self.width = grid.shape

    # --- coordinates ------------------------------------------------------
    def cell_of(self, x, y):
        return (int((y - self.origin[1]) / self.resolution),
                int((x - self.origin[0]) / self.resolution))

    def world_of(self, iy, ix):
        """The *centre* of a cell, which is what a path should aim at."""
        return (self.origin[0] + (ix + 0.5) * self.resolution,
                self.origin[1] + (iy + 0.5) * self.resolution)

    def inside(self, cell):
        return 0 <= cell[0] < self.height and 0 <= cell[1] < self.width

    def counts(self):
        return {name: int((self.grid == value).sum())
                for name, value in (("free", FREE), ("occupied", OCCUPIED),
                                    ("unknown", UNKNOWN))}

    # --- what a robot of a given size may occupy --------------------------
    def passable(self, robot_radius=0.0, avoid_unknown=False):
        """Cells whose *centre* a robot of that radius may sit on.

        `avoid_unknown` asks the harder question. Growing only the obstacles
        asks whether the walls are far enough apart; growing the unknown cells
        too asks whether the holes in the reconstruction are small enough, and
        those are different findings. nav2 does the first -- its inflation layer
        grows lethal obstacles and leaves NO_INFORMATION alone -- so the first
        is what predicts a real stack's behaviour.
        """
        blocked = (self.grid != FREE) if avoid_unknown else (self.grid == OCCUPIED)
        return (self.grid == FREE) & ~inflate(blocked, robot_radius, self.resolution)


def load(yaml_path) -> NavMap:
    from PIL import Image

    meta = read_yaml(yaml_path)
    for required in REQUIRED_KEYS:
        if required not in meta:
            raise SystemExit(f"{yaml_path} has no `{required}` -- map_server would refuse it")

    image_path = os.path.join(os.path.dirname(os.path.abspath(yaml_path)),
                              str(meta["image"]))
    if not os.path.isfile(image_path):
        raise SystemExit(f"image {image_path} is missing")

    pixels = np.asarray(Image.open(image_path).convert("L"), dtype=np.float64)
    negate = int(meta["negate"])
    occ = pixels / 255.0 if negate else (255.0 - pixels) / 255.0

    grid = np.full(pixels.shape, UNKNOWN, dtype=np.int8)
    grid[occ > float(meta["occupied_thresh"])] = OCCUPIED
    grid[occ < float(meta["free_thresh"])] = FREE
    # map_server publishes row 0 of the message as the *lowest* y, while row 0
    # of the image is the top of the picture. Flipping here is what makes cell
    # (0, 0) of everything above sit at the origin named in the YAML.
    return NavMap(np.flipud(grid), meta["resolution"], meta["origin"], meta)


def inflate(blocked, radius_m, resolution):
    """Grow a boolean mask by a disc, in plain numpy."""
    if radius_m <= 0:
        return blocked
    height, width = blocked.shape
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


def shift(values, dy, dx, fill):
    """`values` translated by (dy, dx), `fill` at the edges -- no wrap."""
    out = np.full_like(values, fill)
    height, width = values.shape
    hh, ww = height - abs(dy), width - abs(dx)
    if hh <= 0 or ww <= 0:
        return out
    sy, sx, gy, gx = max(dy, 0), max(dx, 0), max(-dy, 0), max(-dx, 0)
    out[sy:sy + hh, sx:sx + ww] = values[gy:gy + hh, gx:gx + ww]
    return out


NEIGHBOURS = tuple((dy, dx, math.hypot(dy, dx))
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx)


def clearance(blocked, resolution, cap_m=1.0):
    """Metres from each cell to the nearest blocked one, capped at `cap_m`.

    A chamfer transform: repeated min-plus relaxation over the eight
    neighbours, which converges to a good approximation of the Euclidean
    distance and is a handful of vectorised passes rather than a Python loop
    over every cell. Capped because nothing downstream cares how roomy the
    middle of a large room is, and the cap is what bounds the pass count.
    """
    distance = np.where(blocked, 0.0, np.inf).astype(np.float32)
    for _ in range(int(math.ceil(cap_m / resolution))):
        updated = distance
        for dy, dx, step in NEIGHBOURS:
            updated = np.minimum(updated, shift(distance, dy, dx, np.inf) + step)
        if np.array_equal(updated, distance):
            break
        distance = updated
    return np.minimum(distance * resolution, cap_m)


def reachable(passable, seed):
    """Cells of `passable` connected to `seed`, 4-connected.

    Needed because "the furthest passable cell" is not the same as "the
    furthest cell you can get to": pick the first and a route to it may not
    exist, which reads as a planning failure when it is really a goal that was
    never sensible. Four rather than eight, matching `build_navmap.py` -- a
    diagonal pinhole between two obstacle corners is not a gap.
    """
    reach = np.zeros_like(passable)
    if not passable[seed]:
        return reach
    reach[seed] = True
    while True:
        grown = reach.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            grown |= shift(reach, dy, dx, False) & passable
        if grown.sum() == reach.sum():
            return reach
        reach = grown


def furthest_reachable(passable, seed, room=None, min_clearance=0.0):
    """The reachable cell furthest from `seed`, or None.

    With `room` (a clearance field) the search is restricted to cells with at
    least `min_clearance` metres around them, falling back to the unrestricted
    answer if none qualify. Without that, the furthest reachable cell is by
    construction pressed against the edge of the passable region -- so the
    default goal asks the robot to finish standing with no room at all, and
    every clearance statistic along the route is pinned to zero by its own
    endpoint.
    """
    reach = reachable(passable, seed)
    if room is not None and min_clearance > 0:
        roomy = reach & (room >= min_clearance)
        if roomy.any():
            reach = roomy
    cells = np.argwhere(reach)
    if not len(cells):
        return None
    return tuple(int(v) for v in
                 cells[int(np.argmax(((cells - np.array(seed)) ** 2).sum(axis=1)))])


def nearest_passable(passable, cell):
    """The passable cell closest to `cell`, or None if there are none."""
    cells = np.argwhere(passable)
    if not len(cells):
        return None
    return tuple(int(v) for v in
                 cells[int(np.argmin(((cells - np.array(cell)) ** 2).sum(axis=1)))])


def clearance_cost(clear_m, prefer_m, weight):
    """A per-cell multiplier that makes hugging a wall expensive.

    Plain A* takes the shortest route, and the shortest route through an
    inflated map runs along the inflation boundary -- so the robot is sent to
    skim every wall and every hole by exactly its own radius, with nothing left
    for tracking error. This is nav2's inflation layer in one line: full price
    at `prefer_m` of clearance or more, up to `1 + weight` times that when
    pressed against something.
    """
    shortfall = np.clip((prefer_m - clear_m) / prefer_m, 0.0, 1.0)
    return 1.0 + weight * shortfall


def plan(passable, start, goal, cost=None):
    """8-connected A*, the shape of planner a nav stack runs on a costmap.

    `cost` is an optional per-cell multiplier -- see `clearance_cost`. The
    heuristic stays the plain straight-line distance, which stays admissible
    because every multiplier is at least 1.
    """
    height, width = passable.shape

    def heuristic(cell):
        return math.hypot(cell[0] - goal[0], cell[1] - goal[1])

    open_set = [(heuristic(start), 0.0, start)]
    came, best = {}, {start: 0.0}
    while open_set:
        _, walked, current = heapq.heappop(open_set)
        if current == goal:
            path = [current]
            while path[-1] in came:
                path.append(came[path[-1]])
            return path[::-1]
        if walked > best.get(current, math.inf):
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
                move = math.hypot(dy, dx)
                if cost is not None:
                    move *= float(cost[ny, nx])
                step = walked + move
                if step < best.get((ny, nx), math.inf):
                    best[(ny, nx)] = step
                    came[(ny, nx)] = current
                    heapq.heappush(open_set, (step + heuristic((ny, nx)), step, (ny, nx)))
    return None


def visible(passable, a, b):
    """Is every cell on the segment a->b passable?

    Sampled at half a cell, which cannot skip a cell on a straight line and is
    far simpler to be sure of than a supercover Bresenham.
    """
    dy, dx = b[0] - a[0], b[1] - a[1]
    steps = int(math.ceil(2 * max(abs(dy), abs(dx))))
    if steps == 0:
        return True
    for i in range(steps + 1):
        t = i / steps
        cell = (int(round(a[0] + dy * t)), int(round(a[1] + dx * t)))
        if not passable[cell]:
            return False
    return True


def shortcut(path, passable):
    """String-pull an 8-connected path into straight runs.

    A grid path is a staircase, and a robot asked to follow it corner by corner
    weaves. Dropping every waypoint that the previous one can see directly
    leaves the same route with far fewer, longer legs -- shorter as well, since
    the staircase is longer than its diagonal.
    """
    if not path:
        return path
    pulled = [path[0]]
    anchor = 0
    while anchor < len(path) - 1:
        furthest = anchor + 1
        for candidate in range(len(path) - 1, anchor, -1):
            if visible(passable, path[anchor], path[candidate]):
                furthest = candidate
                break
        pulled.append(path[furthest])
        anchor = furthest
    return pulled


def path_length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))
