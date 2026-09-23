"""Tests for `vlnviews`: where the cameras go, and whether it would notice if
they went somewhere else.

The probe is only worth having if it fails on a wrong frame, so the test that
matters is `test_a_wrong_frame_puts_viewpoints_in_the_walls`. Every viewpoint
of `reference-rooms` passing proves nothing by itself -- a probe that always
said yes would pass it too.

Needs numpy and trimesh (CI installs both) and the committed reference fixture.

Run:
    python -m pytest scripts/isaac-sim/test_vlnviews.py -q
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import vlnviews  # noqa: E402

WORLDS = Path(__file__).resolve().parents[2] / "public" / "worlds"
REFERENCE = WORLDS / "reference-rooms" / "export" / "reference-rooms-vlngraph.json"
needs_reference = pytest.mark.skipif(not REFERENCE.is_file(), reason="reference fixture not checked out")


def test_map_and_viewer_frames_round_trip():
    points = np.array([[1.0, 2.0, 3.0], [-4.0, 0.5, 0.0]])
    viewer = vlnviews.map_to_viewer(points)
    # Map up (+z) is viewer up (+y); map +y is viewer -z.
    np.testing.assert_allclose(viewer[0], [1.0, 3.0, -2.0])
    np.testing.assert_allclose(vlnviews.viewer_to_map(viewer), points)


def test_headings_are_mp3d_clockwise_from_map_y():
    np.testing.assert_allclose(vlnviews.viewer_to_map(vlnviews.heading_vector(0.0)), [0, 1, 0], atol=1e-12)
    np.testing.assert_allclose(vlnviews.viewer_to_map(vlnviews.heading_vector(math.pi / 2)), [1, 0, 0], atol=1e-12)


def test_skybox_faces_close_into_a_cube():
    faces = vlnviews.skybox_rotations()
    assert len(faces) == 6
    for rotation in faces:
        np.testing.assert_allclose(rotation.T @ rotation, np.eye(3), atol=1e-12)
        assert np.linalg.det(rotation) == pytest.approx(1.0)
    up, h000, h090, h180, h270, down = faces
    np.testing.assert_allclose(up[:, 2], [0, 1, 0], atol=1e-12)
    np.testing.assert_allclose(down[:, 2], [0, -1, 0], atol=1e-12)
    # Turning right from one horizon face lands on the next one's forward axis.
    for left, right in ((h000, h090), (h090, h180), (h180, h270), (h270, h000)):
        np.testing.assert_allclose(left[:, 0], right[:, 2], atol=1e-12)
    # The horizon faces share the up face's image-bottom edge on the h000 side.
    np.testing.assert_allclose(up[:, 1], h000[:, 2], atol=1e-12)
    np.testing.assert_allclose(down[:, 1], h180[:, 2], atol=1e-12)


def test_background_matrix_follows_the_viewer_rule():
    # flip_y alone is a half turn about X; an explicit rotation_x wins, even 0.
    flipped = vlnviews.background_matrix(True, None, 1.0, 0.0)
    np.testing.assert_allclose(flipped[:3, :3], np.diag([1, -1, -1]), atol=1e-12)
    np.testing.assert_allclose(vlnviews.background_matrix(True, 0.0, 1.0, 0.0), np.eye(4))
    # Scale first, then the ground offset is added in metres, unscaled.
    matrix = vlnviews.background_matrix(False, None, 2.0, 1.5)
    np.testing.assert_allclose(matrix @ [1, 1, 1, 1], [2, 3.5, 2, 1])


def test_raycast_finds_the_nearest_face_from_either_side():
    floor = np.array([[[-1, 0, -1], [1, 0, -1], [0, 0, 1]]], dtype=np.float64)
    shelf = floor + [0, 1, 0]
    triangles = np.concatenate([shelf, floor])
    distance, index = vlnviews.raycast(triangles, [[0, 2, 0], [0, 0.5, 0]], [[0, -1, 0], [0, -1, 0]])
    np.testing.assert_allclose(distance, [1.0, 0.5])
    assert index.tolist() == [0, 1]
    distance, index = vlnviews.raycast(triangles, [[5, 2, 5]], [[0, -1, 0]])
    assert distance[0] == np.inf and index[0] == -1


@needs_reference
def test_every_reference_viewpoint_stands_on_floor_with_room_to_look():
    summary, viewpoints, connectivity, episodes, job, _ = vlnviews.build("reference-rooms", WORLDS)
    assert summary["included"] == summary["viewpoints"] == 212
    assert all(vp["floor_label"] == "floor" for vp in viewpoints)
    assert min(vp["clearance"] for vp in viewpoints) >= 0.25 - 0.05
    assert len(episodes) == 100
    # Eye at EYE_HEIGHT above the floor it stands on, in the map frame.
    for vp, entry in zip(viewpoints, connectivity):
        assert entry["pose"][11] == pytest.approx(vp["floor_z"] + vlnviews.EYE_HEIGHT, abs=1e-4)


@needs_reference
def test_connectivity_is_the_graph_in_mp3d_shape():
    import json

    graph = json.loads(REFERENCE.read_text(encoding="utf-8"))
    _, _, connectivity, episodes, job, _ = vlnviews.build("reference-rooms", WORLDS)
    ids = [entry["image_id"] for entry in connectivity]
    for entry in connectivity:
        assert set(entry) == {"image_id", "pose", "height", "included", "unobstructed", "visible"}
        assert len(entry["pose"]) == 16 and len(entry["unobstructed"]) == len(ids)
    links = sum(sum(entry["unobstructed"]) for entry in connectivity)
    assert links == 2 * len(graph["graph"]["edges"])
    for a, entry in enumerate(connectivity):
        for b, linked in enumerate(entry["unobstructed"]):
            assert linked == connectivity[b]["unobstructed"][a]
    for episode in episodes:
        path = episode["path"]
        assert all(p in ids for p in path)
        for a, b in zip(path, path[1:]):
            assert connectivity[ids.index(a)]["unobstructed"][ids.index(b)]
        assert episode["instructions"] == []
    assert len(job["cameras"]) == 212 and len(job["cameras"][0]["world_to_camera"]) == 6


@needs_reference
def test_a_wrong_frame_puts_viewpoints_in_the_walls(monkeypatch):
    right = vlnviews.map_to_viewer
    # Mirroring the map, and separately shifting it by 30 cm: both are the
    # kind of mistake a frame conversion makes, and both must be caught.
    for wrong in (lambda p: right(np.asarray(p) * [1, -1, 1]),
                  lambda p: right(np.asarray(p) + [0.3, 0, 0])):
        monkeypatch.setattr(vlnviews, "map_to_viewer", wrong)
        summary, *_ = vlnviews.build("reference-rooms", WORLDS)
        assert summary["excluded"] >= 20, summary


@needs_reference
def test_labels_see_floor_below_and_walls_around():
    summary, viewpoints, connectivity, _, job, labels = vlnviews.build(
        "reference-rooms", WORLDS, labels_size=8)
    names = summary["labels"]
    label, depth = labels[job["cameras"][0]["image_id"]]
    assert label.shape == (6, 8, 8)
    down_centre = label[5, 3:5, 3:5]
    assert {names[i] for i in down_centre.ravel()} == {"floor"}
    np.testing.assert_allclose(depth[5, 3:5, 3:5], vlnviews.EYE_HEIGHT, atol=0.05)
    horizon = {names[i] for i in label[1:5].ravel() if i >= 0}
    assert "wall" in horizon
