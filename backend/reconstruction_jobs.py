"""Persistent local video-to-Splatfacto reconstruction jobs.

Uploads are streamed to disk by FastAPI, while this module owns the durable job
state and the one-at-a-time GPU worker.  The browser can disconnect without
canceling a reconstruction and interrupted jobs can reuse SfM/checkpoints.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

try:  # package import under pytest, script import under `python backend/server.py`
    from .gpu_scheduler import GPU_JOB_LOCK
except ImportError:  # pragma: no cover - exercised by the production entrypoint
    from gpu_scheduler import GPU_JOB_LOCK

# Set by server.py at import time. Reconstruction runs `ns-train` in a
# subprocess, which cannot see -- let alone reuse -- the VRAM the FastAPI
# process is holding, so the models loaded by earlier requests have to go before
# the training starts or a 16 GB card runs out. Left as None when this module is
# imported on its own, as the tests do.
release_gpu_models: Callable[[], Any] | None = None


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOB_ROOT = REPO_ROOT / ".artifacts" / "reconstruction-jobs"
JOB_ROOT = Path(os.environ.get("IMAGEWORLD_RECONSTRUCTION_JOB_ROOT", DEFAULT_JOB_ROOT))
MAX_VIDEO_BYTES = int(os.environ.get("IMAGEWORLD_MAX_VIDEO_BYTES", 5 * 1024**3))
UPLOAD_CHUNK_BYTES = 8 * 1024**2

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
VIDEO_CONTENT_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-matroska",
    "video/webm",
    "video/x-msvideo",
    "video/x-m4v",
    "application/octet-stream",
}
QUALITY_PROFILES: dict[str, dict[str, int | float]] = {
    "preview": {
        "target_frames": 140,
        "iterations": 7_000,
        "camera_scale": 0.5,
        "frames_per_window": 2,
        "sequential_overlap": 5,
        "min_registration_rate": 0.0,
        "min_registered_frames": 0,
        "min_trajectory_coverage": 0.0,
    },
    "standard": {
        "target_frames": 280,
        "iterations": 20_000,
        "camera_scale": 0.75,
        "frames_per_window": 2,
        "sequential_overlap": 5,
        "min_registration_rate": 0.0,
        "min_registered_frames": 0,
        "min_trajectory_coverage": 0.0,
    },
    "quality": {
        "target_frames": 400,
        "iterations": 30_000,
        "camera_scale": 0.75,
        "frames_per_window": 1,
        "sequential_overlap": 6,
        "min_registration_rate": 0.0,
        "min_registered_frames": 0,
        "min_trajectory_coverage": 0.0,
    },
    # This is deliberately a separate profile: navigation needs coverage and
    # a connected camera track, so it trades GPU time for more overlap and
    # rejects partial captures before they become published worlds.
    "navigation": {
        "target_frames": 480,
        "iterations": 45_000,
        "camera_scale": 0.75,
        "frames_per_window": 1,
        "sequential_overlap": 8,
        "min_registration_rate": 0.75,
        "min_registered_frames": 24,
        "min_trajectory_coverage": 0.05,
    },
}
PREVIEW_ITERATIONS = 2000

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
STEP_RE = re.compile(r"\bStep\s+(\d+)\b", re.IGNORECASE)
REGISTERED_RE = re.compile(r"registered\s+(\d+)/(\d+)\s+frames", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def progress_from_output(text: str, iterations: int) -> dict[str, Any] | None:
    """Map human-readable COLMAP/Nerfstudio output into stable UI progress."""
    clean = ANSI_RE.sub("", text)
    lowered = clean.lower()
    registered = REGISTERED_RE.search(clean)
    if registered:
        complete, total = (int(value) for value in registered.groups())
        return {
            "stage": "sfm",
            "progress": 30,
            "message": "Camera tracking complete",
            "detail": f"Registered {complete}/{total} frames",
            "registered_frames": complete,
            "extracted_frames": total,
        }

    steps = [int(value) for value in STEP_RE.findall(clean)]
    if steps:
        step = max(steps)
        fraction = min(max(step / max(iterations, 1), 0.0), 1.0)
        return {
            "stage": "training",
            "progress": round(32 + fraction * 57),
            "message": "Optimizing the Gaussian world",
            "detail": f"Splatfacto step {step:,}/{iterations:,}",
            "training_step": step,
        }

    if "extracting features" in lowered:
        return {"stage": "sfm", "progress": 12, "message": "Tracking visual features"}
    if "matching sequential pairs" in lowered:
        return {"stage": "sfm", "progress": 18, "message": "Matching adjacent frames"}
    if "incremental mapping" in lowered:
        return {"stage": "sfm", "progress": 23, "message": "Solving camera positions"}
    if "evaluate_reconstruction.py" in lowered or "[quality]" in lowered:
        return {"stage": "evaluating", "progress": 96, "message": "Checking held-out views"}
    if "ns-export" in lowered or "gaussians have" in lowered:
        return {"stage": "exporting", "progress": 94, "message": "Exporting Gaussian PLY"}
    return None


class ReconstructionJobManager:
    def __init__(self, root: Path = JOB_ROOT):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._recover_interrupted_jobs()

    def _job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", job_id):
            raise KeyError(job_id)
        return self.root / job_id

    def _state_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _read(self, job_id: str) -> dict[str, Any]:
        path = self._state_path(job_id)
        if not path.exists():
            raise KeyError(job_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, job_id: str, state: dict[str, Any]) -> dict[str, Any]:
        state["updated_at"] = utc_now()
        atomic_json(self._state_path(job_id), state)
        return state

    def _recover_interrupted_jobs(self) -> None:
        for state_path in self.root.glob("*/job.json"):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("status") in {"queued", "running", "canceling"}:
                state.update(
                    {
                        "status": "interrupted",
                        "stage": "interrupted",
                        "message": "Backend restarted; this job can be resumed",
                        "resumable": True,
                        "updated_at": utc_now(),
                    }
                )
                atomic_json(state_path, state)

    def create(
        self,
        *,
        name: str,
        quality: str,
        filename: str,
        content_type: str | None,
        camera_height: float | None,
        preview_first: bool = False,
    ) -> tuple[dict[str, Any], Path]:
        if quality not in QUALITY_PROFILES:
            raise ValueError(f"Unknown quality profile: {quality}")
        suffix = Path(filename).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            raise ValueError("Unsupported video extension")
        if content_type and content_type not in VIDEO_CONTENT_TYPES and not content_type.startswith("video/"):
            raise ValueError("Unsupported video content type")

        job_id = uuid.uuid4().hex[:12]
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True)
        source_path = job_dir / f"source{suffix}"
        profile = QUALITY_PROFILES[quality]
        state: dict[str, Any] = {
            "id": job_id,
            "name": name,
            "quality": quality,
            "preview_first": preview_first,
            "phase": "preview" if preview_first else "final",
            "run_iterations": min(PREVIEW_ITERATIONS, int(profile["iterations"])) if preview_first else profile["iterations"],
            "status": "uploading",
            "stage": "uploading",
            "progress": 1,
            "message": "Uploading source video",
            "detail": filename,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "source_path": str(source_path),
            "source_filename": filename,
            "content_type": content_type,
            "camera_height": camera_height,
            "target_frames": profile["target_frames"],
            "iterations": profile["iterations"],
            "camera_scale": profile["camera_scale"],
            "frames_per_window": profile["frames_per_window"],
            "sequential_overlap": profile["sequential_overlap"],
            "min_registration_rate": profile["min_registration_rate"],
            "min_registered_frames": profile["min_registered_frames"],
            "min_trajectory_coverage": profile["min_trajectory_coverage"],
            "uploaded_bytes": 0,
            "resumable": False,
            "cancel_requested": False,
        }
        with self._lock:
            self._write(job_id, state)
        return state, source_path

    def update_upload(self, job_id: str, uploaded_bytes: int) -> None:
        with self._lock:
            state = self._read(job_id)
            state["uploaded_bytes"] = uploaded_bytes
            self._write(job_id, state)

    def fail(self, job_id: str, message: str) -> dict[str, Any]:
        with self._lock:
            state = self._read(job_id)
            state.update(
                {
                    "status": "failed",
                    "stage": "failed",
                    "message": message,
                    "error": message,
                    "resumable": self._has_reusable_work(job_id),
                }
            )
            return self._write(job_id, state)

    def _has_reusable_work(self, job_id: str) -> bool:
        work_dir = self._job_dir(job_id) / "work"
        return (work_dir / "sfm" / "scene_sfm.json").exists() or any(work_dir.rglob("*.ckpt"))

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._read(job_id)

    def list(self) -> list[dict[str, Any]]:
        states: list[dict[str, Any]] = []
        for path in self.root.glob("*/job.json"):
            try:
                states.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(states, key=lambda state: state.get("created_at", ""), reverse=True)

    def start(self, job_id: str, *, resume: bool = False) -> dict[str, Any]:
        with self._lock:
            state = self._read(job_id)
            if state["status"] in {"queued", "running", "canceling"}:
                raise ValueError("Job is already active")
            if state["status"] == "awaiting_review":
                raise ValueError("Choose refinement or accept the preview before continuing")
            if resume and not self._has_reusable_work(job_id):
                raise ValueError("Job has no reusable SfM data or checkpoint")
            state.update(
                {
                    "status": "queued",
                    "stage": "queued",
                    "progress": max(2, int(state.get("progress", 0))),
                    "message": "Waiting for the local reconstruction worker",
                    "detail": "GPU jobs run one at a time",
                    "cancel_requested": False,
                    "resumable": resume,
                    "error": None,
                }
            )
            self._write(job_id, state)
            thread = threading.Thread(
                target=self._run_job,
                args=(job_id, resume),
                name=f"reconstruction-{job_id}",
                daemon=True,
            )
            self._threads[job_id] = thread
            thread.start()
            return state

    def mark_uploaded_and_start(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._read(job_id)
            state.update({"status": "ready", "stage": "queued", "progress": 3})
            self._write(job_id, state)
        return self.start(job_id)

    def _update_progress(self, job_id: str, update: dict[str, Any]) -> None:
        with self._lock:
            state = self._read(job_id)
            if state.get("cancel_requested"):
                return
            old_step = int(state.get("training_step", 0))
            new_step = int(update.get("training_step", old_step))
            stage_changed = update.get("stage") != state.get("stage")
            if new_step and not stage_changed and new_step - old_step < 100:
                return
            state.update(update)
            self._write(job_id, state)

    def _run_job(self, job_id: str, resume: bool) -> None:
        try:
            with GPU_JOB_LOCK:
                with self._lock:
                    state = self._read(job_id)
                    if state.get("cancel_requested"):
                        return
                    profile = QUALITY_PROFILES.get(str(state.get("quality")), QUALITY_PROFILES["standard"])
                    # Jobs created before the navigation profile was added can
                    # still be resumed; fill in the new knobs from their saved
                    # quality name instead of failing on a missing key.
                    for key in (
                        "frames_per_window",
                        "sequential_overlap",
                        "min_registration_rate",
                        "min_registered_frames",
                        "min_trajectory_coverage",
                    ):
                        state.setdefault(key, profile[key])
                    state.update(
                        {
                            "status": "running",
                            "stage": "initializing",
                            "progress": max(4, int(state.get("progress", 0))),
                            "message": "Preparing video reconstruction",
                            "detail": "Starting COLMAP and Splatfacto",
                        }
                    )
                    self._write(job_id, state)

                job_dir = self._job_dir(job_id)
                work_dir = job_dir / "work"
                command = [
                    sys.executable,
                    str(REPO_ROOT / "backend" / "tools" / "splatfacto_prototype.py"),
                    state["source_path"],
                    "--out",
                    str(work_dir),
                    "--target-frames",
                    str(state["target_frames"]),
                    "--iterations",
                    str(state.get("run_iterations", state["iterations"])),
                    "--camera-scale",
                    str(state["camera_scale"]),
                    "--frames-per-window",
                    str(state["frames_per_window"]),
                    "--sequential-overlap",
                    str(state["sequential_overlap"]),
                    "--min-registration-rate",
                    str(state["min_registration_rate"]),
                    "--min-registered-frames",
                    str(state["min_registered_frames"]),
                    "--min-trajectory-coverage",
                    str(state["min_trajectory_coverage"]),
                    "--save-every",
                    "5000",
                    "--eval-every",
                    "50000",
                    "--evaluation-split",
                    "val" if state.get("phase") == "preview" else "test",
                    "--scale-regularization",
                    state.get("scale_regularization", "off"),
                ]
                if resume:
                    command.extend(["--reuse-sfm", "--resume-training"])
                if state.get("reuse_training"):
                    command.append("--reuse-training")

                # The GPU lock is held here, so nothing is mid-inference: this is
                # the one safe moment to hand the card over to the subprocess.
                if release_gpu_models is not None:
                    try:
                        release_gpu_models()
                    except Exception as error:  # pragma: no cover - best effort
                        print(f"Could not release model weights before training: {error}")

                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                environment = os.environ.copy()
                environment["PYTHONUNBUFFERED"] = "1"
                process = subprocess.Popen(
                    command,
                    cwd=REPO_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                    env=environment,
                )
                with self._lock:
                    self._processes[job_id] = process

                log_path = job_dir / "job.log"
                with log_path.open("a", encoding="utf-8", errors="replace") as log:
                    assert process.stdout is not None
                    for line in process.stdout:
                        log.write(line)
                        progress = progress_from_output(line, int(state.get("run_iterations", state["iterations"])))
                        if progress:
                            self._update_progress(job_id, progress)

                return_code = process.wait()
                with self._lock:
                    self._processes.pop(job_id, None)
                    current = self._read(job_id)
                    if current.get("cancel_requested"):
                        current.update(
                            {
                                "status": "canceled",
                                "stage": "canceled",
                                "message": "Reconstruction canceled",
                                "detail": "Saved checkpoints can be resumed",
                                "resumable": self._has_reusable_work(job_id),
                            }
                        )
                        self._write(job_id, current)
                        return

                prototype_path = work_dir / "prototype_state.json"
                prototype = (
                    json.loads(prototype_path.read_text(encoding="utf-8"))
                    if prototype_path.exists()
                    else {}
                )
                if return_code != 0 or prototype.get("status") != "complete":
                    detail = prototype.get("error") or f"Reconstruction process exited with code {return_code}"
                    self.fail(job_id, str(detail))
                    return

                output_ply = Path(prototype["output_ply"]).resolve()
                if not output_ply.exists() or work_dir.resolve() not in output_ply.parents:
                    self.fail(job_id, "Reconstruction completed without a safe PLY output")
                    return

                thumbnail = job_dir / "thumbnail.jpg"
                images = sorted((work_dir / "dataset" / "images").glob("*"))
                if images:
                    shutil.copy2(images[len(images) // 2], thumbnail)

                with self._lock:
                    completed = self._read(job_id)
                    completed.update(
                        {
                            "status": "awaiting_review" if state.get("phase") == "preview" else "complete",
                            "stage": "awaiting_review" if state.get("phase") == "preview" else "complete",
                            "progress": 100,
                            "message": "Preview ready for review" if state.get("phase") == "preview" else "Gaussian world ready",
                            "detail": f"{prototype.get('gaussian_count', 0):,} Gaussians",
                            "registered_frames": prototype.get("registered_frames"),
                            "extracted_frames": prototype.get("extracted_frames"),
                            "registration_rate": prototype.get("registration_rate"),
                            "trajectory_span": prototype.get("trajectory_span"),
                            "trajectory_length": prototype.get("trajectory_length"),
                            "trajectory_coverage": prototype.get("trajectory_coverage"),
                            "gaussian_count": prototype.get("gaussian_count"),
                            "output_bytes": prototype.get("output_bytes"),
                            "output_ply": str(output_ply),
                            "config": prototype.get("config"),
                            "initial_view": prototype.get("initial_view"),
                            "quality_report": prototype.get("quality_report"),
                            "quality_report_path": prototype.get("quality_report_path"),
                            "camera_metadata_path": prototype.get("camera_metadata_path"),
                            "dataset_fingerprint": prototype.get("dataset_fingerprint"),
                            "independent_test": prototype.get("independent_test"),
                            "thumbnail_path": str(thumbnail) if thumbnail.exists() else None,
                            "resumable": True,
                        }
                    )
                    self._write(job_id, completed)
        except Exception as error:  # keep worker failures visible to polling clients
            try:
                self.fail(job_id, f"{type(error).__name__}: {error}")
            except Exception:
                pass
        finally:
            with self._lock:
                # Review can queue the next phase as soon as the previous
                # phase persists its result. Its worker must keep its handles.
                if self._threads.get(job_id) is threading.current_thread():
                    self._processes.pop(job_id, None)
                    self._threads.pop(job_id, None)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._read(job_id)
            if state["status"] in {"complete", "failed", "canceled", "interrupted"}:
                return state
            if state["status"] in {"awaiting_review", "queued"}:
                state.update({"status": "canceled", "stage": "canceled", "cancel_requested": True,
                              "message": "Reconstruction canceled", "resumable": self._has_reusable_work(job_id)})
                return self._write(job_id, state)
            state.update(
                {
                    "status": "canceling",
                    "stage": "canceling",
                    "message": "Canceling reconstruction",
                    "cancel_requested": True,
                }
            )
            self._write(job_id, state)
            process = self._processes.get(job_id)

        if process and process.poll() is None:
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            check=False,
                            capture_output=True,
                        )
                else:
                    process.terminate()
            except OSError:
                pass
        return self.get(job_id)

    def resume(self, job_id: str) -> dict[str, Any]:
        return self.start(job_id, resume=True)

    def review(self, job_id: str, *, refine: bool) -> dict[str, Any]:
        with self._lock:
            state = self._read(job_id)
            if state["status"] != "awaiting_review":
                raise ValueError("This job is not waiting for preview review")
            if not any((self._job_dir(job_id) / "work" / "training").rglob("*.ckpt")):
                raise ValueError("Preview checkpoint is missing; refinement cannot resume safely")
            state.update({"status": "ready", "phase": "final", "progress": 3,
                          "reuse_training": not refine,
                          "preview_quality_report": state.get("quality_report"),
                          "quality_report": None,
                          "run_iterations": state["iterations"] if refine else state["run_iterations"]})
            self._write(job_id, state)
            return self.start(job_id, resume=True)

    def preview_image(self, job_id: str, index: int) -> Path:
        state = self.get(job_id)
        if state["status"] not in {"awaiting_review", "complete"} or index not in range(3):
            raise KeyError("Preview image is not available")
        root = self._job_dir(job_id).resolve()
        path = (root / "work" / "evaluation" / f"comparison_{index:04d}.jpg").resolve()
        if root not in path.parents or not path.is_file():
            raise KeyError("Preview image is not available")
        return path


manager = ReconstructionJobManager()
router = APIRouter(prefix="/api/reconstruction-jobs", tags=["video reconstruction"])


def get_job_or_404(job_id: str) -> dict[str, Any]:
    try:
        return manager.get(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Reconstruction job not found") from error


# Absolute host paths. `finalize` needs them from the single-job endpoint (it
# validates each one against JOB_ROOT/<id> before touching it), but the list is
# a browsable index, and there they are only a map of the operator's disk.
LISTING_HIDDEN_FIELDS = ("source_path", "output_ply", "thumbnail_path", "quality_report_path", "camera_metadata_path")


@router.get("")
def list_reconstruction_jobs():
    jobs = [
        {key: value for key, value in state.items() if key not in LISTING_HIDDEN_FIELDS}
        for state in manager.list()
    ]
    return {"jobs": jobs}


@router.post("", status_code=202)
async def create_reconstruction_job(
    file: UploadFile = File(...),
    name: str = Form(...),
    quality: str = Form("standard"),
    camera_height: float | None = Form(None),
    preview_first: bool = Form(False),
):
    name = name.strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="World name must be 1-80 characters")
    if camera_height is not None and not 0.5 <= camera_height <= 3.0:
        raise HTTPException(status_code=400, detail="Camera height must be between 0.5 and 3.0 metres")
    try:
        state, source_path = manager.create(
            name=name,
            quality=quality,
            filename=file.filename or "source.mp4",
            content_type=file.content_type,
            camera_height=camera_height,
            preview_first=preview_first,
        )
    except ValueError as error:
        raise HTTPException(status_code=415, detail=str(error)) from error

    uploaded = 0
    try:
        with source_path.open("wb") as destination:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                uploaded += len(chunk)
                if uploaded > MAX_VIDEO_BYTES:
                    raise HTTPException(status_code=413, detail="Video exceeds the configured upload limit")
                destination.write(chunk)
                manager.update_upload(state["id"], uploaded)
    except Exception as error:
        source_path.unlink(missing_ok=True)
        manager.fail(state["id"], str(error))
        raise
    finally:
        await file.close()

    return manager.mark_uploaded_and_start(state["id"])


@router.get("/{job_id}")
def get_reconstruction_job(job_id: str):
    return get_job_or_404(job_id)


@router.delete("/{job_id}")
def cancel_reconstruction_job(job_id: str):
    get_job_or_404(job_id)
    return manager.cancel(job_id)


@router.post("/{job_id}/resume", status_code=202)
def resume_reconstruction_job(job_id: str):
    get_job_or_404(job_id)
    try:
        return manager.resume(job_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{job_id}/refine", status_code=202)
def refine_reconstruction_job(job_id: str):
    get_job_or_404(job_id)
    try:
        return manager.review(job_id, refine=True)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{job_id}/accept", status_code=202)
def accept_reconstruction_preview(job_id: str):
    get_job_or_404(job_id)
    try:
        return manager.review(job_id, refine=False)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/{job_id}/preview/{index}")
def reconstruction_preview_image(job_id: str, index: int):
    try:
        return FileResponse(manager.preview_image(job_id, index), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Preview image not found") from error
