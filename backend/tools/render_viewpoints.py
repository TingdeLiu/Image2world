"""Render the six skybox faces an agent sees at every viewpoint of a VLN graph.

The second half of `scripts/isaac-sim/vlnviews.py`, which writes the job this
reads: one camera rig per viewpoint that passed its collider probes, the
world's background transform, and the PLY to render. It runs wherever gsplat
does -- on this machine that is the WSL Splatfacto venv, not the Windows
backend env:

    wsl -d Ubuntu-22.04 -- ~/.venvs/image2world-splatfacto/bin/python \\
        backend/tools/render_viewpoints.py public/worlds/<slug>/export/vln/render_job.json

Per viewpoint it writes `<image_id>_skybox<k>_sami.jpg` for k = 0..5 -- the
Matterport3D skybox naming R2R feature extractors already look for -- and one
`<image_id>_depth.npz` of per-face depth in metres along the optical axis.

It also says how much of each face the splat actually covers. A splat is only
the surfaces some camera saw, so a viewpoint looking at the unseen side of the
room gets a face that is mostly background colour. That is the part of the
panorama an agent would be trained on as if it were a real observation, and it
is invisible in the graph and in the collider (which is closed off where the
splat is not). `coverage` is the fraction of pixels with alpha >= 0.5, per face
and per viewpoint, written to `render_report.json`; the horizon faces are the
ones that matter, because up and down are ceiling and floor.

Coverage catches holes, not smear. `multiview-meetingroom` covers 87-95% of
every horizon face, and those faces are dominated by SHARP's stretch artifacts
once the camera leaves the input views. A high number here is necessary, not
sufficient: look at the faces.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np

SH_C0 = 0.28209479177387814
ALPHA_SEEN = 0.5
HORIZON_FACES = (1, 2, 3, 4)


def local_path(value: str) -> Path:
    """A Windows path as this process can open it. `D:/x` -> `/mnt/d/x` on POSIX."""
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
    if match and sys.platform != "win32":
        return Path("/mnt") / match.group(1).lower() / match.group(2).replace("\\", "/")
    return Path(value)


def read_ply(path: Path) -> dict[str, np.ndarray]:
    """The vertex element of a binary little-endian 3DGS PLY, by property name.

    Only the first element is read. SHARP appends `intrinsic`, `extrinsic` and
    friends after the vertices; they describe the source photo, not the scene.
    """
    types = {"float": "<f4", "double": "<f8", "uchar": "u1", "char": "i1", "short": "<i2",
             "ushort": "<u2", "int": "<i4", "uint": "<u4"}
    with path.open("rb") as handle:
        header = []
        while True:
            line = handle.readline()
            if not line:
                raise ValueError(f"{path}: no end_header")
            header.append(line.decode("ascii", "replace").strip())
            if header[-1] == "end_header":
                break
        if "format binary_little_endian 1.0" not in header:
            raise ValueError(f"{path}: only binary little-endian PLY is supported")
        count, fields, in_vertex = 0, [], False
        for line in header:
            parts = line.split()
            if parts[:2] == ["element", "vertex"]:
                count, in_vertex = int(parts[2]), True
            elif parts[:1] == ["element"]:
                in_vertex = False
            elif in_vertex and parts[:1] == ["property"]:
                fields.append((parts[2], types[parts[1]]))
        data = np.fromfile(handle, dtype=np.dtype(fields), count=count)
    return {name: data[name] for name, _ in fields}


def quaternion_of(rotation: np.ndarray) -> np.ndarray:
    """wxyz quaternion of a proper rotation matrix."""
    w = math.sqrt(max(0.0, 1.0 + rotation[0, 0] + rotation[1, 1] + rotation[2, 2])) / 2.0
    x = math.copysign(math.sqrt(max(0.0, 1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2])) / 2.0,
                      rotation[2, 1] - rotation[1, 2])
    y = math.copysign(math.sqrt(max(0.0, 1.0 - rotation[0, 0] + rotation[1, 1] - rotation[2, 2])) / 2.0,
                      rotation[0, 2] - rotation[2, 0])
    z = math.copysign(math.sqrt(max(0.0, 1.0 - rotation[0, 0] - rotation[1, 1] + rotation[2, 2])) / 2.0,
                      rotation[1, 0] - rotation[0, 1])
    return np.array([w, x, y, z])


def quaternion_multiply(a, b):
    """Hamilton product, wxyz, broadcasting over b's leading axis."""
    aw, ax, ay, az = a
    bw, bx, by, bz = b[..., 0], b[..., 1], b[..., 2], b[..., 3]
    return np.stack([
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ], axis=-1)


def gaussians_in_viewer(ply: dict[str, np.ndarray], matrix: np.ndarray):
    """Means, quats, scales, opacities and SH colours with the background applied.

    The transform is a similarity (rotation times a uniform scale, plus a
    shift), so it can go onto the gaussians exactly: means through the whole
    matrix, rotations composed with its rotation part, log-scales shifted by
    its log-scale. Applying it to the camera instead would need a non-rigid
    view matrix, which the rasteriser does not take.
    """
    linear = matrix[:3, :3]
    scale = float(np.cbrt(np.linalg.det(linear)))
    if scale <= 0:
        raise ValueError("background transform must preserve handedness")
    rotation = linear / scale
    means = np.stack([ply["x"], ply["y"], ply["z"]], axis=1).astype(np.float64)
    means = means @ linear.T + matrix[:3, 3]
    quats = np.stack([ply[f"rot_{i}"] for i in range(4)], axis=1).astype(np.float64)
    quats /= np.linalg.norm(quats, axis=1, keepdims=True).clip(1e-12)
    quats = quaternion_multiply(quaternion_of(rotation), quats)
    scales = np.exp(np.stack([ply[f"scale_{i}"] for i in range(3)], axis=1).astype(np.float64)) * scale
    opacities = 1.0 / (1.0 + np.exp(-ply["opacity"].astype(np.float64)))
    dc = np.stack([ply[f"f_dc_{i}"] for i in range(3)], axis=1)[:, None, :]
    rest_names = sorted((k for k in ply if k.startswith("f_rest_")), key=lambda k: int(k.split("_")[-1]))
    degree = 0
    if rest_names:
        per_channel = len(rest_names) // 3
        degree = int(round(math.sqrt(per_channel + 1))) - 1
        rest = np.stack([ply[k] for k in rest_names], axis=1).reshape(-1, 3, per_channel).transpose(0, 2, 1)
        sh = np.concatenate([dc, rest], axis=1)
    else:
        sh = dc
    # View-dependent colour rotates with the scene. Degree 0 is isotropic and
    # needs nothing; higher bands would need a Wigner rotation, so they are
    # dropped rather than rendered pointing the wrong way.
    if degree and not np.allclose(rotation, np.eye(3), atol=1e-6):
        sh, degree = dc, 0
    return means, quats, scales, opacities, sh, degree


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("job", type=Path, help="render_job.json written by vlnviews.py")
    parser.add_argument("--out", type=Path, help="output directory (default: <job dir>/skybox)")
    parser.add_argument("--splat", help="PLY to render instead of the job's")
    parser.add_argument("--limit", type=int, default=0, help="render only the first N viewpoints")
    parser.add_argument("--background", type=float, nargs=3, default=(0.0, 0.0, 0.0))
    args = parser.parse_args()

    import torch
    from gsplat import rasterization
    from PIL import Image

    job = json.loads(args.job.read_text(encoding="utf-8"))
    splat = local_path(args.splat or job["splat"])
    if not job.get("splat") and not args.splat:
        print(f"{job['slug']} has no PLY splat to render", file=sys.stderr)
        return 1
    out = args.out or args.job.parent / "skybox"
    out.mkdir(parents=True, exist_ok=True)

    started = time.time()
    means, quats, scales, opacities, sh, degree = gaussians_in_viewer(
        read_ply(splat), np.asarray(job["background_matrix"], dtype=np.float64))
    device = torch.device("cuda")
    as_tensor = lambda a: torch.as_tensor(np.ascontiguousarray(a), dtype=torch.float32, device=device)  # noqa: E731
    means_t, quats_t, scales_t = as_tensor(means), as_tensor(quats), as_tensor(scales)
    opacities_t, sh_t = as_tensor(opacities), as_tensor(sh)
    size = int(job["face_size"])
    intrinsics = as_tensor(np.asarray(job["intrinsics"]))[None].expand(6, 3, 3)
    background = torch.tensor(args.background, dtype=torch.float32, device=device)[None].expand(6, 3)
    print(f"{job['slug']}: {len(means):,} gaussians (SH degree {degree}) from {splat.name}, "
          f"loaded in {time.time() - started:.1f}s")

    cameras = job["cameras"][: args.limit] if args.limit else job["cameras"]
    report = []
    started = time.time()
    with torch.no_grad():
        for camera in cameras:
            viewmats = as_tensor(np.asarray(camera["world_to_camera"]))
            colours, alphas, _ = rasterization(
                means=means_t, quats=quats_t, scales=scales_t, opacities=opacities_t,
                colors=sh_t, sh_degree=degree, viewmats=viewmats, Ks=intrinsics,
                width=size, height=size, render_mode="RGB+ED", backgrounds=background,
            )
            rgb = colours[..., :3].clamp(0, 1).mul(255).round().byte().cpu().numpy()
            depth = colours[..., 3].cpu().numpy()
            alpha = alphas[..., 0].cpu().numpy()
            image_id = camera["image_id"]
            for face in range(6):
                Image.fromarray(rgb[face]).save(out / f"{image_id}_skybox{face}_sami.jpg", quality=92)
            np.savez_compressed(out / f"{image_id}_depth.npz",
                                depth=np.where(alpha >= ALPHA_SEEN, depth, np.nan).astype(np.float16))
            seen = (alpha >= ALPHA_SEEN).mean(axis=(1, 2))
            report.append({"image_id": image_id,
                           "coverage": [round(float(v), 4) for v in seen],
                           "horizon_coverage": round(float(seen[list(HORIZON_FACES)].mean()), 4)})

    horizon = np.array([r["horizon_coverage"] for r in report])
    summary = {
        "slug": job["slug"],
        "splat": str(splat),
        "viewpoints": len(report),
        "face_size": size,
        "seconds": round(time.time() - started, 1),
        "horizon_coverage": ({f"p{p}": round(float(np.percentile(horizon, p)), 4) for p in (0, 10, 50, 90)}
                             if len(horizon) else {}),
        "viewpoints_under_90pct": int((horizon < 0.9).sum()),
    }
    (out / "render_report.json").write_text(
        json.dumps({"summary": summary, "viewpoints": report}, indent=2) + "\n", encoding="utf-8")
    print(f"rendered {len(report)} viewpoints x 6 faces at {size}px in {summary['seconds']}s; "
          f"horizon coverage {summary['horizon_coverage']}, "
          f"{summary['viewpoints_under_90pct']} viewpoints under 90%")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
