"""Evaluate a trained Nerfstudio checkpoint against its actual held-out images.

Run in the same environment as Splatfacto. This loads no GAE code or weights.
The JSON contract and split checks live in a separate CPU-only module.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reconstruction_quality import comparison_pixels, measured_report, unavailable_report, verify_holdout


def evaluate(config: Path, output: Path, split: str = "test") -> dict:
    import torch
    from PIL import Image
    from nerfstudio.utils.eval_utils import eval_setup

    settings, pipeline, checkpoint, step = eval_setup(config, test_mode=split)
    manager = pipeline.datamanager
    train = manager.train_dataset.image_filenames
    evaluation = manager.eval_dataset.image_filenames
    reason = verify_holdout(train, evaluation)
    if reason:
        return unavailable_report(reason)
    per_view, examples = [], []
    pipeline.eval()
    with torch.inference_mode():
        for index, (camera, batch) in enumerate(manager.fixed_indices_eval_dataloader):
            outputs = pipeline.model.get_outputs_for_camera(camera=camera)
            metrics, _ = pipeline.model.get_image_metrics_and_images(outputs, batch)
            per_view.append({"image": evaluation[index].name,
                             **{key: float(metrics[key]) for key in ("psnr", "ssim", "lpips")}})
            if index < 3:
                # Real observation on the left, reconstruction on the right.
                pixels = comparison_pixels(batch["image"].cpu().numpy(), outputs["rgb"].cpu().numpy())
                name = f"comparison_{index:04d}.jpg"
                Image.fromarray(pixels).save(output.parent / name, quality=90)
                examples.append(name)
    report = measured_report(len(train), len(evaluation), per_view, examples)
    report.update({"evaluation_split": split, "checkpoint": checkpoint.name, "checkpoint_step": int(step)})
    dataset = Path(settings.pipeline.datamanager.dataparser.data)
    if dataset.is_file():
        dataset = dataset.parent
    manifest_path = dataset / "dataset_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report.update({"dataset_fingerprint": manifest["fingerprint"], "independent_test": manifest["independent_test"]})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=("val", "test"), default="test")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = evaluate(args.load_config, args.output, args.split)
    except Exception as error:
        # A reconstruction is still usable when evaluation fails, but must not
        # acquire a successful quality label. Keep detailed paths in the log.
        print(f"[quality] {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        report = unavailable_report("Quality evaluation failed; see the reconstruction job log.")
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    print(f"[quality] {report['status']}: {report.get('metrics') or report.get('reason')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
