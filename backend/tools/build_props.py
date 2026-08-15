"""Cut a movable prop out of a reconstructed scene.

The room comes back from SHARP as one field of gaussians. Connected components
cannot separate the furniture from it -- measured across three worlds, a single
component holds 90-98% of the mesh, because a chair standing on the floor is
fused to the floor at voxel resolution. Splitting has to come from somewhere
else: a 2D mask (SAM, prompted by where the user clicked) or a 3D box the user
dragged.

    gaussians -> selection (mask or box) -> depth gate -> voxel occupancy
              -> marching cubes -> collision hull

Both selection routes converge on the same thing, a set of gaussian indices, so
everything after the first step is shared.

**The depth gate is the part that is easy to get wrong.** A mask covers
everything along the line of sight, so the wall behind a chair projects into
exactly the same pixels. Taking every gaussian under the mask would hand back a
"chair" with a slab of wall welded to it.
"""

from __future__ import annotations

import numpy as np
import trimesh
from scipy import ndimage
from skimage import measure

# A prop needs enough gaussians to voxelise into something with volume.
MIN_PROP_GAUSSIANS = 200
# Below this a marching-cubes shell is speckle, not an object.
MIN_PROP_FACES = 60
# Props are small, so they get a finer grid than the room's 0.05 m.
PROP_VOXEL_SIZE = 0.03


def project_to_pixels(
    xyz: np.ndarray,
    intrinsics: np.ndarray,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Project gaussian centres through the splat's own camera.

    Returns (u, v, valid) with pixel coordinates as int32 and a mask of which
    gaussians landed on the sensor at all. Mirrors what `locate_objects` in
    server.py does, which is where this trick came from: SHARP embeds the
    camera it reconstructed with, so every gaussian can be traced back to the
    pixel it came from.
    """
    fx, fy = intrinsics[0, 0], intrinsics[1, 1]
    cx, cy = intrinsics[0, 2], intrinsics[1, 2]

    depth = xyz[:, 2]
    valid = depth > 1e-6
    u = np.full(len(xyz), -1.0)
    v = np.full(len(xyz), -1.0)
    u[valid] = fx * xyz[valid, 0] / depth[valid] + cx
    v[valid] = fy * xyz[valid, 1] / depth[valid] + cy
    valid &= (u >= 0) & (u < width) & (v >= 0) & (v < height)

    return (
        np.clip(u, 0, width - 1).astype(np.int32),
        np.clip(v, 0, height - 1).astype(np.int32),
        valid,
    )


def nearest_surface_gate(
    depths: np.ndarray,
    *,
    bins: int = 60,
    min_bin_fraction: float = 0.005,
    min_gap_metres: float = 0.1,
) -> float:
    """Depth beyond which a masked gaussian belongs to the background.

    Cut at the first empty *stretch* of depth, not at the tallest peak. Two
    details, both of which were wrong in earlier versions and both of which a
    test now pins:

    - **Not the tallest peak.** The wall behind an object is always larger in
      pixel terms, so it routinely carries far more gaussians. On a
      500-gaussian object against a 9000-gaussian wall, a peak-relative
      threshold skipped the object and returned the wall. Empty space between
      two surfaces is empty whatever their relative size.
    - **The gap is measured in metres, not bins.** Bin width scales with the
      spread of the selection, so "three empty bins" means 15 cm on a scene
      that spans metres and 6 mm on a single surface — which chopped the tail
      off a lone gaussian peak. A real foreground/background split leaves a gap
      you could put a hand through; the sparse tail of one surface does not.

    Returns the cut depth; everything at or before it is the object. Surfaces
    that touch have no gap, and none is invented — the caller gets `inf` and
    keeps the whole selection rather than slicing an object in half.
    """
    if len(depths) == 0:
        return float("inf")

    histogram, edges = np.histogram(depths, bins=bins)
    # Smooth so a ragged surface does not read as several surfaces.
    smoothed = ndimage.uniform_filter1d(histogram.astype(np.float64), size=3)
    # A bin holds a surface if it carries a non-trivial share of the selection,
    # which keeps a few stray gaussians from bridging the gap.
    occupied = smoothed >= max(1.0, len(depths) * min_bin_fraction)
    if not occupied.any():
        return float("inf")

    bin_width = float(edges[1] - edges[0])
    needed = max(1, int(np.ceil(min_gap_metres / bin_width))) if bin_width > 0 else 1

    run = 0
    for i in range(int(np.argmax(occupied)) + 1, len(occupied)):
        run = 0 if occupied[i] else run + 1
        if run >= needed:
            return float(edges[i - run + 1])

    return float("inf")


def gaussians_under_mask(
    xyz: np.ndarray,
    intrinsics: np.ndarray,
    width: int,
    height: int,
    mask: np.ndarray,
    *,
    opacity: np.ndarray | None = None,
    opacity_threshold: float = 0.35,
) -> np.ndarray:
    """Indices of the gaussians a 2D mask selects, without the wall behind it."""
    u, v, valid = project_to_pixels(xyz, intrinsics, width, height)
    if opacity is not None:
        valid &= (1.0 / (1.0 + np.exp(-opacity))) >= opacity_threshold

    candidates = np.flatnonzero(valid & mask[v, u])
    if candidates.size == 0:
        return candidates

    depths = xyz[candidates, 2]
    return candidates[depths <= nearest_surface_gate(depths)]


def gaussians_in_box(
    xyz: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    opacity: np.ndarray | None = None,
    opacity_threshold: float = 0.35,
) -> np.ndarray:
    """Indices of the gaussians inside a 3D box, for the hand-drawn route.

    No depth gate: the user already said where the object ends in all three
    axes, which is the whole point of dragging a box instead of clicking.
    """
    inside = np.all((xyz >= lower) & (xyz <= upper), axis=1)
    if opacity is not None:
        inside &= (1.0 / (1.0 + np.exp(-opacity))) >= opacity_threshold
    return np.flatnonzero(inside)


def prop_collider(
    points: np.ndarray,
    *,
    voxel_size: float = PROP_VOXEL_SIZE,
    min_hits: int = 1,
) -> trimesh.Trimesh:
    """Turn a prop's gaussian centres into a closed-ish collision shell.

    Same voxel-then-marching-cubes route the room collider takes, at a finer
    grid because props are small. The viewer wraps the result in a convex hull
    for physics, so small holes here do not matter -- the silhouette does.
    """
    if len(points) < MIN_PROP_GAUSSIANS:
        raise ValueError(f"need at least {MIN_PROP_GAUSSIANS} gaussians, got {len(points)}")

    origin = points.min(axis=0) - voxel_size * 2
    dims = np.maximum(
        np.ceil((points.max(axis=0) + voxel_size * 2 - origin) / voxel_size).astype(int), 3
    )
    if dims.prod() > 20_000_000:
        raise ValueError(f"prop voxel volume too large ({dims.prod():,}); raise voxel_size")

    index = np.clip(np.floor((points - origin) / voxel_size).astype(np.int32), 0, dims - 1)
    flat = (index[:, 0] * dims[1] + index[:, 1]) * dims[2] + index[:, 2]
    occupancy = np.bincount(flat, minlength=int(dims.prod())).reshape(dims) >= min_hits
    occupancy = ndimage.binary_closing(occupancy, iterations=1)

    padded = np.pad(occupancy.astype(np.float32), 1, mode="constant", constant_values=0)
    vertices, faces, _, _ = measure.marching_cubes(padded, level=0.5)
    vertices = (vertices - 1.0) * voxel_size + origin

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    if len(mesh.faces) < MIN_PROP_FACES:
        raise ValueError(f"prop shell has only {len(mesh.faces)} faces; too small to collide with")

    # Keep the largest piece: a mask edge often catches a few stray gaussians
    # that voxelise into their own islands.
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:
        mesh = max(parts, key=lambda part: len(part.faces))

    return mesh


def merge_props(existing: bytes | None, mesh: trimesh.Trimesh, prop_id: str) -> bytes:
    """Add one prop to a world's props GLB and return the whole file back.

    Props are carved out one at a time but travel as a single GLB, one node per
    prop, matched to the sidecar JSON by node name. Merging here rather than in
    the Next.js route keeps GLB parsing on the side that already has trimesh --
    the route only ever reads and writes bytes.

    Re-using an existing id replaces that prop, which is what re-cutting one
    the user was not happy with should do.
    """
    geometries: dict[str, trimesh.Trimesh] = {}
    if existing:
        import io

        loaded = trimesh.load(io.BytesIO(existing), file_type="glb", process=False)
        if isinstance(loaded, trimesh.Scene):
            for name, geometry in loaded.geometry.items():
                if isinstance(geometry, trimesh.Trimesh):
                    geometries[name] = geometry
        elif isinstance(loaded, trimesh.Trimesh):
            geometries["prop-0"] = loaded

    geometries[prop_id] = mesh
    return trimesh.Scene(geometries).export(file_type="glb")


def prop_descriptor(
    mesh: trimesh.Trimesh,
    *,
    density: float = 250.0,
    metric_scale: float = 1.0,
) -> dict:
    """Physics metadata the viewer needs to make the prop behave.

    Mass comes from the hull volume at a furniture-ish density (chipboard and
    upholstery land near 250 kg/m3). It only has to be plausible: too light and
    a nudge launches the thing across the room, too heavy and it will not move.

    `mesh` is in the world's own reconstruction units, the same ones the props
    GLB is exported in, so `metric_scale` converts the volume to cubic metres
    before the density is applied. Cubing means the error is not subtle: at the
    3.66 of a real splatfacto world an ungoverned chair comes out 49x too heavy.

    `centre` and `bounds` stay in the mesh's own units on purpose — they are
    read alongside the GLB, which the viewer scales itself.
    """
    hull = mesh.convex_hull
    volume = float(abs(hull.volume)) * (float(metric_scale) ** 3)
    lower, upper = mesh.bounds
    centre = hull.center_mass if volume > 0 else mesh.centroid

    return {
        "mass": round(max(0.5, volume * density), 3),
        "volume": round(volume, 5),
        "centre": [round(float(v), 4) for v in centre],
        "bounds": {
            "min": [round(float(v), 4) for v in lower],
            "max": [round(float(v), 4) for v in upper],
        },
        "faces": int(len(mesh.faces)),
    }
