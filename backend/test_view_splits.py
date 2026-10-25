import json

import pytest

from backend.tools.view_splits import assign_view_splits, freeze_dataset, freeze_recipe


@pytest.mark.parametrize("count", [5, 9, 10, 20, 108])
def test_three_splits_are_disjoint_complete_and_keep_capture_endpoints(count):
    source = {"frames": [{"file_path": f"images/{i}.jpg"} for i in range(count)]}
    result = assign_view_splits(source)
    pools = [set(result[f"{s}_filenames"]) for s in ("train", "val", "test")]
    assert all(pools)
    assert not any(pools[a] & pools[b] for a, b in ((0, 1), (0, 2), (1, 2)))
    assert len(set.union(*pools)) == count
    assert {"images/0.jpg", f"images/{count - 1}.jpg"} <= pools[0]
    assert "train_filenames" not in source


def fixture_dataset(tmp_path):
    (tmp_path / "images").mkdir()
    frames = []
    for i in range(10):
        (tmp_path / f"images/{i}.jpg").write_bytes(f"unique frame {i}".encode())
        frames.append({"file_path": f"images/{i}.jpg", "transform_matrix": [i]})
    transforms = assign_view_splits({"frames": frames})
    (tmp_path / "transforms.json").write_text(json.dumps(transforms))
    return tmp_path


def test_continuation_refuses_a_changed_image(tmp_path):
    dataset = fixture_dataset(tmp_path)
    original = freeze_dataset(dataset)
    assert freeze_dataset(dataset)["fingerprint"] == original["fingerprint"]
    (dataset / "images/0.jpg").write_bytes(b"replacement")
    with pytest.raises(ValueError, match="Frozen dataset changed"):
        freeze_dataset(dataset)


def test_duplicate_content_cannot_leak_between_validation_and_test(tmp_path):
    dataset = fixture_dataset(tmp_path)
    source = json.loads((dataset / "transforms.json").read_text())
    validation = dataset / source["val_filenames"][0]
    testing = dataset / source["test_filenames"][0]
    testing.write_bytes(validation.read_bytes())
    with pytest.raises(ValueError, match="Identical images cross"):
        freeze_dataset(dataset)


def test_finer_training_keeps_model_settings_and_resolution_fixed(tmp_path):
    original = {"camera_scale": 0.5, "seed": 42, "scale_regularization": "off"}
    freeze_recipe(tmp_path, original)
    freeze_recipe(tmp_path, original)
    with pytest.raises(ValueError, match="original resolution"):
        freeze_recipe(tmp_path, {**original, "camera_scale": 0.75})
