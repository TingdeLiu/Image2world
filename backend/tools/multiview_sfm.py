"""Recover metric camera poses from a walkthrough video, for multi-view SHARP.

SHARP is monocular: it reconstructs only the surfaces one camera saw, so a
world built from a single photo has no geometry behind the viewpoint and none
behind any occluder. Walking to the edge of the visible region passes straight
through it (`build_collider.py` documents the same limitation from the collision
side).

The fix that needs no new model and no API is to run SHARP several times and
merge the results. SHARP already supports this: `unproject_gaussians` takes the
camera extrinsics, and the backend currently hardcodes them to the identity
matrix. Feed it a real world-to-camera matrix per frame and every prediction
lands in one shared world frame, ready to concatenate.

This module produces those matrices:

    video -> ffmpeg frames -> sharpness pick -> COLMAP SfM -> metric rescale

Two things make the last step necessary rather than optional:

1. **COLMAP is up-to-scale.** Structure from motion recovers the shape of the
   camera trajectory but not how big it is; the same image sequence fits a
   doll's house and a cathedral equally well. SHARP, by contrast, is metric.
   Aligning the two is what keeps the project's "units are already metres"
   convention true.

2. **Extracted video frames carry no EXIF.** Without a focal length SHARP falls
   back to a 30 mm equivalent (`sharp/utils/io.py`), which silently scales the
   whole scene by however wrong that guess was. COLMAP estimates the real focal
   length as part of SfM, so we pass it back in.

Usage:
    python backend/tools/multiview_sfm.py <video> --out <dir> [--target-frames 60]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# A walkthrough is mostly redundant: at 30 fps a slow pan moves a few pixels per
# frame. COLMAP wants baseline between views, not more of the same view, and
# every extra frame costs matching time quadratically-ish. Sampling candidates
# at 4 fps and keeping the sharpest one per window lands ~2 usable frames per
# second of video, which is plenty of overlap for sequential matching.
CANDIDATE_FPS = 4.0
FRAMES_PER_WINDOW = 2

# Below this Laplacian variance a frame is motion-blurred past the point where
# SIFT finds stable keypoints. Absolute thresholds do not transfer across
# cameras and lighting, so this is only a floor; the real selection is relative
# (sharpest within each window).
MIN_SHARPNESS = 12.0

# CPU SfM budget. The PyPI pycolmap wheel has no CUDA, so both feature
# extraction and matching run on the CPU and these caps decide the runtime.
# 1600 px keeps a 4K frame's detail well past what SIFT resolves anyway, and
# 4096 features is roughly double what an indoor frame yields as stable
# keypoints -- lowering either further starts costing registrations.
SFM_MAX_IMAGE_SIZE = 1600
SFM_MAX_FEATURES = 4096
SFM_SEQUENTIAL_OVERLAP = 5

# Below this fraction of frames registered, the reconstruction is real but
# covers only part of the walk -- worth warning about, because it fails by
# quietly producing a smaller world rather than by raising.
MIN_REGISTRATION_RATE = 0.6


@dataclass
class FrameCamera:
    """One registered frame: where the camera was, and what it saw through."""

    name: str
    path: Path
    # World-to-camera, 4x4, OpenCV convention (x right, y down, z forward) --
    # the same convention SHARP uses, which is why these can be handed straight
    # to `unproject_gaussians`.
    world_to_camera: np.ndarray
    # 3x3 pixel intrinsics at the frame's own resolution.
    intrinsics: np.ndarray
    width: int
    height: int
    # Indices into the sparse cloud that this frame observes, and their depths
    # along its optical axis. Used for metric calibration.
    point3d_ids: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int64))

    @property
    def focal_px(self) -> float:
        """Mean focal length in pixels; SHARP takes a single scalar."""
        return float((self.intrinsics[0, 0] + self.intrinsics[1, 1]) / 2.0)

    @property
    def camera_centre(self) -> np.ndarray:
        """Camera position in world coordinates."""
        rotation = self.world_to_camera[:3, :3]
        translation = self.world_to_camera[:3, 3]
        return -rotation.T @ translation


@dataclass
class SparseScene:
    """A solved reconstruction: posed frames plus the sparse cloud tying them."""

    frames: list[FrameCamera]
    points_xyz: np.ndarray          # (N, 3) world coordinates
    point_ids: np.ndarray           # (N,) COLMAP point3D ids, aligned to points_xyz
    scale_applied: float = 1.0      # metric rescale already baked in, for reporting

    def point_index(self) -> dict[int, int]:
        return {int(pid): i for i, pid in enumerate(self.point_ids)}


def _measure_sharpness(paths: list[Path]) -> np.ndarray:
    """Laplacian variance per image -- high means crisp, low means motion blur."""
    import cv2

    values = []
    for path in paths:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            values.append(0.0)
            continue
        # Downscale first: Laplacian variance on a 4K frame is dominated by
        # sensor noise, which is sharp everywhere and tells us nothing.
        if max(image.shape) > 960:
            factor = 960.0 / max(image.shape)
            image = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
        values.append(float(cv2.Laplacian(image, cv2.CV_64F).var()))
    return np.asarray(values)


def _pick_windowed(sharpness: np.ndarray, target: int) -> list[int]:
    """Choose `target` indices: the sharpest one within each equal-width window.

    Windowed rather than global. Picking the globally sharpest N instead would
    cluster them wherever the camera paused, which is exactly where the baseline
    between views is smallest and the frames are least useful for triangulation.
    """
    count = len(sharpness)
    window_count = max(1, min(target, count))
    bounds = np.linspace(0, count, window_count + 1).astype(int)

    chosen = []
    for index in range(window_count):
        start, stop = bounds[index], bounds[index + 1]
        if stop <= start:
            continue
        best = start + int(np.argmax(sharpness[start:stop]))
        if sharpness[best] < MIN_SHARPNESS:
            continue
        chosen.append(best)
    return chosen


def prepare_frames(images_dir: Path, out_dir: Path, target_frames: int = 60) -> list[Path]:
    """Select frames from an existing image sequence, in filename order.

    The multi-image entry point: the same selection the video path applies after
    decoding, for callers who already have stills. Filename order is assumed to
    be capture order, which is what makes sequential matching valid -- an
    unordered pile of photos would need exhaustive matching instead.
    """
    candidates = sorted(
        path for path in images_dir.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if not candidates:
        raise RuntimeError(f"no images found in {images_dir}")

    sharpness = _measure_sharpness(candidates)
    chosen = _pick_windowed(sharpness, target_frames)

    out_dir.mkdir(parents=True, exist_ok=True)
    kept: list[Path] = []
    origins: dict[str, str] = {}
    for index in chosen:
        destination = out_dir / f"frame_{len(kept):04d}{candidates[index].suffix.lower()}"
        shutil.copyfile(candidates[index], destination)
        origins[destination.name] = candidates[index].name
        kept.append(destination)

    # Frames are renamed to a stable sequence for COLMAP, which loses the
    # original filename -- worth keeping, both for debugging a bad merge and for
    # checking a solved trajectory against a dataset's own ground truth.
    (out_dir / "source_names.json").write_text(
        json.dumps(origins, indent=2), encoding="utf-8"
    )

    if len(kept) < 3:
        raise RuntimeError(f"only {len(kept)} usable images in {images_dir}")
    print(f"[frames] selected {len(kept)} of {len(candidates)} images "
          f"(sharpness {sharpness.min():.0f}-{sharpness.max():.0f})")
    return kept


def extract_frames(video: Path, out_dir: Path, target_frames: int = 60) -> list[Path]:
    """Pull evenly spaced, sharp frames out of a walkthrough video."""
    if not video.exists():
        raise FileNotFoundError(f"video not found: {video}")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not on PATH; it is needed to decode the video")

    candidates_dir = out_dir / ".candidates"
    if candidates_dir.exists():
        shutil.rmtree(candidates_dir)
    candidates_dir.mkdir(parents=True)

    # -qscale:v 2 keeps JPEG artefacts well below SIFT's noise floor while
    # staying far smaller than PNG for a few hundred frames.
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-vf", f"fps={CANDIDATE_FPS}",
        "-qscale:v", "2",
        str(candidates_dir / "cand_%05d.jpg"),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to decode {video.name}: {result.stderr.strip()}")

    candidates = sorted(candidates_dir.glob("cand_*.jpg"))
    if not candidates:
        raise RuntimeError(f"no frames decoded from {video.name}")

    sharpness = _measure_sharpness(candidates)
    chosen = _pick_windowed(sharpness, min(target_frames, len(candidates) // FRAMES_PER_WINDOW or 1))

    out_dir.mkdir(parents=True, exist_ok=True)
    kept: list[Path] = []
    for index in chosen:
        destination = out_dir / f"frame_{len(kept):04d}.jpg"
        shutil.copyfile(candidates[index], destination)
        kept.append(destination)

    shutil.rmtree(candidates_dir, ignore_errors=True)

    if len(kept) < 3:
        raise RuntimeError(
            f"only {len(kept)} usable frames from {video.name}; the footage is too "
            "short or too blurred for structure from motion"
        )
    print(f"[frames] kept {len(kept)} of {len(candidates)} candidates "
          f"(sharpness {sharpness.min():.0f}-{sharpness.max():.0f})")
    return kept


def solve_poses(frames_dir: Path, work_dir: Path, use_gpu: bool = False) -> SparseScene:
    """Run COLMAP structure from motion over the extracted frames.

    Sequential matching, not exhaustive: video frames arrive in order, so only
    neighbours need comparing, which turns an O(n^2) match into O(n). Loop
    detection stays off because it needs a vocabulary tree we would have to
    download; walking a full circle round a room still closes acceptably
    through the sequential overlap as long as the walk ends where it started.

    **This runs on CPU.** The pycolmap wheel on PyPI is built without CUDA
    (`pycolmap.has_cuda` is False), so GPU matching is simply unavailable and
    GPU extraction would fall back to an OpenGL path that needs a display. Every
    default below is therefore chosen for CPU cost, not for maximum accuracy:
    capping image size and feature count matters far more here than it would on
    a GPU build, because a 4K frame at 8192 features turns a ten-minute solve
    into an hour-long one for no measurable gain on indoor footage.
    """
    import pycolmap

    if use_gpu and not getattr(pycolmap, "has_cuda", False):
        print("[sfm] pycolmap has no CUDA support; falling back to CPU")
        use_gpu = False

    work_dir.mkdir(parents=True, exist_ok=True)
    database_path = work_dir / "database.db"
    sparse_dir = work_dir / "sparse"
    if database_path.exists():
        database_path.unlink()
    if sparse_dir.exists():
        shutil.rmtree(sparse_dir)
    sparse_dir.mkdir(parents=True)

    extraction_options = pycolmap.FeatureExtractionOptions()
    extraction_options.use_gpu = use_gpu
    extraction_options.gpu_index = "0" if use_gpu else "-1"
    extraction_options.max_image_size = SFM_MAX_IMAGE_SIZE
    extraction_options.sift.max_num_features = SFM_MAX_FEATURES

    # One physical camera shot the whole video, so every frame shares one set of
    # intrinsics. Telling COLMAP that pools evidence from every frame into a
    # single focal-length estimate instead of solving a noisy one per image --
    # and that estimate is what replaces SHARP's 30 mm EXIF fallback.
    print(f"[sfm] extracting features ({'GPU' if use_gpu else 'CPU'})...")
    pycolmap.extract_features(
        database_path=database_path,
        image_path=frames_dir,
        camera_mode=pycolmap.CameraMode.SINGLE,
        extraction_options=extraction_options,
    )

    print("[sfm] matching sequential pairs...")
    matching_options = pycolmap.FeatureMatchingOptions()
    matching_options.use_gpu = use_gpu
    matching_options.gpu_index = "0" if use_gpu else "-1"
    pairing_options = pycolmap.SequentialPairingOptions()
    # Each extra frame of overlap costs a full CPU match. Five neighbours plus
    # quadratic spacing (1, 2, 4, 8, 16 frames back) buys long-range links for
    # the price of short-range ones, which is what keeps a loop closing.
    pairing_options.overlap = SFM_SEQUENTIAL_OVERLAP
    pairing_options.quadratic_overlap = True
    pycolmap.match_sequential(
        database_path=database_path,
        matching_options=matching_options,
        pairing_options=pairing_options,
    )

    print("[sfm] incremental mapping...")
    reconstructions = pycolmap.incremental_mapping(
        database_path=database_path,
        image_path=frames_dir,
        output_path=sparse_dir,
    )
    if not reconstructions:
        raise RuntimeError(
            "COLMAP registered no cameras. The walk was probably too fast, the "
            "room too featureless, or consecutive frames share too little overlap."
        )

    # Several disconnected sub-models mean the walk broke somewhere; the largest
    # is the one worth keeping.
    reconstruction = max(reconstructions.values(), key=lambda r: len(r.images))

    points_xyz = []
    point_ids = []
    for point_id, point in reconstruction.points3D.items():
        points_xyz.append(point.xyz)
        point_ids.append(point_id)
    points_xyz = np.asarray(points_xyz, dtype=np.float64).reshape(-1, 3)
    point_ids = np.asarray(point_ids, dtype=np.int64)

    frames: list[FrameCamera] = []
    for image in reconstruction.images.values():
        camera = reconstruction.camera(image.camera_id)
        world_to_camera = np.eye(4, dtype=np.float64)
        world_to_camera[:3, :4] = image.cam_from_world().matrix()

        observed = np.asarray(
            [p.point3D_id for p in image.points2D if p.has_point3D()], dtype=np.int64
        )

        frames.append(FrameCamera(
            name=image.name,
            path=frames_dir / image.name,
            world_to_camera=world_to_camera,
            intrinsics=camera.calibration_matrix().astype(np.float64),
            width=int(camera.width),
            height=int(camera.height),
            point3d_ids=observed,
        ))

    frames.sort(key=lambda f: f.name)
    registered = len(frames)
    total = len(list(frames_dir.glob("frame_*.*")))
    print(f"[sfm] registered {registered}/{total} frames, {len(points_xyz)} sparse points")
    if registered < 3:
        raise RuntimeError(f"only {registered} frames registered; not enough for a merge")

    # A low registration rate is the failure that looks like a success: SfM
    # returns a valid reconstruction of whichever handful of frames did match,
    # and the merge then covers a fraction of the room while reporting no error.
    # The usual cause is too much camera motion between consecutive frames --
    # SIFT stops matching reliably past roughly 25 degrees of rotation, so
    # either the walk was fast or too few frames were sampled from it.
    if registered < total * MIN_REGISTRATION_RATE:
        print(
            f"[sfm] WARNING: only {registered / total:.0%} of frames registered. "
            f"The merge will cover just the part of the room those {registered} "
            f"frames saw.\n"
            f"       Most likely consecutive frames are too far apart. Try "
            f"--target-frames {min(total * 3, 300)}, or re-shoot walking more "
            f"slowly."
        )

    return SparseScene(frames=frames, points_xyz=points_xyz, point_ids=point_ids)


def calibrate_metric_scale(
    scene: SparseScene,
    reference_depths: dict[str, np.ndarray],
) -> float:
    """Find the factor that turns COLMAP's arbitrary units into metres.

    `reference_depths` maps a frame name to SHARP's predicted metric depth map
    for that frame. For every sparse point the frame observes we have two depths
    of the same physical surface: COLMAP's (arbitrary scale) and SHARP's
    (metres). Their ratio is the scale, estimated as a median over all such
    points so a few mismatched or badly triangulated ones cannot drag it.

    Returns the multiplier to apply to COLMAP translations and point positions.
    """
    index = scene.point_index()
    ratios: list[float] = []

    for frame in scene.frames:
        depth_map = reference_depths.get(frame.name)
        if depth_map is None or frame.point3d_ids.size == 0:
            continue

        rows = [index[int(pid)] for pid in frame.point3d_ids if int(pid) in index]
        if not rows:
            continue
        world_points = scene.points_xyz[np.asarray(rows, dtype=np.int64)]

        # Depth along the optical axis in COLMAP units.
        camera_points = (frame.world_to_camera[:3, :3] @ world_points.T).T + frame.world_to_camera[:3, 3]
        colmap_depth = camera_points[:, 2]

        fx, fy = frame.intrinsics[0, 0], frame.intrinsics[1, 1]
        cx, cy = frame.intrinsics[0, 2], frame.intrinsics[1, 2]
        in_front = colmap_depth > 1e-6
        u = np.full(len(camera_points), -1.0)
        v = np.full(len(camera_points), -1.0)
        u[in_front] = fx * camera_points[in_front, 0] / colmap_depth[in_front] + cx
        v[in_front] = fy * camera_points[in_front, 1] / colmap_depth[in_front] + cy

        map_height, map_width = depth_map.shape[:2]
        # The depth map may be at a different resolution than the frame COLMAP saw.
        u_scaled = u * (map_width / frame.width)
        v_scaled = v * (map_height / frame.height)
        valid = (
            in_front
            & (u_scaled >= 0) & (u_scaled < map_width)
            & (v_scaled >= 0) & (v_scaled < map_height)
        )
        if valid.sum() < 20:
            continue

        sampled = depth_map[
            v_scaled[valid].astype(np.int32), u_scaled[valid].astype(np.int32)
        ]
        finite = np.isfinite(sampled) & (sampled > 1e-3)
        if finite.sum() < 20:
            continue

        ratios.extend((sampled[finite] / colmap_depth[valid][finite]).tolist())

    if len(ratios) < 50:
        raise RuntimeError(
            f"only {len(ratios)} depth correspondences; cannot pin down the metric "
            "scale. The sparse cloud and the SHARP depth maps barely overlap."
        )

    ratio_array = np.asarray(ratios)
    scale = float(np.median(ratio_array))
    spread = float(np.percentile(ratio_array, 75) - np.percentile(ratio_array, 25))
    print(f"[scale] metric scale {scale:.4f} from {len(ratios)} correspondences "
          f"(IQR {spread:.4f}, {spread / max(scale, 1e-9):.1%} of median)")
    if not np.isfinite(scale) or scale <= 0:
        raise RuntimeError(f"metric scale came out as {scale}, which cannot be right")
    return scale


def apply_metric_scale(scene: SparseScene, scale: float) -> SparseScene:
    """Rescale a reconstruction into metres.

    Only translation scales. Rotation is scale-invariant, so the world-to-camera
    matrices keep their 3x3 block and take a scaled translation column -- which
    is the same as moving every camera centre outward from the origin by `scale`.
    """
    frames = []
    for frame in scene.frames:
        scaled = frame.world_to_camera.copy()
        scaled[:3, 3] *= scale
        frames.append(FrameCamera(
            name=frame.name,
            path=frame.path,
            world_to_camera=scaled,
            intrinsics=frame.intrinsics,
            width=frame.width,
            height=frame.height,
            point3d_ids=frame.point3d_ids,
        ))
    return SparseScene(
        frames=frames,
        points_xyz=scene.points_xyz * scale,
        point_ids=scene.point_ids,
        scale_applied=scene.scale_applied * scale,
    )


def save_scene(scene: SparseScene, path: Path) -> None:
    """Persist the solved reconstruction so a merge can re-run without redoing SfM.

    Poses go to JSON because they are worth reading by eye when a merge looks
    wrong; the sparse cloud and per-frame observations go to a sibling .npz
    because they run to tens of thousands of points and JSON would be both huge
    and lossy about float precision.
    """
    payload = {
        "scale_applied": scene.scale_applied,
        "frames": [
            {
                "name": frame.name,
                "path": str(frame.path),
                "world_to_camera": frame.world_to_camera.tolist(),
                "intrinsics": frame.intrinsics.tolist(),
                "width": frame.width,
                "height": frame.height,
                "focal_px": frame.focal_px,
                "camera_centre": frame.camera_centre.tolist(),
            }
            for frame in scene.frames
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    np.savez_compressed(
        path.with_suffix(".npz"),
        points_xyz=scene.points_xyz,
        point_ids=scene.point_ids,
        **{f"obs_{i}": frame.point3d_ids for i, frame in enumerate(scene.frames)},
    )


def load_scene(path: Path) -> SparseScene:
    """Read back what `save_scene` wrote, sparse cloud included."""
    payload = json.loads(path.read_text(encoding="utf-8"))

    cloud_path = path.with_suffix(".npz")
    if cloud_path.exists():
        cloud = np.load(cloud_path)
        points_xyz = cloud["points_xyz"]
        point_ids = cloud["point_ids"]
        observations = [
            cloud[f"obs_{i}"] if f"obs_{i}" in cloud else None
            for i in range(len(payload["frames"]))
        ]
    else:
        points_xyz = np.empty((0, 3))
        point_ids = np.empty(0, dtype=np.int64)
        observations = [None] * len(payload["frames"])

    frames = [
        FrameCamera(
            name=entry["name"],
            path=Path(entry["path"]),
            world_to_camera=np.asarray(entry["world_to_camera"], dtype=np.float64),
            intrinsics=np.asarray(entry["intrinsics"], dtype=np.float64),
            width=int(entry["width"]),
            height=int(entry["height"]),
            point3d_ids=(observed if observed is not None
                         else np.empty(0, dtype=np.int64)),
        )
        for entry, observed in zip(payload["frames"], observations)
    ]
    return SparseScene(
        frames=frames,
        points_xyz=points_xyz,
        point_ids=point_ids,
        scale_applied=float(payload.get("scale_applied", 1.0)),
    )


def select_keyframes(scene: SparseScene, count: int) -> list[FrameCamera]:
    """Pick the frames to actually run SHARP on.

    SHARP emits roughly a million Gaussians per frame, so the merge is bounded by
    how many frames we predict from, not by how many COLMAP registered. Greedy
    farthest-point selection on camera centre keeps the chosen views spread
    around the walk instead of bunched where the walker slowed down.
    """
    if count >= len(scene.frames):
        return list(scene.frames)

    centres = np.asarray([frame.camera_centre for frame in scene.frames])
    chosen = [0]
    distances = np.linalg.norm(centres - centres[0], axis=1)
    while len(chosen) < count:
        pick = int(np.argmax(distances))
        chosen.append(pick)
        distances = np.minimum(distances, np.linalg.norm(centres - centres[pick], axis=1))

    chosen.sort()
    selected = [scene.frames[i] for i in chosen]
    span = np.linalg.norm(centres.max(axis=0) - centres.min(axis=0))
    print(f"[keyframes] selected {len(selected)} of {len(scene.frames)} "
          f"across a {span:.2f} m trajectory")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path,
                        help="walkthrough video (mp4, mov, ...) or a directory of stills")
    parser.add_argument("--out", type=Path, required=True, help="working directory")
    parser.add_argument("--target-frames", type=int, default=60,
                        help="how many frames to hand to COLMAP")
    parser.add_argument("--gpu", action="store_true",
                        help="try GPU SIFT (ignored: the PyPI pycolmap wheel has no CUDA)")
    args = parser.parse_args()

    frames_dir = args.out / "frames"
    if args.source.is_dir():
        frames = prepare_frames(args.source, frames_dir, target_frames=args.target_frames)
    else:
        frames = extract_frames(args.source, frames_dir, target_frames=args.target_frames)
    scene = solve_poses(frames_dir, args.out / "colmap", use_gpu=args.gpu)

    # Scale calibration needs SHARP depth maps, which live on the GPU side of the
    # pipeline; without them the trajectory is still useful, just not metric.
    save_scene(scene, args.out / "scene_sfm.json")
    print(f"[done] wrote {args.out / 'scene_sfm.json'} "
          f"({len(scene.frames)} posed frames, {len(frames)} extracted)")
    print("       scale is still arbitrary -- run the merge step to make it metric")
    return 0


if __name__ == "__main__":
    sys.exit(main())
