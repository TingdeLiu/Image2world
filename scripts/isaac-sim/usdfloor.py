"""Where the floor actually is under a point, read from the collider's `floor` mesh.

`navigate.py` and `walk_robot.py` both drop a robot from a fixed height above
"the floor", and both used to take that floor from the floor part's bounding
box minimum. That is the ground under the robot only when the floor is flat --
marble-office's is, to 4 mm. A 70 m scanned storey is not: `dinghao-2f`'s floor
part spans 0.37 m, so Spot was dropped from 0.59 m above the real floor rather
than 0.80. Its feet started inside the triangles, a zero-thickness surface
pushes a foot whose centre is already through it further through, and the robot
was lying on its belly 1.5 s in. Measured: the same start with the spawn taken
from here walked 6.86 m to its goal at 3.4 cm mean off the line.

Pure USD and numpy, no physics, so it runs before the timeline plays -- which is
when the policy robots have to be built.
"""
from __future__ import annotations

import numpy as np


def floor_under(prim, x, y):
    """Top of the floor mesh at world (x, y), or None where it has no triangle."""
    from pxr import Usd, UsdGeom

    mesh = UsdGeom.Mesh(prim)
    points = np.asarray(mesh.GetPointsAttr().Get(), dtype=np.float64)
    counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get())
    if points.size == 0 or not np.all(counts == 3):
        return None
    to_world = np.asarray(
        UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default()),
        dtype=np.float64)
    points = points @ to_world[:3, :3] + to_world[3, :3]   # USD matrices are row-vector
    tri = points[np.asarray(mesh.GetFaceVertexIndicesAttr().Get()).reshape(-1, 3)]
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    v0, v1 = (c - a)[:, :2], (b - a)[:, :2]
    v2 = np.array([x, y]) - a[:, :2]
    d00, d01, d11 = (v0 * v0).sum(1), (v0 * v1).sum(1), (v1 * v1).sum(1)
    d20, d21 = (v2 * v0).sum(1), (v2 * v1).sum(1)
    den = d00 * d11 - d01 * d01
    ok = np.abs(den) > 1e-12
    den = np.where(ok, den, 1.0)
    u, v = (d11 * d20 - d01 * d21) / den, (d00 * d21 - d01 * d20) / den
    inside = ok & (u >= 0) & (v >= 0) & (u + v <= 1)
    if not inside.any():
        return None
    z = a[inside, 2] + u[inside] * (c - a)[inside, 2] + v[inside] * (b - a)[inside, 2]
    return float(z.max())


def spawn_floor(stage, floor_path, x, y, bounds_cache):
    """The floor height to drop a robot onto at (x, y), and a note if it had to guess.

    Falls back to the floor part's lowest point where the part has no triangle
    under (x, y) -- a spawn over a hole -- and to 0 with no floor part at all.
    """
    prim = stage.GetPrimAtPath(floor_path)
    if not (prim and prim.IsValid()):
        return 0.0, "no floor part; assuming z=0"
    z = floor_under(prim, x, y)
    if z is not None:
        return z, None
    low = float(bounds_cache.ComputeWorldBound(prim).ComputeAlignedRange().GetMin()[2])
    return low, (f"no floor triangle under ({x:.2f}, {y:.2f}); using the floor "
                 f"part's lowest point, z={low:.3f}")
