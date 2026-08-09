"""Regression tests for the multi-view merge maths.

Every check here is on synthetic data with a known answer, and none of them
needs a GPU, a model download or COLMAP. That is deliberate: the parts of the
multi-view path that fail *loudly* (SfM not converging, SHARP not loading) are
obvious in a run, while the parts that fail *silently* are all arithmetic --
a scale off by a constant factor, a quaternion convention mismatch, a fuse that
keeps the wrong candidate. Those produce a world that looks plausible and is
wrong, so they are what gets pinned down here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent / "tools"))

from multiview_sfm import (  # noqa: E402
    FrameCamera,
    SparseScene,
    apply_metric_scale,
    calibrate_metric_scale,
    select_keyframes,
)
from multiview_splat import (  # noqa: E402
    FrameGaussians,
    _matrix_to_quaternion,
    _quaternion_to_matrix,
    camera_centres_in_reference,
    carve_free_space,
    estimate_up_direction,
    fuse_gaussians,
    level_rotation,
)

WIDTH, HEIGHT, FOCAL = 1920, 1080, 1400.0
INTRINSICS = np.array([[FOCAL, 0, WIDTH / 2], [0, FOCAL, HEIGHT / 2], [0, 0, 1]])


def _straight_walk(true_scale: float, frame_count: int = 4, depth_noise: float = 0.02):
    """A camera sliding along +x, plus SHARP-style metric depth maps.

    The depth maps are deliberately at half the frame resolution, because that
    mismatch is real -- SHARP predicts on its own internal grid -- and getting
    the resampling wrong is exactly the kind of silent scale error worth
    catching.
    """
    rng = np.random.default_rng(0)
    points = np.column_stack([
        rng.uniform(-3, 3, 800), rng.uniform(-2, 2, 800), rng.uniform(2, 8, 800),
    ])

    frames, depth_maps = [], {}
    for index in range(frame_count):
        world_to_camera = np.eye(4)
        world_to_camera[0, 3] = -0.5 * index

        camera_points = points + world_to_camera[:3, 3]
        depth = camera_points[:, 2]
        u = FOCAL * camera_points[:, 0] / depth + WIDTH / 2
        v = FOCAL * camera_points[:, 1] / depth + HEIGHT / 2
        visible = (depth > 0.1) & (u >= 0) & (u < WIDTH) & (v >= 0) & (v < HEIGHT)

        name = f"f{index}.jpg"
        frames.append(FrameCamera(
            name=name, path=Path(name), world_to_camera=world_to_camera,
            intrinsics=INTRINSICS, width=WIDTH, height=HEIGHT,
            point3d_ids=np.flatnonzero(visible).astype(np.int64),
        ))

        depth_map = np.zeros((HEIGHT // 2, WIDTH // 2))
        noise = 1 + rng.normal(0, depth_noise, visible.sum())
        depth_map[(v[visible] * 0.5).astype(int), (u[visible] * 0.5).astype(int)] = (
            depth[visible] * true_scale * noise
        )
        depth_maps[name] = depth_map

    scene = SparseScene(
        frames=frames, points_xyz=points,
        point_ids=np.arange(len(points), dtype=np.int64),
    )
    return scene, depth_maps


@pytest.mark.parametrize("true_scale", [0.35, 1.0, 3.7, 12.5])
def test_metric_scale_recovered(true_scale):
    """COLMAP's arbitrary units must resolve back to metres within a percent."""
    scene, depth_maps = _straight_walk(true_scale)
    estimated = calibrate_metric_scale(scene, depth_maps)
    assert estimated == pytest.approx(true_scale, rel=0.02)


def test_metric_scale_refuses_when_no_overlap():
    """No depth correspondences must raise, not silently return a wrong scale."""
    scene, _ = _straight_walk(2.0)
    empty = {frame.name: np.zeros((10, 10)) for frame in scene.frames}
    with pytest.raises(RuntimeError, match="metric scale"):
        calibrate_metric_scale(scene, empty)


def test_apply_metric_scale_moves_cameras_but_not_orientation():
    scene, _ = _straight_walk(1.0)
    scaled = apply_metric_scale(scene, 4.0)

    before = np.linalg.norm(
        scene.frames[1].camera_centre - scene.frames[0].camera_centre)
    after = np.linalg.norm(
        scaled.frames[1].camera_centre - scaled.frames[0].camera_centre)
    assert after == pytest.approx(before * 4.0)

    for original, rescaled in zip(scene.frames, scaled.frames):
        assert np.allclose(original.world_to_camera[:3, :3],
                           rescaled.world_to_camera[:3, :3])


def test_quaternion_matrix_roundtrip():
    """Both SVD branches must round-trip -- the trace<=0 one is the fragile half."""
    rng = np.random.default_rng(7)
    quaternions = rng.normal(size=(4000, 4))
    quaternions /= np.linalg.norm(quaternions, axis=1, keepdims=True)
    quaternions[quaternions[:, 0] < 0] *= -1

    matrices = _quaternion_to_matrix(quaternions)
    traces = matrices[:, 0, 0] + matrices[:, 1, 1] + matrices[:, 2, 2]
    assert (traces <= 0).sum() > 100, "sample never exercises the trace<=0 branch"

    recovered = _matrix_to_quaternion(matrices)
    recovered[recovered[:, 0] < 0] *= -1
    assert np.abs(quaternions - recovered).max() < 1e-9

    identity = matrices @ matrices.transpose(0, 2, 1)
    assert np.abs(identity - np.eye(3)).max() < 1e-9
    assert np.allclose(np.linalg.det(matrices), 1.0)


def test_fuse_keeps_the_nearest_observation():
    """Where two views overlap, the closer camera's Gaussian must survive.

    SHARP degrades with range -- far surfaces arrive as stretched streaks -- so
    a fuse that kept the farther candidate would actively make the merge worse
    than a single view in the overlap.
    """
    positions = np.array([[0.0, 0, 0], [1, 0, 0], [5, 5, 5]])
    near_colour = np.tile([0.9, 0.1, 0.1], (3, 1))
    far_colour = np.tile([0.1, 0.1, 0.9], (3, 1))

    fused = fuse_gaussians(
        positions=[positions, positions.copy()],
        quaternions=[np.tile([1.0, 0, 0, 0], (3, 1))] * 2,
        scales=[np.ones((3, 3)) * 0.01] * 2,
        colors=[far_colour, near_colour],
        opacities=[np.ones(3) * 0.9] * 2,
        source_distance=[np.full(3, 9.0), np.full(3, 1.0)],
        voxel_size=0.05,
    )

    assert len(fused["xyz"]) == 3
    assert np.allclose(fused["colors"], near_colour[0]), "kept the farther view"


def test_fuse_preserves_distinct_surfaces():
    """Points further apart than a voxel must not be merged into one."""
    positions = np.arange(30, dtype=float).reshape(10, 3) * 0.5
    fused = fuse_gaussians(
        positions=[positions],
        quaternions=[np.tile([1.0, 0, 0, 0], (10, 1))],
        scales=[np.ones((10, 3)) * 0.01],
        colors=[np.ones((10, 3)) * 0.5],
        opacities=[np.ones(10) * 0.9],
        source_distance=[np.ones(10)],
        voxel_size=0.02,
    )
    assert len(fused["xyz"]) == 10


def test_select_keyframes_spreads_across_the_walk():
    """Keyframes must sample the whole trajectory, not cluster where it paused."""
    centres = np.array([[0.0, 0, 0]] * 8 + [[float(i), 0, 0] for i in range(1, 5)])
    frames = [
        FrameCamera(f"f{i}.jpg", Path(f"f{i}.jpg"),
                    np.block([[np.eye(3), -centre.reshape(3, 1)], [np.zeros(3), 1]]),
                    INTRINSICS, WIDTH, HEIGHT)
        for i, centre in enumerate(centres)
    ]
    scene = SparseScene(frames=frames, points_xyz=np.empty((0, 3)),
                        point_ids=np.empty(0, dtype=np.int64))

    selected = select_keyframes(scene, 4)
    chosen = np.array([frame.camera_centre for frame in selected])
    assert len(selected) == 4
    # The eight duplicated poses at the origin must not take more than one slot.
    at_origin = (np.linalg.norm(chosen, axis=1) < 1e-6).sum()
    assert at_origin <= 1, f"{at_origin} keyframes collapsed onto the same pose"


def test_select_keyframes_returns_all_when_asked_for_more():
    scene, _ = _straight_walk(1.0, frame_count=3)
    assert len(select_keyframes(scene, 10)) == 3


# --- free-space carving -----------------------------------------------------

DEPTH_W, DEPTH_H, DEPTH_F = 256, 256, 200.0
DEPTH_K = np.array([[DEPTH_F, 0, DEPTH_W / 2], [0, DEPTH_F, DEPTH_H / 2], [0, 0, 1]])
WALL_Z = 10.0


def _wall_scene():
    """Three cameras looking at one flat wall, world = camera 0's frame.

    Every frame's depth map sees only the wall. Anything a merge places well in
    front of it is, by construction, a floater.
    """
    predictions, reference_to_camera = [], {}
    for index, offset_x in enumerate([0.0, 2.0, -2.0]):
        transform = np.eye(4)
        transform[0, 3] = -offset_x          # world (=cam0) -> this camera

        # Depth of the wall along each pixel's ray, in this camera.
        u, v = np.meshgrid(np.arange(DEPTH_W), np.arange(DEPTH_H))
        # The wall is the plane z = WALL_Z in world space; in a purely
        # translated camera it stays the plane z = WALL_Z.
        depth_map = np.full((DEPTH_H, DEPTH_W), WALL_Z)

        predictions.append(FrameGaussians(
            frame=FrameCamera(f"c{index}.jpg", Path(f"c{index}.jpg"), transform,
                              DEPTH_K, DEPTH_W, DEPTH_H),
            xyz=np.empty((0, 3)), scales=np.empty((0, 3)),
            quaternions=np.empty((0, 4)), colors=np.empty((0, 3)),
            opacities=np.empty(0),
            depth_map=depth_map, depth_intrinsics=DEPTH_K,
        ))
        reference_to_camera[f"c{index}.jpg"] = transform
    return predictions, reference_to_camera


def test_carving_removes_floaters_and_keeps_the_wall():
    predictions, transforms = _wall_scene()

    wall = np.column_stack([
        np.linspace(-1, 1, 50), np.linspace(-1, 1, 50), np.full(50, WALL_Z),
    ])
    floaters = np.column_stack([
        np.linspace(-0.5, 0.5, 20), np.zeros(20), np.full(20, 5.0),
    ])
    points = np.vstack([wall, floaters])

    keep = carve_free_space(points, predictions, transforms,
                            margin=0.2, min_violations=2)

    assert keep[:len(wall)].all(), "carved away real surface"
    assert not keep[len(wall):].any(), "kept floaters in front of an observed wall"


def test_carving_spares_points_behind_the_wall():
    """Occluded geometry must survive -- it is unobserved, not contradicted."""
    predictions, transforms = _wall_scene()
    behind = np.column_stack([
        np.linspace(-0.5, 0.5, 20), np.zeros(20), np.full(20, WALL_Z + 3),
    ])
    assert carve_free_space(behind, predictions, transforms,
                            margin=0.2, min_violations=2).all()


def test_carving_needs_a_quorum():
    """One dissenting view is not enough -- occlusion edges produce false votes."""
    predictions, transforms = _wall_scene()
    floater = np.array([[0.0, 0.0, 5.0]])

    assert not carve_free_space(floater, predictions, transforms,
                                margin=0.2, min_violations=2)[0]
    # Raise the quorum above the number of frames that can possibly object and
    # the same point has to survive.
    assert carve_free_space(floater, predictions, transforms,
                            margin=0.2, min_violations=4)[0]


def test_carving_respects_margin():
    """A point just barely in front of the wall is noise, not a floater."""
    predictions, transforms = _wall_scene()
    near_wall = np.array([[0.0, 0.0, WALL_Z - 0.1]])

    assert carve_free_space(near_wall, predictions, transforms,
                            margin=0.5, min_violations=2)[0], "carved within noise"
    assert not carve_free_space(near_wall, predictions, transforms,
                                margin=0.02, min_violations=2)[0]


def test_carving_ignores_unobserved_pixels():
    """Zero depth means "no coverage", which is not evidence of empty space."""
    predictions, transforms = _wall_scene()
    for prediction in predictions:
        prediction.depth_map[:] = 0.0

    floater = np.array([[0.0, 0.0, 5.0]])
    assert carve_free_space(floater, predictions, transforms,
                            margin=0.2, min_violations=2)[0]


# --- levelling ---------------------------------------------------------------

def _tilted_room(tilt_deg: float, seed: int = 0):
    """A floor slab plus a walking camera track, both tilted by the same angle.

    Mirrors the real failure: the world frame sits on a handheld camera, so
    floor and trajectory tilt together and nothing in the scene is upright.
    """
    rng = np.random.default_rng(seed)
    angle = np.radians(tilt_deg)
    tilt = np.array([[1, 0, 0],
                     [0, np.cos(angle), -np.sin(angle)],
                     [0, np.sin(angle), np.cos(angle)]])

    floor = np.column_stack([
        rng.uniform(-6, 6, 6000), np.zeros(6000), rng.uniform(-6, 6, 6000),
    ])
    walls = np.column_stack([
        rng.choice([-6, 6], 2000), rng.uniform(-2.4, 0, 2000), rng.uniform(-6, 6, 2000),
    ])
    cloud = np.vstack([floor, walls]) @ tilt.T

    # An arc, not a straight line. Someone walking a room turns; a track that
    # only goes straight has singular values [big, ~0, ~0] and defines no
    # plane, which is exactly the degenerate case the estimator must reject.
    step = np.linspace(0, 1.5 * np.pi, 40)
    track = np.column_stack([
        np.sin(step) * 3.0, np.full(40, -1.6), np.cos(step) * 3.0,
    ]) @ tilt.T
    return cloud, track, tilt


@pytest.mark.parametrize("tilt_deg", [0.0, 2.0, 5.0, 12.0])
def test_levelling_recovers_the_tilt(tilt_deg):
    cloud, track, _ = _tilted_room(tilt_deg)
    up, report = estimate_up_direction(cloud, track)
    assert report["tilt_deg"] == pytest.approx(tilt_deg, abs=0.5)
    assert report["source"].startswith("trajectory"), \
        f"expected the trajectory to lead, got {report['source']}"

    rotation = level_rotation(up)
    levelled = cloud @ rotation.T
    # After levelling the floor slab must be flat in Y.
    floor_after = levelled[:6000, 1]
    assert floor_after.std() < 0.05, f"floor still sloped: std {floor_after.std():.3f}"


def test_levelling_is_stable_across_subsets():
    """The estimate must not move when an unrelated parameter changes.

    Floor RANSAC on a real room swung between 2.5 and 5.4 degrees purely
    because the keyframe count changed the point set it saw -- an estimate that
    drifts with an unrelated parameter cannot be the primary one. The
    trajectory does not depend on the cloud at all, which is why it leads.
    """
    cloud, track, _ = _tilted_room(5.0)
    rng = np.random.default_rng(4)

    tilts = []
    for fraction in (0.25, 0.5, 1.0):
        subset = cloud[rng.permutation(len(cloud))[:int(len(cloud) * fraction)]]
        _, report = estimate_up_direction(subset, track)
        tilts.append(report["tilt_deg"])

    assert max(tilts) - min(tilts) < 0.25, f"estimate drifted across subsets: {tilts}"


def test_levelling_leaves_yaw_alone():
    """Only pitch and roll should change -- the scene keeps its facing."""
    cloud, track, _ = _tilted_room(6.0)
    up, _ = estimate_up_direction(cloud, track)
    rotation = level_rotation(up)
    # A rotation that only fixes tilt moves the vertical axis but leaves the
    # component of a horizontal heading in the ground plane essentially intact.
    heading = np.array([0.0, 0.0, 1.0])
    rotated = rotation @ heading
    assert abs(rotated[0]) < 0.02, "introduced yaw"


def test_levelling_declines_when_evidence_is_bad():
    """A straight-line walk with no visible floor must not be guessed at."""
    rng = np.random.default_rng(1)
    blob = rng.normal(size=(3000, 3)) * 2.0            # no dominant plane
    straight = np.column_stack([                        # a line, not a plane
        np.linspace(-3, 3, 30), np.zeros(30), np.zeros(30),
    ])
    up, report = estimate_up_direction(blob, straight)
    assert report.get("tilt_deg") is None
    assert np.allclose(up, [0.0, -1.0, 0.0]), "rotated the scene on bad evidence"


def test_camera_centres_land_in_the_reference_frame():
    """The centre must match transforming the true world-space centre by hand.

    Guards a bug that survived a spot check: the reference camera maps to the
    origin under both the correct formula and an extra inversion, so checking
    "does the reference camera sit at 0?" proves nothing. Only the other
    cameras expose it.
    """
    rng = np.random.default_rng(11)

    def random_pose():
        quaternion = rng.normal(size=4)
        quaternion /= np.linalg.norm(quaternion)
        pose = np.eye(4)
        pose[:3, :3] = _quaternion_to_matrix(quaternion[None])[0]
        pose[:3, 3] = rng.normal(size=3) * 3
        return pose

    reference = random_pose()
    others = [random_pose() for _ in range(5)]

    centres = camera_centres_in_reference([reference] + others, reference)
    assert np.allclose(centres[0], 0), "reference camera is not at its own origin"

    for pose, centre in zip(others, centres[1:]):
        in_world = -pose[:3, :3].T @ pose[:3, 3]
        expected = reference[:3, :3] @ in_world + reference[:3, 3]
        assert np.allclose(centre, expected)


def test_camera_centres_preserve_distances():
    """A rigid change of frame cannot alter how far apart the cameras are."""
    rng = np.random.default_rng(12)
    poses = []
    for _ in range(6):
        pose = np.eye(4)
        pose[:3, 3] = rng.normal(size=3) * 2
        poses.append(pose)

    centres = camera_centres_in_reference(poses, poses[0])
    world = np.array([-p[:3, :3].T @ p[:3, 3] for p in poses])
    for i in range(len(poses)):
        for j in range(i + 1, len(poses)):
            assert np.linalg.norm(centres[i] - centres[j]) == pytest.approx(
                np.linalg.norm(world[i] - world[j]))


def test_level_rotation_is_a_proper_rotation():
    for vector in ([0.0, -1.0, 0.0], [0.1, -0.99, 0.05], [0.3, -0.9, -0.3]):
        rotation = level_rotation(np.asarray(vector))
        assert np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-9)
        assert np.linalg.det(rotation) == pytest.approx(1.0)
