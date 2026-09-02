"""Run the whole generated-world -> navigable-map pipeline, and say what broke.

    export  ->  map  ->  check  ->  vln  ->  nav2  ->  drive

Every stage of this already existed and every stage was documented, as a list of
commands for a person to type in order. Nothing ran the list. Two things follow
from that, and both were found the hard way on 2026-09-01:

  * `nav2_check.sh` had been unrunnable on any Windows checkout for as long as
    `core.autocrlf` had been on, because the shell it is written in does not
    accept CRLF. The step that proves the map works in a real ROS stack was
    broken and green.
  * Numbers in the docs went stale silently. A recorded nav2 result named a goal
    that later map fixes turned into an unknown cell; the run had never been
    repeated, so nothing said so.

Both are the same failure: a pipeline that is a description rather than a thing
you can run. This is the thing you can run.

    python scripts/isaac-sim/pipeline.py marble-office
    python scripts/isaac-sim/pipeline.py --all --json report.json

It shells out rather than importing, because the stages genuinely do not share
an interpreter: the exporter is Node, `build_navmap.py` and `navigate.py` need
Isaac Sim's Python, `check_navmap.py` runs anywhere, and nav2 lives in WSL.
Pretending otherwise is how the CRLF bug survived.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
WORLDS = os.path.join(ROOT, "public", "worlds")

STAGES = ("export", "map", "check", "vln", "nav2", "drive")

# The patterns that turn each stage's stdout into the report, named rather than
# written inline so `test_pipeline.py` can exercise *these* and not a copy of
# them. A test that defines its own regex and then checks that regex passes
# whatever this file does, which is the same "check that agrees with itself"
# this whole directory has been unpicking.
SAYS = {
    "probe": r"box blocks (?P<box>\d+) \((?P<box_only>\d+) the ray walked past",
    "cells": (r"free=(?P<free>\d+) \((?P<free_m2>[0-9.]+) m2\)"
              r"\s+occupied=(?P<occupied>\d+)"),
    "route": r"planned .*?: (?P<cells>\d+) cells, (?P<metres>[0-9.]+) m",
    "poses": r"planned (?P<poses>\d+) poses, (?P<metres>[0-9.]+) m",
    # To the end of the line: `\w+` caught only the first word of "did not
    # arrive" and reported the failure as `result=did`.
    "result": r"result:\s+(?P<result>.+)",
    "tracking": r"mean (?P<mean>[0-9.]+) m, p95 [0-9.]+ m, max (?P<max>[0-9.]+) m",
    "distance": r"planned (?P<planned>[0-9.]+) m, drove (?P<drove>[0-9.]+) m",
    "size": r"(?P<mb>[0-9.]+)\s*MB",
    # `build_navmap.py` marks a deliberate refusal so it does not arrive here as
    # a bare `exit 1` with the reason buried in Kit's log.
    "refused": r"REFUSED: (?P<why>[^\n]+)",
    "vln": (r"VLN-READY: (?P<ready>yes|no) \((?P<met>\d+)/(?P<total>\d+) "
            r"criteria met"),
    "viewpoints": (r"graph\s+(?P<viewpoints>\d+) viewpoints, (?P<links>\d+) edges"),
}


# --------------------------------------------------------------------------
# Finding the tools, rather than assuming where they are
# --------------------------------------------------------------------------


def find_isaac(explicit=None):
    """Isaac Sim's own Python launcher, which is not this interpreter.

    `build_navmap.py` and `navigate.py` import `isaacsim`, so they have to run
    under the interpreter that ships with it. Everything here runs under a bare
    Python, so the path has to be found rather than inherited.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("ISAACSIM_ROOT"):
        candidates.append(os.environ["ISAACSIM_ROOT"])
    if os.environ.get("ISAAC_PATH"):
        candidates.append(os.environ["ISAAC_PATH"])
    for drive in ("D:", "C:"):
        candidates.extend(sorted(glob.glob(drive + "/isaacsim*"), reverse=True))
    for root in candidates:
        launcher = os.path.join(root, "python.bat")
        if os.path.isfile(launcher):
            return launcher
        if os.path.isfile(root) and os.path.basename(root).startswith("python"):
            return root
    return None


def wsl_available():
    """True if WSL is here and can see this repository.

    nav2 only exists on the Linux side. Reporting the stage as skipped is honest;
    failing the run because a Windows box has no ROS is not.
    """
    if not shutil.which("wsl"):
        return False
    try:
        done = subprocess.run(["wsl", "test", "-d", wsl_path(ROOT)], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def foreign_python_env():
    """The environment for a Python that is not this one.

    `PYTHONHOME` and `PYTHONPATH` belong to whichever interpreter is running
    this script, and a child interpreter that inherits them loads the wrong
    standard library. Under a uv- or venv-managed Python the failure is
    `AssertionError: SRE module mismatch` from deep inside `re`, which reads as
    a broken Isaac Sim install and is not one. It also only happens when a
    program launches the stage -- typing the same command in a shell works,
    because the shell has no such variables set. That is precisely the gap a
    runner exists to close.
    """
    env = dict(os.environ)
    for leaked in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        env.pop(leaked, None)
    return env


def wsl_path(windows_path):
    """C:/x/y -> /mnt/c/x/y, which is the translation nav2_check.sh needs.

    The drive letter is looked for *before* `abspath`, so this translates a
    Windows path wherever it runs. Calling `abspath` first only works on
    Windows: on Linux `D:/a/b` is a relative path, so it comes back as
    `<cwd>/D:/a/b` and the drive letter is no longer in position 1. That is not
    a hypothetical -- the tests for this ran on Windows and failed in CI.
    """
    full = str(windows_path).replace("\\", "/")
    if not (len(full) > 1 and full[1] == ":"):
        # Relative, or already POSIX. Resolving against the working directory is
        # what supplies the drive letter on Windows.
        full = os.path.abspath(full).replace("\\", "/")
    if len(full) > 1 and full[1] == ":":
        return "/mnt/" + full[0].lower() + full[2:]
    return full


# --------------------------------------------------------------------------
# Running one stage
# --------------------------------------------------------------------------


class Stage:
    def __init__(self, name, command, cwd=ROOT, env=None):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.env = env
        self.status = "not run"
        self.seconds = 0.0
        self.output = ""
        self.notes = {}

    def run(self, timeout):
        start = time.time()
        try:
            done = subprocess.run(self.command, cwd=self.cwd, timeout=timeout,
                                  env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.output = done.stdout.decode("utf-8", "replace")
            self.status = "ok" if done.returncode == 0 else f"exit {done.returncode}"
        except subprocess.TimeoutExpired as expired:
            self.output = (expired.stdout or b"").decode("utf-8", "replace")
            self.status = f"timed out after {timeout}s"
        except OSError as err:
            self.status = f"could not run: {err}"
        self.seconds = time.time() - start
        return self

    def pick(self, pattern, *names):
        """Pull named groups out of the stage's output into `notes`."""
        found = re.search(pattern, self.output)
        if not found:
            return
        for name in names:
            self.notes[name] = found.group(name)


def one_world(slug, isaac, skip, timeout, robot, lod):
    export_dir = os.path.join(WORLDS, slug, "export")
    usdz = os.path.join(export_dir, slug + ".usdz")
    yaml = os.path.join(export_dir, slug + "-navmap.yaml")
    stages = []

    def add(name, command, cwd=ROOT, env=None):
        stage = Stage(name, command, cwd, env)
        stages.append(stage)
        if name in skip:
            stage.status = "skipped"
            return None
        return stage.run(timeout)

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    stage = add("export", [npm or "npm", "run", "export:usd", "--",
                           slug, "--no-splat", "--up-axis", "z", "--lod", lod])
    if stage and stage.status == "ok" and not os.path.isfile(usdz):
        stage.status = "no .usdz written"
    if stage:
        stage.pick(SAYS["size"], "mb")

    # When the map was last written, so a stage that exits 0 without rewriting it
    # can be caught. That is not hypothetical: `build_navmap.py` refusing to
    # guess a floor printed its refusal in full and still exited 0, because Kit
    # swallows SystemExit -- and the run then read the map a previous run had
    # left in place.
    was = os.path.getmtime(yaml) if os.path.isfile(yaml) else None
    stage = add("map", [isaac or "python", os.path.join(HERE, "build_navmap.py"),
                        usdz, "--out", export_dir], env=foreign_python_env())
    if stage:
        stage.pick(SAYS["probe"], "box", "box_only")
        stage.pick(SAYS["cells"], "free", "free_m2", "occupied")
        refusal = re.search(SAYS["refused"], stage.output)
        if refusal and stage.status != "ok":
            stage.status = "refused: " + refusal.group("why").strip()
        if stage.status == "ok" and not os.path.isfile(yaml):
            stage.status = "no -navmap.yaml written"
        elif stage.status == "ok" and was is not None and os.path.getmtime(yaml) == was:
            stage.status = "exited 0 without rewriting the map -- see its output"
        elif stage.status == "ok":
            stage.status = map_is_usable(os.path.join(export_dir, slug + "-navmap.json"),
                                         stage.notes)
        if stage.status != "ok":
            # A failed build leaves the previous run's map on disk, and the three
            # stages below would then read that one and report on it. Measured:
            # `singleview-meetingroom` came back `map:XX check:ok`, the check
            # green against a map built an hour earlier under different code.
            # `--skip map` is deliberately not affected -- re-checking an
            # existing map is what that flag is for.
            skip = skip | {"check", "vln", "nav2", "drive"}
            print(f"  .. the map did not build, so check/nav2/drive are not run "
                  f"against the {slug} map that was already on disk")

    stage = add("check", [sys.executable, os.path.join(HERE, "check_navmap.py"), yaml])
    if stage:
        stage.pick(SAYS["route"], "cells", "metres")
        if stage.status == "ok" and "counts match" not in stage.output:
            stage.status = "counts did not match the build"

    # Whether the map is good enough to *train* on, which is a different and
    # much higher bar than whether a planner can cross it -- and one every world
    # in this tree currently misses. Deliberately not fatal to the stages below:
    # a map that is too small for a dataset is still a map nav2 can drive, and
    # conflating the two would stop reporting the driving result that has been
    # green all along.
    stage = add("vln", [sys.executable, os.path.join(HERE, "vln_check.py"), yaml])
    if stage:
        stage.pick(SAYS["viewpoints"], "viewpoints", "links")
        stage.pick(SAYS["vln"], "ready", "met", "total")
        if stage.status == "ok" and stage.notes.get("ready") != "yes":
            missed = re.findall(r"^  (\w+) -- ", stage.output, re.M)
            stage.status = ("not VLN-ready: "
                            + (", ".join(missed) if missed else "see its output"))

    # nav2's goal comes from the map, not from memory. A hardcoded one is what
    # went stale: the recorded goal (3.79, 9.02) became an unknown cell two map
    # fixes later and nothing noticed, because nothing re-ran it.
    goal = nav2_goal(yaml) if (os.path.isfile(yaml) and "nav2" not in skip) else None
    if goal is None:
        stages.append(Stage("nav2", []))
        stages[-1].status = ("skipped" if "nav2" in skip
                             else "skipped (no map to pick a goal on)")
    else:
        command = ["wsl", "bash", wsl_path(os.path.join(HERE, "nav2_check.sh")),
                   wsl_path(yaml), f"{goal[0]},{goal[1]}", "0,0"]
        stage = add("nav2", command)
        if stage:
            stage.notes["goal"] = f"{goal[0]},{goal[1]}"
            stage.pick(SAYS["poses"], "poses", "metres")
            if stage.status == "ok" and "EMPTY path" in stage.output:
                stage.status = "nav2 returned an empty path"

    stage = add("drive", [isaac or "python", os.path.join(HERE, "navigate.py"), usdz,
                          "--map", yaml, "--robot", robot, "--headless",
                          "--out", export_dir], env=foreign_python_env())
    if stage:
        stage.pick(SAYS["result"], "result")
        stage.pick(SAYS["tracking"], "mean", "max")
        stage.pick(SAYS["distance"], "planned", "drove")
        if stage.status == "ok" and stage.notes.get("result", "").strip() != "ARRIVED":
            stage.status = "did not arrive: " + stage.notes.get("result", "?").strip()
        elif stage.status == "ok" and float(stage.notes.get("drove", 1.0)) < 0.5:
            # `multiview-meetingroom` reported ARRIVED having moved 0.00 m. On a
            # map with 3.2 m2 of floor the furthest reachable cell is inside the
            # arrival radius of the spawn, so the run succeeds without the robot
            # taking a step. That is true and useless, and a green tick for it is
            # worse than useless.
            stage.status = (f"arrived without moving ({stage.notes.get('drove')} m) "
                            "-- the map is too small for the goal to mean anything")

    return stages


def map_is_usable(report_path, notes):
    """Exit 0 is not the same as "this world produced a map worth having".

    `build_navmap.py` succeeds on any stage PhysX will load. Run across every
    world in the tree, one of them -- a splatfacto reconstruction with no metric
    scale -- came back measuring 74.8 x 70.3 x 20.3 m with its floor 19.5 m up
    and **eight** walkable cells, 0.02 m2, and the stage reported ok. A runner
    that only reads exit codes would have called that a working pipeline.

    Both signals used here are ones the build already computes for itself:
    whether the flood fill kept the region containing the world origin -- where
    the viewer spawns a character, so it is the room -- and how much floor
    survived. Neither is a threshold invented here for the occasion.
    """
    try:
        with open(report_path, encoding="utf-8") as fh:
            built = json.load(fh)
    except (OSError, ValueError) as err:
        return f"could not read the build report: {err}"
    free_m2 = built.get("raycast", {}).get("free_m2")
    if free_m2 is not None:
        notes["free_m2"] = free_m2
    reach = built.get("reachability") or {}
    if reach.get("world_origin_reachable") is False:
        return f"unusable map: nothing walkable at the world origin ({free_m2} m2 free)"
    if free_m2 is not None and free_m2 < 1.0:
        return f"unusable map: only {free_m2} m2 of floor"
    return "ok"


def nav2_goal(yaml_path):
    """The furthest pose nav2's own costmap would let a robot stand on.

    nav2 inflates lethal obstacles by `robot_radius` and leaves NO_INFORMATION
    alone, so this asks the same question its costmap does -- picking a goal by
    any other rule produces a plan failure that looks like a broken map.
    """
    sys.path.insert(0, HERE)
    try:
        import navmap
    except Exception:  # noqa: BLE001 - numpy or Pillow missing is a skip, not a crash
        return None
    try:
        carte = navmap.load(yaml_path)
        passable = carte.passable(0.25, avoid_unknown=False)
        start = carte.cell_of(0.0, 0.0)
        if not (carte.inside(start) and passable[start]):
            moved = navmap.nearest_passable(passable, start)
            if moved is None:
                return None
            start = moved
        room = navmap.clearance(~passable, carte.resolution, cap_m=1.0)
        cell = navmap.furthest_reachable(passable, start, room=room, min_clearance=0.20)
        if cell is None:
            return None
        return tuple(round(float(v), 2) for v in carte.world_of(*cell))
    except SystemExit:
        return None


# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("slugs", nargs="*", help="world slugs under public/worlds/")
    parser.add_argument("--all", action="store_true", help="every world with a scene.json")
    parser.add_argument("--isaac", help="Isaac Sim root, or its python.bat")
    parser.add_argument("--robot", default="spot", help="which policy drives the map")
    parser.add_argument("--lod", default="500k", help="splat LOD passed to the exporter")
    parser.add_argument("--skip", default="", help="comma-separated stages to skip: "
                                                   + ", ".join(STAGES))
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds per stage")
    parser.add_argument("--json", help="write the full report here")
    args = parser.parse_args()

    slugs = list(args.slugs)
    if args.all:
        slugs = sorted(os.path.basename(os.path.dirname(p))
                       for p in glob.glob(os.path.join(WORLDS, "*", "scene.json")))
    if not slugs:
        raise SystemExit("name at least one world slug, or pass --all")

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    unknown = skip - set(STAGES)
    if unknown:
        raise SystemExit(f"unknown stage(s) to skip: {', '.join(sorted(unknown))}")

    isaac = find_isaac(args.isaac)
    if not isaac and not {"map", "drive"} <= skip:
        print("!! no Isaac Sim python.bat found -- pass --isaac, or --skip map,drive")
    if "nav2" not in skip and not wsl_available():
        print("-- no WSL that can see this repo; skipping nav2")
        skip.add("nav2")

    print(f"Isaac Sim: {isaac or 'not found'}")
    print(f"worlds:    {', '.join(slugs)}")
    if skip:
        print(f"skipping:  {', '.join(sorted(skip))}")

    report, failures = {}, 0
    for slug in slugs:
        print(f"\n=== {slug} " + "=" * max(0, 60 - len(slug)))
        stages = one_world(slug, isaac, skip, args.timeout, args.robot, args.lod)
        report[slug] = {}
        for stage in stages:
            notes = "  ".join(f"{k}={v}" for k, v in stage.notes.items())
            mark = "ok " if stage.status == "ok" else (
                "-- " if stage.status.startswith("skipped") else "XX ")
            print(f"  {mark}{stage.name:<7} {stage.seconds:6.1f}s  {stage.status}"
                  + (f"   {notes}" if notes else ""))
            report[slug][stage.name] = {"status": stage.status,
                                        "seconds": round(stage.seconds, 1),
                                        **stage.notes}
            if mark == "XX ":
                failures += 1
                tail = [line for line in stage.output.strip().split("\n") if line.strip()]
                for line in tail[-6:]:
                    print(f"        | {line}")

    print("\n" + "=" * 68)
    for slug, stages in report.items():
        marks = " ".join(f"{n}:{'ok' if s['status'] == 'ok' else ('--' if s['status'].startswith('skipped') else 'XX')}"
                         for n, s in stages.items())
        print(f"  {slug:<40} {marks}")
    print(f"\n{failures} stage(s) failed")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote {args.json}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
