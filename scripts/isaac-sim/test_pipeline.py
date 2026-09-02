"""Tests for `pipeline.py`, the runner that drives the whole chain.

Two things in it are worth pinning, and neither needs Isaac Sim, WSL, Node or a
generated world -- so CI can run them:

  * the Windows -> WSL path translation, because nav2 is the one stage that
    crosses that boundary and the boundary is where this repo's bugs live;
  * the regexes that turn each stage's stdout into the report, because if one
    stops matching the report goes quietly blank rather than red. That is the
    same failure as every other one found on 2026-09-01: a check that keeps
    passing while it has stopped checking.

Run:
    python -m pytest scripts/isaac-sim/test_pipeline.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import pipeline  # noqa: E402


# --------------------------------------------------------------------------
# The boundary nav2 has to cross
# --------------------------------------------------------------------------


@pytest.mark.parametrize("windows, wsl", [
    ("D:/Tyndall Labs/image2world/a.yaml", "/mnt/d/Tyndall Labs/image2world/a.yaml"),
    ("C:/x/y.sh", "/mnt/c/x/y.sh"),
])
def test_a_drive_letter_becomes_a_mount_point(windows, wsl):
    assert pipeline.wsl_path(windows) == wsl


def test_backslashes_are_translated_too():
    """Windows hands these out and WSL does not take them."""
    assert pipeline.wsl_path(r"D:\a\b\c.yaml") == "/mnt/d/a/b/c.yaml"


def test_a_windows_path_translates_on_a_posix_host_too(monkeypatch):
    """These tests passed on Windows and failed in CI, which runs Linux.

    `wsl_path` used to call `os.path.abspath` first. On Linux `D:/a/b` is a
    *relative* path, so that returned `<cwd>/D:/a/b`, the drive letter was no
    longer in position 1, and the translation silently did nothing. Standing in
    for the other host is the only way to catch that from either one.
    """
    import posixpath
    monkeypatch.setattr("os.path.abspath",
                        lambda path: posixpath.normpath(
                            posixpath.join("/home/runner/work/repo", path)))
    assert pipeline.wsl_path("D:/a/b.yaml") == "/mnt/d/a/b.yaml"
    assert pipeline.wsl_path("C:/x/y.sh") == "/mnt/c/x/y.sh"


def test_a_path_with_no_drive_letter_is_left_alone():
    # os.path.abspath will have made it absolute against the cwd; the point is
    # that nothing is mangled when there is no drive letter to translate.
    assert not pipeline.wsl_path("/already/posix").startswith("/mnt//")


# --------------------------------------------------------------------------
# The environment a different interpreter must not inherit
# --------------------------------------------------------------------------


def test_a_foreign_python_does_not_inherit_our_home(monkeypatch):
    """Isaac Sim ships its own Python. Handing it this one's `PYTHONHOME` makes
    it load the wrong standard library and die in `re` with `SRE module
    mismatch`, which reads as a broken Isaac Sim install and is not one. It only
    happens when a program launches the stage -- a shell has no such variable --
    so it is invisible until something automates the pipeline."""
    monkeypatch.setenv("PYTHONHOME", "/somewhere/else")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else/lib")
    env = pipeline.foreign_python_env()
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env


def test_the_rest_of_the_environment_survives(monkeypatch):
    """Isaac Sim needs PATH, and on Windows a good deal else besides."""
    monkeypatch.setenv("ISAAC_PATH", "D:/isaacsim-6.0.1")
    env = pipeline.foreign_python_env()
    assert env.get("ISAAC_PATH") == "D:/isaacsim-6.0.1"
    assert "PATH" in env or "Path" in env


# --------------------------------------------------------------------------
# Exit 0 is not the same as a map worth having
# --------------------------------------------------------------------------


def build_report(tmp_path, free_m2, origin_reachable):
    path = tmp_path / "w-navmap.json"
    path.write_text(json.dumps({
        "raycast": {"free_m2": free_m2},
        "reachability": {"world_origin_reachable": origin_reachable},
    }), encoding="utf-8")
    return str(path)


def test_a_real_room_passes(tmp_path):
    notes = {}
    assert pipeline.map_is_usable(build_report(tmp_path, 65.62, True), notes) == "ok"
    assert notes["free_m2"] == 65.62


def test_a_map_with_nothing_at_the_origin_is_called_unusable(tmp_path):
    """The failure this exists for. A splatfacto world with no metric scale came
    back measuring 74.8 x 70.3 x 20.3 m with its floor 19.5 m up and eight
    walkable cells -- and `build_navmap.py` exited 0, because PhysX had loaded
    the stage and the raycasts had run. Only reading exit codes would have
    called that a working pipeline."""
    verdict = pipeline.map_is_usable(build_report(tmp_path, 0.02, False), {})
    assert "unusable" in verdict and "0.02" in verdict


def test_a_map_with_almost_no_floor_is_called_unusable(tmp_path):
    verdict = pipeline.map_is_usable(build_report(tmp_path, 0.4, True), {})
    assert "unusable" in verdict


def test_a_missing_build_report_is_reported_not_raised(tmp_path):
    verdict = pipeline.map_is_usable(str(tmp_path / "absent.json"), {})
    assert "could not read" in verdict


# --------------------------------------------------------------------------
# The regexes that make the report say anything
# --------------------------------------------------------------------------


def stage_with(output):
    stage = pipeline.Stage("test", [])
    stage.output = output
    return stage


def test_the_map_stage_reads_the_probe_comparison():
    stage = stage_with(
        "  clearance: ray blocks 5050 cells, cell-wide box blocks 7054 "
        "(2004 the ray walked past, 0 the box missed) -- map written from the box\n"
    )
    stage.pick(pipeline.SAYS["probe"], "box", "box_only")
    assert stage.notes == {"box": "7054", "box_only": "2004"}


def test_the_map_stage_reads_the_cell_counts():
    stage = stage_with(
        "  free=26246 (65.6 m2)  occupied=12223 (30.6 m2)  unmapped=41127 (102.8 m2)\n")
    stage.pick(pipeline.SAYS["cells"], "free", "free_m2", "occupied")
    assert stage.notes == {"free": "26246", "free_m2": "65.6", "occupied": "12223"}


def test_the_check_stage_reads_the_planned_route():
    stage = stage_with(
        "  planned (-0.01, 0.02) -> (3.29, 9.07): 185 cells, 10.50 m, straight 9.63 m\n")
    stage.pick(pipeline.SAYS["route"], "cells", "metres")
    assert stage.notes == {"cells": "185", "metres": "10.50"}


def test_the_nav2_stage_reads_the_pose_count():
    stage = stage_with(
        "nav2 planned 384 poses, 9.75 m (straight line 8.95 m, detour x1.09)\n")
    stage.pick(pipeline.SAYS["poses"], "poses", "metres")
    assert stage.notes == {"poses": "384", "metres": "9.75"}


def test_the_drive_stage_reads_the_result_and_the_tracking():
    stage = stage_with(
        "result:   ARRIVED\n"
        "  off the planned line: mean 0.079 m, p95 0.280 m, max 0.296 m\n")
    stage.pick(pipeline.SAYS["result"], "result")
    stage.pick(pipeline.SAYS["tracking"], "mean", "max")
    assert stage.notes["result"].strip() == "ARRIVED"
    assert stage.notes["mean"] == "0.079" and stage.notes["max"] == "0.296"


def test_a_multi_word_result_is_read_whole():
    """The pattern used to be `\w+`, which caught only the first word and
    reported a failed run as `result=did` -- a parsing accident that reads like
    a robot state. This test uses the module's own pattern, so replacing it with
    a narrower one fails here rather than quietly degrading the report."""
    stage = stage_with("result:   did not arrive\n")
    stage.pick(pipeline.SAYS["result"], "result")
    assert stage.notes["result"].strip() == "did not arrive"


def test_the_patterns_the_tests_use_are_the_ones_the_runner_uses():
    """The point of `SAYS`. Every name the stages ask for must exist."""
    assert set(pipeline.SAYS) == {"probe", "cells", "route", "poses", "result",
                                  "tracking", "distance", "size", "refused"}


def test_a_refusal_is_read_out_of_the_stage_output():
    """`build_navmap.py` refusing to guess a floor exits 1 with a full
    explanation. Without this the report said `exit 1` and the sentence stayed
    buried in Kit's log, which is where nobody reads it."""
    stage = stage_with("REFUSED: this collider has no classified `floor` part, "
                       "so there is nothing here that says where the floor is.\n"
                       "  Its parts are: geometry_0\n")
    stage.pick(pipeline.SAYS["refused"], "why")
    assert stage.notes["why"].startswith("this collider has no classified")


def test_a_stage_that_says_nothing_recognisable_records_nothing():
    """It must not invent a value, and it must not raise -- a stage can fail
    before it prints anything, and the runner still has to report the failure."""
    stage = stage_with("Traceback (most recent call last):\n")
    stage.pick(pipeline.SAYS["result"], "result")
    assert stage.notes == {}


def test_a_fallen_robot_is_not_read_as_arrival():
    stage = stage_with("result:   FELL\n")
    stage.pick(pipeline.SAYS["result"], "result")
    assert stage.notes["result"].strip() == "FELL"


# --------------------------------------------------------------------------


def test_every_stage_name_is_known_to_the_skip_flag():
    """`--skip` validates against this tuple, so a stage missing from it could
    never be skipped and a typo in it would silently skip nothing."""
    assert pipeline.STAGES == ("export", "map", "check", "nav2", "drive")


def test_find_isaac_prefers_what_it_was_told():
    assert pipeline.find_isaac("/no/such/place") != "/no/such/place"
