"""Is this map good enough to train a VLN agent on? Say which criterion fails.

`check_navmap.py` asks whether a ROS stack can read and cross the map.
Everything in this repository so far has been aimed at that bar, and it is met.
This asks the harder question, and the two are not close together: a map a
planner drives happily can still be useless for vision-and-language navigation.

The criteria below, and why each one is here rather than a number that sounded
reasonable. R2R (Anderson et al., 2018) is built on 90 Matterport3D buildings
and 10,800 panoramic viewpoints -- about 120 viewpoints per scan -- with paths
averaging ~10 m over 4-6 viewpoint hops. RxR keeps the same graph and adds
longer routes. Those are the shapes a world has to be able to produce.

  nodes >= 20            Fewer than that cannot be split into train and val
                         routes that are actually different from each other.
                         Well under an MP3D scan; this is a floor, not a target.

  largest fraction       An episode whose goal sits in another component is one
    >= 0.90              the agent can never complete. A handful of those in a
                         training set teaches it that some instructions are
                         simply impossible, which is the opposite of the lesson.

  coverage >= 0.90       The graph has to stand for the space. `sample_nodes`
                         prefers roomy cells, so this is measured over every
                         passable cell -- the one check the sampler's bias
                         cannot flatter.

  detour p90 >= 1.20     The one that matters most. If routes are within 20%
                         of the straight line, the environment does not
                         require navigating:
                         "walk towards the goal" solves it, and an agent trained
                         on it has no reason to read the instruction. This is a
                         fact about the captured space, not about the map -- no
                         post-processing puts a corner in a room that has none.

                         The percentile is p90 because the median cannot tell
                         the two cases apart. On a synthetic 10x10 m box with a
                         wall hanging 80% of the way across -- two rooms by any
                         reading -- the median detour is 1.01, the same as the
                         open box, because most pairs of viewpoints share a side
                         of the wall. The p90s are 2.32 and 1.01. A criterion on
                         the median, which is what this file first shipped with,
                         would have failed a real two-room capture and blamed
                         the reconstruction.

  episodes >= 50         With length 3-20 m and at least 4 hops, matching R2R's
                         path statistics. The yield is what says whether a
                         world contributes to a dataset or is a curiosity.

What the eight worlds here score, because no single criterion explains it and
the tidy summary is wrong. Three of the five are the *sole* blocker for some
world: `marble-office` has the scale -- 99 viewpoints, full coverage, 100
episodes -- and fails on detour alone at 1.12, with 3.7% of its 4,186 viewpoint
pairs having to go round anything; `marble-from-photo-7y19ti` fails on episode
yield alone, 19 of 50, in 9.6 m2; `singleview-meetingroom` on viewpoint count
alone. "Every world is one open room" -- which is what the first draft of this
file said -- was reading a bug that had viewpoints standing 0.5 m from every
wall, and so was blind to exactly the routes that go round things.

That every captured world fails is a statement about the captures, not about
these thresholds: `reference-rooms`, a synthetic five-room fixture built by
`make_reference_world.py` and put through the same unmodified chain, meets all
five -- 212 viewpoints in one component, detour p90 2.36, 100 episodes at a
median of 12.5 m over 8 hops. A gate that had only ever refused would have been
indistinguishable from an unreachable one.

The verdict is written to JSON and printed; the exit code stays 0 unless
something actually broke. A world that is honestly too small is a finding, not
a crash, and `pipeline.py` already learned once that judging a stage by its exit
code hides exactly this kind of answer.

    python scripts/isaac-sim/vln_check.py public/worlds/<slug>/export/<slug>-navmap.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import navmap  # noqa: E402
import vlngraph  # noqa: E402

# (name, getter, threshold, remedy, is-this-measurable, why it is not)
#
# The last two are not decoration. Without them the gate told a world that had
# produced *zero* viewpoints that its problem was being "one open room" and it
# should go and capture more rooms -- a confident diagnosis of a number that was
# 0.0 because there was nothing to measure. A criterion that cannot state its
# own preconditions will eventually answer a question nobody asked.
CRITERIA = (
    ("nodes", lambda r: r["nodes"], 20,
     "the walkable area is too small to place viewpoints in",
     lambda r: True, ""),
    ("largest_fraction", lambda r: r["largest_fraction"], 0.90,
     "the walkable area is in pieces -- check the reachability filter and the "
     "holes in the floor",
     lambda r: r["nodes"] > 0, "there are no viewpoints to be connected"),
    ("coverage", lambda r: r["coverage"], 0.90,
     "the viewpoints do not stand for the space; try a smaller --spacing",
     # Coverage is a fraction of the passable cells, so with none of them it is
     # 0.0 for want of a denominator, not because the viewpoints missed
     # anything. Left unguarded it advised a map with no floor at all to use a
     # smaller viewpoint spacing.
     lambda r: r["passable_m2"] > 0.0,
     "the map has no passable cells to cover"),
    ("detour_p90", lambda r: r["detour"].get("p90", 0.0), 1.20,
     "the space is one open room, so every route is a straight line -- this "
     "needs a capture with more than one room in it, not a better map",
     # A p90 is the tenth-largest of a hundred. Over the 66 pairs that
     # `multiview-meetingroom`'s 32 viewpoints leave connected it is the
     # seventh-largest, and it reads 3.25 -- measured inside the one fragment
     # of a graph that is in 15 pieces, which is not a statement about the
     # building. The threshold is on the sample, not on the world.
     lambda r: r["detour_pairs"] >= 100,
     "fewer than 100 viewpoint pairs, and a p90 over that few is noise"),
    ("episodes", lambda r: r["episodes"], 50,
     "too few start/goal pairs of a usable length",
     # n viewpoints admit n(n-1)/2 pairs before any filtering, so under 20 the
     # count cannot reach 50 whatever the map looks like. Reporting it as its
     # own failure would be the `nodes` failure counted twice.
     lambda r: r["nodes"] >= 20,
     "fewer than 20 viewpoints cannot make 50 episodes, whatever the map"),
)


def verdict(report):
    """Each criterion with its value, threshold, and whether it was even asked.

    Three outcomes, not two. A criterion whose precondition fails is `met:
    None` -- not passed and not failed, because the number underneath it does
    not mean anything yet.
    """
    out = []
    for name, get, floor, remedy, measurable, why_not in CRITERIA:
        value = get(report)
        can = bool(measurable(report))
        out.append({"criterion": name, "value": value, "at_least": floor,
                    "met": bool(value >= floor) if can else None,
                    "assessable": can, "remedy": remedy,
                    "not_assessable": "" if can else why_not})
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("yaml", help="the -navmap.yaml written by build_navmap.py")
    parser.add_argument("--spacing", type=float, default=0.75,
                        help="metres between viewpoints (default 0.75; MP3D is "
                             "about 2.25, but these worlds are single rooms)")
    parser.add_argument("--max-edge", type=float, default=2.5,
                        help="longest straight step between viewpoints, in metres")
    parser.add_argument("--robot-radius", type=float, default=0.25,
                        help="viewpoints and edges must clear obstacles by this")
    parser.add_argument("--episodes", type=int, default=100,
                        help="how many start/goal pairs to keep in the export")
    parser.add_argument("--min-hops", type=int, default=4,
                        help="shortest route an episode may take, in graph hops")
    parser.add_argument("--out", help="directory for <slug>-vlngraph.json "
                                      "(default: beside the map)")
    parser.add_argument("--no-png", action="store_true",
                        help="skip the picture of the map with its graph on top")
    args = parser.parse_args()

    carte = navmap.load(args.yaml)
    report = vlngraph.build(
        carte, spacing_m=args.spacing, max_edge_m=args.max_edge,
        robot_radius=args.robot_radius, min_hops=args.min_hops,
        episode_range=(3.0, 20.0), episodes=args.episodes)

    print(f"map        {os.path.basename(args.yaml)}")
    print(f"  passable {report['passable_m2']:.2f} m2 at r={args.robot_radius} m")
    print(f"  graph    {report['nodes']} viewpoints, {report['edges']} edges, "
          f"mean degree {report['mean_degree']}, {report['components']} components")
    if report["detour"]:
        print(f"  detour   p50 {report['detour']['p50']:.2f}  "
              f"p90 {report['detour']['p90']:.2f}  "
              f"p100 {report['detour']['p100']:.2f}  "
              f"over {report['detour_pairs']} pairs; "
              f"{report['detour_over_1_25']:.1%} of them go round something")
    if report["episode_length_m"]:
        print(f"  episodes {report['episodes']}, "
              f"{report['episode_length_m']['p50']:.1f} m median, "
              f"{report['episode_hops']['p50']:.0f} hops median")

    checks = verdict(report)
    print()
    for row in checks:
        mark = "-- " if row["met"] is None else ("ok " if row["met"] else "XX ")
        print(f"  {mark}{row['criterion']:<18} {row['value']:>7.2f} "
              f"(need >= {row['at_least']})"
              + (f"   not assessed: {row['not_assessable']}"
                 if row["met"] is None else ""))
    failed = [row for row in checks if row["met"] is False]
    unknown = [row for row in checks if row["met"] is None]
    # One line, fixed shape, so `pipeline.py` can read the verdict off stdout
    # rather than re-deriving it and risking a second opinion. A world with an
    # unassessable criterion is not ready either -- "we could not tell" is not
    # a pass.
    met = sum(1 for row in checks if row["met"] is True)
    print(f"\nVLN-READY: {'yes' if not failed and not unknown else 'no'} "
          f"({met}/{len(checks)} criteria met"
          + (f", {len(unknown)} not assessable" if unknown else "") + ")")
    for row in failed:
        print(f"  {row['criterion']} -- {row['remedy']}")

    out_dir = args.out or os.path.dirname(os.path.abspath(args.yaml))
    slug = os.path.basename(args.yaml).replace("-navmap.yaml", "")
    out_path = os.path.join(out_dir, slug + "-vlngraph.json")
    payload = vlngraph.to_world(carte, report)
    payload["criteria"] = checks
    payload["vln_ready"] = not failed and not unknown
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nwrote {out_path}")
    if not args.no_png:
        print("wrote " + vlngraph.draw(carte, report,
                                       os.path.join(out_dir, slug + "-vlngraph.png")))


if __name__ == "__main__":
    main()
