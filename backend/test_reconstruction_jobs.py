import json
import math
from pathlib import Path

import pytest

from backend.reconstruction_jobs import QUALITY_PROFILES, ReconstructionJobManager, progress_from_output
from backend.tools.splatfacto_prototype import capture_metrics, recommended_initial_view, validate_capture


def test_progress_parser_maps_sfm_and_training():
    evaluation = progress_from_output("[prototype] evaluate_reconstruction.py --load-config model.yml", 20_000)
    assert evaluation["stage"] == "evaluating"
    assert evaluation["progress"] == 96
    sfm = progress_from_output("[sfm] registered 293/293 frames, 1000 sparse points", 20_000)
    assert sfm == {
        "stage": "sfm",
        "progress": 30,
        "message": "Camera tracking complete",
        "detail": "Registered 293/293 frames",
        "registered_frames": 293,
        "extracted_frames": 293,
    }

    training = progress_from_output("Step 10000 (50.00%)", 20_000)
    assert training["stage"] == "training"
    assert training["training_step"] == 10_000
    assert training["progress"] == 60


def test_job_creation_uses_quality_profile(tmp_path: Path):
    manager = ReconstructionJobManager(tmp_path)
    state, source = manager.create(
        name="Courtyard",
        quality="quality",
        filename="walkthrough.mp4",
        content_type="video/mp4",
        camera_height=1.65,
    )

    assert source == tmp_path / state["id"] / "source.mp4"
    assert state["target_frames"] == 400
    assert state["iterations"] == 30_000
    assert state["camera_scale"] == 0.75
    assert json.loads((tmp_path / state["id"] / "job.json").read_text())["status"] == "uploading"


def preview_waiting_for_review(tmp_path, monkeypatch):
    manager = ReconstructionJobManager(tmp_path)
    state, source = manager.create(name="Staged room", quality="standard", filename="walk.mp4",
                                   content_type="video/mp4", camera_height=1.6, preview_first=True)
    assert state["iterations"] == 20_000
    assert state["run_iterations"] == 2000
    checkpoint = source.parent / "work/training/run/nerfstudio_models/step-000001999.ckpt"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.touch()
    state.update(status="awaiting_review", quality_report={"status": "measured", "evaluation_split": "val"})
    manager._write(state["id"], state)
    # Exercise the transition without launching a GPU worker in a CPU test.
    monkeypatch.setattr(manager, "start", lambda job_id, resume=False: {**manager.get(job_id), "resume_requested": resume})
    return manager, state


def test_refinement_reuses_checkpoint_and_keeps_the_original_capture_settings(tmp_path, monkeypatch):
    manager, original = preview_waiting_for_review(tmp_path, monkeypatch)
    refined = manager.review(original["id"], refine=True)
    assert refined["resume_requested"] is True
    assert refined["phase"] == "final" and refined["run_iterations"] == 20_000
    assert refined["reuse_training"] is False
    assert refined["preview_quality_report"]["evaluation_split"] == "val"
    assert refined["quality_report"] is None
    for key in ("source_path", "target_frames", "camera_scale", "frames_per_window", "sequential_overlap"):
        assert refined[key] == original[key]
    with pytest.raises(ValueError, match="not waiting"):
        manager.review(original["id"], refine=True)


def test_accept_preview_runs_final_evaluation_without_more_training(tmp_path, monkeypatch):
    manager, original = preview_waiting_for_review(tmp_path, monkeypatch)
    accepted = manager.review(original["id"], refine=False)
    assert accepted["phase"] == "final" and accepted["run_iterations"] == 2000
    assert accepted["reuse_training"] is True and accepted["resume_requested"] is True


def test_preview_survives_restart_and_can_be_discarded_without_a_running_process(tmp_path, monkeypatch):
    manager, original = preview_waiting_for_review(tmp_path, monkeypatch)
    recovered = ReconstructionJobManager(tmp_path)
    assert recovered.get(original["id"])["status"] == "awaiting_review"
    assert recovered.cancel(original["id"])["status"] == "canceled"


def test_preview_cannot_refine_without_a_real_checkpoint(tmp_path):
    manager = ReconstructionJobManager(tmp_path)
    state, _ = manager.create(name="Room", quality="standard", filename="walk.mp4",
                              content_type="video/mp4", camera_height=None, preview_first=True)
    state["status"] = "awaiting_review"
    manager._write(state["id"], state)
    with pytest.raises(ValueError, match="checkpoint is missing"):
        manager.review(state["id"], refine=True)


def test_preview_worker_pauses_and_does_not_clear_the_next_phase_handles(tmp_path, monkeypatch):
    import threading
    from backend import reconstruction_jobs as jobs

    manager = ReconstructionJobManager(tmp_path)
    state, source = manager.create(name="Room", quality="standard", filename="walk.mp4",
                                   content_type="video/mp4", camera_height=None, preview_first=True)
    work = source.parent / "work"
    work.mkdir()
    ply = work / "world.ply"
    ply.write_bytes(b"fixture result")
    (work / "prototype_state.json").write_text(json.dumps({
        "status": "complete", "output_ply": str(ply), "gaussian_count": 10,
        "quality_report": {"evaluation_split": "val"},
    }))
    commands = []

    class Process:
        stdout = []

        def __init__(self, command, **kwargs):
            commands.append(command)

        def wait(self):
            return 0

    monkeypatch.setattr(jobs.subprocess, "Popen", Process)
    next_worker, next_process = threading.Thread(), object()
    original_write = manager._write

    def write(job_id, value):
        result = original_write(job_id, value)
        if value.get("status") == "awaiting_review":
            manager._threads[job_id] = next_worker
            manager._processes[job_id] = next_process
        return result

    monkeypatch.setattr(manager, "_write", write)
    manager._threads[state["id"]] = threading.current_thread()
    manager._run_job(state["id"], resume=False)
    assert manager.get(state["id"])["status"] == "awaiting_review"
    assert commands[0][commands[0].index("--iterations") + 1] == "2000"
    assert commands[0][commands[0].index("--evaluation-split") + 1] == "val"
    assert manager._threads[state["id"]] is next_worker
    assert manager._processes[state["id"]] is next_process


def test_navigation_profile_prioritizes_coverage_and_has_capture_gates():
    profile = QUALITY_PROFILES["navigation"]
    assert profile["target_frames"] > QUALITY_PROFILES["quality"]["target_frames"]
    assert profile["iterations"] > QUALITY_PROFILES["quality"]["iterations"]
    assert profile["frames_per_window"] == 1
    assert profile["sequential_overlap"] >= 8
    assert profile["min_registration_rate"] >= 0.75
    assert profile["min_registered_frames"] >= 24
    assert profile["min_trajectory_coverage"] >= 0.05


def test_capture_metrics_and_navigation_gate(tmp_path: Path):
    scene_path = tmp_path / "scene_sfm.json"
    scene_path.write_text(
        json.dumps({
            "frames": [
                {"camera_centre": [0, 0, 0]},
                {"camera_centre": [2, 0, 0]},
                {"camera_centre": [4, 0, 0]},
            ]
        }),
        encoding="utf-8",
    )
    import numpy as np

    np.savez(scene_path.with_suffix(".npz"), points_xyz=np.asarray([
        [0, 0, 0], [2, 0, 0], [4, 0, 0],
    ], dtype=float))

    metrics = capture_metrics(scene_path, total_frames=4)
    assert metrics["registered_frames"] == 3
    assert metrics["extracted_frames"] == 4
    assert metrics["registration_rate"] == pytest.approx(0.75)
    assert metrics["trajectory_span"] == pytest.approx(4.0)
    assert metrics["trajectory_length"] == pytest.approx(4.0)
    assert metrics["trajectory_coverage"] > 1.0
    validate_capture(
        metrics,
        min_registration_rate=0.75,
        min_registered_frames=3,
        min_trajectory_coverage=0.05,
    )

    with pytest.raises(RuntimeError, match="Navigation capture rejected"):
        validate_capture(metrics, min_registration_rate=0.9)


def test_rejects_non_video_upload(tmp_path: Path):
    manager = ReconstructionJobManager(tmp_path)
    with pytest.raises(ValueError, match="video extension"):
        manager.create(
            name="Not video",
            quality="preview",
            filename="still.png",
            content_type="image/png",
            camera_height=None,
        )


def test_running_job_becomes_resumable_after_restart(tmp_path: Path):
    manager = ReconstructionJobManager(tmp_path)
    state, _ = manager.create(
        name="Interrupted",
        quality="preview",
        filename="clip.mov",
        content_type="video/quicktime",
        camera_height=None,
    )
    persisted = manager.get(state["id"])
    persisted["status"] = "running"
    manager._write(state["id"], persisted)

    recovered = ReconstructionJobManager(tmp_path).get(state["id"])
    assert recovered["status"] == "interrupted"
    assert recovered["resumable"] is True


def test_recommended_view_converts_nerfstudio_z_up_to_viewer_y_up(tmp_path: Path):
    dataset = tmp_path / "dataset"
    run_dir = tmp_path / "training" / "run"
    dataset.mkdir()
    run_dir.mkdir(parents=True)
    (dataset / "transforms.json").write_text(
        json.dumps({
            "frames": [{
                "transform_matrix": [
                    [1, 0, 0, 1],
                    [0, 1, 0, 2],
                    [0, 0, 1, 3],
                    [0, 0, 0, 1],
                ]
            }]
        }),
        encoding="utf-8",
    )
    (run_dir / "dataparser_transforms.json").write_text(
        json.dumps({
            "transform": [
                [1, 0, 0, 0],
                [0, 1, 0, 0],
                [0, 0, 1, 0],
            ],
            "scale": 2,
        }),
        encoding="utf-8",
    )
    config = run_dir / "config.yml"
    config.write_text("test: true\n", encoding="utf-8")

    view = recommended_initial_view(dataset, config)

    assert view is not None
    assert view["position"] == pytest.approx([2, 6, -4])
    assert math.isfinite(view["yaw"])
    assert math.isfinite(view["pitch"])
