"""Regression tests for the collider surface split.

The GLB node names these produce are a contract with the viewer:
`src/modules/collider/surfaces.ts` reads them to decide friction, and a world
whose nodes it does not recognise silently falls back to uniform collision.
"""

import json
import struct

import numpy as np
import pytest
import trimesh

from backend.tools.build_collider import (
    FLOOR_BAND,
    SURFACE_CLASSES,
    classify_surfaces,
    detect_floor_height,
    detect_ground_offset,
    labeled_collider_scene,
)

FLOOR_TOP = 0.05
DESK_TOP = 0.775


@pytest.fixture
def room() -> trimesh.Trimesh:
    """A Y-up room: floor slab, one wall, and a desk standing above the floor.

    Every box contributes faces of several classes at once -- a slab has an
    upward top, a downward underside and vertical sides -- which is exactly the
    mixture the classifier has to pull apart on a real mesh.
    """
    floor = trimesh.creation.box(extents=(4.0, 0.1, 4.0))
    wall = trimesh.creation.box(extents=(4.0, 3.0, 0.1))
    wall.apply_translation((0.0, 1.5, 2.0))
    desk = trimesh.creation.box(extents=(1.0, 0.05, 1.0))
    desk.apply_translation((0.0, 0.75, 0.0))
    room = trimesh.util.concatenate([floor, wall, desk])
    # A box top is two triangles, and detect_floor_height wants at least ten
    # upward faces before it will commit to a floor. Subdividing gives the
    # fixture the face density a marching-cubes mesh has, without changing any
    # normal or height the assertions below depend on.
    return room.subdivide().subdivide()


def test_floor_height_finds_the_slab_top(room: trimesh.Trimesh):
    # flip_y=False: the room is built Y-up already, the way the viewer sees it.
    assert detect_floor_height(room, flip_y=False) == pytest.approx(FLOOR_TOP, abs=0.1)


def test_ground_offset_still_negates_the_floor_height(room: trimesh.Trimesh):
    # detect_ground_offset was refactored to share the floor search with
    # classification; it must keep returning the value that puts the floor on 0.
    floor_y = detect_floor_height(room, flip_y=False)
    assert detect_ground_offset(room, flip_y=False) == pytest.approx(-floor_y)


def test_classification_conserves_every_face(room: trimesh.Trimesh):
    groups = classify_surfaces(room, floor_y=FLOOR_TOP, flip_y=False)
    covered = np.concatenate(list(groups.values()))
    assert len(covered) == len(room.faces)
    assert len(np.unique(covered)) == len(room.faces), "a face landed in two classes"


def test_classes_match_what_the_geometry_should_be(room: trimesh.Trimesh):
    groups = classify_surfaces(room, floor_y=FLOOR_TOP, flip_y=False)
    normals = room.face_normals
    centers = room.triangles_center

    for face in groups["floor"]:
        assert normals[face][1] > 0, "floor faces point up"
        assert centers[face][1] <= FLOOR_TOP + FLOOR_BAND

    for face in groups["elevated"]:
        assert normals[face][1] > 0, "elevated faces point up too"
        assert centers[face][1] > FLOOR_TOP + FLOOR_BAND

    for face in groups["ceiling"]:
        assert normals[face][1] < 0, "ceiling faces point down"

    for face in groups["wall"]:
        assert abs(normals[face][1]) < 0.6, "wall faces are near-vertical"

    # The desk top is the reason `elevated` exists; it must not read as floor.
    elevated_heights = centers[groups["elevated"]][:, 1]
    assert np.any(np.isclose(elevated_heights, DESK_TOP, atol=0.01))


def test_no_detected_floor_calls_every_upward_face_floor(room: trimesh.Trimesh):
    # The conservative fallback: floor friction is the high one, so a room with
    # no recognisable floor walks the way it did before the split existed.
    groups = classify_surfaces(room, floor_y=None, flip_y=False)
    assert "elevated" not in groups
    assert len(groups["floor"]) > 0


def test_flip_y_swaps_floor_and_ceiling(room: trimesh.Trimesh):
    # SHARP's +Y points down, so reading its output as-is would label the
    # ceiling as the floor. The flag is what keeps the two apart.
    upright = classify_surfaces(room, floor_y=FLOOR_TOP, flip_y=False)
    flipped = classify_surfaces(room, floor_y=-FLOOR_TOP, flip_y=True)
    # Every face that faces up one way faces down the other. `floor` and
    # `elevated` are the same orientation split by height, so both go over.
    assert set(upright["floor"]) | set(upright["elevated"]) == set(flipped["ceiling"])


def glb_node_names(data: bytes) -> list[str]:
    """Node names from a GLB's JSON chunk — what GLTFLoader puts on Object3D."""
    assert data[:4] == b"glTF"
    offset = 12
    while offset < len(data):
        length, kind = struct.unpack_from("<II", data, offset)
        if kind == 0x4E4F534A:  # 'JSON'
            gltf = json.loads(data[offset + 8: offset + 8 + length].decode("utf-8"))
            return [node.get("name") for node in gltf.get("nodes", [])]
        offset += 8 + length + (-length % 4)
    raise AssertionError("no JSON chunk in GLB")


def test_export_names_nodes_after_their_class(room: trimesh.Trimesh):
    groups = classify_surfaces(room, floor_y=FLOOR_TOP, flip_y=False)
    names = glb_node_names(labeled_collider_scene(room, groups).export(file_type="glb"))

    for name in groups:
        assert name in names, f"the viewer looks for a node called {name!r}"
    assert set(names) - {"world"} <= set(SURFACE_CLASSES)


def test_export_falls_back_to_one_node_when_unclassified(room: trimesh.Trimesh):
    data = labeled_collider_scene(room, {}).export(file_type="glb")
    # Nothing named after a class, so the viewer takes its legacy path.
    assert not set(glb_node_names(data)) & set(SURFACE_CLASSES)
