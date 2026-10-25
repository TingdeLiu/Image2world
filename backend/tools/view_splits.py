"""Frozen camera/image manifests shared by previews, refinement and experiments."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assign_view_splits(transforms: dict) -> dict:
    """Interleave validation/test views; keep the first and last views in train.

    Explicit filenames override Nerfstudio's default shared val/test pool.
    Tiny captures retain the legacy split and cannot claim independent testing.
    """
    paths = [frame["file_path"] for frame in transforms["frames"]]
    if len(paths) < 5:
        return transforms
    if len(set(paths)) != len(paths):
        raise ValueError("Dataset contains duplicate frame paths")
    validation = set(range(4, len(paths) - 1, 10)) or {len(paths) // 3}
    testing = set(range(9, len(paths) - 1, 10)) or {2 * len(paths) // 3}
    if validation & testing:
        raise ValueError("Validation and final-test cameras must be distinct")
    return {**transforms,
            "train_filenames": [p for i, p in enumerate(paths) if i not in validation | testing],
            "val_filenames": [p for i, p in enumerate(paths) if i in validation],
            "test_filenames": [p for i, p in enumerate(paths) if i in testing]}


def dataset_snapshot(dataset: Path) -> dict:
    root = dataset.resolve()
    transforms = json.loads((root / "transforms.json").read_text(encoding="utf-8"))
    filenames = [frame["file_path"] for frame in transforms["frames"]]
    if transforms.get("ply_file_path"):
        filenames.append(transforms["ply_file_path"])
    hashes = {}
    for name in sorted(set(filenames)):
        path = (root / name).resolve()
        if root not in path.parents:
            raise ValueError("Dataset asset must stay inside the dataset directory")
        hashes[name] = sha256(path)
    pools = {s: transforms.get(f"{s}_filenames", []) for s in ("train", "val", "test")}
    independent = all(pools.values())
    if independent:
        image_hashes = {s: {hashes[p] for p in paths} for s, paths in pools.items()}
        if any(image_hashes[a] & image_hashes[b] for a, b in (("train", "val"), ("train", "test"), ("val", "test"))):
            raise ValueError("Identical images cross training, validation or final-test splits")
    payload = {"transforms": transforms, "asset_sha256": hashes}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"version": 1, "fingerprint": fingerprint, "independent_test": independent,
            "views": {s: len(paths) for s, paths in pools.items()}, **payload}


def freeze_dataset(dataset: Path) -> dict:
    current = dataset_snapshot(dataset)
    path = dataset / "dataset_manifest.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("fingerprint") != current["fingerprint"]:
            raise ValueError("Frozen dataset changed; refinement must use the original images, cameras and splits")
    else:
        path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return current


def freeze_recipe(directory: Path, recipe: dict) -> None:
    path = directory / "training_recipe.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != recipe:
        raise ValueError("Refinement must keep the original resolution, seed and model settings")
    path.write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")
