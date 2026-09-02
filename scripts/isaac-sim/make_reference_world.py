"""Build a synthetic multi-room world, so the pipeline's success path can run.

**This is not a reconstruction and it is not training data.** It is a fixture.

Every generated world in this tree fails `vln_check.py`, and until this existed
they all failed for reasons no test could distinguish from "the gate never says
yes to anything". A gate that has only ever refused is the mirror image of the
check-that-cannot-fail this directory keeps unpicking: nothing in the repository
demonstrated that `export -> map -> check -> vln` can be walked all the way to a
pass on a real Isaac Sim build.

So: a small building with rooms and doorways, run through the *unmodified*
pipeline against the *unmodified* criteria. If it passes, then "no world here is
VLN-ready" is a statement about the captures. If it fails, the pipeline has a
limit nobody had found, which is worth more.

    python scripts/isaac-sim/make_reference_world.py
    python scripts/isaac-sim/pipeline.py reference-rooms --skip nav2,drive

The layout is chosen for the one property the captures lack -- topology. Five
rooms open onto a corridor, so crossing the building means going out to the
corridor and along it. Nothing else about it is meant to be realistic: the
walls are boxes and there is no texture, because the map builder only ever sees
collision geometry.

Written Y-up, which is what the GLB->USD exporter expects; it applies the z-up
rotation itself.
"""
from __future__ import annotations

import json
import os

import numpy as np
import trimesh

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SLUG = "reference-rooms"

WIDTH, DEPTH = 16.0, 12.0        # x, z in metres
# Centred on the origin, because the viewer spawns a character at world (0, 0)
# and `pipeline.py` calls a map with nothing walkable there unusable. Built in
# a corner first, and that is exactly what it said.
X0, Z0 = -WIDTH / 2, -DEPTH / 2
WALL, TALL = 0.2, 2.5            # wall thickness and height
# The floor's top surface must not sit at z=0, because `/World/GroundPlane` is
# an infinite collider there and `build_navmap.py` deliberately ignores hits on
# it -- so a coplanar floor makes every raycast a coin flip between the two, and
# the ones that come up GroundPlane leave the cell unmapped. Built flat on zero
# first: the floor came back in patches, and moving the building sideways
# changed which patches, which is what a numerical tie looks like.
LIFT = 0.1
DOOR = 1.0                       # doorway width


def box(x0, x1, y0, y1, z0, z1):
    """An axis-aligned box from two corners, in the centred frame."""
    mesh = trimesh.creation.box(extents=(x1 - x0, y1 - y0, z1 - z0))
    mesh.apply_translation((X0 + (x0 + x1) / 2, LIFT + (y0 + y1) / 2,
                            Z0 + (z0 + z1) / 2))
    return mesh


def wall_with_doors(along_x, fixed, span, doors):
    """A wall broken by doorways, as a list of boxes.

    `doors` are the centres of the gaps; each gap is DOOR wide. Building the
    wall as the segments *between* the gaps rather than subtracting them keeps
    every piece a clean convex box, which is what PhysX wants to cook.
    """
    edges = [span[0]]
    for centre in sorted(doors):
        edges += [centre - DOOR / 2, centre + DOOR / 2]
    edges.append(span[1])
    pieces = []
    for a, b in zip(edges[::2], edges[1::2]):
        if b - a <= 1e-6:
            continue
        if along_x:
            pieces.append(box(a, b, 0.0, TALL, fixed - WALL / 2, fixed + WALL / 2))
        else:
            pieces.append(box(fixed - WALL / 2, fixed + WALL / 2, 0.0, TALL, a, b))
    return pieces


def build():
    # One continuous slab under everything. Walls sit on top of it, which is
    # what makes them read as obstacle rather than as missing floor: the
    # downward ray finds the slab and the upward box probe then finds the wall.
    # A wall standing on bare ground would be a column of unknown instead --
    # still impassable, but it would show up in the map as a hole in the
    # reconstruction, which is a different claim.
    floor = [box(0.0, WIDTH, -0.1, 0.0, 0.0, DEPTH)]

    walls = []
    # Outside.
    walls += wall_with_doors(True, 0.0, (0.0, WIDTH), [])
    walls += wall_with_doors(True, DEPTH, (0.0, WIDTH), [])
    walls += wall_with_doors(False, 0.0, (0.0, DEPTH), [])
    walls += wall_with_doors(False, WIDTH, (0.0, DEPTH), [])
    # The corridor runs east-west between z=5 and z=7. Every room opens onto it
    # and onto nothing else, so any cross-building route has to use it.
    walls += wall_with_doors(True, 5.0, (0.0, WIDTH), [3.0, 11.5])
    walls += wall_with_doors(True, 7.0, (0.0, WIDTH), [2.5, 8.0, 13.5])
    # Between the two south rooms, and between the three north ones.
    walls += wall_with_doors(False, 6.5, (0.0, 5.0), [])
    walls += wall_with_doors(False, 5.0, (7.0, DEPTH), [])
    walls += wall_with_doors(False, 11.0, (7.0, DEPTH), [])

    # Something to walk around, so the rooms are not empty boxes. Heights are
    # above `--floor-tolerance` (0.12 m) and below the 0.72 m the upward probe
    # reaches, which is the band the map can actually see.
    stuff = [
        box(1.0, 3.0, 0.0, 0.75, 1.0, 2.0),      # table, room A
        box(4.5, 5.5, 0.0, 0.45, 3.0, 4.0),      # seat, room A
        box(8.0, 12.0, 0.0, 0.75, 1.5, 2.5),     # long desk, room B
        box(14.0, 15.0, 0.0, 0.60, 3.0, 4.0),    # cabinet, room B
        box(1.0, 2.0, 0.0, 0.60, 9.0, 11.0),     # shelf, room C
        box(7.0, 9.0, 0.0, 0.75, 9.5, 10.5),     # table, room D
        box(12.5, 15.0, 0.0, 0.45, 10.5, 11.5),  # bench, room E
    ]

    scene = trimesh.Scene()
    for name, parts in (("floor", floor), ("wall", walls), ("elevated", stuff)):
        # One mesh per collider part: the exporter names USD meshes after the
        # GLB geometries, and `build_navmap.py` looks for a prim called `floor`.
        scene.add_geometry(trimesh.util.concatenate(parts), geom_name=name,
                           node_name=name)
    return scene


def main():
    world_dir = os.path.join(ROOT, "public", "worlds", SLUG)
    out_dir = os.path.join(world_dir, "output", "world")
    os.makedirs(out_dir, exist_ok=True)

    scene = build()
    glb = os.path.join(out_dir, "0-world.glb")
    scene.export(glb)
    floor_m2 = (WIDTH * DEPTH)
    print(f"wrote {glb}  ({os.path.getsize(glb) / 1024:.0f} KB, "
          f"{floor_m2:.0f} m2 footprint, {len(scene.geometry)} collider parts)")

    # No splat: this world has no appearance, and `pipeline.py` exports with
    # --no-splat anyway. Scale 1.0 and offset 0.0 because the geometry is
    # already in metres with its floor at y=0, which is the whole point of
    # generating it rather than reconstructing it.
    with open(os.path.join(out_dir, "0-world.json"), "w", encoding="utf-8") as fh:
        json.dump({"world_id": SLUG, "display_name": "Reference rooms",
                   "assets": {"splats": {"spz_urls": {},
                                         "semantics_metadata": {
                                             "metric_scale_factor": 1.0,
                                             "ground_plane_offset": 0.0,
                                             "flip_y": False}}}}, fh, indent=2)
    with open(os.path.join(world_dir, "scene.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "instances": [],
                   "sun": {"intensity": 1, "rotation": [0, 0, 0],
                           "environmentIntensity": 1},
                   "metricScaleFactor": 1.0, "groundPlaneOffset": 0.0,
                   "groundPlaneColliderEnabled": True}, fh, indent=2)
    with open(os.path.join(world_dir, "project.json"), "w", encoding="utf-8") as fh:
        json.dump({"slug": SLUG, "display_name": "Reference rooms",
                   "created_at": "2026-09-03T00:00:00.000Z",
                   "updated_at": "2026-09-03T00:00:00.000Z",
                   "notes": "Synthetic fixture, not a reconstruction and not "
                            "training data. Five rooms off a corridor, built by "
                            "scripts/isaac-sim/make_reference_world.py so the "
                            "pipeline's success path can be exercised."}, fh, indent=2)
    print(f"wrote the manifests -- now run "
          f"`python scripts/isaac-sim/pipeline.py {SLUG} --skip nav2,drive`")


if __name__ == "__main__":
    main()
