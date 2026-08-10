import json
import math
from pathlib import Path

import pytest

from backend.reconstruction_jobs import ReconstructionJobManager, progress_from_output
from backend.tools.splatfacto_prototype import recommended_initial_view


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
