"""Tests for the one unit the whole pipeline hangs on.

A reconstruction is in whatever units it came out in; the viewer multiplies by
`metric_scale_factor` to get metres. Every threshold in the collider tools is
written in metres — 5 cm voxels, a 1.6 m headroom test, 250 kg per cubic metre —
so anything measured before that multiplication is measured in the wrong unit.

Nothing crashes when this is wrong. The map is drawn, the mass is reported, the
prop is cut; they are simply not describing the room the user is standing in.
The worlds in this repository run from 1.0 to 3.66, and at 3.66 the headroom
test asks for a 5.9 m ceiling and a chair comes back 49x too heavy.
"""

import json

import numpy as np
import pytest
import trimesh

from backend.tools.build_collider import read_metric_scale
from backend.tools.build_props import prop_descriptor
from backend.tools.build_walkable import build_walkable_map


# --- Where the scale comes from ----------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A world directory laid out the way `build_collider` expects to find one."""
    import backend.tools.build_collider as build_collider

    worlds = tmp_path / "worlds"
    world_dir = worlds / "demo" / "output" / "world"
    world_dir.mkdir(parents=True)
    monkeypatch.setattr(build_collider, "WORLDS_DIR", worlds)
    return world_dir, worlds / "demo"


def write_manifest(world_dir, scale):
    (world_dir / "0-world.json").write_text(
        json.dumps({"assets": {"splats": {"semantics_metadata": {"metric_scale_factor": scale}}}}),
        encoding="utf-8",
    )


def test_defaults_to_one_when_nothing_says_otherwise(world):
    world_dir, _ = world
    assert read_metric_scale(world_dir, "demo", "0") == 1.0


def test_reads_the_manifest(world):
    world_dir, _ = world
    write_manifest(world_dir, 3.66)
    assert read_metric_scale(world_dir, "demo", "0") == pytest.approx(3.66)


def test_scene_json_wins_over_the_manifest(world):
    # The viewer and the USD exporter both prefer scene.json, so a world the
    # user rescaled in the editor has to measure the way it looks on screen.
    world_dir, world_root = world
    write_manifest(world_dir, 3.66)
    (world_root / "scene.json").write_text(json.dumps({"metricScaleFactor": 2.0}), encoding="utf-8")
    assert read_metric_scale(world_dir, "demo", "0") == pytest.approx(2.0)


@pytest.mark.parametrize("bad", [0, -1, "3.66", None, float("nan")])
def test_rejects_unusable_values_rather_than_dividing_by_them(world, bad):
    # A zero here would propagate into a division and take the collider with it.
    world_dir, world_root = world
    write_manifest(world_dir, 3.66)
    (world_root / "scene.json").write_text(json.dumps({"metricScaleFactor": bad}), encoding="utf-8")
    assert read_metric_scale(world_dir, "demo", "0") == pytest.approx(3.66)


def test_survives_a_corrupt_scene_json(world):
    world_dir, world_root = world
    write_manifest(world_dir, 3.66)
    (world_root / "scene.json").write_text("{not json", encoding="utf-8")
    assert read_metric_scale(world_dir, "demo", "0") == pytest.approx(3.66)


# --- What it is used for ------------------------------------------------------


def test_prop_mass_scales_with_the_cube(tmp_path):
    # The error is cubed, which is why it shows up as "the physics is broken"
    # rather than as a number that looks slightly off.
    mesh = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    plain = prop_descriptor(mesh)
    scaled = prop_descriptor(mesh, metric_scale=3.66)
    assert scaled["mass"] == pytest.approx(plain["mass"] * 3.66**3, rel=1e-6)


def test_prop_geometry_stays_in_world_units(tmp_path):
    # Only the mass converts. `centre` and `bounds` are read alongside the GLB,
    # which the viewer scales itself -- converting them too would double it.
    mesh = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    mesh.apply_translation((1.0, 2.0, 3.0))
    plain = prop_descriptor(mesh)
    scaled = prop_descriptor(mesh, metric_scale=3.66)
    assert scaled["centre"] == plain["centre"]
    assert scaled["bounds"] == plain["bounds"]


def floor_slab(size: float = 4.0, height: float = 0.0) -> trimesh.Trimesh:
    box = trimesh.creation.box(extents=(size, 0.05, size))
    box.apply_translation((0.0, height, 0.0))
    return box


def test_walkable_map_records_the_scale_it_was_measured_at(tmp_path):
    # Without this the viewer cannot tell a grid in metres from the older ones
    # in raw splat units, and would place the player by the wrong ratio.
    mesh = floor_slab()
    groups = {"floor": np.arange(len(mesh.faces))}
    _, meta = build_walkable_map(mesh, groups, flip_y=False, metric_scale_factor=3.66)
    assert meta["metric_scale_factor"] == pytest.approx(3.66)


def test_walkable_map_defaults_to_one(tmp_path):
    mesh = floor_slab()
    groups = {"floor": np.arange(len(mesh.faces))}
    _, meta = build_walkable_map(mesh, groups, flip_y=False)
    assert meta["metric_scale_factor"] == pytest.approx(1.0)


def test_headroom_is_measured_in_metres(tmp_path):
    """A ceiling 1 m above the floor is too low to stand under, at any scale.

    This is the failure the metric fix exists for: feed the grid a mesh in raw
    units and the 1.6 m headroom test silently becomes a 5.9 m one, so a normal
    room reports almost no walkable area at all.
    """
    floor = floor_slab()
    groups = {"floor": np.arange(len(floor.faces))}

    # The ceiling overhangs the floor so no cell escapes it through the edge of
    # the grid; a stray fringe cell would say nothing about the headroom rule.
    low = trimesh.util.concatenate([floor, floor_slab(size=6.0, height=1.0)])
    _, meta_low = build_walkable_map(low, groups, flip_y=False)
    assert meta_low["standable_area"] < 0.5, "1 m of headroom should not count as standable"

    # The same room with the ceiling out of the way does count.
    tall = trimesh.util.concatenate([floor, floor_slab(size=6.0, height=3.0)])
    _, meta_tall = build_walkable_map(tall, groups, flip_y=False)
    assert meta_tall["standable_area"] > 10
