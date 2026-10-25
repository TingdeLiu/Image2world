"""CPU-only contracts for held-out appearance checks and camera provenance."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

PROTOCOL = "held-out-rgb-v1"
LIMITATION = (
    "Appearance validation on views excluded from photometric training. "
    "Camera estimation and the seed cloud may use all input frames. "
    "These metrics do not certify geometry, navigation, or unseen-room completion."
)


def unavailable_report(reason: str) -> dict:
    return {"version": 1, "protocol": PROTOCOL, "status": "unavailable",
            "reason": reason, "metrics": {}, "limitation": LIMITATION}


def verify_holdout(train: list[Path], evaluation: list[Path]) -> str | None:
    """Refuse empty, shared, aliased or byte-identical evaluation evidence."""
    if not train or not evaluation:
        return "No held-out images are available in this training run."
    if {p.resolve() for p in train} & {p.resolve() for p in evaluation}:
        return "Training and evaluation images overlap."
    # Stream hashes: image datasets are large and Python 3.10 has no file_digest.
    def digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    train_hashes = {digest(p) for p in train}
    if any(digest(p) in train_hashes for p in evaluation):
        return "Evaluation contains images identical to training images."
    return None


def measured_report(train_count: int, evaluation_count: int, per_view: list[dict], examples: list[str]) -> dict:
    if len(per_view) != evaluation_count or not evaluation_count:
        raise ValueError("Evaluation did not measure every held-out image")
    metrics = {}
    for name in ("psnr", "ssim", "lpips"):
        values = np.asarray([view[name] for view in per_view], dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"Evaluation returned non-finite {name}")
        metrics[name] = float(values.mean())
        metrics[f"{name}_std"] = float(values.std())
    return {"version": 1, "protocol": PROTOCOL, "status": "measured",
            "train_views": train_count, "evaluation_views": evaluation_count,
            "split_verification": "resolved-paths-and-sha256", "metrics": metrics,
            "per_view": per_view, "examples": examples, "limitation": LIMITATION}


def comparison_pixels(observed: np.ndarray, predicted: np.ndarray) -> np.ndarray:
    """Compare uint8-cached or float observations with a float RGB render."""
    reference = np.asarray(observed)[..., :3]
    render = np.asarray(predicted)[..., :3]
    if reference.shape != render.shape or reference.ndim != 3 or reference.shape[-1] != 3:
        raise ValueError("Observation and render must have the same RGB dimensions")
    if not np.isfinite(reference).all() or not np.isfinite(render).all():
        raise ValueError("Comparison contains non-finite pixels")
    reference = reference.astype(np.float32) / (255 if reference.dtype == np.uint8 else 1)
    return np.rint(np.clip(np.concatenate([reference, render], axis=1), 0, 1) * 255).astype(np.uint8)


def export_camera_bundle(dataset: Path, parser_transform: Path, output: Path) -> dict:
    """Map original OpenGL c2w poses into the exported splat's normalized frame.

    Nerfstudio applies the dataparser rigid transform first, then scales camera
    translations. No preview centering or unit-cube normalization is applied.
    """
    source = json.loads((dataset / "transforms.json").read_text(encoding="utf-8"))
    parser = json.loads(parser_transform.read_text(encoding="utf-8"))
    rigid = np.eye(4)
    matrix = np.asarray(parser["transform"], dtype=float)
    if matrix.shape not in ((3, 4), (4, 4)) or not np.isfinite(matrix).all():
        raise ValueError("Invalid dataparser transform")
    rigid[:3] = matrix[:3]
    if matrix.shape == (4, 4) and not np.allclose(matrix[3], [0, 0, 0, 1]):
        raise ValueError("Invalid homogeneous dataparser transform")
    if not np.allclose(rigid[:3, :3].T @ rigid[:3, :3], np.eye(3), atol=1e-5):
        raise ValueError("Dataparser transform must be rigid")
    if not np.isclose(np.linalg.det(rigid[:3, :3]), 1.0, atol=1e-5):
        raise ValueError("Dataparser transform must preserve handedness")
    scale = float(parser["scale"])
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Invalid dataparser scale")
    frames = []
    for frame in source["frames"]:
        camera = np.asarray(frame["transform_matrix"], dtype=float)
        if (camera.shape != (4, 4) or not np.isfinite(camera).all()
                or not np.allclose(camera[3], [0, 0, 0, 1])
                or not np.allclose(camera[:3, :3].T @ camera[:3, :3], np.eye(3), atol=1e-5)
                or not np.isclose(np.linalg.det(camera[:3, :3]), 1.0, atol=1e-5)):
            raise ValueError("Invalid source camera pose")
        normalized = rigid @ camera
        normalized[:3, 3] *= scale
        intrinsics = {key: frame.get(key, source.get(key)) for key in ("fl_x", "fl_y", "cx", "cy", "w", "h")}
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in intrinsics.values()):
            raise ValueError("Invalid camera intrinsics")
        if any(intrinsics[key] <= 0 for key in ("fl_x", "fl_y", "w", "h")):
            raise ValueError("Invalid camera image size or focal length")
        frames.append({"image": Path(frame["file_path"]).name,
                       "camera_to_world": normalized.tolist(), **intrinsics})
    if not frames:
        raise ValueError("Camera bundle has no frames")
    bundle = {"version": 1, "camera_convention": "opengl", "pose_type": "camera-to-world",
              "world_frame": "exported-splat", "units": "arbitrary",
              "pose_source": "colmap-dataparser-before-camera-optimization",
              "source_to_splat": {"rigid_transform": rigid.tolist(), "translation_scale": scale},
              "frames": frames}
    output.write_text(json.dumps(bundle, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return bundle
