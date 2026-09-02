"""Turning an occupancy grid into the graph a VLN dataset is built on.

`check_navmap.py` answers "can a planner cross this map". That is a lower bar
than the one this file is for. Vision-and-language navigation in the R2R/RxR
sense does not act on cells: an agent stands at a *viewpoint*, sees a panorama,
and picks one of the neighbouring viewpoints to step to. The environment is a
graph, and the map is only good enough for training if a decent graph can be
laid over it.

So the interesting questions are different ones, and none of them is answered
by a successful plan:

  * Is the walkable space one piece? A goal in a second component is an episode
    the agent cannot ever complete, and a training set salted with those teaches
    it that some instructions are simply impossible.
  * Do the nodes cover the space, or only the roomy middle of it?
  * Are the routes anything other than straight lines? If the geodesic is the
    euclidean distance everywhere, the task is "walk towards the goal" and the
    language is decoration.
  * How many usable episodes does the world actually yield?

Deliberately dependency-light, for the same reason `navmap` is: numpy only, no
ROS and no scipy, so it runs under Isaac Sim's interpreter, under a bare system
python, and in CI. Everything here works on the shipped `-navmap.yaml`, which
means the whole of it is testable without a GPU or a simulator.

Node placement prefers open space -- a panorama taken 10 cm from a wall is not
a viewpoint anyone would annotate. That is a bias, and it is deliberately not
the thing that gets measured: `coverage` is computed over every free cell, so
if preferring the middle leaves the edges of the room unrepresented, the report
says so rather than agreeing with the sampler.
"""
from __future__ import annotations

import heapq
import math

import numpy as np

import navmap


def sample_nodes(passable, clear_m, spacing_cells, min_clearance=0.0):
    """Well-spread viewpoints: greedy, roomiest-first, Poisson-disc spacing.

    Deterministic. Candidates are ordered by clearance and then by position, so
    two runs over the same map produce the same graph -- a dataset whose node
    ids move between builds is one whose episodes cannot be compared.

    Returns a list of (iy, ix).
    """
    candidates = np.argwhere(passable & (clear_m >= min_clearance))
    if not len(candidates):
        return []
    order = np.lexsort((candidates[:, 1], candidates[:, 0],
                        -clear_m[candidates[:, 0], candidates[:, 1]]))
    chosen = []
    # Squared distances in cells, so the spacing test stays in integers.
    limit = spacing_cells * spacing_cells
    for index in order:
        cy, cx = int(candidates[index, 0]), int(candidates[index, 1])
        for (oy, ox) in chosen:
            if (cy - oy) ** 2 + (cx - ox) ** 2 < limit:
                break
        else:
            chosen.append((cy, cx))
    return chosen


def connect(nodes, passable, max_edge_cells):
    """Edges between viewpoints with an unobstructed straight line between them.

    `passable` is expected to be inflated by the robot's radius already, in
    which case a clear centre line means a clear corridor -- the same
    equivalence the planner relies on.

    Returns a list of (i, j, length_in_cells) with i < j.
    """
    edges = []
    limit = max_edge_cells * max_edge_cells
    for i, a in enumerate(nodes):
        for j in range(i + 1, len(nodes)):
            b = nodes[j]
            d2 = (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
            if d2 > limit:
                continue
            if navmap.visible(passable, a, b):
                edges.append((i, j, math.sqrt(d2)))
    return edges


def adjacency(node_count, edges):
    out = [[] for _ in range(node_count)]
    for i, j, length in edges:
        out[i].append((j, length))
        out[j].append((i, length))
    return out


def components(node_count, adj):
    """Connected components as a list of node-index lists, largest first."""
    seen = [False] * node_count
    found = []
    for start in range(node_count):
        if seen[start]:
            continue
        stack, group = [start], []
        seen[start] = True
        while stack:
            node = stack.pop()
            group.append(node)
            for (other, _) in adj[node]:
                if not seen[other]:
                    seen[other] = True
                    stack.append(other)
        found.append(sorted(group))
    found.sort(key=len, reverse=True)
    return found


def geodesics(source, adj):
    """Dijkstra from one node. Returns (distance, previous) over edge lengths."""
    distance = [math.inf] * len(adj)
    previous = [-1] * len(adj)
    distance[source] = 0.0
    queue = [(0.0, source)]
    while queue:
        so_far, node = heapq.heappop(queue)
        if so_far > distance[node]:
            continue
        for (other, length) in adj[node]:
            through = so_far + length
            if through < distance[other] - 1e-9:
                distance[other] = through
                previous[other] = node
                heapq.heappush(queue, (through, other))
    return distance, previous


def path_between(source, target, previous):
    """Walk a Dijkstra `previous` array back into a node list."""
    if source == target:
        return [source]
    if previous[target] < 0:
        return []
    route = [target]
    while route[-1] != source:
        step = previous[route[-1]]
        if step < 0:
            return []
        route.append(step)
    route.reverse()
    return route


def coverage(nodes, passable, spacing_cells):
    """Fraction of passable cells within `spacing_cells` of some viewpoint.

    Computed over the whole passable mask on purpose. `sample_nodes` prefers
    roomy cells, so asking the nodes whether they cover the space they were
    chosen from would be a check that cannot fail; asking the space is what
    catches a graph that represents only the middle of the room.
    """
    if not len(nodes):
        return 0.0
    total = int(passable.sum())
    if not total:
        return 0.0
    reached = np.zeros(passable.shape, dtype=bool)
    height, width = passable.shape
    span = int(math.ceil(spacing_cells))
    limit = spacing_cells * spacing_cells
    for (cy, cx) in nodes:
        y0, y1 = max(0, cy - span), min(height, cy + span + 1)
        x0, x1 = max(0, cx - span), min(width, cx + span + 1)
        ys = np.arange(y0, y1)[:, None]
        xs = np.arange(x0, x1)[None, :]
        reached[y0:y1, x0:x1] |= ((ys - cy) ** 2 + (xs - cx) ** 2) <= limit
    return float((reached & passable).sum()) / total


def detour(nodes, adj, group, resolution):
    """Geodesic / euclidean over every pair in one component.

    The number VLN cares about. At 1.0 the world is an empty box and the task
    degenerates into walking towards the goal; a real building sits well above
    it because the route has to go around things.

    Read the *upper* percentiles, not the median. Measured on a synthetic
    10x10 m box with a wall hanging 80% of the way across it -- unambiguously
    two rooms -- the median detour is still 1.01, because most pairs of
    viewpoints share a side of the wall and can see each other. The p90 of the
    same space is 2.20 against 1.01 for the open box, which is what actually
    separates them. A criterion on the median would have failed a genuine
    two-room capture and blamed the map.
    """
    ratios = []
    for source in group:
        distance, _ = geodesics(source, adj)
        for target in group:
            if target <= source:
                continue
            straight = math.dist(nodes[source], nodes[target]) * resolution
            if straight < 1e-6 or not math.isfinite(distance[target]):
                continue
            ratios.append(distance[target] * resolution / straight)
    return ratios


def sample_episodes(nodes, adj, group, resolution, min_m, max_m, min_hops, want):
    """Start/goal pairs spread across the achievable path lengths.

    Not the first `want` pairs: enumerating in node order clusters the episodes
    in one corner of the building. Sorting by length and striding takes the
    whole range, which is what a training split needs and also what makes the
    yield number mean something.
    """
    found = []
    for source in group:
        distance, previous = geodesics(source, adj)
        for target in group:
            if target <= source or not math.isfinite(distance[target]):
                continue
            metres = distance[target] * resolution
            if not (min_m <= metres <= max_m):
                continue
            route = path_between(source, target, previous)
            if len(route) - 1 < min_hops:
                continue
            found.append((metres, source, target, route))
    found.sort(key=lambda item: (item[0], item[1], item[2]))
    if want <= 0 or len(found) <= want:
        return found
    stride = len(found) / float(want)
    return [found[min(len(found) - 1, int(i * stride))] for i in range(want)]


def percentiles(values, points=(0, 50, 90, 100)):
    if not len(values):
        return {}
    array = np.asarray(sorted(values), dtype=np.float64)
    return {f"p{p}": round(float(np.percentile(array, p)), 4) for p in points}


def build(carte, spacing_m=1.0, max_edge_m=2.5, robot_radius=0.25,
          min_clearance=None, episode_range=(2.0, 20.0), min_hops=3,
          episodes=100):
    """Everything above, over one loaded map. Returns a report dict.

    `min_clearance` defaults to the robot's radius: a viewpoint the robot
    cannot stand on is not somewhere it can be asked to go.
    """
    if min_clearance is None:
        min_clearance = robot_radius
    resolution = carte.resolution
    # nav2's rule, and the one `check_navmap` uses: inflate lethal obstacles
    # only. Growing the unknown as well asks a different question, and answering
    # that one here would make the graph disagree with the stack that drives it.
    passable = carte.passable(robot_radius=robot_radius, avoid_unknown=False)
    clear_m = navmap.clearance(~passable, resolution,
                              cap_m=max(1.0, min_clearance + resolution))

    spacing_cells = spacing_m / resolution
    nodes = sample_nodes(passable, clear_m, spacing_cells, min_clearance)
    edges = connect(nodes, passable, max_edge_m / resolution)
    adj = adjacency(len(nodes), edges)
    groups = components(len(nodes), adj)
    largest = groups[0] if groups else []

    degrees = [len(a) for a in adj]
    ratios = detour(nodes, adj, largest, resolution) if len(largest) > 1 else []
    picked = sample_episodes(nodes, adj, largest, resolution,
                             episode_range[0], episode_range[1], min_hops,
                             episodes) if len(largest) > 1 else []

    return {
        "settings": {
            "spacing_m": spacing_m, "max_edge_m": max_edge_m,
            "robot_radius_m": robot_radius, "min_clearance_m": min_clearance,
            "episode_range_m": list(episode_range), "min_hops": min_hops,
        },
        "passable_m2": round(float(passable.sum()) * resolution * resolution, 2),
        "nodes": len(nodes),
        "edges": len(edges),
        "mean_degree": round(float(np.mean(degrees)), 2) if degrees else 0.0,
        "isolated": int(sum(1 for d in degrees if d == 0)),
        "components": len(groups),
        "largest_component": len(largest),
        "largest_fraction": round(len(largest) / len(nodes), 4) if nodes else 0.0,
        "coverage": round(coverage(nodes, passable, spacing_cells), 4),
        "detour": percentiles(ratios),
        "detour_pairs": len(ratios),
        # The same finding as p90, said in a way that is easier to argue about:
        # what share of the viewpoint pairs have to go round something.
        "detour_over_1_25": (round(float(np.mean(np.asarray(ratios) >= 1.25)), 4)
                             if ratios else 0.0),
        "episodes": len(picked),
        "episode_length_m": percentiles([e[0] for e in picked]),
        "episode_hops": percentiles([len(e[3]) - 1 for e in picked]),
        "_nodes": nodes,
        "_edges": edges,
        "_episodes": picked,
    }


def draw(carte, report, path, width=900):
    """The map with its graph on top, because the verdict should be checkable.

    `marble-office` scoring 1.03 is a number until you see the picture: an
    L-shaped hall with no interior walls and a nearly complete graph across the
    middle of it. That is one glance against a paragraph of argument, and it is
    also how you would catch the opposite mistake -- a map whose walls the
    reconstruction lost would score 1.03 too, and look nothing like this.
    """
    from PIL import Image, ImageDraw

    # Row 0 of the grid is the lowest y; a picture wants it at the bottom.
    grid = np.flipud(carte.grid)
    rgb = np.zeros(grid.shape + (3,), dtype=np.uint8)
    rgb[grid == navmap.UNKNOWN] = (60, 60, 66)
    rgb[grid == navmap.OCCUPIED] = (20, 20, 24)
    rgb[grid == navmap.FREE] = (232, 232, 227)
    radius = report["settings"]["robot_radius_m"]
    rgb[np.flipud(carte.passable(robot_radius=radius, avoid_unknown=False))] = (250, 250, 245)

    scale = max(1, int(width / max(grid.shape)))
    image = Image.fromarray(rgb).resize(
        (grid.shape[1] * scale, grid.shape[0] * scale), Image.NEAREST)
    pen = ImageDraw.Draw(image)
    rows = grid.shape[0]

    def point(cell):
        iy, ix = cell
        return ((ix + 0.5) * scale, (rows - 1 - iy + 0.5) * scale)

    nodes = report["_nodes"]
    for (i, j, _) in report["_edges"]:
        pen.line([point(nodes[i]), point(nodes[j])], fill=(0, 130, 220), width=1)
    for cell in nodes:
        x, y = point(cell)
        r = max(2, scale)
        pen.ellipse([x - r, y - r, x + r, y + r], fill=(220, 40, 40))
    image.save(path)
    return path


def to_world(carte, report):
    """The report with cell indices replaced by metres, ready to serialise."""
    nodes = [carte.world_of(iy, ix) for (iy, ix) in report["_nodes"]]
    out = {k: v for k, v in report.items() if not k.startswith("_")}
    out["graph"] = {
        "nodes": [{"id": i, "x": round(x, 4), "y": round(y, 4)}
                  for i, (x, y) in enumerate(nodes)],
        "edges": [{"a": i, "b": j,
                   "length_m": round(length * carte.resolution, 4)}
                  for (i, j, length) in report["_edges"]],
    }
    out["episode_list"] = [
        {"start": source, "goal": target,
         "length_m": round(metres, 4), "path": route}
        for (metres, source, target, route) in report["_episodes"]
    ]
    return out
