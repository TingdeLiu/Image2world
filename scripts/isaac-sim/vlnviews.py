"""From a VLN graph to what an agent would stand on and see at each viewpoint.

`vln_check.py` stops at the graph: viewpoints in the Isaac Sim map frame, edges,
episodes. A VLN dataset needs more than that -- R2R hands the agent a panorama
at every viewpoint and a connectivity file saying which ones it can step
between -- and this is the first half of getting there:

  * **Where the camera goes.** The graph lives in the map frame (Z-up, metres,
    what Isaac Sim sees after `export_world_usd.py --up-axis z`). The splat and
    the collider live in the viewer frame (Y-up), under the world's background
    transform. The map is the viewer frame turned +90 deg about X, so
    `(x, y, z)_map == (x, -z, y)_viewer`, and nothing else moves: the exporter
    applies the background transform *inside* that rotation.
  * **Whether the camera is somewhere sane.** Every viewpoint is probed against
    the collider before anything is rendered: a ray down must land on floor
    (not a desk, not nothing), there must be headroom for the eye, and the
    viewpoint must be as clear of walls as the graph claimed. This is the check
    that the frame conversion above is right, and it is an independent one --
    the graph came out of PhysX raycasts against the USD the exporter wrote,
    and these probes go through this file's own transform against the GLB. If
    the two disagreed about which way is up, the eyes would be in the walls.
  * **The files a VLN codebase reads.** `connectivity.json` in the Matterport3D
    simulator's shape (one entry per viewpoint: `image_id`, a camera-to-world
    `pose`, `included`, `unobstructed`, `visible`, `height`) and an R2R-style
    episode file with empty `instructions` -- the language is the part nobody
    has written yet. Poses stay in the map frame because MP3D's is Z-up too.
  * **A render job** for `backend/tools/render_viewpoints.py`, which runs where
    gsplat does (the WSL Splatfacto venv) and turns it into six skybox faces per
    viewpoint. Skybox, not equirectangular, because that is what MP3D ships
    and what R2R feature extractors already consume.

Headings follow MP3D: 0 looks along +y of the map, increasing clockwise
(towards +x). Skybox faces are 0 up, 1-4 the horizon at headings 0/90/180/270,
5 down. Cameras are OpenCV (x right, y down, z forward).

The collider's parts (`floor`, `wall`, `elevated`, `ramp`, `ceiling`) are
written by `build_collider.py` and are the only semantics any world has today.
`--labels` rasterises them into per-face label maps by ray casting. Coarse,
but it is ground truth about the geometry, which a segmentation model's output
would not be.

    python scripts/isaac-sim/vlnviews.py <slug>
    python scripts/isaac-sim/vlnviews.py reference-rooms --labels 64
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "usd-export"))

# Where the eye goes, above the floor directly under the viewpoint. MP3D's
# tripod stood at roughly this height; R2R's viewpoints are a person's eye.
EYE_HEIGHT = 1.5
# The downward probe starts this far above the top of the map's ground band.
# Every viewpoint is on a passable cell, which `build_navmap.py` cleared to the
# robot's height (0.6 m) above that band, so nothing solid sits in between.
PROBE_START = 0.05
# Horizontal probes: height above the local floor, and how many directions.
BODY_HEIGHT = 0.3
BODY_RAYS = 16
# How far a probe is allowed to look before it is "nothing there".
PROBE_REACH = 4.0
# Slack below the lowest ground the map measured.
FLOOR_TOLERANCE = 0.05

FACE_NAMES = ("up", "h000", "h090", "h180", "h270", "down")


# -- frames ---------------------------------------------------------------

def map_to_viewer(points):
    """(x, y, z) in the Z-up map frame -> (x, y, z) in the Y-up viewer frame."""
    points = np.asarray(points, dtype=np.float64)
    return np.stack([points[..., 0], points[..., 2], -points[..., 1]], axis=-1)


def viewer_to_map(points):
    points = np.asarray(points, dtype=np.float64)
    return np.stack([points[..., 0], -points[..., 2], points[..., 1]], axis=-1)


def background_matrix(flip_y, rotation_x, metric_scale, ground_offset):
    """The world's background transform, as a 4x4 on viewer-frame points.

    The same composition `worldTransform.ts` spreads onto the splat and the
    collider, and `export_world_usd.py` authors: translate, then rotate about X,
    then scale uniformly.
    """
    angle = rotation_x if rotation_x is not None else (math.pi if flip_y else 0.0)
    c, s = math.cos(angle), math.sin(angle)
    rotate = np.array([[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1]], dtype=np.float64)
    scale = np.diag([metric_scale, metric_scale, metric_scale, 1.0])
    translate = np.eye(4)
    translate[1, 3] = ground_offset
    return translate @ rotate @ scale


def heading_vector(heading):
    """Unit viewer-frame direction for an MP3D heading (radians)."""
    return map_to_viewer([math.sin(heading), math.cos(heading), 0.0])


def skybox_rotations():
    """Camera-to-world rotations (columns x, y, z) for the six faces.

    Right is +x of the viewer frame on every face, so neighbouring faces meet
    along shared edges the way a cube map expects: the horizon faces turn
    clockwise, the up face has heading 180 at its top edge (as it would for
    someone tipping their head back from heading 0) and the down face heading 0.
    """
    up = np.array([0.0, 1.0, 0.0])
    forwards = [up] + [heading_vector(math.radians(h)) for h in (0, 90, 180, 270)] + [-up]
    downs = ([heading_vector(0.0)] + [-up] * 4 + [heading_vector(math.pi)])
    out = []
    for forward, image_down in zip(forwards, downs):
        right = np.cross(image_down, forward)
        out.append(np.stack([right, image_down, forward], axis=1))
    return out


def world_to_camera(rotation, centre):
    matrix = np.eye(4)
    matrix[:3, :3] = rotation.T
    matrix[:3, 3] = -rotation.T @ centre
    return matrix


def face_intrinsics(size):
    """A 90 deg pinhole, so six faces close into a cube."""
    focal = size / 2.0
    return np.array([[focal, 0, size / 2.0], [0, focal, size / 2.0], [0, 0, 1]], dtype=np.float64)


# -- collider -------------------------------------------------------------

def load_collider(path, matrix):
    """Triangles in the viewer frame, a label index per triangle, label names."""
    import trimesh

    scene = trimesh.load(str(path), force="scene")
    names, triangles, labels = [], [], []
    for node in scene.graph.nodes_geometry:
        transform, geometry_name = scene.graph[node]
        geometry = scene.geometry[geometry_name]
        if not len(geometry.faces):
            continue
        vertices = np.asarray(geometry.vertices, dtype=np.float64)
        vertices = vertices @ transform[:3, :3].T + transform[:3, 3]
        vertices = vertices @ matrix[:3, :3].T + matrix[:3, 3]
        if geometry_name not in names:
            names.append(geometry_name)
        triangles.append(vertices[np.asarray(geometry.faces)])
        labels.append(np.full(len(geometry.faces), names.index(geometry_name), dtype=np.int16))
    if not triangles:
        raise ValueError(f"collider {path} has no triangles")
    return np.concatenate(triangles), np.concatenate(labels), names


def near(triangles, centre, radius):
    """Indices of triangles whose bounding box comes within `radius` of `centre`."""
    low = triangles.min(axis=1)
    high = triangles.max(axis=1)
    closest = np.clip(centre, low, high)
    return np.flatnonzero(((closest - centre) ** 2).sum(axis=1) <= radius * radius)


def raycast(triangles, origins, directions, reach=np.inf, chunk=4096):
    """Nearest hit of each ray, two-sided. Returns (distance, triangle index).

    Moller-Trumbore, brute force over the triangles it is given -- callers
    narrow those with `near` first. `inf` / -1 where nothing is hit.
    """
    origins = np.atleast_2d(np.asarray(origins, dtype=np.float64))
    directions = np.atleast_2d(np.asarray(directions, dtype=np.float64))
    best = np.full(len(origins), np.inf)
    index = np.full(len(origins), -1, dtype=np.int64)
    if not len(triangles):
        return best, index
    for start in range(0, len(triangles), chunk):
        tri = triangles[start:start + chunk]
        v0 = tri[:, 0]
        e1 = tri[:, 1] - v0
        e2 = tri[:, 2] - v0
        p = np.cross(directions[:, None, :], e2[None])
        det = np.einsum("tk,rtk->rt", e1, p)
        valid = np.abs(det) > 1e-12
        inverse = np.where(valid, 1.0 / np.where(valid, det, 1.0), 0.0)
        s = origins[:, None, :] - v0[None]
        u = np.einsum("rtk,rtk->rt", s, p) * inverse
        q = np.cross(s, e1[None])
        v = np.einsum("rk,rtk->rt", directions, q) * inverse
        t = np.einsum("tk,rtk->rt", e2, q) * inverse
        hit = valid & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-6) & (t <= reach)
        t = np.where(hit, t, np.inf)
        local = t.argmin(axis=1)
        nearest = t[np.arange(len(origins)), local]
        better = nearest < best
        best[better] = nearest[better]
        index[better] = local[better] + start
    return best, index


# -- probes ---------------------------------------------------------------

def probe(triangles, labels, names, node, ground, robot_radius):
    """Stand at one viewpoint and look down, up and around.

    `ground` is the map's own account of where walking happens: `low`, the
    lowest walkable ground `build_navmap.py` measured, and `high`, the top of
    the band it accepted as ground. The floor under a viewpoint is judged by
    that band and not by the collider's part label, because the band is what
    the map was built with -- and because the labels come from face normals,
    which call the downward-facing skin of a splat-derived floor `ceiling`
    (measured: 94 of `marble-office`'s 99 viewpoints stand on one, 0.1 m up).
    The label is recorded; it is not the verdict.

    Returns a dict. `ok` is False with a `fault` and a `reason` when the camera
    cannot go there: no floor under it, the floor is outside the ground band
    (a desk, a shelf), there is no room for the eye, or a wall is closer than
    the graph said.
    """
    x, y = node
    start = map_to_viewer([x, y, ground["high"] + PROBE_START])
    nearby = near(triangles, start, PROBE_REACH + EYE_HEIGHT)
    local, local_labels = triangles[nearby], labels[nearby]
    down = np.array([0.0, -1.0, 0.0])
    reach = ground["high"] - ground["low"] + PROBE_START + 1.0
    distance, hit = raycast(local, start, down, reach=reach)
    out = {"floor_label": None, "floor_z": None, "headroom": None, "clearance": None}
    if hit[0] < 0:
        return {**out, "ok": False, "fault": "no_floor", "reason": "no floor under the viewpoint"}
    label = names[local_labels[hit[0]]]
    local_floor = ground["high"] + PROBE_START - float(distance[0])
    out.update(floor_label=label, floor_z=round(local_floor, 4))
    if local_floor < ground["low"] - FLOOR_TOLERANCE:
        return {**out, "ok": False, "fault": "floor_height",
                "reason": f"floor is {local_floor - ground['low']:+.2f} m below the map's lowest ground"}
    if local_floor > ground["high"]:
        return {**out, "ok": False, "fault": "not_floor",
                "reason": f"stands on '{label}' {local_floor:.2f} m up, above the ground band"}

    floor_point = map_to_viewer([x, y, local_floor])
    up_start = floor_point + np.array([0.0, 0.05, 0.0])
    distance, _ = raycast(local, up_start, -down, reach=PROBE_REACH)
    headroom = 0.05 + float(distance[0])
    out["headroom"] = round(headroom, 4) if math.isfinite(headroom) else None
    if headroom < EYE_HEIGHT + 0.05:
        return {**out, "ok": False, "fault": "headroom",
                "reason": f"only {headroom:.2f} m of headroom for a {EYE_HEIGHT} m eye"}

    body = floor_point + np.array([0.0, BODY_HEIGHT, 0.0])
    angles = np.arange(BODY_RAYS) * (2 * math.pi / BODY_RAYS)
    directions = np.stack([heading_vector(a) for a in angles])
    distance, _ = raycast(local, np.repeat(body[None], BODY_RAYS, axis=0), directions, reach=PROBE_REACH)
    clearance = float(distance.min())
    out["clearance"] = round(clearance, 4) if math.isfinite(clearance) else None
    # Half a map cell of slack: the graph measured clearance on a 5 cm grid.
    if clearance < robot_radius - 0.05:
        return {**out, "ok": False, "fault": "clearance",
                "reason": f"a wall {clearance:.2f} m away, closer than the {robot_radius} m the graph cleared"}
    return {**out, "ok": True, "fault": "", "reason": ""}


# -- labels ---------------------------------------------------------------

def face_rays(size):
    """Unit camera-frame directions for every pixel centre of one face."""
    focal = size / 2.0
    ys, xs = np.mgrid[0:size, 0:size] + 0.5
    rays = np.stack([(xs - size / 2.0) / focal, (ys - size / 2.0) / focal, np.ones_like(xs)], axis=-1)
    return (rays / np.linalg.norm(rays, axis=-1, keepdims=True)).reshape(-1, 3)


def label_skybox(triangles, labels, eye, size, reach=30.0):
    """Per-face label (-1 = nothing) and depth along the ray, by ray casting."""
    rays = face_rays(size)
    local = near(triangles, eye, reach)
    faces_label, faces_depth = [], []
    for rotation in skybox_rotations():
        world = rays @ rotation.T
        distance, hit = raycast(triangles[local], np.repeat(eye[None], len(world), axis=0), world, reach=reach)
        label = np.where(hit >= 0, labels[local][np.maximum(hit, 0)], -1)
        faces_label.append(label.reshape(size, size).astype(np.int16))
        faces_depth.append(distance.reshape(size, size).astype(np.float32))
    return np.stack(faces_label), np.stack(faces_depth)


# -- world ----------------------------------------------------------------

def pick_ply(world_dir, index):
    """The densest PLY the world has. The renderer reads PLY, not SPZ."""
    for lod in ("full_res", "500k", "150k", "100k"):
        candidate = world_dir / f"{index}-world-{lod}.ply"
        if candidate.is_file():
            return candidate
    return None


def build(slug, worlds_dir, robot_radius=0.25, face_size=512, labels_size=0):
    import export_world_usd as exporter

    exporter.WORLDS_DIR = Path(worlds_dir)
    world = exporter.resolve_world(slug, "full_res", need_splat=False)
    if world.collider_path is None:
        raise SystemExit(f"error: {slug} has no collider GLB to probe against")
    export_dir = Path(worlds_dir) / slug / "export"
    graph = json.loads((export_dir / f"{slug}-vlngraph.json").read_text(encoding="utf-8"))
    navmeta = json.loads((export_dir / f"{slug}-navmap.json").read_text(encoding="utf-8"))
    # Not `floor_top_z`: that is the top of the floor part's bounding box
    # (0.37 m on `marble-office`, whose floor is at 0.004). The walkable band
    # is what the map was actually built from.
    band = navmeta["walkable_ground_z"]
    ground = {"low": float(band["p0"]), "high": float(band["band_top"])}
    floor_z = float(band["p50"])

    matrix = background_matrix(world.flip_y, world.rotation_x,
                               world.metric_scale_factor, world.ground_plane_offset)
    triangles, tri_labels, names = load_collider(world.collider_path, matrix)

    nodes = graph["graph"]["nodes"]
    count = len(nodes)
    neighbours = [[False] * count for _ in range(count)]
    for edge in graph["graph"]["edges"]:
        neighbours[edge["a"]][edge["b"]] = neighbours[edge["b"]][edge["a"]] = True

    image_ids = [f"{node['id']:04d}" for node in nodes]
    viewpoints, connectivity, cameras = [], [], []
    rotations = skybox_rotations()
    for node, image_id in zip(nodes, image_ids):
        report = probe(triangles, tri_labels, names, (node["x"], node["y"]), ground, robot_radius)
        stand = report["floor_z"] if report["floor_z"] is not None else floor_z
        eye_map = np.array([node["x"], node["y"], stand + EYE_HEIGHT])
        eye = map_to_viewer(eye_map)
        pose = np.eye(4)
        pose[:3, 3] = eye_map
        connectivity.append({
            "image_id": image_id,
            "pose": [round(float(v), 6) for v in pose.reshape(-1)],
            "height": EYE_HEIGHT,
            "included": bool(report["ok"]),
            "unobstructed": neighbours[node["id"]],
            "visible": neighbours[node["id"]],
        })
        viewpoints.append({"image_id": image_id, "map_xyz": [round(float(v), 4) for v in eye_map],
                           **report})
        cameras.append({
            "image_id": image_id,
            "eye_viewer": [round(float(v), 6) for v in eye],
            "world_to_camera": [world_to_camera(r, eye).round(8).tolist() for r in rotations],
        })

    included = {vp["image_id"] for vp in viewpoints if vp["ok"]}
    episodes, dropped = [], 0
    for number, episode in enumerate(graph.get("episode_list", [])):
        path = [image_ids[i] for i in episode["path"]]
        if not all(p in included for p in path):
            dropped += 1
            continue
        first, second = (nodes[episode["path"][0]], nodes[episode["path"][1]])
        heading = math.atan2(second["x"] - first["x"], second["y"] - first["y"]) % (2 * math.pi)
        episodes.append({"path_id": number, "scan": slug, "path": path,
                         "heading": round(heading, 6), "distance": episode["length_m"],
                         "instructions": []})

    labels = None
    if labels_size:
        labels = {}
        for camera in cameras:
            if camera["image_id"] in included:
                labels[camera["image_id"]] = label_skybox(
                    triangles, tri_labels, np.asarray(camera["eye_viewer"]), labels_size)

    failures = {}
    for vp in viewpoints:
        if not vp["ok"]:
            failures[vp["fault"]] = failures.get(vp["fault"], 0) + 1
    clear = [vp["clearance"] for vp in viewpoints if vp["clearance"] is not None]
    summary = {
        "slug": slug,
        "viewpoints": count,
        "included": len(included),
        "excluded": count - len(included),
        "exclusions": failures,
        "episodes": len(episodes),
        "episodes_dropped": dropped,
        "floor_z": floor_z,
        "ground_band": [ground["low"], ground["high"]],
        "eye_height": EYE_HEIGHT,
        "clearance_p0": round(min(clear), 4) if clear else None,
        "labels": names,
    }
    job = {
        "slug": slug,
        "splat": str(pick_ply(world.collider_path.parent, world.version_index) or ""),
        "background_matrix": matrix.round(10).tolist(),
        "face_size": face_size,
        "intrinsics": face_intrinsics(face_size).tolist(),
        "faces": list(FACE_NAMES),
        "cameras": [c for c in cameras if c["image_id"] in included],
    }
    return summary, viewpoints, connectivity, episodes, job, labels


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("slug")
    parser.add_argument("--worlds", default=str(REPO_ROOT / "public" / "worlds"))
    parser.add_argument("--robot-radius", type=float, default=0.25,
                        help="the radius the VLN graph was built for")
    parser.add_argument("--face-size", type=int, default=512, help="skybox face size for the render job")
    parser.add_argument("--labels", type=int, default=0, metavar="SIZE",
                        help="also ray-cast collider part labels per face at this size (slow on "
                             "captured colliders; seconds on the reference fixture)")
    parser.add_argument("--out", help="output directory (default: <world>/export/vln)")
    args = parser.parse_args()

    summary, viewpoints, connectivity, episodes, job, labels = build(
        args.slug, args.worlds, args.robot_radius, args.face_size, args.labels)
    out = Path(args.out or Path(args.worlds) / args.slug / "export" / "vln")
    out.mkdir(parents=True, exist_ok=True)

    def write(name, payload):
        (out / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return out / name

    write(f"{args.slug}_connectivity.json", connectivity)
    write(f"{args.slug}_episodes.json", episodes)
    write("viewpoints.json", {"summary": summary, "viewpoints": viewpoints})
    write("render_job.json", job)
    if labels:
        np.savez_compressed(out / "labels.npz",
                            names=np.array(summary["labels"]),
                            **{f"label_{k}": v[0] for k, v in labels.items()},
                            **{f"depth_{k}": v[1].astype(np.float16) for k, v in labels.items()})

    print(f"{args.slug}: {summary['included']}/{summary['viewpoints']} viewpoints usable, "
          f"{summary['episodes']} episodes ({summary['episodes_dropped']} dropped for "
          f"passing through an excluded viewpoint)")
    for reason, n in sorted(summary["exclusions"].items(), key=lambda kv: -kv[1]):
        print(f"  excluded {n:>4}  {reason}")
    if not job["splat"]:
        print("  no PLY splat: nothing for render_viewpoints.py to render")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
