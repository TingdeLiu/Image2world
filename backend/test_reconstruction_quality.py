import json
from pathlib import Path

import numpy as np
import pytest

from backend.tools.reconstruction_quality import comparison_pixels, export_camera_bundle, measured_report, verify_holdout


def test_repeated_video_frames_are_not_independent_evidence(tmp_path):
    train, evaluation = tmp_path / "train.jpg", tmp_path / "eval.jpg"
    train.write_bytes(b"same recorded frame")
    evaluation.write_bytes(train.read_bytes())
    assert "identical" in verify_holdout([train], [evaluation])
    assert "overlap" in verify_holdout([train], [tmp_path / ".." / tmp_path.name / "train.jpg"])
    evaluation.write_bytes(b"different recorded frame")
    assert verify_holdout([train], [evaluation]) is None


def test_empty_evaluation_is_not_a_quality_pass(tmp_path):
    assert "No held-out" in verify_holdout([tmp_path / "train.jpg"], [])
    with pytest.raises(ValueError, match="every held-out"):
        measured_report(10, 2, [{"psnr": 30, "ssim": 0.9, "lpips": 0.1}], [])
    with pytest.raises(ValueError, match="non-finite"):
        measured_report(10, 1, [{"psnr": float("nan"), "ssim": 0.9, "lpips": 0.1}], [])


def test_single_heldout_view_has_finite_population_statistics():
    report = measured_report(10, 1, [{"image": "frame_03.jpg", "psnr": 30, "ssim": 0.9, "lpips": 0.1}], [])
    assert report["status"] == "measured"
    assert report["metrics"]["psnr_std"] == 0
    json.dumps(report, allow_nan=False)


def test_uint8_cached_observation_is_not_clipped_to_white_in_comparisons():
    reference = np.array([[[32, 128, 224], [0, 0, 255]]], dtype=np.uint8)
    render = reference.astype(np.float32) / 255
    comparison = comparison_pixels(reference, render)
    np.testing.assert_array_equal(comparison[:, :2], reference)
    np.testing.assert_array_equal(comparison[:, 2:], reference)
    np.testing.assert_array_equal(comparison_pixels(render, render), comparison)


def test_evaluation_failure_is_persisted_as_unavailable_not_a_success(tmp_path, monkeypatch):
    import sys
    from backend.tools import evaluate_reconstruction as runner

    def fail(*args):
        raise RuntimeError("private-checkpoint-path")

    output = tmp_path / "quality/report.json"
    monkeypatch.setattr(runner, "evaluate", fail)
    monkeypatch.setattr(sys, "argv", ["evaluate", "--load-config", "local.yml", "--output", str(output)])
    assert runner.main() == 0  # An optional evaluation failure does not lose a reconstructed world.
    report = json.loads(output.read_text())
    assert report["status"] == "unavailable"
    assert report["metrics"] == {}
    assert "private-checkpoint-path" not in output.read_text()


def camera_fixture(tmp_path: Path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    camera = np.eye(4)
    camera[:3, 3] = [1, 2, 3]
    (dataset / "transforms.json").write_text(json.dumps({"frames": [{
        "file_path": "images/frame_0000.jpg", "transform_matrix": camera.tolist(),
        "fl_x": 100, "fl_y": 100, "cx": 50, "cy": 40, "w": 100, "h": 80,
    }]}))
    parser = tmp_path / "dataparser_transforms.json"
    # Rotate +90 degrees about Z, translate, then scale translations by 2.
    parser.write_text(json.dumps({"transform": [
        [0, -1, 0, 10], [1, 0, 0, 20], [0, 0, 1, 30],
    ], "scale": 2}))
    return dataset, parser


def test_camera_export_matches_splat_frame_without_preview_normalization(tmp_path):
    dataset, parser = camera_fixture(tmp_path)
    bundle = export_camera_bundle(dataset, parser, tmp_path / "cameras.json")
    camera = np.asarray(bundle["frames"][0]["camera_to_world"])
    assert camera[:3, 3] == pytest.approx([16, 42, 66])
    np.testing.assert_allclose(camera[:3, :3], [[0, -1, 0], [1, 0, 0], [0, 0, 1]])
    assert bundle["units"] == "arbitrary"
    assert bundle["frames"][0]["image"] == "frame_0000.jpg"
    # Source files are observations and must survive metadata export unchanged.
    source = json.loads((dataset / "transforms.json").read_text())
    assert source["frames"][0]["transform_matrix"][0][3] == 1


@pytest.mark.parametrize("scale", [0, -1, float("inf")])
def test_invalid_normalization_is_not_published_as_camera_metadata(tmp_path, scale):
    dataset, parser = camera_fixture(tmp_path)
    payload = json.loads(parser.read_text())
    payload["scale"] = scale
    parser.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="scale"):
        export_camera_bundle(dataset, parser, tmp_path / "cameras.json")
