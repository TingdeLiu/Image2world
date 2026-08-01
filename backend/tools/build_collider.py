"""Derive a visible-surface collision proxy and ground calibration from a splat PLY.

SHARP reconstructs the background as Gaussians, which the viewer renders but
cannot collide with — the character controller only ever has the flat ground
plane, and a USD export carries no background geometry into Isaac Sim.

    splat centres -> distance cull -> voxel occupancy -> marching cubes
                  -> component filter -> quadric decimation -> GLB

**This is not room geometry.** SHARP is single-view: the point cloud is the
surface visible from one camera, so the output covers the visible floor, the
walls facing the camera, and the fronts of furniture. There is nothing behind
the camera and nothing behind an occluder, and the outline in plan view is the
shape of the camera's visible region, not the room's footprint. A character
walking to the edge of that region passes straight through. Treat the result as
"obstacles you can see", not as a sealed room; the flat ground plane remains the
thing that stops a fall.

Poisson reconstruction is deliberately not used. The cloud is an open shell
with no back faces, and Poisson would try to close it into a blob.

The dominant low horizontal surface also gives the floor height, which is what
`ground_plane_offset` in the world manifest needs in order to line the flat
ground collider up with the actual floor. That calibration is independent of
the coverage caveats above — it only needs the floor to be visible.

Usage:
    python backend/tools/build_collider.py <world-slug> [--voxel-size 0.05]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import trimesh
from scipy import ndimage
from skimage import measure

REPO_ROOT = Path(__file__).resolve().parents[2]
WORLDS_DIR = REPO_ROOT / "public" / "worlds"

PLY_SCALAR_NUMPY = {
    "float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8",
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
    "short": "<i2", "int16": "<i2", "ushort": "<u2", "uint16": "<u2",
    "int": "<i4", "int32": "<i4", "uint": "<u4", "uint32": "<u4",
}


def read_splat_ply(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Read positions and (logit) opacity from a binary little-endian 3DGS PLY."""
    with open(path, "rb") as handle:
        header = b""
        while b"end_header" not in header:
            chunk = handle.read(4096)
            if not chunk:
                raise ValueError(f"{path.name}: no end_header found")
            header += chunk
        header_end = header.find(b"\n", header.find(b"end_header")) + 1
        header_text = header[:header_end].decode("ascii")

        if "format binary_little_endian" not in header_text:
            raise ValueError(f"{path.name}: only binary_little_endian PLY is supported")

        vertex_count = 0
        properties: list[tuple[str, str]] = []
        in_vertex_element = False
        for line in (l.strip() for l in header_text.splitlines()):
            if line.startswith("element vertex"):
                vertex_count = int(line.split()[2])
                in_vertex_element = True
            elif line.startswith("element"):
                in_vertex_element = False
            elif line.startswith("property") and in_vertex_element:
                parts = line.split()
                if parts[1] == "list":
                    raise ValueError(f"{path.name}: list properties are not supported")
                properties.append((parts[2], parts[1]))

        dtype = np.dtype([(name, PLY_SCALAR_NUMPY[kind]) for name, kind in properties])
        handle.seek(header_end)
        raw = np.frombuffer(handle.read(vertex_count * dtype.itemsize), dtype=dtype, count=vertex_count)

    missing = {"x", "y", "z"} - set(raw.dtype.names)
    if missing:
        raise ValueError(f"{path.name}: missing position properties {sorted(missing)}")

    xyz = np.stack([raw["x"], raw["y"], raw["z"]], axis=1).astype(np.float32)
    opacity = raw["opacity"].astype(np.float32) if "opacity" in raw.dtype.names else None
    return xyz, opacity


def build_collider(
    xyz: np.ndarray,
    opacity: np.ndarray | None,
    *,
    voxel_size: float = 0.05,
    opacity_threshold: float = 0.35,
    max_distance: float = 15.0,
    min_hits: int = 2,
    close_iterations: int = 1,
    target_faces: int = 60_000,
    verbose: bool = True,
) -> trimesh.Trimesh:
    def log(message: str) -> None:
        if verbose:
            print(f"  {message}")

    if opacity is not None:
        alpha = 1.0 / (1.0 + np.exp(-opacity))  # PLY stores logit opacity
        keep = alpha >= opacity_threshold
        log(f"opacity filter: {int(keep.sum()):,} / {len(keep):,} kept")
        xyz = xyz[keep]

    # SHARP places anything seen through a window hundreds of metres away. It is
    # unreachable and would dominate the voxel volume.
    near = np.linalg.norm(xyz, axis=1) <= max_distance
    log(f"distance cull: {int(near.sum()):,} points within {max_distance} m")
    xyz = xyz[near]
    if len(xyz) < 1000:
        raise ValueError("too few points survived filtering to build a collider")

    lo = np.percentile(xyz, 0.5, axis=0)
    hi = np.percentile(xyz, 99.5, axis=0)
    xyz = xyz[np.all((xyz >= lo) & (xyz <= hi), axis=1)]

    origin = xyz.min(axis=0) - voxel_size * 3
    dims = np.maximum(np.ceil((xyz.max(axis=0) + voxel_size * 3 - origin) / voxel_size).astype(int), 3)
    if dims.prod() > 80_000_000:
        raise ValueError(f"voxel volume too large ({dims.prod():,}); increase --voxel-size")
    log(f"voxel volume: {dims.tolist()} @ {voxel_size} m")

    index = np.clip(np.floor((xyz - origin) / voxel_size).astype(np.int32), 0, dims - 1)
    flat = (index[:, 0] * dims[1] + index[:, 1]) * dims[2] + index[:, 2]
    occupancy = np.bincount(flat, minlength=int(dims.prod())).reshape(dims) >= min_hits

    if close_iterations > 0:
        # Bridge single-voxel gaps so the shell is continuous enough to stand on.
        occupancy = ndimage.binary_closing(occupancy, iterations=close_iterations)
    log(f"occupied voxels: {int(occupancy.sum()):,}")

    padded = np.pad(occupancy.astype(np.float32), 1, mode="constant", constant_values=0)
    vertices, faces, _, _ = measure.marching_cubes(padded, level=0.5)
    vertices = (vertices - 1.0) * voxel_size + origin
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    log(f"marching cubes: {len(mesh.vertices):,} verts / {len(mesh.faces):,} faces")

    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:
        # A 0.05 m shell around even a small object is a few hundred faces, so
        # anything below that is reconstruction speckle.
        kept = [p for p in parts if len(p.faces) >= 120]
        if kept:
            dropped = sum(len(p.faces) for p in parts) - sum(len(p.faces) for p in kept)
            mesh = trimesh.util.concatenate(kept)
            log(f"components: {len(parts)} found, {len(kept)} kept ({dropped:,} speckle faces dropped)")

    if len(mesh.faces) > target_faces:
        mesh = mesh.simplify_quadric_decimation(face_count=target_faces)
        log(f"decimated: {len(mesh.vertices):,} verts / {len(mesh.faces):,} faces")

    return mesh


def detect_ground_offset(mesh: trimesh.Trimesh, *, flip_y: bool = True, bins: int = 40) -> float | None:
    """Return the `ground_plane_offset` that puts the floor on y=0.

    Work in viewer space, not SHARP space. SHARP's +Y points *down* (hence
    `flip_y` in the manifest), so the strongest upward-facing plane in raw
    coordinates is the ceiling, not the floor.
    """
    sign = -1.0 if flip_y else 1.0
    normals_y = mesh.face_normals[:, 1] * sign
    centers_y = mesh.triangles_center[:, 1] * sign
    areas = mesh.area_faces

    upward = normals_y > 0.85
    if upward.sum() < 10:
        return None

    histogram, edges = np.histogram(centers_y[upward], bins=bins, weights=areas[upward])
    if histogram.max() <= 0:
        return None

    # The floor is the strongest upward-facing band in the lower half of the
    # room; desks and shelves contribute smaller bands higher up.
    lower = histogram[: max(1, bins // 2)]
    floor_y = float(edges[int(np.argmax(lower))])
    return -floor_y


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug", help="World slug under public/worlds/")
    parser.add_argument("--voxel-size", type=float, default=0.05, help="Voxel edge in metres (default: 0.05)")
    parser.add_argument("--target-faces", type=int, default=60_000, help="Face budget after decimation")
    parser.add_argument("--max-distance", type=float, default=15.0, help="Cull splats beyond this radius")
    parser.add_argument("--dry-run", action="store_true", help="Report results without writing files")
    args = parser.parse_args()

    world_dir = WORLDS_DIR / args.slug / "output" / "world"
    if not world_dir.is_dir():
        print(f"error: no world at {world_dir}", file=sys.stderr)
        raise SystemExit(1)

    plys = sorted(world_dir.glob("*-world-full_res.ply"))
    if not plys:
        print(f"error: no <n>-world-full_res.ply in {world_dir}", file=sys.stderr)
        raise SystemExit(1)
    ply_path = plys[-1]
    index = ply_path.name.split("-", 1)[0]

    print(f"reading {ply_path.name}")
    xyz, opacity = read_splat_ply(ply_path)
    print(f"  {len(xyz):,} splats")

    mesh = build_collider(
        xyz,
        opacity,
        voxel_size=args.voxel_size,
        max_distance=args.max_distance,
        target_faces=args.target_faces,
    )

    size = mesh.bounds[1] - mesh.bounds[0]
    print(f"  room size: {size[0]:.2f} x {size[1]:.2f} x {size[2]:.2f} m (X x height x Z)")

    ground_offset = detect_ground_offset(mesh)
    if ground_offset is None:
        print("  warning: no dominant floor surface found; leaving ground_plane_offset unchanged")
    else:
        print(f"  floor at y={-ground_offset:+.2f} (viewer space) -> ground_plane_offset = {ground_offset:+.3f}")

    if args.dry_run:
        print("dry run; nothing written")
        return

    glb_path = world_dir / f"{index}-world.glb"
    mesh.export(glb_path)
    print(f"wrote {glb_path.relative_to(REPO_ROOT)} ({glb_path.stat().st_size / 1024 / 1024:.1f} MB)")

    if ground_offset is None:
        return

    manifest_path = world_dir / f"{index}-world.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        semantics = manifest.setdefault("assets", {}).setdefault("splats", {}).setdefault("semantics_metadata", {})
        semantics["ground_plane_offset"] = round(ground_offset, 4)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"updated {manifest_path.relative_to(REPO_ROOT)} (ground_plane_offset)")

    # scene.json wins over the manifest in both the viewer and the USD exporter,
    # so a leftover default of 0 there would keep the ground plane misaligned.
    # Only touch it while it still holds that default -- a value the user tuned
    # in the editor is theirs.
    scene_path = WORLDS_DIR / args.slug / "scene.json"
    if not scene_path.is_file():
        return
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    current = scene.get("groundPlaneOffset")
    if current not in (0, 0.0, None):
        print(f"note: scene.json keeps its edited groundPlaneOffset ({current}); not overwriting")
        return
    scene["groundPlaneOffset"] = round(ground_offset, 4)
    scene_path.write_text(json.dumps(scene, indent=2), encoding="utf-8")
    print(f"updated {scene_path.relative_to(REPO_ROOT)} (groundPlaneOffset)")


if __name__ == "__main__":
    main()
