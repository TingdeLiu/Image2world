"""Throwaway video-to-Splatfacto prototype.

This is deliberately an isolated experiment, not the production API.  It
reuses the project's existing video frame selection + COLMAP pose solve, writes
a small Nerfstudio dataset, jointly optimises one Gaussian scene in WSL, and
exports a standard 3DGS PLY.

One-command smoke run (from the repository root):

    python backend/tools/splatfacto_prototype.py docs/media/demo-loop.mp4

The WSL environment is intentionally external to the Windows backend because
gsplat's CUDA extension is not distributed as a Windows wheel for this GPU.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

try:
    from .reconstruction_quality import export_camera_bundle, unavailable_report
    from .view_splits import assign_view_splits, freeze_dataset, freeze_recipe
    from .checkpoint_binding import remaining_training_steps
except ImportError:  # CLI entrypoint
    from reconstruction_quality import export_camera_bundle, unavailable_report
    from view_splits import assign_view_splits, freeze_dataset, freeze_recipe
    from checkpoint_binding import remaining_training_steps


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / ".artifacts" / "splatfacto-prototype"
DEFAULT_WSL_DISTRO = "Ubuntu-22.04"
DEFAULT_WSL_VENV = "/home/tingde/.venvs/image2world-splatfacto"


def write_state(path: Path, state: dict[str, object]) -> None:
    """Persist progress atomically so the API can poll while the job runs."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def run(command: list[str], *, cwd: Path = REPO_ROOT) -> float:
    """Run one visible stage and return its wall-clock duration."""
    printable = subprocess.list2cmdline(command)
    print(f"\n[prototype] $ {printable}", flush=True)
    started = time.perf_counter()
    subprocess.run(command, cwd=cwd, check=True)
    return time.perf_counter() - started


def wsl_path(path: Path, distro: str) -> str:
    result = subprocess.run(
        ["wsl.exe", "-d", distro, "--", "wslpath", "-a", str(path.resolve())],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def prepare_nerfstudio_dataset(scene_path: Path, dataset_dir: Path) -> tuple[int, bool]:
    """Convert the existing OpenCV world-to-camera JSON to Nerfstudio JSON."""
    payload = json.loads(scene_path.read_text(encoding="utf-8"))
    frames = payload.get("frames", [])
    if len(frames) < 3:
        raise RuntimeError(f"SfM registered only {len(frames)} frames")

    images_dir = dataset_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    transforms: dict[str, object] = {
        "camera_model": "OPENCV",
        "k1": 0.0,
        "k2": 0.0,
        "p1": 0.0,
        "p2": 0.0,
        "frames": [],
    }

    # COLMAP/SHARP camera axes are OpenCV (+x right, +y down, +z forward).
    # Nerfstudio transforms use OpenGL/Blender (+x right, +y up, +z back).
    cv_camera_to_gl_camera = np.diag([1.0, -1.0, -1.0, 1.0])

    for index, frame in enumerate(frames):
        source = Path(frame["path"])
        if not source.exists():
            # Old work directories can contain paths relative to the repository.
            source = (REPO_ROOT / source).resolve()
        if not source.exists():
            raise FileNotFoundError(f"registered frame is missing: {source}")

        destination = images_dir / f"frame_{index:04d}{source.suffix.lower()}"
        shutil.copy2(source, destination)

        world_to_camera = np.asarray(frame["world_to_camera"], dtype=np.float64)
        camera_to_world_gl = np.linalg.inv(world_to_camera) @ cv_camera_to_gl_camera
        intrinsics = np.asarray(frame["intrinsics"], dtype=np.float64)
        transforms["frames"].append(
            {
                "file_path": f"images/{destination.name}",
                "transform_matrix": camera_to_world_gl.tolist(),
                "fl_x": float(intrinsics[0, 0]),
                "fl_y": float(intrinsics[1, 1]),
                "cx": float(intrinsics[0, 2]),
                "cy": float(intrinsics[1, 2]),
                "w": int(frame["width"]),
                "h": int(frame["height"]),
            }
        )

    # Splatfacto converges faster and more reliably when it can seed Gaussian
    # means from the SfM cloud.  The demo video in this repository is a rendered
    # showcase rather than raw capture and sometimes yields fewer than four
    # points; in that degenerate case training falls back to random init.
    cloud_path = scene_path.with_suffix(".npz")
    has_seed_cloud = False
    if cloud_path.exists():
        points = np.load(cloud_path)["points_xyz"]
        finite = points[np.isfinite(points).all(axis=1)]
        if len(finite) >= 4:
            ply_path = dataset_dir / "sparse_pc.ply"
            lines = [
                "ply",
                "format ascii 1.0",
                f"element vertex {len(finite)}",
                "property float x",
                "property float y",
                "property float z",
                "property uchar red",
                "property uchar green",
                "property uchar blue",
                "end_header",
            ]
            lines.extend(f"{x:.9g} {y:.9g} {z:.9g} 128 128 128" for x, y, z in finite)
            ply_path.write_text("\n".join(lines) + "\n", encoding="ascii")
            transforms["ply_file_path"] = ply_path.name
            has_seed_cloud = True

    transforms = assign_view_splits(transforms)
    (dataset_dir / "transforms.json").write_text(
        json.dumps(transforms, indent=2), encoding="utf-8"
    )
    return len(frames), has_seed_cloud


def capture_metrics(scene_path: Path, total_frames: int | None = None) -> dict[str, int | float]:
    """Summarise whether the registered camera track is useful for navigation.

    A successful COLMAP exit is not enough: it can register a short part of a
    walkthrough and still produce a plausible looking splat. These metrics are
    persisted with the job so the UI and published world can explain what was
    actually reconstructed.
    """
    payload = json.loads(scene_path.read_text(encoding="utf-8"))
    frames = payload.get("frames", [])
    centres: list[np.ndarray] = []
    for frame in frames:
        centre = frame.get("camera_centre")
        if centre is None:
            matrix = np.asarray(frame.get("world_to_camera"), dtype=np.float64)
            if matrix.shape != (4, 4):
                continue
            centre = -matrix[:3, :3].T @ matrix[:3, 3]
        value = np.asarray(centre, dtype=np.float64)
        if value.shape == (3,) and np.isfinite(value).all():
            centres.append(value)

    registered = len(frames)
    extracted = max(registered, int(total_frames if total_frames is not None else registered))
    registration_rate = registered / extracted if extracted else 0.0
    scene_extent = 0.0
    cloud_path = scene_path.with_suffix(".npz")
    if cloud_path.exists():
        with np.load(cloud_path) as cloud:
            points = np.asarray(
                cloud["points_xyz"] if "points_xyz" in cloud.files else np.empty((0, 3)),
                dtype=np.float64,
            )
        finite_points = points[np.isfinite(points).all(axis=1)] if points.ndim == 2 else np.empty((0, 3))
        if len(finite_points) >= 2:
            # COLMAP's scale is arbitrary. A robust scene extent makes the
            # coverage ratio invariant to that scale and ignores outlier points.
            scene_extent = float(np.linalg.norm(
                np.percentile(finite_points, 95, axis=0)
                - np.percentile(finite_points, 5, axis=0)
            ))

    if centres:
        track = np.asarray(centres)
        trajectory_span = float(np.linalg.norm(track.max(axis=0) - track.min(axis=0)))
        trajectory_length = float(np.linalg.norm(np.diff(track, axis=0), axis=1).sum()) if len(track) > 1 else 0.0
    else:
        trajectory_span = 0.0
        trajectory_length = 0.0
    trajectory_coverage = trajectory_span / scene_extent if scene_extent > 1e-9 else 0.0

    return {
        "registered_frames": registered,
        "extracted_frames": extracted,
        "registration_rate": registration_rate,
        "trajectory_span": trajectory_span,
        "trajectory_length": trajectory_length,
        "scene_extent": scene_extent,
        "trajectory_coverage": trajectory_coverage,
    }


def validate_capture(
    metrics: dict[str, int | float],
    *,
    min_registration_rate: float = 0.0,
    min_registered_frames: int = 0,
    min_trajectory_coverage: float = 0.0,
) -> None:
    """Reject a partial or stationary capture before expensive GPU training."""
    failures: list[str] = []
    registered = int(metrics.get("registered_frames", 0))
    rate = float(metrics.get("registration_rate", 0.0))
    coverage = float(metrics.get("trajectory_coverage", 0.0))
    if min_registered_frames and registered < min_registered_frames:
        failures.append(f"only {registered} registered frames (need at least {min_registered_frames})")
    if min_registration_rate and rate < min_registration_rate:
        failures.append(
            f"registration is {rate:.0%} (need at least {min_registration_rate:.0%})"
        )
    if min_trajectory_coverage and coverage < min_trajectory_coverage:
        failures.append(
            f"camera coverage is {coverage:.0%} of the reconstructed scene (need at least {min_trajectory_coverage:.0%})"
        )
    if failures:
        raise RuntimeError(
            "Navigation capture rejected: "
            + "; ".join(failures)
            + ". Walk slowly through connected spaces, keep the camera moving, "
            "and include a return pass with textured surfaces in view."
        )


def newest_config(training_dir: Path) -> Path:
    configs = list(training_dir.rglob("config.yml"))
    if not configs:
        raise RuntimeError(f"training finished without a config.yml under {training_dir}")
    return max(configs, key=lambda path: path.stat().st_mtime)


def newest_checkpoint(training_dir: Path) -> Path | None:
    checkpoints = list(training_dir.rglob("*.ckpt"))
    if not checkpoints:
        return None
    return max(checkpoints, key=lambda path: path.stat().st_mtime)


def ply_vertex_count(path: Path) -> int | None:
    with path.open("rb") as handle:
        header = handle.read(16_384).decode("ascii", errors="ignore")
    for line in header.splitlines():
        if line.startswith("element vertex "):
            return int(line.rsplit(" ", 1)[1])
    return None


def recommended_initial_view(dataset_dir: Path, config: Path) -> dict[str, object] | None:
    """Return the first registered camera in exported Nerfstudio coordinates."""
    transforms_path = dataset_dir / "transforms.json"
    dataparser_path = config.parent / "dataparser_transforms.json"
    if not transforms_path.exists() or not dataparser_path.exists():
        return None

    transforms = json.loads(transforms_path.read_text(encoding="utf-8"))
    frames = transforms.get("frames", [])
    if not frames:
        return None
    pose = np.asarray(frames[0]["transform_matrix"], dtype=np.float64)
    dataparser = json.loads(dataparser_path.read_text(encoding="utf-8"))
    transform = np.asarray(dataparser["transform"], dtype=np.float64)
    scale = float(dataparser["scale"])
    if pose.shape != (4, 4) or transform.shape != (3, 4):
        return None

    rotation = transform[:, :3] @ pose[:3, :3]
    position = (transform[:, :3] @ pose[:3, 3] + transform[:, 3]) * scale
    # Nerfstudio's oriented scene is Z-up; the product viewer is Three.js
    # Y-up. Rotate the exported splat, collider and camera together by -90° X.
    viewer_from_nerfstudio = np.asarray(
        [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]],
        dtype=np.float64,
    )
    rotation = viewer_from_nerfstudio @ rotation
    position = viewer_from_nerfstudio @ position
    forward = -rotation[:, 2]
    yaw = math.atan2(float(-forward[0]), float(-forward[2]))
    pitch = math.asin(float(np.clip(forward[1], -1.0, 1.0)))
    return {
        "position": [float(value) for value in position],
        "yaw": yaw,
        "pitch": pitch,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="walkthrough video or ordered image directory")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="disposable experiment directory")
    parser.add_argument("--target-frames", type=int, default=60, help="maximum frames handed to SfM")
    parser.add_argument(
        "--frames-per-window",
        type=int,
        default=2,
        help="candidate frames represented by each selected frame; 1 keeps every sharp frame",
    )
    parser.add_argument(
        "--sequential-overlap",
        type=int,
        default=5,
        help="number of neighbouring frames COLMAP matches",
    )
    parser.add_argument(
        "--min-registration-rate",
        type=float,
        default=0.0,
        help="reject captures with a lower registered/extracted frame ratio",
    )
    parser.add_argument(
        "--min-registered-frames",
        type=int,
        default=0,
        help="reject captures with fewer registered frames",
    )
    parser.add_argument(
        "--min-trajectory-coverage",
        type=float,
        default=0.0,
        help="reject captures whose camera path covers too little of the reconstructed scene",
    )
    parser.add_argument("--iterations", type=int, default=5_000, help="Splatfacto optimisation steps")
    parser.add_argument(
        "--camera-scale",
        type=float,
        default=0.75,
        help="training image scale; 0.75 is safe for 16 GB GPUs",
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=5_000,
        help="checkpoint interval so long jobs remain resumable",
    )
    parser.add_argument(
        "--eval-every",
        type=int,
        default=50_000,
        help="training-time image evaluation interval; keep above iterations to avoid huge logs",
    )
    parser.add_argument("--wsl-distro", default=DEFAULT_WSL_DISTRO)
    parser.add_argument("--wsl-venv", default=DEFAULT_WSL_VENV)
    parser.add_argument("--reuse-sfm", action="store_true", help="reuse <out>/sfm/scene_sfm.json")
    parser.add_argument("--reuse-training", action="store_true", help="reuse newest completed training run")
    parser.add_argument("--skip-evaluation", action="store_true", help="record evaluation as unavailable instead of measuring held-out views")
    parser.add_argument("--evaluation-split", choices=("val", "test"), default="test")
    parser.add_argument("--scale-regularization", choices=("off", "on"), default="off")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--resume-training",
        action="store_true",
        help="resume the newest checkpoint under <out>/training when available",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    out = args.out.resolve()
    sfm_dir = out / "sfm"
    dataset_dir = out / "dataset"
    training_dir = out / "training"
    export_dir = out / "export"
    state_path = out / "prototype_state.json"
    out.mkdir(parents=True, exist_ok=True)

    state: dict[str, object] = {
        "prototype": True,
        "source": str(source),
        "status": "running",
        "stage": "initializing",
        "progress": 1,
        "target_frames": args.target_frames,
        "iterations": args.iterations,
        "camera_scale": args.camera_scale,
        "frames_per_window": args.frames_per_window,
        "sequential_overlap": args.sequential_overlap,
        "scale_regularization": args.scale_regularization,
        "seed": args.seed,
        "evaluation_split": args.evaluation_split,
        "min_registration_rate": args.min_registration_rate,
        "min_registered_frames": args.min_registered_frames,
        "min_trajectory_coverage": args.min_trajectory_coverage,
        "timings_seconds": {},
    }
    write_state(state_path, state)

    try:
        scene_path = sfm_dir / "scene_sfm.json"
        if not (args.reuse_sfm and scene_path.exists()):
            state.update({"stage": "sfm", "progress": 8})
            write_state(state_path, state)
            state["timings_seconds"]["sfm"] = run(
                [
                    sys.executable,
                    str(REPO_ROOT / "backend" / "tools" / "multiview_sfm.py"),
                    str(source),
                    "--out",
                    str(sfm_dir),
                    "--target-frames",
                    str(args.target_frames),
                    "--frames-per-window",
                    str(args.frames_per_window),
                    "--sequential-overlap",
                    str(args.sequential_overlap),
                ]
            )
        else:
            print(f"[prototype] reusing {scene_path}")

        frames_dir = sfm_dir / "frames"
        extracted_frames = len(list(frames_dir.glob("frame_*.*")))
        metrics = capture_metrics(scene_path, extracted_frames)
        state.update(metrics)
        write_state(state_path, state)
        validate_capture(
            metrics,
            min_registration_rate=args.min_registration_rate,
            min_registered_frames=args.min_registered_frames,
            min_trajectory_coverage=args.min_trajectory_coverage,
        )

        # Refresh referenced files in place. Nerfstudio may briefly keep image
        # directory handles open after a failed WSL run, so deleting the whole
        # directory here makes an otherwise harmless retry flaky on Windows.
        if (args.resume_training or args.reuse_training) and (dataset_dir / "transforms.json").exists():
            frozen = json.loads((dataset_dir / "transforms.json").read_text(encoding="utf-8"))
            registered, has_seed_cloud = len(frozen["frames"]), bool(frozen.get("ply_file_path"))
        else:
            registered, has_seed_cloud = prepare_nerfstudio_dataset(scene_path, dataset_dir)
        manifest = freeze_dataset(dataset_dir)
        state["dataset_fingerprint"] = manifest["fingerprint"]
        state["independent_test"] = manifest["independent_test"]
        freeze_recipe(out, {"camera_scale": args.camera_scale, "seed": args.seed,
                            "scale_regularization": args.scale_regularization})
        state["registered_frames"] = registered
        state["sparse_seed_cloud"] = has_seed_cloud
        state.update({"stage": "training", "progress": 32})
        write_state(state_path, state)

        ns_train = f"{args.wsl_venv}/bin/ns-train"
        ns_export = f"{args.wsl_venv}/bin/ns-export"
        cuda_environment = [
            "env",
            "CUDA_HOME=/usr/local/cuda-12.8",
            "TORCH_CUDA_ARCH_LIST=12.0",
            # Nerfstudio 1.1.5 predates PyTorch 2.6's weights_only=True default.
            # The checkpoint is produced locally by this same command.
            "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1",
            # nvcc can consume several GiB per translation unit.  WSL chooses
            # ten Ninja workers on this machine by default and gets OOM-killed;
            # a one-time serial build is slower but reliable and then cached.
            "MAX_JOBS=1",
            f"PATH={args.wsl_venv}/bin:/usr/local/cuda-12.8/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64",
        ]
        dataset_wsl = wsl_path(dataset_dir, args.wsl_distro)
        training_wsl = wsl_path(training_dir, args.wsl_distro)
        train_entrypoint = [f"{args.wsl_venv}/bin/python", wsl_path(REPO_ROOT / "backend/tools/train_splatfacto.py", args.wsl_distro)] if args.resume_training else [ns_train]

        train_command = [
            "wsl.exe",
            "-d",
            args.wsl_distro,
            "--",
            *cuda_environment,
            *train_entrypoint,
            "splatfacto",
            "--data",
            dataset_wsl,
            "--output-dir",
            training_wsl,
            "--max-num-iterations",
            str(args.iterations),
            "--steps-per-save",
            str(max(1, min(args.save_every, args.iterations))),
            "--steps-per-eval-batch",
            str(args.eval_every),
            "--steps-per-eval-image",
            str(args.eval_every),
            "--steps-per-eval-all-images",
            str(args.eval_every),
            "--pipeline.datamanager.camera-res-scale-factor",
            str(args.camera_scale),
            "--pipeline.model.use-scale-regularization",
            str(args.scale_regularization == "on"),
            "--machine.seed",
            str(args.seed),
            "--vis",
            "tensorboard",
        ]
        remaining = args.iterations
        if args.resume_training:
            checkpoint = newest_checkpoint(training_dir)
            if checkpoint is not None:
                completed = int(checkpoint.stem.split("-")[-1]) + 1
                remaining = remaining_training_steps(args.iterations, completed)
                state["resume_from_step"] = completed
                train_command[train_command.index("--max-num-iterations") + 1] = str(remaining)
                print(f"[prototype] resuming {checkpoint}", flush=True)
                train_command.extend(
                    ["--load-dir", wsl_path(checkpoint.parent, args.wsl_distro)]
                )
        if not has_seed_cloud:
            train_command.extend(
                [
                    "--pipeline.model.random-init",
                    "True",
                    "--pipeline.model.random-scale",
                    "2.0",
                ]
            )
        state["training_steps_this_run"] = 0 if args.reuse_training else remaining
        if args.reuse_training or remaining == 0:
            print(f"[prototype] reusing newest training run under {training_dir}")
        else:
            state["timings_seconds"]["train"] = run(train_command)

        config = newest_config(training_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        state.update({"stage": "exporting", "progress": 92, "config": str(config)})
        write_state(state_path, state)
        state["timings_seconds"]["export"] = run(
            [
                "wsl.exe",
                "-d",
                args.wsl_distro,
                "--",
                *cuda_environment,
                ns_export,
                "gaussian-splat",
                "--load-config",
                wsl_path(config, args.wsl_distro),
                "--output-dir",
                wsl_path(export_dir, args.wsl_distro),
            ]
        )

        plys = list(export_dir.rglob("*.ply"))
        if not plys:
            raise RuntimeError(f"export finished without a PLY under {export_dir}")
        output_ply = max(plys, key=lambda path: path.stat().st_size)
        # Preserve cameras in splat coordinates, not display-preview units.
        # Reused checkpoints retain their original training/evaluation split.
        try:
            camera_path = out / "cameras.json"
            export_camera_bundle(dataset_dir, config.parent / "dataparser_transforms.json", camera_path)
            state["camera_metadata_path"] = str(camera_path)
        except (ValueError, KeyError, OSError) as error:
            state["camera_metadata_error"] = f"{type(error).__name__}: {error}"
            print(f"[quality] camera export unavailable: {error}", flush=True)

        state.update({"stage": "evaluating", "progress": 96})
        write_state(state_path, state)
        evaluation_path = out / "evaluation" / "report.json"
        evaluation_path.parent.mkdir(parents=True, exist_ok=True)
        quality = unavailable_report("Quality evaluation was skipped for this run.")
        if not args.skip_evaluation:
            try:
                state["timings_seconds"]["evaluation"] = run([
                    "wsl.exe", "-d", args.wsl_distro, "--", *cuda_environment,
                    f"{args.wsl_venv}/bin/python",
                    wsl_path(REPO_ROOT / "backend" / "tools" / "evaluate_reconstruction.py", args.wsl_distro),
                    "--load-config", wsl_path(config, args.wsl_distro),
                    "--output", wsl_path(evaluation_path, args.wsl_distro),
                    "--split", args.evaluation_split,
                ])
                quality = json.loads(evaluation_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                print(f"[quality] evaluation unavailable: {error}", flush=True)
                quality = unavailable_report("Quality evaluation failed; see the reconstruction job log.")
        write_state(evaluation_path, quality)
        state["quality_report"] = quality
        state["quality_report_path"] = str(evaluation_path)
        state.update(
            {
                "status": "complete",
                "stage": "complete",
                "progress": 100,
                "config": str(config),
                "output_ply": str(output_ply),
                "output_bytes": output_ply.stat().st_size,
                "gaussian_count": ply_vertex_count(output_ply),
                "initial_view": recommended_initial_view(dataset_dir, config),
            }
        )
        print(f"\n[prototype] complete: {output_ply}")
        print(f"[prototype] size: {output_ply.stat().st_size / 1_048_576:.1f} MiB")
        return 0
    except Exception as error:
        state["status"] = "failed"
        state["stage"] = "failed"
        state["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        write_state(state_path, state)


if __name__ == "__main__":
    raise SystemExit(main())
