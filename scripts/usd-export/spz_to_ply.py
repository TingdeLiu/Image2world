"""Decode a world's `.spz` splat back to a 3DGS binary PLY.

The mirror of `npm run assets:spz`. That direction exists to make the splat
small enough to ship to a browser; this one exists because simulators want the
uncompressed thing: Isaac Sim's neural renderer (`nrend`, the 3dgs/3dgut
backends behind NuRec) reads PLY, not SPZ.

Marble worlds only ever carry `.spz` -- the API returns nothing else -- so for
those this is the only way to get a PLY at all.

Usage:
    python scripts/usd-export/spz_to_ply.py <world-slug> [--lod 500k] [--out PATH]

Runs in the `scripts/usd-export/.venv` interpreter, which is where
`usd-convert-gsplat` lives; see README.md for the environment setup.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORLDS_DIR = REPO_ROOT / "public" / "worlds"

LODS = ("full_res", "500k", "150k", "100k")


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def display(path: Path) -> str:
    """Repo-relative when it can be, absolute when `--out` points elsewhere."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def find_spz(slug: str, lod: str) -> Path:
    world_dir = WORLDS_DIR / slug / "output" / "world"
    if not world_dir.is_dir():
        fail(f"no world at {world_dir}")

    # Same `<n>-world-<lod>.spz` naming the viewer and the USD exporter use;
    # the highest index is the newest version of the world.
    candidates = sorted(world_dir.glob(f"*-world-{lod}.spz"))
    if not candidates:
        have = sorted({p.name.split("-world-")[-1].removesuffix(".spz")
                       for p in world_dir.glob("*-world-*.spz")})
        fail(f"no {lod} splat in {world_dir}" + (f" (have: {', '.join(have)})" if have else ""))
    return candidates[-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("slug", help="world slug under public/worlds/")
    parser.add_argument("--lod", default="500k", choices=LODS,
                        help="which splat LOD to decode (default: 500k)")
    parser.add_argument("--out", help="output .ply path (default: beside the .spz)")
    args = parser.parse_args()

    try:
        from usd_convert_gsplat import read_spz, write_ply
    except ImportError:
        fail("usd-convert-gsplat is missing -- see scripts/usd-export/README.md "
             "for the venv setup")

    spz_path = find_spz(args.slug, args.lod)
    out_path = Path(args.out).resolve() if args.out else spz_path.with_suffix(".ply")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"reading {display(spz_path)}")
    data = read_spz(str(spz_path))
    rest = 0 if data.f_rest is None else data.f_rest.shape[1]
    print(f"  {data.count:,} splats, {rest} SH rest coefficients")

    written = write_ply(data, str(out_path))
    size_in = spz_path.stat().st_size / 1_048_576
    size_out = Path(written).stat().st_size / 1_048_576
    print(f"wrote {display(Path(written))} "
          f"({size_in:.1f} MB spz -> {size_out:.1f} MB ply)")


if __name__ == "__main__":
    main()
