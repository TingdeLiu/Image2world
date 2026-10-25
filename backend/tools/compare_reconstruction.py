"""Run a fresh, paired Splatfacto regularization experiment from an existing SfM solve.

    python backend/tools/compare_reconstruction.py <scene_sfm.json> --out <new-directory>

Both variants start from scratch with identical inputs, split, seed, image
resolution and step budget. Only scale regularization differs. Validation LPIPS
selects a candidate, then only that candidate's final test is revealed.
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

try:
    from .render_viewpoints import read_ply
except ImportError:
    from render_viewpoints import read_ply

ROOT = Path(__file__).resolve().parents[2]


def shape_statistics(path: Path) -> dict:
    vertices = read_ply(path)
    logs = np.stack([vertices[f"scale_{i}"] for i in range(3)], axis=-1).astype(float)
    delta = np.max(logs, axis=1) - np.min(logs, axis=1)
    valid = np.isfinite(delta)
    if not valid.any():
        raise ValueError("Export has no finite Gaussian scales")
    return {"gaussians": len(delta), "fraction_axis_ratio_over_10": float((delta[valid] > np.log(10)).mean()),
            "axis_ratio_p95": float(np.exp(np.percentile(delta[valid], 95)))}


def compare(scene: Path, output: Path, iterations: int, camera_scale: float, seed: int) -> dict:
    if output.exists():
        raise ValueError("Use a new experiment directory; existing results are never overwritten")
    output.mkdir(parents=True)
    variants = []
    for name in ("off", "on"):
        work = output / name
        sfm = work / "sfm"
        sfm.mkdir(parents=True)
        shutil.copy2(scene, sfm / "scene_sfm.json")
        if scene.with_suffix(".npz").exists():
            shutil.copy2(scene.with_suffix(".npz"), sfm / "scene_sfm.npz")
        command = [sys.executable, str(ROOT / "backend/tools/splatfacto_prototype.py"), str(scene.parent),
                   "--out", str(work), "--reuse-sfm", "--iterations", str(iterations),
                   "--camera-scale", str(camera_scale), "--seed", str(seed),
                   "--scale-regularization", name, "--evaluation-split", "val"]
        with (work / "run.log").open("w", encoding="utf-8") as log:
            subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        state = json.loads((work / "prototype_state.json").read_text(encoding="utf-8"))
        quality = state["quality_report"]
        if quality["status"] != "measured" or not state.get("independent_test"):
            raise ValueError(f"{name}: independent validation is unavailable; see {work / 'run.log'}")
        shutil.copytree(work / "evaluation", work / "validation")
        variants.append({"scale_regularization": name, "dataset_fingerprint": state["dataset_fingerprint"],
                         "validation": quality, "shape": shape_statistics(Path(state["output_ply"])),
                         "training_seconds": state["timings_seconds"]["train"], "command": command})
        print(f"[experiment] {name}: {quality['metrics']}", flush=True)
    if len({v["dataset_fingerprint"] for v in variants}) != 1:
        raise ValueError("Variants used different image/camera datasets; comparison refused")
    selected = min(variants, key=lambda v: v["validation"]["metrics"]["lpips"])
    report = {"version": 1, "iterations": iterations, "camera_scale": camera_scale, "seed": seed,
              "changed_parameter": "use_scale_regularization", "selection_split": "val",
              "selection_metric": "lpips", "selected": selected["scale_regularization"],
              "variants": variants, "final_test": {"status": "pending"},
              "limitation": "A single-scene pilot. Axis ratio is a shape proxy, not a direct smear or navigation score."}
    (output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    # Reveal final test only after validation-based selection was persisted.
    command = selected["command"] + ["--reuse-training", "--evaluation-split", "test"]
    with (output / "final-test.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    state = json.loads((output / selected["scale_regularization"] / "prototype_state.json").read_text(encoding="utf-8"))
    report["final_test"] = state["quality_report"]
    (output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    rows = "".join(f"<tr><td>{v['scale_regularization']}</td><td>{v['validation']['metrics']['psnr']:.2f}</td>"
                   f"<td>{v['validation']['metrics']['ssim']:.3f}</td><td>{v['validation']['metrics']['lpips']:.3f}</td>"
                   f"<td>{v['shape']['fraction_axis_ratio_over_10']:.1%}</td>"
                   f"<td><a href='{v['scale_regularization']}/validation/comparison_0000.jpg'>Compare images</a></td></tr>" for v in variants)
    page = f"""<!doctype html><html lang='en'><meta charset='utf-8'><title>Reconstruction experiment</title>
<style>body{{font:16px/1.6 system-ui;max-width:1050px;margin:48px auto;padding:0 24px;color:#222;background:#faf9f6}}table{{border-collapse:collapse;width:100%}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #ccc}}a{{color:#17604b}}</style>
<h1>Scale regularization: paired experiment</h1><p>Same images, cameras, split, seed {seed}, image scale {camera_scale}, {iterations} steps. Only regularization differs.</p>
<table><tr><th>Regularization</th><th>Val PSNR ↑</th><th>Val SSIM ↑</th><th>Val LPIPS ↓</th><th>Axis ratio over 10</th><th>Evidence</th></tr>{rows}</table>
<p>Selected on validation LPIPS: <strong>{html.escape(report['selected'])}</strong>. Final test: {html.escape(json.dumps(report['final_test'].get('metrics', {})))}.</p>
<p>{html.escape(report['limitation'])} This experiment does not change production defaults.</p><a href='comparison.json'>Full report</a></html>"""
    (output / "comparison.html").write_text(page, encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--camera-scale", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.iterations < 1 or not 0 < args.camera_scale <= 1:
        parser.error("iterations must be positive and camera scale must be in (0, 1]")
    compare(args.scene.resolve(), args.out.resolve(), args.iterations, args.camera_scale, args.seed)
