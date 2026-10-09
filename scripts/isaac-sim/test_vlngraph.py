"""Tests for `vlngraph` and the VLN gate built on it.

The gate's whole job is to say a map is not good enough, so the test that
matters most is the one proving it can also say yes. Every world in this
repository fails on `detour`, and a metric that is simply stuck at 1.0 would
produce exactly that result while measuring nothing. `test_a_wall_*` builds a
space with a corner in it and checks the number moves.

No Isaac Sim and no generated world: synthetic grids plus the `demo-office` map
a fresh clone already has. numpy and Pillow, both of which CI installs.

Run:
    python -m pytest scripts/isaac-sim/test_vlngraph.py -q
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import navmap  # noqa: E402
import vlngraph  # noqa: E402
import vln_check  # noqa: E402
from navmap import FREE, OCCUPIED  # noqa: E402


def box(rows=100, cols=100, resolution=0.1):
    """An empty room with walls, 10 x 10 m by default."""
    grid = np.full((rows, cols), FREE, dtype=np.int8)
    grid[:2, :] = grid[-2:, :] = OCCUPIED
    grid[:, :2] = grid[:, -2:] = OCCUPIED
    return navmap.NavMap(grid, resolution, (0.0, 0.0))


def walled(fraction, rows=100, cols=100, resolution=0.1):
    """The same room with a wall hanging `fraction` of the way down from +y."""
    carte = box(rows, cols, resolution)
    carte.grid[int(rows * (1.0 - fraction)):, cols // 2 - 2:cols // 2 + 2] = OCCUPIED
    return carte


# --- the metric can move ---------------------------------------------------

def test_an_open_room_has_no_detour():
    report = vlngraph.build(box(), spacing_m=0.75)
    assert report["detour"]["p90"] < 1.05
    assert report["detour_over_1_25"] == 0.0


@pytest.mark.parametrize("fraction,least", [(0.6, 1.3), (0.8, 1.8)])
def test_a_wall_makes_routes_go_round(fraction, least):
    """Without this the finding "every world scores 1.0" means nothing.

    A metric pinned at 1.0 by a bug would report the same thing about every
    map in the repository, and the conclusion drawn from it -- that the
    captures are single open rooms -- would be unfounded.
    """
    report = vlngraph.build(walled(fraction), spacing_m=0.75)
    assert report["detour"]["p90"] >= least
    assert report["detour_over_1_25"] > 0.1


def test_the_median_detour_cannot_tell_a_wall_from_an_open_room():
    """Why the gate reads p90. Pins the reason, so it does not get "simplified".

    Both spaces have a median detour of about 1.0, because most pairs of
    viewpoints sit on the same side of the wall and see each other. Only the
    upper tail separates them.
    """
    open_room = vlngraph.build(box(), spacing_m=0.75)
    two_rooms = vlngraph.build(walled(0.8), spacing_m=0.75)
    assert abs(open_room["detour"]["p50"] - two_rooms["detour"]["p50"]) < 0.05
    assert two_rooms["detour"]["p90"] - open_room["detour"]["p90"] > 0.5


def test_a_wall_all_the_way_across_splits_the_graph():
    report = vlngraph.build(walled(0.98), spacing_m=0.75)
    assert report["components"] >= 2
    assert report["largest_fraction"] < 0.95


# --- the graph is what it claims to be -------------------------------------

def test_nodes_keep_their_spacing():
    carte = box()
    report = vlngraph.build(carte, spacing_m=0.75)
    nodes = report["_nodes"]
    least = 0.75 / carte.resolution
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            assert math.dist(a, b) >= least - 1e-9


def test_a_corridor_the_robot_fits_gets_viewpoints():
    """Clearance is measured from the obstacles, not from the edge of `passable`.

    This corridor is 0.8 m wide, so its centre line is 0.4 m from either wall --
    comfortably passable for a 0.25 m robot, and it should carry viewpoints.

    It got none. `min_clearance` defaults to the robot's radius and was applied
    to a clearance field computed from `~passable`, which has already had that
    radius taken off it, so a viewpoint needed 0.5 m of wall clearance for a
    0.25 m robot. Every small world in the tree failed `coverage` on the fringe
    that excluded, and the detour figures were understated with it, because the
    viewpoints nearest the walls are the ones whose routes go round things.
    """
    grid = np.full((120, 40), OCCUPIED, dtype=np.int8)
    grid[10:110, 16:24] = FREE           # 0.8 m wide at 0.1 m per cell
    carte = navmap.NavMap(grid, 0.1, (0.0, 0.0))
    report = vlngraph.build(carte, spacing_m=0.75, robot_radius=0.25)
    assert report["nodes"] > 5, report["nodes"]
    assert report["coverage"] > 0.9


def test_nodes_are_places_the_robot_fits():
    carte = box()
    report = vlngraph.build(carte, spacing_m=0.75, robot_radius=0.25)
    passable = carte.passable(robot_radius=0.25, avoid_unknown=False)
    for cell in report["_nodes"]:
        assert passable[cell]


def test_every_edge_is_an_unobstructed_line():
    carte = walled(0.8)
    report = vlngraph.build(carte, spacing_m=0.75, robot_radius=0.25)
    passable = carte.passable(robot_radius=0.25, avoid_unknown=False)
    nodes = report["_nodes"]
    for (i, j, _) in report["_edges"]:
        assert navmap.visible(passable, nodes[i], nodes[j])


def test_no_edge_is_longer_than_the_cap():
    carte = box()
    report = vlngraph.build(carte, spacing_m=0.75, max_edge_m=2.5)
    for (_, _, length) in report["_edges"]:
        assert length * carte.resolution <= 2.5 + 1e-9


def test_two_builds_of_one_map_agree():
    """A dataset whose node ids move between builds cannot be compared to itself."""
    first = vlngraph.build(box(), spacing_m=0.75)
    second = vlngraph.build(box(), spacing_m=0.75)
    assert first["_nodes"] == second["_nodes"]
    assert first["_edges"] == second["_edges"]


# --- coverage is measured over the space, not over the nodes ---------------

def test_coverage_notices_a_graph_that_misses_part_of_the_room():
    """A node in one corner does not represent a 10 x 10 m room."""
    carte = box()
    passable = carte.passable(robot_radius=0.25, avoid_unknown=False)
    assert vlngraph.coverage([(10, 10)], passable, 0.75 / carte.resolution) < 0.05


def test_coverage_is_high_when_the_nodes_are_spread():
    report = vlngraph.build(box(), spacing_m=0.75)
    assert report["coverage"] > 0.9


# --- episodes ---------------------------------------------------------------

def test_episodes_are_real_routes_of_the_right_shape():
    carte = walled(0.8)
    report = vlngraph.build(carte, spacing_m=0.75, min_hops=4,
                            episode_range=(3.0, 20.0), episodes=25)
    edges = {(min(i, j), max(i, j)) for (i, j, _) in report["_edges"]}
    assert report["episodes"] > 0
    for (metres, source, target, route) in report["_episodes"]:
        assert route[0] == source and route[-1] == target
        assert len(route) - 1 >= 4
        assert 3.0 <= metres <= 20.0
        for a, b in zip(route, route[1:]):
            assert (min(a, b), max(a, b)) in edges


def test_episodes_span_the_lengths_rather_than_clustering():
    """Taking the first N pairs in node order puts them all in one corner."""
    carte = box()
    report = vlngraph.build(carte, spacing_m=0.75, min_hops=1,
                            episode_range=(1.0, 20.0), episodes=20)
    lengths = [e[0] for e in report["_episodes"]]
    assert len(lengths) == 20
    assert max(lengths) - min(lengths) > 3.0


def test_an_empty_map_yields_nothing_rather_than_raising():
    grid = np.full((40, 40), OCCUPIED, dtype=np.int8)
    report = vlngraph.build(navmap.NavMap(grid, 0.1, (0.0, 0.0)))
    assert report["nodes"] == 0
    assert report["episodes"] == 0
    assert report["coverage"] == 0.0


# --- the gate ---------------------------------------------------------------

def test_the_gate_names_the_criterion_that_failed():
    report = vlngraph.build(box(), spacing_m=0.75)
    rows = vln_check.verdict(report)
    failed = [row["criterion"] for row in rows if row["met"] is False]
    assert failed == ["detour_p90"]
    assert "more than one room" in dict(
        (row["criterion"], row["remedy"]) for row in rows)["detour_p90"]


def test_the_gate_can_say_yes():
    """Otherwise "no world passes" is a statement about the gate, not the worlds."""
    report = vlngraph.build(walled(0.8), spacing_m=0.75, min_hops=4,
                            episode_range=(3.0, 20.0), episodes=100)
    rows = vln_check.verdict(report)
    assert all(row["met"] is True for row in rows), \
        [row for row in rows if row["met"] is not True]


def test_a_map_with_no_viewpoints_gets_no_diagnosis_of_its_shape():
    """The gate used to tell an empty map to go and capture more rooms.

    Every criterion read 0.0 and every one of them was reported as failed,
    including the detour -- a confident statement about the shape of a space
    that had produced nothing to measure it with.
    """
    grid = np.full((40, 40), OCCUPIED, dtype=np.int8)
    report = vlngraph.build(navmap.NavMap(grid, 0.1, (0.0, 0.0)))
    rows = {row["criterion"]: row for row in vln_check.verdict(report)}
    assert rows["nodes"]["met"] is False
    assert rows["detour_p90"]["met"] is None
    assert rows["episodes"]["met"] is None
    assert rows["detour_p90"]["not_assessable"]


def test_not_being_able_to_tell_is_not_a_pass():
    grid = np.full((40, 40), OCCUPIED, dtype=np.int8)
    rows = vln_check.verdict(
        vlngraph.build(navmap.NavMap(grid, 0.1, (0.0, 0.0))))
    assert not (not [r for r in rows if r["met"] is False]
                and not [r for r in rows if r["met"] is None])


def test_the_picture_covers_the_whole_map(tmp_path):
    """The drawing is evidence, so it has to be of the map it claims to be."""
    from PIL import Image

    carte = walled(0.8)
    report = vlngraph.build(carte, spacing_m=0.75)
    out = vlngraph.draw(carte, report, tmp_path / "g.png", width=400)
    picture = Image.open(out)
    assert picture.width % carte.width == 0
    assert picture.height % carte.height == 0
    assert picture.width // carte.width == picture.height // carte.height
    # The graph is drawn on it, so the red of a viewpoint has to be there.
    assert (220, 40, 40) in {c for _, c in picture.convert("RGB").getcolors(1 << 16)}


def test_every_criterion_has_a_remedy():
    report = vlngraph.build(box(), spacing_m=0.75)
    for row in vln_check.verdict(report):
        assert row["remedy"].strip()


# --- the map a fresh clone already has --------------------------------------

SAMPLE = (Path(__file__).resolve().parents[2] / "public" / "worlds" / "demo-office"
          / "export" / "demo-office-navmap.yaml")


@pytest.mark.skipif(not SAMPLE.is_file(), reason="sample world not checked out")
def test_the_sample_world_fails_on_connectivity_and_yield():
    """The finding this branch records, pinned so a map change has to restate it.

    `demo-office` places 21 viewpoints in 9.7 m2, covers all of it, and has real
    corners -- detour p90 1.27 over 153 pairs, with 10.5% of them having to go
    round something. What it does not have is one piece: three viewpoints sit in
    dead-end spurs no straight edge reaches, so 86% of them are in the largest
    component against a bar of 90%. Nor can 21 viewpoints yield 50 episodes.

    Two earlier versions of this test asserted otherwise, and both were reading
    a bug rather than the map. `min_clearance` defaulted to the robot's radius
    and was applied to a clearance field measured from an already-inflated mask,
    so viewpoints had to stand 0.5 m from a wall for a 0.25 m robot: 9 of them
    instead of 21, coverage 0.85 instead of 1.00, and -- because the viewpoints
    it suppressed are exactly the ones near walls, whose routes are the ones
    that have to go round things -- detour understated everywhere.
    """
    carte = navmap.load(str(SAMPLE))
    rows = {row["criterion"]: row
            for row in vln_check.verdict(
                vlngraph.build(carte, spacing_m=0.75, robot_radius=0.25))}
    assert rows["nodes"]["met"] is True
    assert rows["coverage"]["met"] is True
    assert rows["largest_fraction"]["met"] is False
    assert rows["episodes"]["met"] is False


REFERENCE = (Path(__file__).resolve().parents[2] / "public" / "worlds"
             / "reference-rooms" / "export" / "reference-rooms-navmap.yaml")


@pytest.mark.skipif(not REFERENCE.is_file(), reason="reference fixture not checked out")
def test_a_real_isaac_build_can_pass_every_criterion():
    """The one test that stops this whole file being a gate that only refuses.

    Every *generated* world in the tree fails, and until `reference-rooms`
    existed nothing distinguished "these captures are single rooms" from "the
    gate cannot say yes to anything an Isaac Sim build produces". The synthetic
    ones above cannot settle it either: they are numpy arrays this file wrote,
    so they never touch the exporter, PhysX, or `build_navmap.py`.

    This map did. `make_reference_world.py` writes a GLB of five rooms off a
    corridor, and the unmodified `export -> map -> check -> vln` chain turns it
    into 212 viewpoints in one component with a detour p90 of 2.36 and 100
    episodes at a median of 12.5 m over 8 hops -- R2R's own shape.
    """
    carte = navmap.load(str(REFERENCE))
    report = vlngraph.build(carte, spacing_m=0.75, robot_radius=0.25,
                            min_hops=4, episode_range=(3.0, 20.0), episodes=100)
    rows = vln_check.verdict(report)
    assert all(row["met"] is True for row in rows), \
        [(row["criterion"], row["value"]) for row in rows if row["met"] is not True]


@pytest.mark.skipif(not REFERENCE.is_file(), reason="reference fixture not checked out")
def test_the_reference_world_is_the_shape_the_captures_are_not():
    """What separates it from every capture here, stated as numbers.

    Doorways are what makes a building a graph. `marble-office` has the scale --
    99 viewpoints -- and 3.7% of its viewpoint pairs have to go round anything,
    because it is one hall. This has five rooms on a corridor, and most pairs do.
    """
    carte = navmap.load(str(REFERENCE))
    report = vlngraph.build(carte, spacing_m=0.75, robot_radius=0.25)
    assert report["components"] == 1
    assert report["detour_over_1_25"] > 0.5
    # Mean degree well under the node count: rooms connect through doors, not
    # by everything seeing everything, which is what `marble-office` does at 17.6.
    assert report["mean_degree"] < report["nodes"] / 4


@pytest.mark.skipif(not SAMPLE.is_file(), reason="sample world not checked out")
def test_the_sample_world_exports_a_serialisable_graph():
    carte = navmap.load(str(SAMPLE))
    payload = vlngraph.to_world(carte, vlngraph.build(carte, spacing_m=0.75))
    import json

    json.dumps(payload)
    assert len(payload["graph"]["nodes"]) == payload["nodes"]
    assert len(payload["graph"]["edges"]) == payload["edges"]
    assert not any(key.startswith("_") for key in payload)
    # Node positions come back in metres inside the map's own extent.
    x0, y0 = carte.origin
    for node in payload["graph"]["nodes"]:
        assert x0 <= node["x"] <= x0 + carte.width * carte.resolution
        assert y0 <= node["y"] <= y0 + carte.height * carte.resolution
