"""Regression tests for the collider surface split.

The GLB node names these produce are a contract with the viewer:
`src/modules/collider/surfaces.ts` reads them to decide friction, and a world
whose nodes it does not recognise silently falls back to uniform collision.
"""

import gzip
import json
import struct

import numpy as np
import pytest
import trimesh

from backend.tools.build_collider import (
    FLOOR_BAND,
    SPZ_MAGIC,
    SPZ_SH_DIM,
    SURFACE_CLASSES,
    classify_surfaces,
    detect_floor_height,
    detect_ground_offset,
    find_splat_file,
    labeled_collider_scene,
    read_splat_spz,
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


def make_spz(
    xyz: np.ndarray,
    alpha_bytes: np.ndarray,
    *,
    sh_degree: int = 0,
    fractional_bits: int = 12,
    version: int = 2,
    magic: int = SPZ_MAGIC,
) -> bytes:
    """A minimal SPZ, laid out the way Marble's files are.

    Positions and alpha carry the values under test; the colour, scale and
    rotation planes only have to be the right length, since the reader skips
    them to get at the two planes a collider needs.
    """
    count = len(xyz)
    header = struct.pack("<IIIBBBB", magic, version, count, sh_degree, fractional_bits, 0, 0)
    fixed = np.round(np.asarray(xyz) * (1 << fractional_bits)).astype(np.int64) & 0xFFFFFF
    positions = np.stack(
        [fixed & 0xFF, (fixed >> 8) & 0xFF, (fixed >> 16) & 0xFF], axis=-1
    ).astype(np.uint8)
    others = np.zeros((count, 9 + SPZ_SH_DIM[sh_degree] * 3), dtype=np.uint8)
    body = positions.tobytes() + np.asarray(alpha_bytes, dtype=np.uint8).tobytes() + others.tobytes()
    return gzip.compress(header + body)


@pytest.fixture
def spz_path(tmp_path):
    def write(data: bytes, name: str = "0-world-500k.spz"):
        path = tmp_path / name
        path.write_bytes(data)
        return path
    return write


def test_spz_round_trips_positions_and_opacity(spz_path):
    # Multiples of 1/4096 so the fixed-point encoding is exact and the test is
    # measuring the decoder rather than the quantiser.
    xyz = np.array([[0.0, 0.25, -1.5], [-3.75, 2.0, 0.125]], dtype=np.float32)
    alpha_bytes = np.array([255, 64], dtype=np.uint8)

    decoded_xyz, opacity = read_splat_spz(spz_path(make_spz(xyz, alpha_bytes)))

    np.testing.assert_allclose(decoded_xyz, xyz, atol=1e-6)
    # `build_collider` puts this straight through a sigmoid, so that is where
    # the linear alpha Marble stored has to reappear.
    alpha = 1.0 / (1.0 + np.exp(-opacity))
    np.testing.assert_allclose(alpha, [1.0, 64 / 255], atol=1e-4)


def test_spz_sign_extends_negative_positions(spz_path):
    # 24-bit two's complement: the sign bit is 0x800000, not 0x80000000, so a
    # decoder that forgets to extend it reads -1.0 as +4095.0.
    xyz = np.array([[-1.0, -0.5, -2047.0]], dtype=np.float32)
    decoded, _ = read_splat_spz(spz_path(make_spz(xyz, np.array([255]))))
    np.testing.assert_allclose(decoded, xyz, atol=1e-6)


def test_spz_rejects_a_file_that_is_not_spz(spz_path):
    with pytest.raises(ValueError, match="magic"):
        read_splat_spz(spz_path(make_spz(np.zeros((1, 3)), np.array([255]), magic=0xDEADBEEF)))


def test_spz_rejects_an_unsupported_version(spz_path):
    with pytest.raises(ValueError, match="version"):
        read_splat_spz(spz_path(make_spz(np.zeros((1, 3)), np.array([255]), version=3)))


def test_spz_rejects_an_uncompressed_file(spz_path):
    # SPZ is always gzip; a bare header means something truncated the stream.
    raw = make_spz(np.zeros((1, 3)), np.array([255]))
    with pytest.raises(ValueError, match="gzip"):
        read_splat_spz(spz_path(gzip.decompress(raw)))


def test_spz_rejects_a_truncated_payload(spz_path):
    raw = gzip.decompress(make_spz(np.zeros((4, 3)), np.full(4, 255)))
    with pytest.raises(ValueError, match="payload bytes"):
        read_splat_spz(spz_path(gzip.compress(raw[:-5])))


def test_spz_stride_accounts_for_spherical_harmonics(spz_path):
    # A degree-2 file is 24 bytes wider per gaussian. The planes the reader
    # wants are still at the front, so it must not mistake the extra tail for
    # a length mismatch.
    xyz = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
    decoded, _ = read_splat_spz(spz_path(make_spz(xyz, np.array([200]), sh_degree=2)))
    np.testing.assert_allclose(decoded, xyz, atol=1e-6)


def test_find_splat_file_prefers_full_resolution_ply(tmp_path):
    # A SHARP world carries every LOD as PLY; full_res is the one to build from.
    for name in ("0-world-100k.ply", "0-world-500k.ply", "0-world-full_res.ply"):
        (tmp_path / name).touch()
    assert find_splat_file(tmp_path).name == "0-world-full_res.ply"


def test_find_splat_file_walks_down_the_spz_ladder(tmp_path):
    # Marble's bundled sample ships only the two smaller LODs, so demanding
    # full_res would find nothing at all.
    (tmp_path / "0-world-100k.spz").touch()
    (tmp_path / "0-world-500k.spz").touch()
    assert find_splat_file(tmp_path).name == "0-world-500k.spz"


def test_find_splat_file_reports_nothing_for_an_empty_world(tmp_path):
    (tmp_path / "0-world.glb").touch()
    assert find_splat_file(tmp_path) is None
