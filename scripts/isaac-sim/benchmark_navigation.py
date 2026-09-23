"""Re-score saved navigation maps with fixed settings and source fingerprints.

This does not rebuild assets, start GPU training, or modify published worlds.
Use --require-ready in a dataset acceptance step; unassessable criteria fail it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import navmap
import vln_check
import vlngraph


ROOT = Path(__file__).resolve().parents[2]
SETTINGS = {
    "spacing_m": 0.75,
    "max_edge_m": 2.5,
    "robot_radius": 0.25,
    "min_hops": 4,
    "episode_range": (3.0, 20.0),
    "episodes": 100,
}


def fingerprint(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def assess_map(path: Path) -> dict:
    carte = navmap.load(str(path))
    report = vlngraph.build(carte, **SETTINGS)
    checks = vln_check.verdict(report)
    return {
        "world": path.parent.parent.name,
        "map": fingerprint(path),
        "image": fingerprint(path.parent / str(carte.meta["image"])),
        "vln_ready": all(row["met"] is True for row in checks),
        "metrics": {key: report[key] for key in (
            "passable_m2", "nodes", "edges", "components", "largest_fraction",
            "coverage", "detour", "detour_pairs", "episodes", "episode_length_m",
        )},
        "criteria": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worlds-dir", type=Path, default=ROOT / "public" / "worlds")
    parser.add_argument("--world", action="append", help="world name; repeat to select a subset")
    parser.add_argument("--out", type=Path, default=ROOT / ".artifacts" / "navigation-validation" / "vln-baseline.json")
    parser.add_argument("--require-ready", action="store_true", help="exit 1 if any world fails or has no map")
    args = parser.parse_args()
    selected = set(args.world or [])
    worlds = sorted(p for p in args.worlds_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    if selected:
        worlds = [p for p in worlds if p.name in selected]
    results, errors = [], []
    for missing in sorted(selected - {p.name for p in worlds}):
        errors.append({"world": missing, "error": "world directory missing"})
    for world in worlds:
        maps = sorted((world / "export").glob("*-navmap.yaml"))
        if not maps:
            errors.append({"world": world.name, "error": "no exported navigation map"})
        for path in maps:
            try:
                row = assess_map(path)
                results.append(row)
                metrics = row["metrics"]
                reasons = [c["criterion"] + (" (not assessable)" if c["met"] is None else "")
                           for c in row["criteria"] if c["met"] is not True]
                print(f"{row['world']}: {'PASS' if row['vln_ready'] else 'FAIL'}; "
                      f"{metrics['nodes']} viewpoints, {metrics['episodes']} routes; "
                      f"{', '.join(reasons) or 'all criteria met'}")
            except (Exception, SystemExit) as error:
                errors.append({"world": world.name, "map": str(path), "error": str(error)})
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "saved exported occupancy maps; no GPU reconstruction or simulator rerun",
        "settings": SETTINGS,
        "ready": sum(row["vln_ready"] for row in results),
        "evaluated": len(results),
        "results": results,
        "errors": errors,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Ready: {payload['ready']}/{len(results)}; errors: {len(errors)}; report: {args.out}")
    return int(bool(errors) or not results or (args.require_ready and payload["ready"] != len(results)))


if __name__ == "__main__":
    raise SystemExit(main())
