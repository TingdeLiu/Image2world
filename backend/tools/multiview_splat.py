"""Merge several SHARP predictions into one splat using COLMAP camera poses.

SHARP reconstructs only what one camera saw. Running it on several frames of a
walkthrough and merging the results widens the reconstructed region to the union
of those views -- without a new model, a training run, or an API call.

The merge hinges on one line that was already there. `unproject_gaussians` takes
the camera extrinsics and maps NDC Gaussians straight into world space; the
backend passes `torch.eye(4)`, which is what makes a single prediction come back
in camera coordinates. Hand it a real world-to-camera matrix instead and every
frame's Gaussians land in one shared frame, ready to concatenate.

    frames + poses -> per-frame SHARP -> metric rescale -> pose transform
                   -> voxel fuse -> one PLY

**The world frame is the reference keyframe's camera frame**, not COLMAP's
arbitrary origin. That keeps the output indistinguishable from a single-view
SHARP splat as far as everything downstream is concerned: +Y still points down,
the scene still sits at positive z, so the viewer's `flip_y`, the collider's
floor detection and the PLY's own embedded intrinsics all stay meaningful.

Fusing is not optional. Adjacent views overlap heavily, and two predictions of
one wall differ by a few centimetres, which reads as ghosting and depth
banding rather than as detail. The fuse keeps, per voxel, the Gaussian that its
source camera saw from closest -- SHARP is sharpest near the capture pose and
smears into streaks with distance, so proximity is a good proxy for which of two
guesses to trust.

Usage:
    python backend/tools/multiview_splat.py <sfm-dir> --out world.ply [--keyframes 8]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from multiview_sfm import (  # noqa: E402
    FrameCamera,
    SparseScene,
    apply_metric_scale,
    calibrate_metric_scale,
    load_scene,
    select_keyframes,
)

SHARP_MODEL_URL = "https://ml-site.cdn-apple.com/models/sharp/sharp_2572gikvuh.pt"
SHARP_INTERNAL_SIZE = (1536, 1536)

# Gaussians this transparent contribute nothing to a render but would still win
# voxels in the fuse and still cost bytes in the PLY.
MIN_OPACITY = 0.15

# Fuse resolution. SHARP's own Gaussians are millimetres across up close, so a
# 2.5 cm voxel thins overlap without visibly softening surfaces -- and it is
# coarse enough to actually collapse the few-centimetre disagreement between
# two views of the same wall, which is the artefact being removed.
FUSE_VOXEL_SIZE = 0.025

# Past this range a single-view prediction is mostly extrapolation: it is what
# produces the streaks that trail off behind furniture. The collider already
# culls at 15 m for the same reason.
MAX_GAUSSIAN_DISTANCE = 15.0

# How many frames to actually predict from. Every added frame widens coverage
# and thickens the haze in equal measure, because SHARP's frames disagree about
# depth by a median of 28.6 cm and each one contributes its own version of every
# shared surface. Four already reaches full 36/36 directional coverage -- beyond
# that the extra frames buy area (194 -> 272 m2 at eight) at a visible cost in
# clarity. Four is the default because sharpness is the scarcer resource; raise
# it with --keyframes when coverage matters more.
DEFAULT_KEYFRAMES = 4

# Depth buffers are built at half SHARP's internal grid: enough resolution to
# test occupancy, cheap enough to project millions of Gaussians against.
DEPTH_DOWNSCALE = 2

# Free-space carving. `margin` is how far in front of an observed surface a
# Gaussian must sit before another view is believed over the one that made it.
# Below roughly 0.15 m the depth noise between two SHARP predictions of the same
# wall starts carving real surface away at grazing angles.
CARVE_MARGIN = 0.20
CARVE_MIN_VIOLATIONS = 2

# Levelling. The floor band is sampled around the dominant low surface; RANSAC
# then fits a plane through it.
FLOOR_BAND_HALF_WIDTH = 0.6
RANSAC_ITERATIONS = 400
RANSAC_THRESHOLD = 0.05
# RANSAC finds a plane in anything: a random cloud scored 11% inliers, a real
# but cluttered floor 16.5%. The ratio alone cannot separate them, so the bar
# depends on whether the trajectory is there to corroborate. Corroborated, a
# weak fit is fine. Standing alone, it has to be convincing.
MIN_FLOOR_INLIER_RATIO = 0.10
MIN_FLOOR_INLIER_RATIO_UNCORROBORATED = 0.25
# Two estimates further apart than this are measuring different things; trust
# the trajectory, which has no competing interpretation.
MAX_UP_DISAGREEMENT_DEG = 10.0
# A walk defines a plane only if it is thin in one direction AND spread in the
# other two. A straight corridor satisfies the first and fails the second.
MAX_TRAJECTORY_THINNESS = 0.25
MIN_TRAJECTORY_SPREAD = 0.15


@dataclass
class FrameGaussians:
    """One frame's prediction, still in its own camera frame."""

    frame: FrameCamera
    xyz: np.ndarray            # (N, 3) camera coordinates, metres per SHARP
    scales: np.ndarray         # (N, 3) singular values
    quaternions: np.ndarray    # (N, 4)
    colors: np.ndarray         # (N, 3) linear RGB
    opacities: np.ndarray      # (N,) 0..1
    depth_map: np.ndarray      # (h, w) z-buffer, metres, 0 = no surface seen
    depth_intrinsics: np.ndarray = None  # 3x3 matching depth_map's resolution


def load_predictor(device: str = "cuda"):
    """Load SHARP the same way the backend does, without touching sharp.cli."""
    from sharp.models import PredictorParams, create_predictor

    state_dict = torch.hub.load_state_dict_from_url(SHARP_MODEL_URL, progress=True)
    predictor = create_predictor(PredictorParams())
    predictor.load_state_dict(state_dict)
    predictor.eval()
    predictor.to(device)
    return predictor


def _depth_buffer(
    xyz: np.ndarray,
    opacities: np.ndarray,
    intrinsics: np.ndarray,
    width: int,
    height: int,
    downscale: int = 2,
) -> np.ndarray:
    """Rasterise Gaussian centres into a nearest-surface depth map.

    A z-buffer rather than a reshape of the prediction tensor. SHARP's Gaussians
    are laid out on a 768x768 grid at two per pixel, but each one carries a
    predicted positional offset, so a reshape gives a depth map whose pixels are
    only loosely where they claim to be -- measured offsets ran to hundreds of
    pixels at the frame edges. Projecting explicitly costs one pass and stays
    correct regardless of how the model lays its output out internally.

    Zero means "no surface here"; the calibrator skips those.
    """
    map_width, map_height = width // downscale, height // downscale
    depth = np.zeros((map_height, map_width), dtype=np.float64)

    z = xyz[:, 2]
    visible = (z > 1e-6) & (opacities >= MIN_OPACITY)
    if not visible.any():
        return depth

    fx, fy = intrinsics[0, 0], intrinsics[1, 1]
    cx, cy = intrinsics[0, 2], intrinsics[1, 2]
    u = (fx * xyz[visible, 0] / z[visible] + cx) / downscale
    v = (fy * xyz[visible, 1] / z[visible] + cy) / downscale
    zz = z[visible]

    inside = (u >= 0) & (u < map_width) & (v >= 0) & (v < map_height)
    u, v, zz = u[inside].astype(np.int32), v[inside].astype(np.int32), zz[inside]
    if zz.size == 0:
        return depth

    # np.minimum.at is the correct scatter-min but is famously slow. Sorting far
    # to near and letting later writes win gives the same nearest-surface result
    # in a fraction of the time.
    order = np.argsort(-zz)
    depth[v[order], u[order]] = zz[order]
    return depth


@torch.no_grad()
def predict_frame(predictor, frame: FrameCamera, device: str = "cuda") -> FrameGaussians:
    """Run SHARP on one frame, in that frame's own camera coordinates.

    The focal length comes from COLMAP, not from the file. Frames extracted from
    a video have no EXIF, so SHARP would otherwise assume a 30 mm equivalent
    lens and scale the whole prediction by however wrong that is -- and being
    wrong by a different amount per frame is exactly what a merge cannot absorb.
    """
    import torch.nn.functional as F
    from PIL import Image
    from sharp.utils.gaussians import unproject_gaussians

    image = np.asarray(Image.open(frame.path).convert("RGB"))
    height, width = image.shape[:2]
    focal_px = frame.focal_px * (width / frame.width)  # COLMAP may have downscaled

    image_pt = torch.from_numpy(image.copy()).float().to(device).permute(2, 0, 1) / 255.0
    disparity_factor = torch.tensor([focal_px / width]).float().to(device)
    resized = F.interpolate(
        image_pt[None], size=SHARP_INTERNAL_SIZE, mode="bilinear", align_corners=True
    )

    gaussians_ndc = predictor(resized, disparity_factor)

    intrinsics = torch.tensor([
        [focal_px, 0, width / 2, 0],
        [0, focal_px, height / 2, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1],
    ]).float().to(device)
    intrinsics_resized = intrinsics.clone()
    intrinsics_resized[0] *= SHARP_INTERNAL_SIZE[0] / width
    intrinsics_resized[1] *= SHARP_INTERNAL_SIZE[1] / height

    # Identity extrinsics: keep the result in camera space for now. The metric
    # scale is not known until every frame's depth has been measured, and the
    # pose cannot be applied before it is scaled.
    gaussians = unproject_gaussians(
        gaussians_ndc, torch.eye(4).to(device), intrinsics_resized, SHARP_INTERNAL_SIZE
    )

    xyz = gaussians.mean_vectors[0].cpu().numpy().astype(np.float64)
    opacities = gaussians.opacities[0].cpu().numpy().astype(np.float64)

    internal_intrinsics = np.asarray([
        [focal_px * SHARP_INTERNAL_SIZE[0] / width, 0, SHARP_INTERNAL_SIZE[0] / 2],
        [0, focal_px * SHARP_INTERNAL_SIZE[1] / height, SHARP_INTERNAL_SIZE[1] / 2],
        [0, 0, 1],
    ])
    downscale = DEPTH_DOWNSCALE
    depth_map = _depth_buffer(
        xyz, opacities, internal_intrinsics,
        SHARP_INTERNAL_SIZE[0], SHARP_INTERNAL_SIZE[1], downscale=downscale,
    )
    depth_intrinsics = internal_intrinsics.copy()
    depth_intrinsics[:2] /= downscale

    return FrameGaussians(
        frame=frame,
        xyz=xyz,
        scales=gaussians.singular_values[0].cpu().numpy().astype(np.float64),
        quaternions=gaussians.quaternions[0].cpu().numpy().astype(np.float64),
        colors=gaussians.colors[0].cpu().numpy().astype(np.float64),
        opacities=opacities,
        depth_map=depth_map,
        depth_intrinsics=depth_intrinsics,
    )


def _quaternion_to_matrix(q: np.ndarray) -> np.ndarray:
    """(N, 4) wxyz quaternions -> (N, 3, 3) rotation matrices."""
    q = q / np.linalg.norm(q, axis=1, keepdims=True).clip(1e-12)
    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    return np.stack([
        1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y),
        2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x),
        2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y),
    ], axis=1).reshape(-1, 3, 3)


def _matrix_to_quaternion(m: np.ndarray) -> np.ndarray:
    """(N, 3, 3) rotation matrices -> (N, 4) wxyz quaternions."""
    trace = m[:, 0, 0] + m[:, 1, 1] + m[:, 2, 2]
    q = np.zeros((len(m), 4))

    big = trace > 0
    if big.any():
        s = np.sqrt(trace[big] + 1.0) * 2
        q[big, 0] = 0.25 * s
        q[big, 1] = (m[big, 2, 1] - m[big, 1, 2]) / s
        q[big, 2] = (m[big, 0, 2] - m[big, 2, 0]) / s
        q[big, 3] = (m[big, 1, 0] - m[big, 0, 1]) / s

    # Whichever diagonal entry is largest gives the numerically stable branch.
    rest = ~big
    if rest.any():
        idx = np.argmax(np.stack([m[:, 0, 0], m[:, 1, 1], m[:, 2, 2]], axis=1), axis=1)
        for axis in range(3):
            sel = rest & (idx == axis)
            if not sel.any():
                continue
            a, b = (axis + 1) % 3, (axis + 2) % 3
            s = np.sqrt(1.0 + m[sel, axis, axis] - m[sel, b, b] - m[sel, a, a]) * 2
            q[sel, 0] = (m[sel, b, a] - m[sel, a, b]) / s
            q[sel, axis + 1] = 0.25 * s
            q[sel, a + 1] = (m[sel, a, axis] + m[sel, axis, a]) / s
            q[sel, b + 1] = (m[sel, b, axis] + m[sel, axis, b]) / s

    return q / np.linalg.norm(q, axis=1, keepdims=True).clip(1e-12)


def transform_to_world(
    prediction: FrameGaussians, camera_to_world: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Move one frame's Gaussians into the shared world frame.

    Returns (xyz, quaternions, scales). Scales are untouched: the transform is a
    rigid motion, so it rotates each Gaussian's covariance without stretching
    it, and only the orientation needs recomputing.
    """
    rotation = camera_to_world[:3, :3]
    translation = camera_to_world[:3, 3]

    xyz = prediction.xyz @ rotation.T + translation
    orientations = rotation[None] @ _quaternion_to_matrix(prediction.quaternions)
    return xyz, _matrix_to_quaternion(orientations), prediction.scales


def camera_centres_in_reference(
    world_to_camera: list[np.ndarray], reference_world_to_camera: np.ndarray
) -> np.ndarray:
    """Where each camera sits, expressed in the reference camera's frame.

    `reference_w2c @ inv(w2c_i)` maps camera i into the reference frame, so its
    translation column is already camera i's centre there. Inverting that a
    second time gives -R^T t, a different point entirely -- and because the
    reference camera lands on the origin either way, the error is invisible on
    the one frame most likely to be spot-checked. It showed up only as a
    levelling angle that came out 1.8 deg against a measured 5.1 deg.
    """
    return np.array([
        (reference_world_to_camera @ np.linalg.inv(pose))[:3, 3]
        for pose in world_to_camera
    ])


def estimate_up_direction(
    xyz: np.ndarray, camera_centres: np.ndarray
) -> tuple[np.ndarray, dict]:
    """Work out which way is up, so the floor can be made level.

    The world frame is anchored on the reference keyframe's camera, and a
    handheld camera is never level -- so the whole reconstruction inherits its
    pitch and roll. Measured on a real walkthrough that was 5 degrees, which
    across a 15 m room puts one end 1.3 m below the other and drops 17% of the
    cloud "below the floor". The viewer's `groundPlaneOffset` is a translation
    and cannot undo a rotation, so this has to be fixed in the geometry.

    Two independent estimates, because either alone can be fooled:

    - **Camera trajectory.** Someone walking holds the camera at roughly
      constant height, so the least-variance direction of the camera centres is
      vertical. Fails on stairs, or if the walk was too short to define a plane.
    - **Floor plane.** RANSAC on the dominant low horizontal surface. Fails when
      the floor is barely visible or clutter outvotes it.

    **This is something single-view reconstruction cannot do at all** -- one
    camera provides no trajectory, and its own tilt is unobservable.

    Returns (up_vector, diagnostics). The vector points along SHARP's "up",
    which is -Y.
    """
    report: dict = {}

    # --- trajectory plane ---
    centred = camera_centres - camera_centres.mean(axis=0)
    _, singular, basis = np.linalg.svd(centred, full_matrices=False)
    trajectory_up = basis[2]
    if trajectory_up[1] > 0:
        trajectory_up = -trajectory_up
    # Two conditions, not one. Thinness alone (s2/s0) is also satisfied by a
    # straight-line walk -- which has no plane at all, only a degenerate one
    # whose normal is arbitrary. Requiring real spread in the second direction
    # (s1/s0) is what separates "walked around a room" from "walked down a
    # corridor", and only the former pins down which way is up.
    scale0 = max(singular[0], 1e-12)
    thinness = float(singular[2] / scale0)
    spread = float(singular[1] / scale0)
    planar = thinness < MAX_TRAJECTORY_THINNESS and spread > MIN_TRAJECTORY_SPREAD
    report["trajectory_thinness"] = thinness
    report["trajectory_spread"] = spread
    report["trajectory_up"] = trajectory_up

    # --- floor plane ---
    counts, edges = np.histogram(xyz[:, 1], bins=120)
    floor_y = edges[len(counts) // 2 + int(np.argmax(counts[len(counts) // 2:]))]
    band = xyz[np.abs(xyz[:, 1] - floor_y) < FLOOR_BAND_HALF_WIDTH]

    floor_up, inlier_ratio = None, 0.0
    if len(band) >= 100:
        rng = np.random.default_rng(0)
        best_count = 0
        for _ in range(RANSAC_ITERATIONS):
            sample = band[rng.choice(len(band), 3, replace=False)]
            normal = np.cross(sample[1] - sample[0], sample[2] - sample[0])
            length = np.linalg.norm(normal)
            if length < 1e-9:
                continue
            normal = normal / length
            inliers = int((np.abs((band - sample[0]) @ normal) < RANSAC_THRESHOLD).sum())
            if inliers > best_count:
                best_count, floor_up = inliers, normal
        if floor_up is not None:
            if floor_up[1] > 0:
                floor_up = -floor_up
            inlier_ratio = best_count / len(band)
    report["floor_inlier_ratio"] = inlier_ratio
    report["floor_up"] = floor_up

    # The trajectory leads, for two reasons. Its reliability test is
    # unambiguous (did the walk cover a plane?), while the floor's is not:
    # RANSAC finds a plane in anything, reaching 11% inliers on a random cloud
    # versus 16.5% for a real but cluttered floor. And it is *stable* -- it
    # depends only on the camera poses, so it does not move when the keyframe
    # count changes, whereas floor RANSAC on the same room swung between 2.5
    # and 5.4 degrees across keyframe counts at 11-18% inliers. An estimate
    # that changes with an unrelated parameter cannot be the primary one.
    floor_usable = floor_up is not None and inlier_ratio >= MIN_FLOOR_INLIER_RATIO
    disagreement = None
    if floor_usable and planar:
        disagreement = float(np.degrees(
            np.arccos(np.clip(abs(floor_up @ trajectory_up), -1, 1))))
    report["disagreement_deg"] = disagreement

    if planar:
        chosen, source = trajectory_up, "trajectory"
        if disagreement is not None and disagreement > MAX_UP_DISAGREEMENT_DEG:
            # Not fatal, but worth surfacing: one of them is measuring
            # something other than the horizontal.
            print(f"[level] WARNING: floor plane and trajectory disagree by "
                  f"{disagreement:.1f} deg. Using the trajectory. If the walk "
                  f"went up stairs or a slope, pass --no-level instead.")
            source = "trajectory (floor disagreed)"
    elif floor_up is not None and inlier_ratio >= MIN_FLOOR_INLIER_RATIO_UNCORROBORATED:
        chosen, source = floor_up, "floor plane (walk not planar)"
    else:
        # Neither estimate is trustworthy; leave the scene as it is rather than
        # rotating it by a guess.
        print("[level] no reliable up direction (floor unclear, walk not planar); "
              "leaving the scene as it is")
        return np.array([0.0, -1.0, 0.0]), report

    tilt = float(np.degrees(np.arccos(np.clip(chosen @ np.array([0.0, -1.0, 0.0]), -1, 1))))
    report["source"] = source
    report["tilt_deg"] = tilt
    print(f"[level] up from {source}: tilt {tilt:.2f} deg "
          f"(floor inliers {inlier_ratio:.0%}, trajectory thinness {thinness:.3f} "
          f"spread {spread:.3f})")
    return chosen, report


def level_rotation(up: np.ndarray) -> np.ndarray:
    """Rotation taking `up` onto SHARP's -Y, about the axis between them.

    A minimal rotation on purpose: it fixes pitch and roll and leaves yaw alone,
    so the scene keeps facing the way the reference photo faced.
    """
    target = np.array([0.0, -1.0, 0.0])
    source = up / np.linalg.norm(up)
    axis = np.cross(source, target)
    sine = np.linalg.norm(axis)
    cosine = float(source @ target)
    if sine < 1e-9:
        return np.eye(3) if cosine > 0 else -np.eye(3)
    axis = axis / sine
    skew = np.array([[0, -axis[2], axis[1]],
                     [axis[2], 0, -axis[0]],
                     [-axis[1], axis[0], 0]])
    angle = np.arctan2(sine, cosine)
    return np.eye(3) + np.sin(angle) * skew + (1 - np.cos(angle)) * (skew @ skew)


def carve_free_space(
    xyz_world: np.ndarray,
    predictions: list[FrameGaussians],
    reference_to_camera: dict[str, np.ndarray],
    margin: float = CARVE_MARGIN,
    min_violations: int = CARVE_MIN_VIOLATIONS,
) -> np.ndarray:
    """Drop Gaussians that other views say are floating in empty air.

    This is the one repair multi-view can do that single-view cannot. A single
    prediction has no second opinion: every Gaussian it emits is, as far as that
    frame knows, a surface. Merging several frames stacks all of their mistakes
    into one cloud -- and simultaneously supplies the evidence to find them,
    because a floater sitting in front of what another camera actually saw is a
    contradiction that camera can report.

    For each Gaussian, project it into every keyframe. If it lands closer than
    that frame's observed surface by more than `margin`, that frame is claiming
    the space is empty. Two such votes and it goes.

    Note this is *not* the geometric filtering that was tried and rejected
    before (dropping large, elongated Gaussians): that judged a Gaussian by its
    own shape, with nothing to check it against, and removed real surface along
    with the streaks. This judges it against independent observations.

    `margin` matters. Too small and depth noise carves away genuine surfaces
    seen at a grazing angle; too large and thin floaters survive. `min_votes`
    of 2 guards against a single frame's occlusion-boundary artefacts.

    Returns a boolean keep-mask.
    """
    votes = np.zeros(len(xyz_world), dtype=np.int16)

    for prediction in predictions:
        transform = reference_to_camera[prediction.frame.name]
        points = xyz_world @ transform[:3, :3].T + transform[:3, 3]
        z = points[:, 2]

        depth_map = prediction.depth_map
        map_height, map_width = depth_map.shape
        K = prediction.depth_intrinsics

        in_front = z > 1e-3
        u = np.full(len(points), -1.0)
        v = np.full(len(points), -1.0)
        u[in_front] = K[0, 0] * points[in_front, 0] / z[in_front] + K[0, 2]
        v[in_front] = K[1, 1] * points[in_front, 1] / z[in_front] + K[1, 2]

        inside = in_front & (u >= 0) & (u < map_width) & (v >= 0) & (v < map_height)
        if not inside.any():
            continue

        rows = np.flatnonzero(inside)
        observed = depth_map[v[rows].astype(np.int32), u[rows].astype(np.int32)]

        # observed == 0 means this frame saw nothing there, which is not
        # evidence of empty space -- only of no coverage.
        contradicts = (observed > 1e-3) & (z[rows] < observed - margin)
        votes[rows[contradicts]] += 1

    keep = votes < min_violations
    print(f"[carve] {len(xyz_world):,} -> {keep.sum():,} gaussians "
          f"({(~keep).sum():,} floaters removed, {(~keep).mean():.1%}) "
          f"at margin {margin:.2f} m, {min_violations}+ votes")
    return keep


def fuse_gaussians(
    positions: list[np.ndarray],
    quaternions: list[np.ndarray],
    scales: list[np.ndarray],
    colors: list[np.ndarray],
    opacities: list[np.ndarray],
    source_distance: list[np.ndarray],
    voxel_size: float = FUSE_VOXEL_SIZE,
) -> dict[str, np.ndarray]:
    """Collapse overlapping predictions to one Gaussian per voxel.

    `source_distance` is how far each Gaussian sat from the camera that
    predicted it. Within a voxel the closest-observed one wins, because a
    single-view prediction degrades with range: near the capture pose SHARP
    resolves real surface detail, far from it the same surface arrives as a
    stretched streak. Averaging the candidates instead would blend a sharp
    estimate into a smeared one and land between the two.
    """
    xyz = np.concatenate(positions)
    quats = np.concatenate(quaternions)
    scale = np.concatenate(scales)
    color = np.concatenate(colors)
    opacity = np.concatenate(opacities)
    distance = np.concatenate(source_distance)

    before = len(xyz)

    # Quantise to a voxel lattice, then encode each cell as one integer so the
    # dedupe is a sort rather than a 3D hash table.
    grid = np.floor(xyz / voxel_size).astype(np.int64)
    grid -= grid.min(axis=0)
    dims = grid.max(axis=0) + 1
    if np.prod(dims.astype(float)) > 9e18:
        raise ValueError("scene bounds too large for voxel keys; raise voxel_size")
    keys = (grid[:, 0] * dims[1] + grid[:, 1]) * dims[2] + grid[:, 2]

    # Sort by cell, then by distance within the cell, and take the first of each
    # run -- that is the nearest-observed Gaussian per voxel.
    order = np.lexsort((distance, keys))
    sorted_keys = keys[order]
    first = np.ones(len(sorted_keys), dtype=bool)
    first[1:] = sorted_keys[1:] != sorted_keys[:-1]
    keep = order[first]

    print(f"[fuse] {before:,} -> {len(keep):,} gaussians "
          f"({len(keep) / before:.1%} kept at {voxel_size * 100:.1f} cm)")

    return {
        "xyz": xyz[keep],
        "quaternions": quats[keep],
        "scales": scale[keep],
        "colors": color[keep],
        "opacities": opacity[keep],
    }


def save_merged_ply(merged: dict[str, np.ndarray], focal_px: float,
                    image_shape: tuple[int, int], path: Path) -> None:
    """Write the merged splat in exactly the layout SHARP produces.

    Same vertex properties and same extra elements, so `createBinaryPlyLod`,
    `/api/splat-to-collider` and the viewer all read it without changes. The
    embedded intrinsics describe the reference keyframe, which is the camera the
    world frame is anchored to.
    """
    from plyfile import PlyData, PlyElement
    from sharp.utils import color_space as cs_utils
    from sharp.utils.gaussians import convert_rgb_to_spherical_harmonics

    xyz = merged["xyz"].astype(np.float32)
    # SHARP stores log-scales and opacity logits, matching the INRIA convention.
    scale_logits = np.log(np.clip(merged["scales"], 1e-12, None)).astype(np.float32)
    opacity = np.clip(merged["opacities"], 1e-6, 1 - 1e-6)
    opacity_logits = np.log(opacity / (1 - opacity)).astype(np.float32).reshape(-1, 1)

    colors = convert_rgb_to_spherical_harmonics(
        cs_utils.linearRGB2sRGB(torch.from_numpy(merged["colors"]).float())
    ).numpy().astype(np.float32)

    attributes = np.concatenate(
        [xyz, colors, opacity_logits, scale_logits,
         merged["quaternions"].astype(np.float32)], axis=1
    )

    dtype_full = [(name, "f4") for name in
                  ["x", "y", "z"] + [f"f_dc_{i}" for i in range(3)] + ["opacity"]
                  + [f"scale_{i}" for i in range(3)] + [f"rot_{i}" for i in range(4)]]
    vertices = np.empty(len(xyz), dtype=dtype_full)
    vertices[:] = list(map(tuple, attributes))

    image_height, image_width = image_shape

    image_size = np.empty(2, dtype=[("image_size", "u4")])
    image_size[:] = np.array([image_width, image_height])

    intrinsic = np.empty(9, dtype=[("intrinsic", "f4")])
    intrinsic[:] = np.array([focal_px, 0, image_width * 0.5,
                             0, focal_px, image_height * 0.5,
                             0, 0, 1])

    extrinsic = np.empty(16, dtype=[("extrinsic", "f4")])
    extrinsic[:] = np.eye(4).flatten()

    frame = np.empty(2, dtype=[("frame", "i4")])
    frame[:] = np.array([1, len(xyz)], dtype=np.int32)

    # A merged splat spans the reference camera's own frame, so some Gaussians
    # sit behind it and their disparity is negative or infinite. Quantiles over
    # the finite, in-front subset keep this metadata meaningful instead of NaN.
    disparity_values = 1.0 / xyz[:, 2][xyz[:, 2] > 1e-6]
    disparity = np.empty(2, dtype=[("disparity", "f4")])
    disparity[:] = (np.percentile(disparity_values, [10, 90])
                    if disparity_values.size else np.array([0.0, 1.0]))

    color_space = np.empty(1, dtype=[("color_space", "u1")])
    color_space[:] = cs_utils.encode_color_space("sRGB")

    PlyData([
        PlyElement.describe(vertices, "vertex"),
        PlyElement.describe(image_size, "image_size"),
        PlyElement.describe(intrinsic, "intrinsic"),
        PlyElement.describe(extrinsic, "extrinsic"),
        PlyElement.describe(frame, "frame"),
        PlyElement.describe(disparity, "disparity"),
        PlyElement.describe(color_space, "color_space"),
    ]).write(str(path))


def build_multiview_splat(
    scene: SparseScene,
    out_path: Path,
    keyframe_count: int = DEFAULT_KEYFRAMES,
    device: str = "cuda",
    voxel_size: float = FUSE_VOXEL_SIZE,
    carve: bool = True,
    level: bool = True,
) -> dict:
    """Predict, rescale, merge and write. The whole pipeline after SfM."""
    keyframes = select_keyframes(scene, keyframe_count)

    predictor = load_predictor(device)
    predictions: list[FrameGaussians] = []
    for index, frame in enumerate(keyframes, start=1):
        print(f"[sharp] {index}/{len(keyframes)}  {frame.name}")
        predictions.append(predict_frame(predictor, frame, device))
        if device == "cuda":
            torch.cuda.empty_cache()

    # Metric calibration needs SHARP's depth, which only exists now.
    scale = calibrate_metric_scale(scene, {p.frame.name: p.depth_map for p in predictions})
    metric_scene = apply_metric_scale(scene, scale)
    poses = {frame.name: frame.world_to_camera for frame in metric_scene.frames}

    # Anchor the world on the first keyframe so the output matches a single-view
    # SHARP splat's conventions (see module docstring).
    reference = predictions[0].frame
    reference_w2c = poses[reference.name]

    positions, quats, scales, colors, opacities, distances = [], [], [], [], [], []
    for prediction in predictions:
        world_to_camera = poses[prediction.frame.name]
        # camera -> COLMAP world -> reference camera
        camera_to_world = np.linalg.inv(world_to_camera)
        camera_to_reference = reference_w2c @ camera_to_world

        distance = np.linalg.norm(prediction.xyz, axis=1)
        keep = (prediction.opacities >= MIN_OPACITY) & (distance <= MAX_GAUSSIAN_DISTANCE)

        trimmed = FrameGaussians(
            frame=prediction.frame,
            xyz=prediction.xyz[keep],
            scales=prediction.scales[keep],
            quaternions=prediction.quaternions[keep],
            colors=prediction.colors[keep],
            opacities=prediction.opacities[keep],
            depth_map=prediction.depth_map,
        )
        xyz, quaternion, scale_values = transform_to_world(trimmed, camera_to_reference)

        positions.append(xyz)
        quats.append(quaternion)
        scales.append(scale_values)
        colors.append(trimmed.colors)
        opacities.append(trimmed.opacities)
        distances.append(distance[keep])
        print(f"       {prediction.frame.name}: {keep.sum():,} of {len(keep):,} kept")

    # Carve before fusing, and in world space: a floater has to be tested
    # against every frame, not just the one that produced it. Doing it after the
    # fuse would work too but wastes effort deduplicating points about to be
    # thrown away.
    if carve:
        reference_to_camera = {
            prediction.frame.name: (
                poses[prediction.frame.name] @ np.linalg.inv(reference_w2c)
            )
            for prediction in predictions
        }
        all_xyz = np.concatenate(positions)
        survives = carve_free_space(all_xyz, predictions, reference_to_camera)

        offsets = np.cumsum([0] + [len(p) for p in positions])
        for index in range(len(positions)):
            span = survives[offsets[index]:offsets[index + 1]]
            positions[index] = positions[index][span]
            quats[index] = quats[index][span]
            scales[index] = scales[index][span]
            colors[index] = colors[index][span]
            opacities[index] = opacities[index][span]
            distances[index] = distances[index][span]

    merged = fuse_gaussians(positions, quats, scales, colors, opacities, distances,
                            voxel_size=voxel_size)

    # Levelling last: it is a rigid rotation, so it cannot change which
    # Gaussians survived carving or which won a voxel -- only where the whole
    # cloud sits. Doing it here keeps those earlier stages reasoning in the
    # frame their depth maps were built in.
    level_report: dict = {}
    if level:
        camera_centres = camera_centres_in_reference(
            [poses[frame.name] for frame in metric_scene.frames], reference_w2c
        )
        up, level_report = estimate_up_direction(merged["xyz"], camera_centres)
        rotation = level_rotation(up)
        merged["xyz"] = merged["xyz"] @ rotation.T
        merged["quaternions"] = _matrix_to_quaternion(
            rotation[None] @ _quaternion_to_matrix(merged["quaternions"])
        )

    reference_focal = reference.focal_px
    save_merged_ply(merged, reference_focal,
                    (reference.height, reference.width), out_path)

    extent = merged["xyz"].max(axis=0) - merged["xyz"].min(axis=0)
    summary = {
        "keyframes": len(keyframes),
        "metric_scale": scale,
        "gaussians": int(len(merged["xyz"])),
        "extent_m": [float(v) for v in extent],
        "ply_bytes": out_path.stat().st_size,
        "tilt_corrected_deg": level_report.get("tilt_deg"),
        "up_source": level_report.get("source"),
    }
    print(f"[done] {summary['gaussians']:,} gaussians, "
          f"{extent[0]:.1f} x {extent[1]:.1f} x {extent[2]:.1f} m, "
          f"{summary['ply_bytes'] / 1e6:.1f} MB")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sfm_dir", type=Path, help="directory holding scene_sfm.json")
    parser.add_argument("--out", type=Path, required=True, help="output .ply")
    parser.add_argument("--keyframes", type=int, default=DEFAULT_KEYFRAMES,
                        help="how many frames to run SHARP on; more coverage, more haze")
    parser.add_argument("--voxel-size", type=float, default=FUSE_VOXEL_SIZE)
    parser.add_argument("--no-carve", action="store_true",
                        help="keep floaters other views contradict (for comparison)")
    parser.add_argument("--no-level", action="store_true",
                        help="skip tilt correction, leaving the capture camera's roll")
    parser.add_argument("--cpu", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ml-sharp" / "src"))
    scene = load_scene(args.sfm_dir / "scene_sfm.json")
    if not scene.points_xyz.size:
        print("scene_sfm.json carries no sparse cloud; re-run multiview_sfm.py",
              file=sys.stderr)
        return 1

    build_multiview_splat(scene, args.out, keyframe_count=args.keyframes,
                          device="cpu" if args.cpu else "cuda",
                          voxel_size=args.voxel_size, carve=not args.no_carve,
                          level=not args.no_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
