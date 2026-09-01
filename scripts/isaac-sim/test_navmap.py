"""Tests for `navmap`, the map and planner both the checker and the robot use.

`check_navmap.py` reads a map to verify it and `navigate.py` reads the same map
to drive it, and the whole point of sharing this module is that those two are
the same reading. So it is worth having the reading itself pinned, and it is
cheap to pin: no Isaac Sim, no ROS, no generated world -- numpy and Pillow, both
of which CI already installs.

Run:
    python -m pytest scripts/isaac-sim/test_navmap.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import navmap  # noqa: E402
from navmap import FREE, OCCUPIED, UNKNOWN  # noqa: E402


def write_map(directory: Path, rows: list[str], resolution=0.05, origin=(-1.0, -2.0)):
    """A map_server pair from an ASCII picture, top row = highest y.

    '.' free, '#' occupied, ' ' unknown -- the three greys `build_navmap.py`
    writes, in the row order it writes them.
    """
    from PIL import Image

    shade = {".": 255, "#": 0, " ": 127}
    pixels = np.array([[shade[c] for c in row] for row in rows], dtype=np.uint8)
    Image.fromarray(pixels).save(directory / "m.png")
    (directory / "m.yaml").write_text(
        f"image: m.png\nresolution: {resolution}\n"
        f"origin: [{origin[0]}, {origin[1]}, 0.0]\n"
        "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n",
        encoding="utf-8",
    )
    return directory / "m.yaml"


# --------------------------------------------------------------------------
# Loading, which is where a silent mirror would come from
# --------------------------------------------------------------------------


def test_read_yaml_parses_the_six_keys(tmp_path):
    path = write_map(tmp_path, ["..", ".."])
    meta = navmap.read_yaml(path)
    assert meta["image"] == "m.png"
    assert meta["resolution"] == pytest.approx(0.05)
    assert meta["origin"] == [-1.0, -2.0, 0.0]
    assert meta["negate"] == 0


def test_a_missing_key_is_refused(tmp_path):
    write_map(tmp_path, [".."])
    path = tmp_path / "m.yaml"
    path.write_text("image: m.png\nresolution: 0.05\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        navmap.load(path)


def test_row_zero_of_the_grid_is_the_lowest_y(tmp_path):
    """The one that is invisible in the picture if it is wrong.

    `map_server` treats image row 0 as the top of the picture and the highest y,
    so the grid has to come back flipped. Get it backwards and the room is
    mirrored, the counts are identical, and nothing looks wrong.
    """
    carte = navmap.load(write_map(tmp_path, [
        "##",   # top of the picture -> highest y
        "..",
    ]))
    assert carte.grid[0].tolist() == [FREE, FREE]        # lowest y
    assert carte.grid[1].tolist() == [OCCUPIED, OCCUPIED]


def test_the_three_greys_land_in_the_three_buckets(tmp_path):
    carte = navmap.load(write_map(tmp_path, [".# "]))
    assert sorted(carte.counts().items()) == [("free", 1), ("occupied", 1), ("unknown", 1)]
    assert set(np.unique(carte.grid)) == {FREE, OCCUPIED, UNKNOWN}


def test_cell_and_world_round_trip(tmp_path):
    carte = navmap.load(write_map(tmp_path, ["...", "...", "..."]))
    for iy in range(3):
        for ix in range(3):
            assert carte.cell_of(*carte.world_of(iy, ix)) == (iy, ix)
    # The origin names the *corner* of cell (0, 0), so its centre is half a cell in.
    assert carte.world_of(0, 0) == pytest.approx((-1.0 + 0.025, -2.0 + 0.025))


# --------------------------------------------------------------------------
# Inflation, clearance, reachability
# --------------------------------------------------------------------------


def test_inflate_grows_by_a_disc_not_a_square():
    blocked = np.zeros((11, 11), dtype=bool)
    blocked[5, 5] = True
    grown = navmap.inflate(blocked, 0.10, 0.05)      # two cells
    assert grown[5, 3] and grown[3, 5]               # on the axes, two out
    assert not grown[5, 2]
    assert not grown[3, 3]                           # the diagonal corner is 2.83 away


def test_inflate_is_a_no_op_at_zero_radius():
    blocked = np.zeros((4, 4), dtype=bool)
    blocked[1, 1] = True
    assert (navmap.inflate(blocked, 0.0, 0.05) == blocked).all()


def test_clearance_measures_metres_to_the_nearest_blocked_cell():
    blocked = np.zeros((21, 21), dtype=bool)
    blocked[10, 10] = True
    room = navmap.clearance(blocked, 0.05, cap_m=0.5)
    assert room[10, 10] == 0.0
    assert room[10, 12] == pytest.approx(0.10, abs=1e-6)     # two cells out
    # The chamfer approximates the diagonal; sqrt(2) * 0.05 = 0.0707.
    assert room[11, 11] == pytest.approx(0.0707, abs=0.005)
    assert room[0, 0] == pytest.approx(0.5)                  # capped


def test_clearance_of_an_empty_map_is_the_cap():
    assert (navmap.clearance(np.zeros((5, 5), dtype=bool), 0.05, cap_m=0.3) == 0.3).all()


def test_passable_treats_unknown_as_free_or_not_on_request(tmp_path):
    carte = navmap.load(write_map(tmp_path, [
        ".....",
        "..#..",
        "..   ",
        ".....",
    ]))
    lenient = carte.passable(0.0)                        # nav2's rule
    strict = carte.passable(0.0, avoid_unknown=True)
    # Neither ever calls an unknown cell passable; the difference is what they
    # grow, and with a zero radius there is nothing to grow.
    assert lenient.sum() == strict.sum()
    # With a radius, the unknown cells push the strict answer back.
    assert carte.passable(0.10, avoid_unknown=True).sum() < carte.passable(0.10).sum()


def test_reachable_stops_at_a_wall(tmp_path):
    carte = navmap.load(write_map(tmp_path, [
        "..#..",
        "..#..",
        "..#..",
    ]))
    free = carte.grid == FREE
    reach = navmap.reachable(free, (0, 0))
    assert reach[:, :2].all()
    assert not reach[:, 3:].any()


def test_reachable_is_four_connected_not_eight(tmp_path):
    """A diagonal pinhole between two obstacle corners is not a gap."""
    carte = navmap.load(write_map(tmp_path, [
        ".#",
        "#.",
    ]))
    free = carte.grid == FREE
    reach = navmap.reachable(free, (0, 1))
    assert reach.sum() == 1


def test_furthest_reachable_ignores_a_pocket_it_cannot_get_to(tmp_path):
    carte = navmap.load(write_map(tmp_path, [
        "....#.",     # the cell past the wall is further, and unreachable
        "....#.",
    ]))
    free = carte.grid == FREE
    far = navmap.furthest_reachable(free, (0, 0))
    assert far is not None and far[1] <= 3, "picked a cell on the far side of the wall"


def test_furthest_reachable_prefers_room_when_asked(tmp_path):
    """The furthest cell is up a one-cell spur; asking for room gets a better goal.

    Without this the default goal is by construction pressed against the edge of
    the passable region, so the robot is asked to finish standing with nothing
    around it and every clearance figure along the route is pinned to zero by
    its own endpoint.
    """
    carte = navmap.load(write_map(tmp_path, [
        "#######.",    # the furthest reachable cell, in a spur one cell wide
        "........",
        "........",
        "........",
        "........",
    ]))
    free = carte.grid == FREE
    room = navmap.clearance(~free, carte.resolution, cap_m=0.3)
    cramped = navmap.furthest_reachable(free, (0, 0))
    roomy = navmap.furthest_reachable(free, (0, 0), room=room, min_clearance=0.10)
    assert cramped == (4, 7), "the plain answer should be the tip of the spur"
    assert room[cramped] < 0.10
    assert room[roomy] >= 0.10


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def test_plan_finds_the_way_round_a_wall(tmp_path):
    carte = navmap.load(write_map(tmp_path, [
        ".....",
        "####.",
        ".....",
    ]))
    free = carte.grid == FREE
    route = navmap.plan(free, (0, 0), (2, 0))
    assert route is not None
    assert route[0] == (0, 0) and route[-1] == (2, 0)
    assert all(free[cell] for cell in route)
    assert len(route) > 5, "went straight through the wall"


def test_plan_returns_none_when_there_is_no_way_through(tmp_path):
    carte = navmap.load(write_map(tmp_path, [
        ".....",
        "#####",
        ".....",
    ]))
    free = carte.grid == FREE
    assert navmap.plan(free, (0, 0), (2, 0)) is None


def test_plan_will_not_cut_a_diagonal_between_two_blocked_cells(tmp_path):
    carte = navmap.load(write_map(tmp_path, [
        ".#",
        "#.",
    ]))
    free = carte.grid == FREE
    assert navmap.plan(free, (0, 1), (1, 0)) is None


def test_clearance_cost_pushes_the_route_off_the_wall(tmp_path):
    """A corridor with a pillar: the cheap route hugs it, the cost route does not."""
    carte = navmap.load(write_map(tmp_path, [
        ".........",
        ".........",
        "....#....",
        ".........",
        ".........",
    ]))
    free = carte.grid == FREE
    room = navmap.clearance(~free, carte.resolution, cap_m=0.20)
    start, goal = (2, 0), (2, 8)
    plain = navmap.plan(free, start, goal)
    costed = navmap.plan(free, start, goal,
                         cost=navmap.clearance_cost(room, 0.20, 4.0))
    assert plain is not None and costed is not None
    assert min(room[c] for c in costed) > min(room[c] for c in plain)


def test_clearance_cost_is_never_below_one():
    """The A* heuristic stays admissible only if no move is cheaper than its length."""
    room = np.linspace(0.0, 1.0, 21).reshape(1, 21)
    assert (navmap.clearance_cost(room, 0.5, 3.0) >= 1.0).all()
    assert navmap.clearance_cost(np.array([[1.0]]), 0.5, 3.0)[0, 0] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# String pulling
# --------------------------------------------------------------------------


def test_shortcut_turns_a_staircase_into_one_leg(tmp_path):
    carte = navmap.load(write_map(tmp_path, ["....."] * 5))
    free = carte.grid == FREE
    route = navmap.plan(free, (0, 0), (4, 4))
    pulled = navmap.shortcut(route, free)
    assert len(pulled) < len(route)
    assert pulled[0] == route[0] and pulled[-1] == route[-1]


def test_shortcut_never_leaves_passable_space(tmp_path):
    carte = navmap.load(write_map(tmp_path, [
        "........",
        "...##...",
        "...##...",
        "........",
    ]))
    free = carte.grid == FREE
    route = navmap.plan(free, (0, 0), (3, 7))
    pulled = navmap.shortcut(route, free)
    for a, b in zip(pulled, pulled[1:]):
        assert navmap.visible(free, a, b), f"the leg {a}->{b} crosses something"


def test_shortcut_is_never_longer_than_what_it_pulled():
    """The staircase is longer than its diagonal, so this can only shrink."""
    grid = np.ones((9, 9), dtype=bool)
    route = navmap.plan(grid, (0, 0), (8, 8))
    assert navmap.path_length(navmap.shortcut(route, grid)) <= navmap.path_length(route) + 1e-9


def test_visible_sees_through_free_space_and_not_through_a_wall():
    grid = np.ones((5, 5), dtype=bool)
    assert navmap.visible(grid, (0, 0), (4, 4))
    grid[2, :] = False
    assert not navmap.visible(grid, (0, 0), (4, 4))


def test_path_length_is_euclidean():
    assert navmap.path_length([(0, 0), (3, 4)]) == pytest.approx(5.0)
    assert navmap.path_length([(0, 0)]) == 0.0
