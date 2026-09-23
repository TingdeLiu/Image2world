"""Work out where a person could actually stand, and draw it as a map.

SHARP reconstructs only what one camera saw, so a room's collision mesh is full
of places that look solid but are not reachable: ledges with no route to them,
slivers of floor behind a sofa, and — the one that actually hurts — the edge of
the visible region, past which a walker steps into nothing.

    floor + elevated faces -> XZ grid -> headroom -> flood fill from spawn
                           -> walkable mask (PNG) + metadata (JSON)

Three filters, and the third is the one that matters:

1. **Standable.** Only faces the classifier called `floor` or `elevated`, which
   are already limited to slopes a capsule can hold on.
2. **Headroom.** A surface under a desk is standable and useless.
3. **Reachable.** A flood fill from the spawn point. Without it the map shows
   every disconnected scrap the reconstruction left lying around as though you
   could walk there, which is worse than showing nothing — it is a map that
   lies in exactly the situation it exists to warn about.
"""

from __future__ import annotations

import numpy as np
import trimesh
from scipy import ndimage

# Kept in step with `build_collider.SURFACE_CLASSES` and `surfaces.ts`; imported
# from neither, so this module stays loadable without the heavier collider deps.
SURFACE_CLASSES = ("floor", "elevated", "ramp", "wall", "ceiling")

# Fine enough to show a doorway, coarse enough to stay a small PNG.
CELL_SIZE = 0.15
# From src/modules/character/spawn.ts. A cell you cannot stand upright in is
# not walkable, whatever the floor under it looks like.
CHARACTER_HEIGHT = 1.6
# How far above a cell's floor something has to be before it counts as overhead
# rather than as part of the floor itself.
#
# This was 0.1 m, which is smaller than a voxel on a world whose metric scale is
# above 2 -- `--voxel-size` is in world units, so marble-office's 0.05 is 10.5 cm
# on the ground. The floor's own marching-cubes stepping then read as a ceiling
# 10 cm up and failed the headroom test: 94% of that world's cells were culled
# and its map claimed 3.9 m2 of a 93.5 m2 room. Measured across four worlds,
# 0.20 recovers nearly all of it (marble-office 3.9 -> 76.3 m2, room1
# 18.0 -> 24.5) and 0.30 adds only a few percent more, which is the point where
# a real low obstacle would start being ignored.
FLOOR_SKIRT = 0.20
# Reconstruction noise leaves single-cell islands and pinholes; a real doorway
# is several cells wide and survives this.
DESPECKLE_ITERATIONS = 1


def rasterize_floor(
    triangles: np.ndarray,
    origin: np.ndarray,
    shape: tuple[int, int],
    cell_size: float,
) -> np.ndarray:
    """Lowest supported height at each cell centre, without random floor holes.

    Project each actual floor triangle into the grid and interpolate its height.
    This also handles the two large triangles left by decimating a flat floor.
    No closing or interpolation between triangles is used: real gaps stay empty.
    """
    heights = np.full(shape, np.inf)
    for triangle in triangles:
        planar = (triangle[:, [0, 2]] - origin) / cell_size
        a, b, c = planar
        ab, ac = b - a, c - a
        determinant = ab[0] * ac[1] - ab[1] * ac[0]
        if abs(determinant) < 1e-12:
            continue  # A vertical/degenerate face cannot support a person.
        low = np.maximum(np.ceil(planar.min(axis=0) - 0.5).astype(int), 0)
        high = np.minimum(np.floor(planar.max(axis=0) - 0.5).astype(int), [shape[1] - 1, shape[0] - 1])
        if (low > high).any():
            continue
        # Bound scratch arrays even for very large, decimated floor triangles.
        for row_start in range(low[1], high[1] + 1, 64):
            rows, cols = np.mgrid[row_start:min(row_start + 64, high[1] + 1), low[0]:high[0] + 1]
            dx, dz = cols + 0.5 - a[0], rows + 0.5 - a[1]
            u = (dx * ac[1] - dz * ac[0]) / determinant
            v = (ab[0] * dz - ab[1] * dx) / determinant
            inside = (u >= -1e-10) & (v >= -1e-10) & (u + v <= 1.0 + 1e-10)
            values = triangle[0, 1] + u[inside] * (triangle[1, 1] - triangle[0, 1]) + v[inside] * (triangle[2, 1] - triangle[0, 1])
            y, x = rows[inside], cols[inside]
            heights[y, x] = np.minimum(heights[y, x], values)
    heights[~np.isfinite(heights)] = np.nan
    return heights


def walkable_grid(
    mesh: trimesh.Trimesh,
    standable_faces: np.ndarray,
    *,
    flip_y: bool = True,
    cell_size: float = CELL_SIZE,
    headroom: float = CHARACTER_HEIGHT,
    floor_skirt: float = FLOOR_SKIRT,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Grid the standable surface, returning (walkable, floor_height, meta).

    Works in viewer space. `walkable` is a bool array indexed [row, col] over
    (z, x); `floor_height` carries the standing height per cell, NaN where
    there is nothing to stand on.
    """
    sign = -1.0 if flip_y else 1.0

    def viewer_samples(faces: np.ndarray | None) -> np.ndarray:
        """Points spread over the faces' surface, in viewer space.

        Face centres will not do. Quadric decimation leaves flat regions as a
        few large triangles -- on a real room the flattest 4% of faces carry
        38% of the area -- and one centre per triangle drops most of the floor
        on the ground. Sampling by area keeps coverage proportional to size.
        """
        surface = mesh if faces is None else mesh.submesh([faces], append=True)
        if surface.area <= 0:
            return np.empty((0, 3))
        # Bounded headroom sampling; thin obstacles can still be missed, so
        # this is a minimap precheck, not a substitute for simulator collision.
        count = int(np.clip(np.ceil(surface.area / (cell_size * cell_size) * 4), 1_000, 400_000))
        # Headroom remains an area-sampled approximation. A local fixed seed
        # makes it repeatable without changing any caller's global RNG state.
        points = np.asarray(trimesh.sample.sample_surface(surface, count, seed=0)[0], dtype=np.float64)
        points[:, 1] *= sign
        points[:, 2] *= sign
        return points

    if not np.isfinite(cell_size) or cell_size <= 0:
        raise ValueError("cell_size must be finite and positive")
    vertices = np.array(mesh.vertices, dtype=np.float64, copy=True)
    if not len(vertices) or not np.isfinite(vertices).all():
        raise ValueError("collider must have finite vertices")
    vertices[:, 1:] *= sign
    # Sampling must never decide map placement or move a spawn between cells.
    lower = vertices.min(axis=0)
    upper = vertices.max(axis=0)
    cols = max(1, int(np.ceil((upper[0] - lower[0]) / cell_size)))
    rows = max(1, int(np.ceil((upper[2] - lower[2]) / cell_size)))
    if rows * cols > 4_000_000:
        raise ValueError("walkable grid too large; check reconstruction scale or crop distant geometry")
    all_points = viewer_samples(None)

    def cell_of(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        col = np.clip(((points[:, 0] - lower[0]) / cell_size).astype(np.int32), 0, cols - 1)
        row = np.clip(((points[:, 2] - lower[2]) / cell_size).astype(np.int32), 0, rows - 1)
        return row, col

    # Standing height per cell: the *lowest* standable surface, so a desktop
    # does not hide the floor it stands on.
    floor_height = rasterize_floor(
        vertices[mesh.faces[standable_faces]], lower[[0, 2]], (rows, cols), cell_size,
    )

    # Headroom: the lowest thing overhead that is not the floor itself. The
    # skirt keeps a surface from reading as its own ceiling -- see FLOOR_SKIRT
    # for why it has to clear a voxel of the collider, not just a centimetre.
    ceiling = np.full((rows, cols), np.inf)
    row, col = cell_of(all_points)
    base = floor_height[row, col]
    overhead = np.isfinite(base) & (all_points[:, 1] > base + floor_skirt)
    np.minimum.at(ceiling, (row[overhead], col[overhead]), all_points[overhead, 1])

    standable = ~np.isnan(floor_height)
    clearance = ceiling - np.where(standable, floor_height, 0.0)
    walkable = standable & (clearance >= headroom)

    meta = {
        "cell_size": cell_size,
        "cols": cols,
        "rows": rows,
        # Viewer-space world position of cell [0, 0]'s corner.
        "origin": [float(lower[0]), float(lower[2])],
    }
    return walkable, floor_height, meta


def reachable_from(
    walkable: np.ndarray,
    start: tuple[int, int] | None,
    *,
    despeckle: int = DESPECKLE_ITERATIONS,
) -> np.ndarray:
    """Keep only the region connected to `start`.

    A map that shows unreachable scraps as walkable is worse than no map: it
    invites exactly the walk-into-the-void it exists to prevent. With no usable
    start the largest region is the best guess at "the room".
    """
    cleaned = walkable
    if despeckle > 0:
        # Close pinholes, then drop specks. Order matters: opening first would
        # sever narrow doorways before closing could reinforce them.
        cleaned = ndimage.binary_closing(cleaned, iterations=despeckle)
        cleaned = ndimage.binary_opening(cleaned, iterations=despeckle)

    labels, count = ndimage.label(cleaned)
    if count == 0:
        return np.zeros_like(walkable, dtype=bool)

    def region(label: int) -> np.ndarray:
        # Intersect with the input: closing bridges noise holes so the flood
        # fill can cross them, but a hole is not somewhere you can stand, and
        # letting it through would report more walkable area than there is
        # standable area -- which is how this bug announced itself.
        return (labels == label) & walkable

    if start is not None:
        row, col = start
        if 0 <= row < labels.shape[0] and 0 <= col < labels.shape[1] and labels[row, col]:
            return region(labels[row, col])
        # The spawn can land a cell off the mesh; take the nearest labelled cell
        # rather than silently falling back to "the biggest blob somewhere".
        occupied = np.argwhere(labels > 0)
        if len(occupied):
            nearest = occupied[np.argmin(np.sum((occupied - np.array([row, col])) ** 2, axis=1))]
            return region(labels[nearest[0], nearest[1]])

    sizes = ndimage.sum(cleaned, labels, range(1, count + 1))
    return region(int(np.argmax(sizes)) + 1)


def walkable_png(walkable: np.ndarray) -> bytes:
    """Encode the mask as a 1-bit-looking greyscale PNG (white = walkable)."""
    from io import BytesIO

    from PIL import Image

    image = Image.fromarray(np.where(walkable, 255, 0).astype(np.uint8), mode="L")
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def collider_groups(scene) -> tuple[trimesh.Trimesh, dict[str, np.ndarray]]:
    """Rebuild (mesh, face groups) from a collider GLB's per-class nodes.

    The surface split is already in the file — `build_collider` writes one node
    per class — so a walkable map can be derived from the GLB alone, without
    voxelising a point cloud again. That matters because the metric scale of a
    video-reconstructed world is calculated *from* the collider's floor height:
    the map has to be built after the collider, not during it.

    Older colliders are a single unnamed mesh. They come back with no groups,
    and the caller decides what to do about that -- there is no way to guess
    which faces were floor.
    """
    meshes: list[trimesh.Trimesh] = []
    groups: dict[str, np.ndarray] = {}
    offset = 0

    for name, geometry in scene.geometry.items():
        if not isinstance(geometry, trimesh.Trimesh):
            continue
        meshes.append(geometry)
        if name in SURFACE_CLASSES:
            groups[name] = np.arange(offset, offset + len(geometry.faces))
        offset += len(geometry.faces)

    if not meshes:
        raise ValueError("collider GLB contains no triangle meshes")

    mesh = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
    return mesh, groups


def build_walkable_map(
    mesh: trimesh.Trimesh,
    groups: dict[str, np.ndarray],
    *,
    flip_y: bool = True,
    spawn_xz: tuple[float, float] | None = None,
    cell_size: float = CELL_SIZE,
    metric_scale_factor: float = 1.0,
) -> tuple[bytes, dict]:
    """Full pipeline: classified mesh in, PNG mask and metadata out.

    `mesh` and `spawn_xz` must already be in metres — `CELL_SIZE` and
    `CHARACTER_HEIGHT` are metric, and a grid measured in reconstruction units
    would demand a 5.9 m ceiling on a world whose scale factor is 3.66.

    `metric_scale_factor` is not applied here; it is recorded so the viewer can
    map a player position back onto the grid. The two only differ if the world
    is rescaled after the map is built.
    """
    standable = np.concatenate([
        groups.get("floor", np.array([], dtype=int)),
        groups.get("elevated", np.array([], dtype=int)),
    ]).astype(int)

    walkable, _, meta = walkable_grid(mesh, standable, flip_y=flip_y, cell_size=cell_size)

    start = None
    if spawn_xz is not None:
        start = (
            int((spawn_xz[1] - meta["origin"][1]) / cell_size),
            int((spawn_xz[0] - meta["origin"][0]) / cell_size),
        )
    reachable = reachable_from(walkable, start)

    cell_area = cell_size * cell_size
    meta.update({
        "version": 1,
        # The scale the grid was measured at. The viewer divides the live scale
        # by this to place the player; a file without it predates the metric
        # fix and was measured at 1.0, which is what the fallback assumes.
        "metric_scale_factor": round(float(metric_scale_factor), 6),
        "standable_area": round(float(walkable.sum()) * cell_area, 2),
        "walkable_area": round(float(reachable.sum()) * cell_area, 2),
    })
    return walkable_png(reachable), meta


def main() -> None:
    """Refresh one world's walkable map from its collider GLB.

    The collider already carries the surface split, so this needs neither the
    point cloud nor another voxelisation. That is what makes a change to
    `FLOOR_SKIRT` or `CELL_SIZE` cheap to roll out: every existing world's map
    can be rebuilt without touching the collision mesh it was derived from.

    Usage:
        python backend/tools/build_walkable.py <world-slug> [--dry-run]
    """
    import argparse
    import json
    import sys
    from pathlib import Path

    # Imported here, not at module scope: the collider pulls in scikit-image,
    # and the note at the top of this file is that the map stays importable
    # without it.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build_collider import (
        WORLDS_DIR,
        oriented_for_classification,
        read_metric_scale,
        read_orientation,
    )

    parser = argparse.ArgumentParser(description="Rebuild a world's walkable map from its collider GLB")
    parser.add_argument("slug", help="World slug under public/worlds/")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = parser.parse_args()

    world_dir = WORLDS_DIR / args.slug / "output" / "world"
    glbs = sorted(world_dir.glob("*-world.glb"))
    if not glbs:
        print(f"error: no <n>-world.glb in {world_dir}", file=sys.stderr)
        raise SystemExit(1)
    glb_path = glbs[-1]
    index = glb_path.name.split("-", 1)[0]

    mesh, groups = collider_groups(trimesh.load(glb_path))
    if not groups:
        print(
            f"error: {glb_path.name} has no per-class nodes, so there is no way to tell "
            "which faces were floor. Rebuild it with build_collider.py first.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    metric_scale = read_metric_scale(world_dir, args.slug, index)
    flip_y, rotation_x = read_orientation(world_dir, index)
    oriented, oriented_flip_y = oriented_for_classification(mesh, flip_y, rotation_x)

    spawn = None
    scene_path = WORLDS_DIR / args.slug / "scene.json"
    if scene_path.is_file():
        spawn_point = json.loads(scene_path.read_text(encoding="utf-8")).get("spawnPoint")
        if isinstance(spawn_point, list) and len(spawn_point) == 2:
            spawn = (float(spawn_point[0]), float(spawn_point[1]))

    metric_mesh = oriented
    if metric_scale != 1.0:
        metric_mesh = oriented.copy()
        metric_mesh.apply_scale(metric_scale)

    png, meta = build_walkable_map(
        metric_mesh, groups, flip_y=oriented_flip_y, spawn_xz=spawn,
        metric_scale_factor=metric_scale,
    )
    stranded = meta["standable_area"] - meta["walkable_area"]
    print(
        f"{args.slug}: {meta['walkable_area']:.1f} m2 reachable of "
        f"{meta['standable_area']:.1f} m2 standable ({stranded:.1f} m2 stranded) "
        f"at {metric_scale:.4g} units/m"
    )
    if args.dry_run:
        print("dry run; nothing written")
        return

    (world_dir / f"{index}-world-walkable.png").write_bytes(png)
    (world_dir / f"{index}-world-walkable.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(f"wrote {index}-world-walkable.png / .json")


if __name__ == "__main__":
    main()
