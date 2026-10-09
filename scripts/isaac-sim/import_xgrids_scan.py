"""Turn an XGRIDS Lixel scan export into a world the Isaac Sim chain can load.

    python scripts/isaac-sim/import_xgrids_scan.py <export>/output --slug dinghao-2f

`<export>/output` is what LCC Studio writes: `mesh/mesh.ply` (a LiDAR mesh, in
metres, Z-up) and `ply-result/point_cloud/iteration_100/point_cloud*.ply` (a
3DGS splat in the same frame, at five levels of detail). Nothing else in it is
read -- `render/` and `render2/` are LCC's own streaming formats.

Unlike every other world in this tree, a scan arrives already metric, so there
is no scale to calibrate. What it does arrive with is a tilt. The scanner's
SLAM estimates gravity, and on `dinghao-2f` the floor comes out as a plane
leaning 0.42 deg -- 0.3 m of fall across a 70 m building, with residuals of
only +-8 cm around the plane. That is drift, not architecture, and it is not
harmless here: `build_navmap.py` casts from the TOP of the `floor` part and
`navigate.py` spawns from its BOTTOM, so a tilted floor puts those two 0.6 m
apart instead of 0.15. So the scan is levelled -- one rigid transform, applied
to the mesh and to every splat (positions and orientations) alike -- and the
transform is written into the manifest so the scanner's own frame can always
be got back, for instance to line the world up with the robot's `.pcd` map.

The collider is split into `floor` / `wall` / `ceiling` parts, because
`build_navmap.py` refuses a collider with no `floor` part rather than guess
where the floor is (see its comments for the two guesses that were tried).

No ground plane: the scan has a real floor everywhere a robot can stand, and an
infinite plane at z=0 would be coplanar with it -- every downward ray a coin
flip between the two -- and would let a robot walk out over the atrium.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone

import numpy as np
import trimesh

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# The exporter's LOD names. A scan carries five levels; two are enough -- one
# light enough to iterate with, and the full one to look at.
LODS = {"500k": "point_cloud_3.ply", "full_res": "point_cloud.ply"}


def plane_fit(points, weights):
    """Weighted least-squares z = a*x + b*y + c."""
    A = np.c_[points[:, 0], points[:, 1], np.ones(len(points))]
    w = np.sqrt(weights)
    coef, *_ = np.linalg.lstsq(A * w[:, None], points[:, 2] * w, rcond=None)
    return coef


def find_floor(mesh, band=0.15):
    """The main floor's plane, from the mesh's own upward-facing area.

    The floor is the height with the most upward-facing area. On a building
    scan that is not a given -- `dinghao-2f` also sees 135 m2 of the storey
    below through its atrium -- but the floor being scanned wins by a factor of
    four, and the result is printed so a wrong pick is visible.
    """
    normals, centres, areas = mesh.face_normals, mesh.triangles_center, mesh.area_faces
    up = normals[:, 2] > 0.95
    z = centres[up, 2]
    # A sliding 0.5 m window over 0.1 m bins, not fixed 0.5 m bins: a floor
    # that sits on a bin edge -- z=0 once levelled -- splits in two, and the
    # half that wins is not the floor's height. Measured on the second pass.
    bins = np.arange(np.floor(z.min()), np.ceil(z.max()) + 0.1, 0.1)
    hist, edges = np.histogram(z, bins=bins, weights=areas[up])
    window = np.convolve(hist, np.ones(5), mode="same")
    peak = int(np.argmax(window))
    guess = (edges[peak] + edges[peak + 1]) / 2
    print(f"  most upward-facing area: {window[peak]:.0f} m2 within 0.25 m of z={guess:.2f}")

    keep = up & (np.abs(centres[:, 2] - guess) < 0.6)
    for _ in range(3):
        coef = plane_fit(centres[keep], areas[keep])
        resid = centres[:, 2] - (coef[0] * centres[:, 0] + coef[1] * centres[:, 1] + coef[2])
        keep = up & (np.abs(resid) < band)
    resid = resid[keep]
    return coef, keep, resid


def rotation_between(a, b):
    """The smallest rotation taking unit vector a onto unit vector b (Rodrigues)."""
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-12:
        return np.eye(3)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k * (1.0 / (1.0 + c))


def quat_from_matrix(r):
    """(w, x, y, z) for a proper rotation matrix."""
    w = math.sqrt(max(0.0, 1.0 + r[0, 0] + r[1, 1] + r[2, 2])) / 2.0
    return np.array([w, (r[2, 1] - r[1, 2]) / (4 * w),
                     (r[0, 2] - r[2, 0]) / (4 * w), (r[1, 0] - r[0, 1]) / (4 * w)])


def quat_mul(p, q):
    """Hamilton product p * q, both (w, x, y, z); q may be an (N, 4) array."""
    pw, px, py, pz = p
    qw, qx, qy, qz = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    return np.stack([pw * qw - px * qx - py * qy - pz * qz,
                     pw * qx + px * qw + py * qz - pz * qy,
                     pw * qy - px * qz + py * qw + pz * qx,
                     pw * qz + px * qy - py * qx + pz * qw], axis=1)


def read_splat_ply(path):
    """A binary little-endian, all-float32 vertex PLY -- which is what LCC writes."""
    with open(path, "rb") as fh:
        props, count = [], 0
        while True:
            line = fh.readline().decode("ascii", "replace").strip()
            if line.startswith("format") and "binary_little_endian" not in line:
                raise SystemExit(f"{path}: expected binary_little_endian, got {line!r}")
            if line.startswith("element vertex"):
                count = int(line.split()[-1])
            elif line.startswith("property"):
                if line.split()[1] != "float":
                    raise SystemExit(f"{path}: non-float property {line!r}")
                props.append(line.split()[-1])
            elif line == "end_header":
                break
        data = np.fromfile(fh, dtype="<f4", count=count * len(props)).reshape(count, len(props))
    return props, data.copy()


def write_splat_ply(path, props, data):
    header = ["ply", "format binary_little_endian 1.0", f"element vertex {len(data)}"]
    header += [f"property float {p}" for p in props] + ["end_header", ""]
    with open(path, "wb") as fh:
        fh.write("\n".join(header).encode("ascii"))
        np.ascontiguousarray(data, dtype="<f4").tofile(fh)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", help="the LCC export's output/ directory")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", help="display name (default: the slug)")
    parser.add_argument("--floor-band", type=float, default=0.15,
                        help="metres either side of the levelled floor plane that count as floor")
    parser.add_argument("--ceiling-above", type=float, default=1.8,
                        help="downward-facing surfaces above this height are ceiling")
    parser.add_argument("--no-splat", action="store_true", help="collider and manifests only")
    args = parser.parse_args()

    world_dir = os.path.join(ROOT, "public", "worlds", args.slug)
    out_dir = os.path.join(world_dir, "output", "world")
    os.makedirs(out_dir, exist_ok=True)

    mesh_path = os.path.join(args.source, "mesh", "mesh.ply")
    print(f"mesh: {mesh_path}")
    mesh = trimesh.load(mesh_path, process=False)
    print(f"  {len(mesh.vertices)} vertices, {len(mesh.faces)} faces, "
          f"extent {np.round(mesh.extents, 2).tolist()} m")

    # --- level ------------------------------------------------------------
    coef, _, resid = find_floor(mesh, args.floor_band)
    normal = np.array([-coef[0], -coef[1], 1.0])
    tilt = math.degrees(math.acos(normal[2] / np.linalg.norm(normal)))
    rot = rotation_between(normal, np.array([0.0, 0.0, 1.0]))
    # After rotating, put the floor plane's height under the scan origin at z=0,
    # so the spawn the viewer uses (world origin) is on the floor.
    origin_floor = rot @ np.array([0.0, 0.0, coef[2]])
    shift = np.array([0.0, 0.0, -origin_floor[2]])
    transform = np.eye(4)
    transform[:3, :3], transform[:3, 3] = rot, shift
    print(f"  floor plane z = {coef[0]:+.5f} x {coef[1]:+.5f} y {coef[2]:+.3f}: "
          f"tilt {tilt:.3f} deg, residual p5/p50/p95 "
          f"{np.percentile(resid, 5):+.3f}/{np.percentile(resid, 50):+.3f}/"
          f"{np.percentile(resid, 95):+.3f} m")

    mesh.apply_transform(transform)
    check, keep, resid2 = find_floor(mesh, args.floor_band)
    check_tilt = math.degrees(math.atan(math.hypot(check[0], check[1])))
    print(f"  levelled: tilt {check_tilt:.4f} deg, floor at z={check[2]:+.4f} under the origin")
    if check_tilt > 0.02 or abs(check[2]) > 0.01:
        raise SystemExit("levelling did not level -- refusing to write a tilted world")

    # --- split ------------------------------------------------------------
    normals, centres = mesh.face_normals, mesh.triangles_center
    floor = keep
    ceiling = (normals[:, 2] < -0.9) & (centres[:, 2] > args.ceiling_above)
    wall = ~(floor | ceiling)
    scene = trimesh.Scene()
    parts = {}
    for label, mask in (("floor", floor), ("wall", wall), ("ceiling", ceiling)):
        part = mesh.submesh([np.flatnonzero(mask)], append=True)
        scene.add_geometry(part, geom_name=label, node_name=label)
        parts[label] = {"faces": int(mask.sum()), "area_m2": round(float(part.area), 1),
                        "z_range": [round(float(part.bounds[0, 2]), 3),
                                    round(float(part.bounds[1, 2]), 3)]}
        print(f"  {label:8s} {mask.sum():7d} faces {part.area:8.1f} m2  "
              f"z {part.bounds[0, 2]:+.3f}..{part.bounds[1, 2]:+.3f}")
    glb = os.path.join(out_dir, "0-world.glb")
    scene.export(glb)
    # trimesh writes GLB vertices as given; the exporter reads them back with
    # trimesh. Check that rather than assume it -- an axis swap here would turn
    # the building on its side, and the map would grid a wall as floor.
    back = trimesh.load(glb, process=False)
    if not np.allclose(back.bounds, mesh.bounds, atol=1e-3):
        raise SystemExit(f"GLB round trip moved the mesh: {back.bounds} vs {mesh.bounds}")
    print(f"wrote {glb} ({os.path.getsize(glb) / 1e6:.1f} MB)")

    # --- splat ------------------------------------------------------------
    splats = {}
    if not args.no_splat:
        q_rot = quat_from_matrix(rot)
        for lod, filename in LODS.items():
            src = os.path.join(args.source, "ply-result", "point_cloud", "iteration_100", filename)
            props, data = read_splat_ply(src)
            col = {p: i for i, p in enumerate(props)}
            xyz = data[:, [col["x"], col["y"], col["z"]]].astype(np.float64)
            data[:, [col["x"], col["y"], col["z"]]] = xyz @ rot.T + shift
            quat_cols = [col[f"rot_{i}"] for i in range(4)]
            quat = data[:, quat_cols].astype(np.float64)
            data[:, quat_cols] = quat_mul(q_rot, quat)
            dst = os.path.join(out_dir, f"0-world-{lod}.ply")
            write_splat_ply(dst, props, data)
            splats[lod] = {"source": filename, "splats": len(data)}
            print(f"wrote {dst} ({len(data)} splats, {os.path.getsize(dst) / 1e6:.0f} MB)")

    # --- manifests --------------------------------------------------------
    name = args.name or args.slug
    report_path = os.path.join(args.source, "render", "report.json")
    scan = {}
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        scan = {k: raw.get(k) for k in ("name", "deviceModel", "scanDuration", "buildStartTime",
                                         "pointCloudQuantity", "versionInfo")}
    manifest = {
        "world_id": args.slug,
        "display_name": name,
        "assets": {"splats": {
            "spz_urls": {},
            # The scan is Z-up; the exporter and viewer work Y-up and turn the
            # background by rotation_x to get there -- the Nerfstudio case in
            # export_world_usd.background_rotation_degrees.
            "semantics_metadata": {"metric_scale_factor": 1.0, "ground_plane_offset": 0.0,
                                   "flip_y": False, "rotation_x": -math.pi / 2},
        }},
        "source": {
            "kind": "xgrids-lcc-scan",
            "scan": scan,
            "mesh": "mesh/mesh.ply",
            "splats": splats,
            # world = levelling @ scanner. Invert it to get back to the scanner's
            # own frame -- the one dh_map.pcd and LCC's own viewer use.
            "levelling_transform": np.round(transform, 9).tolist(),
            "floor_plane_before": {"a": float(coef[0]), "b": float(coef[1]), "c": float(coef[2]),
                                   "tilt_deg": round(tilt, 4)},
            "floor_residual_m": {"p5": round(float(np.percentile(resid2, 5)), 4),
                                 "p50": round(float(np.percentile(resid2, 50)), 4),
                                 "p95": round(float(np.percentile(resid2, 95)), 4)},
            "collider_parts": parts,
        },
    }
    with open(os.path.join(out_dir, "0-world.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(world_dir, "scene.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "instances": [],
                   "sun": {"intensity": 1, "rotation": [0, 0, 0], "environmentIntensity": 1},
                   "metricScaleFactor": 1.0, "groundPlaneOffset": 0.0,
                   "groundPlaneColliderEnabled": False, "spawnPoint": [0.0, 0.0]}, fh, indent=2)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    project_path = os.path.join(world_dir, "project.json")
    created = now
    if os.path.exists(project_path):
        with open(project_path, encoding="utf-8") as fh:
            created = json.load(fh).get("created_at", now)
    with open(project_path, "w", encoding="utf-8") as fh:
        json.dump({"slug": args.slug, "display_name": name, "created_at": created,
                   "updated_at": now,
                   "notes": f"XGRIDS {scan.get('deviceModel', '')} LiDAR scan, imported by "
                            "scripts/isaac-sim/import_xgrids_scan.py (levelled, metric)."},
                  fh, indent=2, ensure_ascii=False)
    print(f"wrote the manifests -- next: npm run export:usd -- {args.slug} --up-axis z --no-splat")


if __name__ == "__main__":
    main()
