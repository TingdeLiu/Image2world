"""Tests for the walkable map.

The one that matters is reachability. A map that paints unreachable scraps as
walkable is worse than no map at all — it invites the exact walk-into-the-void
it exists to prevent.
"""

import numpy as np
import pytest
import trimesh

from backend.tools.build_walkable import (
    build_walkable_map,
    reachable_from,
    walkable_grid,
    walkable_png,
)

CELL = 0.15


def slab(size_x: float, size_z: float, height: float, offset_x: float = 0.0, offset_z: float = 0.0):
    """A thin standable slab centred at (offset_x, height, offset_z), Y-up."""
    box = trimesh.creation.box(extents=(size_x, 0.05, size_z))
    box.apply_translation((offset_x, height, offset_z))
    return box


def standable_faces(mesh: trimesh.Trimesh) -> np.ndarray:
    """Indices of upward-facing faces, standing in for the classifier."""
    return np.flatnonzero(mesh.face_normals[:, 1] >= 0.7)


@pytest.fixture
def room() -> trimesh.Trimesh:
    return slab(4.0, 4.0, 0.0)


def test_grid_covers_the_floor(room: trimesh.Trimesh):
    walkable, floor_height, meta = walkable_grid(room, standable_faces(room), flip_y=False)

    assert meta["cols"] >= int(4.0 / CELL) - 1
    assert walkable.any()
    # A bare slab has nothing overhead, so all of it should be standable.
    assert np.isfinite(floor_height[walkable]).all()
    assert np.allclose(floor_height[walkable], 0.025, atol=0.05)


def test_a_low_ceiling_makes_a_floor_unwalkable():
    # A slab you cannot stand upright under is not walkable, however solid.
    floor = slab(4.0, 4.0, 0.0)
    lid = slab(4.0, 4.0, 1.0)          # 1.0 m < CHARACTER_HEIGHT
    room = trimesh.util.concatenate([floor, lid])

    walkable, _, _ = walkable_grid(room, standable_faces(room), flip_y=False)
    # The lid's own top surface is standable; the floor beneath it is not.
    assert walkable.sum() < (4.0 / CELL) ** 2 * 0.5


def test_high_ceiling_leaves_the_floor_walkable():
    floor = slab(4.0, 4.0, 0.0)
    lid = slab(4.0, 4.0, 3.0)
    room = trimesh.util.concatenate([floor, lid])

    walkable, _, _ = walkable_grid(room, standable_faces(room), flip_y=False)
    assert walkable.any()


def test_reachability_drops_an_island():
    # Two disconnected regions: the one holding the player, and a ledge across
    # the room that looks walkable but has no route to it.
    grid = np.zeros((40, 40), dtype=bool)
    grid[2:18, 2:18] = True     # the room
    grid[26:38, 26:38] = True   # an unreachable ledge

    kept = reachable_from(grid, start=(10, 10))
    assert kept[10, 10]
    assert not kept[30, 30], "an unreachable ledge must not be painted as walkable"


def test_reachability_falls_back_to_the_largest_region():
    grid = np.zeros((40, 40), dtype=bool)
    grid[2:20, 2:20] = True
    grid[30:34, 30:34] = True

    kept = reachable_from(grid, start=None)
    assert kept[10, 10]
    assert not kept[32, 32]


def test_a_spawn_off_the_mesh_snaps_to_the_nearest_region():
    # Spawn points come from a collider bounding box and can land just outside
    # the walkable cells; falling back to "biggest blob" would be wrong when the
    # player is standing next to a smaller one.
    grid = np.zeros((40, 40), dtype=bool)
    grid[2:30, 2:30] = True     # large region, far away
    grid[35:39, 35:39] = True   # small region, right next to the spawn

    kept = reachable_from(grid, start=(37, 33))
    assert kept[37, 37], "should snap to the region beside the spawn"
    assert not kept[10, 10]


def test_reachability_survives_a_narrow_doorway():
    # Despeckling must not sever a real doorway; opening before closing would.
    grid = np.zeros((30, 40), dtype=bool)
    grid[5:25, 4:18] = True
    grid[5:25, 22:36] = True
    grid[14:16, 18:22] = True   # a two-cell corridor joining them

    kept = reachable_from(grid, start=(15, 10))
    assert kept[15, 30], "the far room should still be reachable through the doorway"


def test_empty_input_yields_an_empty_map():
    assert not reachable_from(np.zeros((10, 10), dtype=bool), start=(5, 5)).any()


def test_reachable_is_never_larger_than_walkable():
    # Despeckling closes pinholes so the flood fill can cross them, but a hole
    # is not somewhere you can stand. Letting it through reported *more*
    # walkable area than standable area on a real world.
    rng = np.random.default_rng(3)
    grid = np.zeros((40, 40), dtype=bool)
    grid[5:35, 5:35] = True
    grid[rng.integers(5, 35, 60), rng.integers(5, 35, 60)] = False  # noise holes

    kept = reachable_from(grid, start=(20, 20))
    assert kept.sum() <= grid.sum()
    assert not (kept & ~grid).any(), 'a filled hole must not become walkable'


def test_png_round_trips_the_mask():
    from io import BytesIO

    from PIL import Image

    grid = np.zeros((8, 12), dtype=bool)
    grid[2:6, 3:9] = True
    decoded = np.array(Image.open(BytesIO(walkable_png(grid)))) > 127
    assert decoded.shape == grid.shape
    assert np.array_equal(decoded, grid)


def test_end_to_end_reports_area_in_square_metres(room: trimesh.Trimesh):
    groups = {"floor": standable_faces(room), "elevated": np.array([], dtype=int)}
    png, meta = build_walkable_map(room, groups, flip_y=False, spawn_xz=(0.0, 0.0))

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # A 4x4 m slab, minus grid quantisation at the edges.
    assert 12.0 < meta["walkable_area"] <= 16.5
    assert meta["walkable_area"] <= meta["standable_area"]
    assert meta["cell_size"] == pytest.approx(CELL)
