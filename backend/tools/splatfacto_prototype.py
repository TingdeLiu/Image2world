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

    (dataset_dir / "transforms.json").write_text(
        json.dumps(transforms, indent=2), encoding="utf-8"
    )
    return len(frames), has_seed_cloud


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
                ]
            )
        else:
            print(f"[prototype] reusing {scene_path}")

        # Refresh referenced files in place. Nerfstudio may briefly keep image
        # directory handles open after a failed WSL run, so deleting the whole
        # directory here makes an otherwise harmless retry flaky on Windows.
        registered, has_seed_cloud = prepare_nerfstudio_dataset(scene_path, dataset_dir)
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

        train_command = [
            "wsl.exe",
            "-d",
            args.wsl_distro,
            "--",
            *cuda_environment,
            ns_train,
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
            "--vis",
            "tensorboard",
        ]
        if args.resume_training:
            checkpoint = newest_checkpoint(training_dir)
            if checkpoint is not None:
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
        if args.reuse_training:
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
