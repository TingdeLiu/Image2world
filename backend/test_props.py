"""Tests for cutting a movable prop out of a reconstructed scene.

The one that matters is the depth gate. A 2D mask selects everything along the
line of sight, so the wall behind a chair falls inside exactly the same pixels;
without the gate the extracted "chair" comes back with a slab of wall attached.
"""

import numpy as np
import pytest

from backend.tools.build_props import (
    MIN_PROP_GAUSSIANS,
    gaussians_in_box,
    gaussians_under_mask,
    nearest_surface_gate,
    project_to_pixels,
    prop_collider,
    prop_descriptor,
)

WIDTH, HEIGHT = 640, 480
INTRINSICS = np.array([[500.0, 0.0, 320.0], [0.0, 500.0, 240.0], [0.0, 0.0, 1.0]])

OBJECT_DEPTH = 2.0
WALL_DEPTH = 5.0


def plane_points(depth: float, half_extent: float, count: int, seed: int) -> np.ndarray:
    """A frontoparallel patch of gaussians at a fixed depth, centred on the axis."""
    rng = np.random.default_rng(seed)
    xy = rng.uniform(-half_extent, half_extent, size=(count, 2))
    z = np.full((count, 1), depth) + rng.normal(0, 0.01, size=(count, 1))
    return np.hstack([xy, z]).astype(np.float32)


@pytest.fixture
def scene() -> tuple[np.ndarray, np.ndarray]:
    """An object floating in front of a much larger wall, plus their masks."""
    obj = plane_points(OBJECT_DEPTH, 0.25, 3000, seed=1)
    wall = plane_points(WALL_DEPTH, 3.0, 8000, seed=2)
    xyz = np.vstack([obj, wall])

    # The mask covers the object's silhouette -- and therefore the patch of wall
    # directly behind it, which is the whole problem.
    u, v, valid = project_to_pixels(obj, INTRINSICS, WIDTH, HEIGHT)
    mask = np.zeros((HEIGHT, WIDTH), dtype=bool)
    mask[v[valid].min(): v[valid].max() + 1, u[valid].min(): u[valid].max() + 1] = True
    return xyz, mask


def test_projection_puts_a_centred_point_at_the_principal_point():
    xyz = np.array([[0.0, 0.0, 2.0]], dtype=np.float32)
    u, v, valid = project_to_pixels(xyz, INTRINSICS, WIDTH, HEIGHT)
    assert valid[0]
    assert (u[0], v[0]) == (320, 240)


def test_projection_rejects_points_behind_and_outside_the_camera():
    xyz = np.array([
        [0.0, 0.0, -1.0],   # behind
        [0.0, 0.0, 0.0],    # on the plane
        [10.0, 0.0, 2.0],   # off the right edge
    ], dtype=np.float32)
    _, _, valid = project_to_pixels(xyz, INTRINSICS, WIDTH, HEIGHT)
    assert not valid.any()


def test_gate_cuts_between_the_object_and_the_wall():
    depths = np.concatenate([
        np.random.default_rng(3).normal(OBJECT_DEPTH, 0.02, 1500),
        np.random.default_rng(4).normal(WALL_DEPTH, 0.05, 4000),
    ])
    cut = nearest_surface_gate(depths)
    assert OBJECT_DEPTH < cut < WALL_DEPTH


def test_gate_keeps_everything_when_there_is_only_one_surface():
    depths = np.random.default_rng(5).normal(OBJECT_DEPTH, 0.02, 2000)
    assert nearest_surface_gate(depths) >= depths.max()


def test_gate_survives_an_empty_selection():
    assert nearest_surface_gate(np.array([])) == float("inf")


def test_gate_prefers_the_near_surface_even_when_the_far_one_is_denser():
    # The wall is always bigger than the object in pixel terms, so the far peak
    # routinely dominates -- here by 18:1. An earlier version ranked peaks by
    # height and selected the wall; this is the case that caught it.
    depths = np.concatenate([
        np.random.default_rng(6).normal(OBJECT_DEPTH, 0.02, 500),
        np.random.default_rng(7).normal(WALL_DEPTH, 0.05, 9000),
    ])
    assert nearest_surface_gate(depths) < WALL_DEPTH


def test_gate_keeps_touching_surfaces_together():
    # An object flush against a wall has no gap to cut at. Inventing one would
    # slice the object in half, so the whole selection is kept instead.
    depths = np.random.default_rng(11).uniform(OBJECT_DEPTH, WALL_DEPTH, 6000)
    assert nearest_surface_gate(depths) == float("inf")


def test_mask_selection_leaves_the_wall_behind(scene):
    xyz, mask = scene
    picked = gaussians_under_mask(xyz, INTRINSICS, WIDTH, HEIGHT, mask)

    assert len(picked) > MIN_PROP_GAUSSIANS
    depths = xyz[picked, 2]
    assert depths.max() < (OBJECT_DEPTH + WALL_DEPTH) / 2, "wall gaussians leaked into the prop"
    assert np.allclose(depths, OBJECT_DEPTH, atol=0.1)


def test_mask_selection_honours_opacity(scene):
    xyz, mask = scene
    # Logit opacity: strongly negative is transparent, so nothing should pass.
    transparent = np.full(len(xyz), -10.0, dtype=np.float32)
    assert len(gaussians_under_mask(xyz, INTRINSICS, WIDTH, HEIGHT, mask, opacity=transparent)) == 0


def test_empty_mask_selects_nothing(scene):
    xyz, _ = scene
    empty = np.zeros((HEIGHT, WIDTH), dtype=bool)
    assert len(gaussians_under_mask(xyz, INTRINSICS, WIDTH, HEIGHT, empty)) == 0


def test_box_selection_takes_exactly_what_is_inside(scene):
    xyz, _ = scene
    lower = np.array([-0.3, -0.3, OBJECT_DEPTH - 0.2])
    upper = np.array([0.3, 0.3, OBJECT_DEPTH + 0.2])
    picked = gaussians_in_box(xyz, lower, upper)

    assert len(picked) > MIN_PROP_GAUSSIANS
    assert np.all(xyz[picked] >= lower) and np.all(xyz[picked] <= upper)


def test_collider_wraps_the_points_it_was_given():
    rng = np.random.default_rng(8)
    # A solid-ish blob, so voxelisation has something to close over.
    points = rng.uniform(-0.2, 0.2, size=(4000, 3)).astype(np.float32)
    mesh = prop_collider(points)

    lower, upper = mesh.bounds
    assert np.all(lower <= points.min(axis=0) + 1e-6)
    assert np.all(upper >= points.max(axis=0) - 1e-6)
    assert len(mesh.faces) > 0


def test_collider_refuses_a_selection_too_small_to_be_an_object():
    with pytest.raises(ValueError, match="at least"):
        prop_collider(np.zeros((10, 3), dtype=np.float32))


def test_descriptor_reports_a_plausible_mass():
    rng = np.random.default_rng(9)
    points = rng.uniform(-0.25, 0.25, size=(5000, 3)).astype(np.float32)
    descriptor = prop_descriptor(prop_collider(points))

    # A 0.5 m cube of furniture-density material is on the order of 30 kg.
    assert 1.0 < descriptor["mass"] < 200.0
    assert descriptor["volume"] > 0
    assert len(descriptor["centre"]) == 3


def test_descriptor_never_reports_a_massless_prop():
    # A flat sliver has near-zero hull volume; a zero-mass dynamic body makes
    # Rapier misbehave, so the floor has to hold.
    rng = np.random.default_rng(10)
    points = np.hstack([
        rng.uniform(-0.2, 0.2, size=(3000, 2)),
        rng.normal(0, 0.001, size=(3000, 1)),
    ]).astype(np.float32)
    assert prop_descriptor(prop_collider(points, voxel_size=0.01))["mass"] >= 0.5
