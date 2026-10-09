import json
import math
from pathlib import Path

import pytest

from backend.reconstruction_jobs import QUALITY_PROFILES, ReconstructionJobManager, progress_from_output
from backend.tools.splatfacto_prototype import capture_metrics, recommended_initial_view, validate_capture


def test_progress_parser_maps_sfm_and_training():
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
