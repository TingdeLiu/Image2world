"""Derive a visible-surface collision proxy and ground calibration from a splat PLY.

SHARP reconstructs the background as Gaussians, which the viewer renders but
cannot collide with — the character controller only ever has the flat ground
plane, and a USD export carries no background geometry into Isaac Sim.

    splat centres -> distance cull -> voxel occupancy -> marching cubes
                  -> component filter -> quadric decimation
                  -> surface classification -> GLB

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

That same floor height then splits the mesh by what a walker would meet: floor,
raised surfaces, ramps, walls, ceiling. Each becomes its own GLB node, so the
viewer can give them different friction instead of treating the whole room as
one undifferentiated slab. See `src/modules/collider/surfaces.ts` for the
coefficients the classes map to.

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

# Face classes, in the order the viewer expects them. `src/modules/collider/
# surfaces.ts` maps each to a friction coefficient; keep the two in sync.
SURFACE_CLASSES = ("floor", "elevated", "ramp", "wall", "ceiling")

# Finding the floor asks "is this flat enough to be the floor", so it wants a
# strict threshold. Classification asks the different question "can a capsule
# stand here", whose answer is the ~45 degree slope limit every physics engine
# uses. Sharing one constant between the two put 36% of the mesh in `ramp`;
# measured across three worlds, splitting them drops that to 9-10%.
FLOOR_DETECT_NORMAL = 0.85
WALKABLE_NORMAL = 0.70   # 45.6 degrees
VERTICAL_NORMAL = 0.60   # steeper than 53 degrees is a wall, not a ramp
# How far above the detected floor a surface can sit and still be the floor.
# Marching cubes on a 0.05 m grid puts real floor faces within a voxel or two.
FLOOR_BAND = 0.15

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


def detect_floor_height(mesh: trimesh.Trimesh, *, flip_y: bool = True, bins: int = 40) -> float | None:
    """Height of the dominant low upward-facing surface, in viewer space.

    Work in viewer space, not SHARP space. SHARP's +Y points *down* (hence
    `flip_y` in the manifest), so the strongest upward-facing plane in raw
    coordinates is the ceiling, not the floor.
    """
    sign = -1.0 if flip_y else 1.0
    normals_y = mesh.face_normals[:, 1] * sign
    centers_y = mesh.triangles_center[:, 1] * sign
    areas = mesh.area_faces

    upward = normals_y > FLOOR_DETECT_NORMAL
    if upward.sum() < 10:
        return None

    histogram, edges = np.histogram(centers_y[upward], bins=bins, weights=areas[upward])
    if histogram.max() <= 0:
        return None

    # The floor is the strongest upward-facing band in the lower half of the
    # room; desks and shelves contribute smaller bands higher up.
    lower = histogram[: max(1, bins // 2)]
    return float(edges[int(np.argmax(lower))])


def detect_ground_offset(mesh: trimesh.Trimesh, *, flip_y: bool = True, bins: int = 40) -> float | None:
    """Return the `ground_plane_offset` that puts the floor on y=0."""
    floor_y = detect_floor_height(mesh, flip_y=flip_y, bins=bins)
    return None if floor_y is None else -floor_y


def classify_surfaces(
    mesh: trimesh.Trimesh,
    *,
    floor_y: float | None,
    flip_y: bool = True,
) -> dict[str, np.ndarray]:
    """Group faces by what a walker meets there.

    Returns class name -> face indices, omitting classes with no faces. The
    split is by face normal first and height second, both in viewer space:

    - `floor`     standable, at the detected floor height
    - `elevated`  standable but higher up — desks, counters, beds
    - `ramp`      too steep to stand on, too shallow to be a wall
    - `wall`      near-vertical
    - `ceiling`   standable if you were upside down

    Without a detected floor height every standable face is called `floor`.
    That is the conservative direction: floor friction is the high one, so an
    unclassified room walks the way it did before this split existed.
    """
    sign = -1.0 if flip_y else 1.0
    normals_y = mesh.face_normals[:, 1] * sign
    centers_y = mesh.triangles_center[:, 1] * sign

    # Faces between the two thresholds are neither standable nor vertical, so
    # `ramp` is the default rather than a case of its own.
    labels = np.full(len(mesh.faces), "ramp", dtype="<U8")
    upward = normals_y >= WALKABLE_NORMAL
    labels[upward] = "floor"
    if floor_y is not None:
        labels[upward & (centers_y > floor_y + FLOOR_BAND)] = "elevated"
    labels[normals_y <= -WALKABLE_NORMAL] = "ceiling"
    labels[np.abs(normals_y) < VERTICAL_NORMAL] = "wall"

    groups = {name: np.flatnonzero(labels == name) for name in SURFACE_CLASSES}
    return {name: faces for name, faces in groups.items() if len(faces)}


def labeled_collider_scene(mesh: trimesh.Trimesh, groups: dict[str, np.ndarray]) -> trimesh.Scene:
    """Pack the classified faces into one GLB node per class.

    trimesh names each node after its geometry key, and three's GLTFLoader
    copies that onto `Object3D.name` — which is how the viewer tells a wall
    from a floor. Older worlds export a single unnamed `geometry_0` node and
    the viewer falls back to uniform friction for them.
    """
    if not groups:
        return trimesh.Scene(mesh)
    return trimesh.Scene({
        name: mesh.submesh([faces], append=True)
        for name, faces in groups.items()
    })


def read_metric_scale(world_dir: Path, slug: str, index: str) -> float:
    """Reconstruction units per metre, read the way the viewer reads it.

    Every threshold in this file is written in metres -- 5 cm voxels, a 15 cm
    floor band, a 15 m cull radius -- but a point cloud is in whatever units its
    reconstruction produced. The viewer multiplies by this factor to get metres,
    so anything measured here has to as well, or the numbers mean different
    things on the two sides. At the 3.66 seen on a real splatfacto world that is
    not a rounding difference: it is centimetres against handspans.

    scene.json wins over the manifest, matching the viewer and the USD exporter,
    so a world the user rescaled in the editor measures the way it looks.
    """

    def positive(value: object) -> float | None:
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

    scene_path = WORLDS_DIR / slug / "scene.json"
    if scene_path.is_file():
        try:
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
            found = positive(scene.get("metricScaleFactor"))
            if found is not None:
                return found
        except (OSError, json.JSONDecodeError):
            pass

    manifest_path = world_dir / f"{index}-world.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            semantics = (
                manifest.get("assets", {})
                .get("splats", {})
                .get("semantics_metadata", {})
            )
            found = positive(semantics.get("metric_scale_factor"))
            if found is not None:
                return found
        except (OSError, json.JSONDecodeError):
            pass

    return 1.0


def read_orientation(world_dir: Path, index: str) -> tuple[bool, float | None]:
    """Which way up this world's point cloud is, from the same manifest.

    `flip_y` and `rotation_x` sit in the dict `read_metric_scale` already opens,
    and reading one without the others is how this file came to assume every
    world was a SHARP world. They are not: a Nerfstudio reconstruction records
    `flip_y: false` with `rotation_x: -pi/2`, and classifying it as if it were
    upside-down finds a wall where the floor should be.

    That mistake is quiet and it spreads. The floor height derived from it is
    written back into the manifest and into scene.json, so a single run leaves
    the world misaligned in files nobody thinks to check. On the splatfacto
    world here it turns a correct 0.617 into 2.547.

    Defaults are SHARP's, which is what every world without the fields is.
    """
    manifest_path = world_dir / f"{index}-world.json"
    if not manifest_path.is_file():
        return True, None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, None

    semantics = (
        manifest.get("assets", {}).get("splats", {}).get("semantics_metadata", {})
    )
    flip_y = semantics.get("flip_y")
    rotation_x = semantics.get("rotation_x")
    # `bool` is a subclass of `int`, so an angle has to exclude it explicitly or
    # `rotation_x: true` would quietly become one radian.
    usable_angle = isinstance(rotation_x, (int, float)) and not isinstance(rotation_x, bool)
    return (
        flip_y if isinstance(flip_y, bool) else True,
        float(rotation_x) if usable_angle else None,
    )


def oriented_for_classification(
    mesh: trimesh.Trimesh, flip_y: bool, rotation_x: float | None
) -> tuple[trimesh.Trimesh, bool]:
    """The mesh in viewer space, plus the flip that space still needs.

    Standing, walking and bumping into things are all decided by which way a
    face points, so they have to be measured where the viewer will see them. A
    rigid rotation leaves face indices alone, so groups taken from the result
    still address the original mesh -- which is what gets exported, because the
    viewer applies the rotation itself.

    Same shape as `splat_to_collider` in backend/server.py; keep them together.
    """
    if rotation_x is None:
        return mesh, flip_y
    oriented = mesh.copy()
    oriented.apply_transform(
        trimesh.transformations.rotation_matrix(rotation_x, [1, 0, 0])
    )
    return oriented, False


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

    # Read but deliberately not applied to the cloud. Scaling here would also
    # reinterpret `--voxel-size` and `--max-distance`, and on the 3.66x world in
    # this repo a 15 m cull discards half a 24 m scene and leaves the classifier
    # calling 1% of it floor. The collider, its ground offset and the exported
    # GLB all stay in world units, which is the convention already on disk and
    # the one `WorldCollider` scales from. Only the measurements that are
    # reported in metres convert -- see the walkable map below.
    metric_scale = read_metric_scale(world_dir, args.slug, index)
    flip_y, rotation_x = read_orientation(world_dir, index)

    mesh = build_collider(
        xyz,
        opacity,
        voxel_size=args.voxel_size,
        max_distance=args.max_distance,
        target_faces=args.target_faces,
    )

    size = mesh.bounds[1] - mesh.bounds[0]
    # World units, not metres -- the two only coincide at a scale of 1, and this
    # line claiming "m" is how the unit confusion stayed invisible for so long.
    metric = " (metres)" if metric_scale == 1.0 else f" = {size[0] * metric_scale:.2f} x {size[1] * metric_scale:.2f} x {size[2] * metric_scale:.2f} m"
    print(f"  room size: {size[0]:.2f} x {size[1]:.2f} x {size[2]:.2f} world units{metric} (X x height x Z)")

    # Both of these read which way a face points, so both need the mesh the
    # viewer will show rather than the one on disk.
    oriented, oriented_flip_y = oriented_for_classification(mesh, flip_y, rotation_x)
    if rotation_x is not None:
        print(f"  orientation: rotation_x={rotation_x:+.4f} (Nerfstudio Z-up), classifying in viewer space")

    floor_y = detect_floor_height(oriented, flip_y=oriented_flip_y)
    ground_offset = None if floor_y is None else -floor_y
    if ground_offset is None:
        print("  warning: no dominant floor surface found; leaving ground_plane_offset unchanged")
    else:
        print(f"  floor at y={-ground_offset:+.2f} (viewer space) -> ground_plane_offset = {ground_offset:+.3f}")

    groups = classify_surfaces(oriented, floor_y=floor_y, flip_y=oriented_flip_y)
    # By area, not face count: decimation leaves flat regions as a few large
    # triangles and creased regions as many small ones, so counting faces
    # reports the opposite of how much surface each class actually covers.
    areas = mesh.area_faces
    total_area = areas.sum()
    breakdown = ", ".join(
        f"{name} {areas[faces].sum() * 100 / total_area:.0f}%" for name, faces in groups.items()
    )
    print(f"  surfaces: {breakdown} of {total_area:.0f} m2")

    if args.dry_run:
        print("dry run; nothing written")
        return

    glb_path = world_dir / f"{index}-world.glb"
    labeled_collider_scene(mesh, groups).export(glb_path)
    print(f"wrote {glb_path.relative_to(REPO_ROOT)} ({glb_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # A map of where you could actually stand, which is a much smaller place
    # than the collision mesh suggests.
    try:
        from build_walkable import build_walkable_map

        scene_path_for_spawn = WORLDS_DIR / args.slug / "scene.json"
        spawn = None
        if scene_path_for_spawn.is_file():
            spawn_point = json.loads(scene_path_for_spawn.read_text(encoding="utf-8")).get("spawnPoint")
            if isinstance(spawn_point, list) and len(spawn_point) == 2:
                spawn = (float(spawn_point[0]), float(spawn_point[1]))

        # This is the one measurement that has to be metric. `CELL_SIZE` and
        # `CHARACTER_HEIGHT` are in metres, and `spawnPoint` is a viewer-space
        # position, so feeding the grid an unscaled mesh would ask for 5.9 m of
        # headroom on the 3.66x world here and report its area 13x over.
        # Scaling only the mesh leaves the collider itself untouched. It is the
        # oriented copy that gets scaled: the grid is a floor plan, so it has to
        # be measured the same way up the classification was.
        metric_mesh = oriented
        if metric_scale != 1.0:
            metric_mesh = oriented.copy()
            metric_mesh.apply_scale(metric_scale)
            print(f"  walkable map measured at {metric_scale:.4g} units per metre")

        png, walkable_meta = build_walkable_map(
            metric_mesh,
            groups,
            flip_y=oriented_flip_y,
            spawn_xz=spawn,
            metric_scale_factor=metric_scale,
        )
        (world_dir / f"{index}-world-walkable.png").write_bytes(png)
        (world_dir / f"{index}-world-walkable.json").write_text(
            json.dumps(walkable_meta, indent=2), encoding="utf-8"
        )
        unreachable = walkable_meta["standable_area"] - walkable_meta["walkable_area"]
        print(
            f"  walkable: {walkable_meta['walkable_area']:.1f} m2 reachable of "
            f"{walkable_meta['standable_area']:.1f} m2 standable ({unreachable:.1f} m2 stranded)"
        )
    except ImportError as error:
        print(f"  note: no walkable map ({error})")

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
