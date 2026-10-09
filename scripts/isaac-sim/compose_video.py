"""Put a filmed navigation run together as one video.

    python scripts/isaac-sim/compose_video.py <film dir> --map <navmap.yaml> --out run.mp4

Reads what `navigate.py --record` wrote and lays it out on a 2560x1080 canvas:

    +--------------------------------+----------+
    |                                |  chase   |
    |   first person, 1920x1080      |----------|
    |                                |  plan    |
    +--------------------------------+----------+

Plain Python -- numpy, Pillow and `ffmpeg` on the PATH -- so the layout can be
changed and re-rendered without driving the robot again.

The plan view is the top-down splat render shown *only on floor the map calls
free*, over the map's own colours everywhere else. Looked at from above, a
splat trained from the ground shows what it never had to get right: the atrium
here fills with metre-wide coloured discs. The map knows where the floor is, so
it decides where the render is believable.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import navmap  # noqa: E402

CANVAS = (2560, 1080)
MAIN = (1920, 1080)
SIDE = (640, 540)
SS = 2                      # the plan view is drawn at 2x and filtered down

BG = (21, 23, 28)
WALL = (128, 136, 154)
PLAN = (90, 160, 255)
TRAIL = (255, 92, 60)
START = (60, 200, 110)
GOAL = (255, 176, 40)

FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"


def font(size, bold=False):
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT, size)
    except OSError:
        return ImageFont.load_default()


def tag(draw, xy, text, size=22, bold=False, pad=(12, 6), fill=(255, 255, 255)):
    """Text on a translucent dark box. `draw` must be on an RGBA layer."""
    f = font(size, bold)
    box = draw.textbbox(xy, text, font=f)
    draw.rounded_rectangle([box[0] - pad[0], box[1] - pad[1], box[2] + pad[0], box[3] + pad[1]],
                           radius=8, fill=(10, 12, 16, 150))
    draw.text(xy, text, font=f, fill=fill)
    return box


class PlanView:
    def __init__(self, film, meta, carte):
        td = meta["topdown"]
        self.xmin, self.xmax, self.ymin, self.ymax = td["bounds"]
        w, h = SIDE[0] * SS, SIDE[1] * SS
        self.size = (w, h)
        u = (np.arange(w) + 0.5) / w
        v = (np.arange(h) + 0.5) / h
        X = self.xmin + u * (self.xmax - self.xmin)
        Y = self.ymax - v * (self.ymax - self.ymin)
        ix = np.clip(np.floor((X - carte.origin[0]) / carte.resolution).astype(int), 0, carte.width - 1)
        iy = np.clip(np.floor((Y - carte.origin[1]) / carte.resolution).astype(int), 0, carte.height - 1)
        grid = carte.grid[np.ix_(iy, ix)]
        render = np.asarray(Image.open(os.path.join(film, td["image"])).convert("RGB")
                            .resize((w, h), Image.LANCZOS)).astype(np.float32)
        base = np.empty((h, w, 3), np.float32)
        base[:] = BG
        free = grid == navmap.FREE
        base[free] = render[free] * 1.08
        base[grid == navmap.OCCUPIED] = WALL
        self.base = Image.fromarray(base.clip(0, 255).astype(np.uint8)).convert("RGBA")

        overlay = ImageDraw.Draw(self.base)
        plan = [self.px(*p) for p in meta["waypoints"]]
        for a, b in zip(plan, plan[1:]):            # dashed: the plan, not the drive
            self._dashed(overlay, a, b, PLAN, 3 * SS, 14 * SS)
        for point, colour in ((meta["start"], START), (meta["goal"], GOAL)):
            x, y = self.px(*point)
            r = 9 * SS
            overlay.ellipse([x - r, y - r, x + r, y + r], fill=colour + (255,),
                            outline=(255, 255, 255, 255), width=2 * SS)
        self.trail = Image.new("RGBA", self.size, (0, 0, 0, 0))
        self.trail_draw = ImageDraw.Draw(self.trail)
        self.last = None

    def px(self, x, y):
        w, h = self.size
        return ((x - self.xmin) / (self.xmax - self.xmin) * w,
                (self.ymax - y) / (self.ymax - self.ymin) * h)

    @staticmethod
    def _dashed(draw, a, b, colour, width, dash):
        length = math.dist(a, b)
        if length < 1e-6:
            return
        n = int(length // dash)
        for i in range(0, n + 1, 2):
            t0, t1 = i * dash / length, min(1.0, (i + 1) * dash / length)
            draw.line([(a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                       (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)],
                      fill=colour + (230,), width=width)

    def frame(self, pose):
        here = self.px(pose["x"], pose["y"])
        if self.last is not None:
            self.trail_draw.line([self.last, here], fill=TRAIL + (255,), width=5 * SS)
            r = 5 * SS / 2
            self.trail_draw.ellipse([here[0] - r, here[1] - r, here[0] + r, here[1] + r],
                                    fill=TRAIL + (255,))
        self.last = here
        img = Image.alpha_composite(self.base, self.trail)
        d = ImageDraw.Draw(img)
        # The robot: a disc with a wedge pointing where it faces.
        x, y = here
        yaw = pose["yaw"]
        r = 11 * SS
        tip = (x + math.cos(yaw) * r * 2.0, y - math.sin(yaw) * r * 2.0)
        left = (x + math.cos(yaw + 2.4) * r, y - math.sin(yaw + 2.4) * r)
        right = (x + math.cos(yaw - 2.4) * r, y - math.sin(yaw - 2.4) * r)
        d.polygon([tip, left, right], fill=(255, 255, 255, 235))
        d.ellipse([x - r * 0.7, y - r * 0.7, x + r * 0.7, y + r * 0.7], fill=TRAIL + (255,),
                  outline=(255, 255, 255, 255), width=2 * SS)
        return img.resize(SIDE, Image.LANCZOS).convert("RGB")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("film", help="the directory navigate.py --record wrote")
    parser.add_argument("--map", required=True, help="the -navmap.yaml the run planned on")
    parser.add_argument("--out", required=True, help="output .mp4")
    parser.add_argument("--title", default="Spot 自主导航")
    parser.add_argument("--subtitle", default="Isaac Sim 6.0  ·  3D 高斯泼溅")
    parser.add_argument("--crf", type=int, default=16, help="x264 quality; lower is sharper")
    parser.add_argument("--limit", type=int, default=0, help="only the first N frames (testing)")
    args = parser.parse_args()

    with open(os.path.join(args.film, "meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    with open(os.path.join(args.film, "poses.jsonl"), encoding="utf-8") as fh:
        poses = [json.loads(line) for line in fh if line.strip()]
    if args.limit:
        poses = poses[:args.limit]
    fps = 1.0 / (meta["render_dt"] * meta["every"])
    plan_view = PlanView(args.film, meta, navmap.load(args.map))
    planned = meta["planned_m"]
    # When it arrived, not when the film stops: the recording runs on past it.
    arrive_t = meta["seconds"]

    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{CANVAS[0]}x{CANVAS[1]}", "-r", f"{fps:g}", "-i", "-",
         "-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-tune", "film",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", args.out],
        stdin=subprocess.PIPE)

    speeds = []
    for n, pose in enumerate(poses):
        index = pose["index"]
        canvas = Image.new("RGB", CANVAS, BG)
        canvas.paste(Image.open(os.path.join(args.film, "first", f"{index:06d}.jpg")), (0, 0))
        chase = Image.open(os.path.join(args.film, "chase", f"{index:06d}.jpg"))
        canvas.paste(chase.resize(SIDE, Image.LANCZOS), (MAIN[0], 0))
        canvas.paste(plan_view.frame(pose), (MAIN[0], SIDE[1]))

        # Speed over the last half second of frames, so the gait does not show.
        back = poses[max(0, n - int(fps / 2))]
        dt = pose["t"] - back["t"]
        speed = math.hypot(pose["x"] - back["x"], pose["y"] - back["y"]) / dt if dt > 0 else 0.0
        speeds.append(speed)

        hud = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        d = ImageDraw.Draw(hud)
        d.line([(MAIN[0], 0), (MAIN[0], CANVAS[1])], fill=(10, 12, 16, 255), width=4)
        d.line([(MAIN[0], SIDE[1]), (CANVAS[0], SIDE[1])], fill=(10, 12, 16, 255), width=4)
        tag(d, (36, 30), args.title, size=40, bold=True)
        tag(d, (38, 98), args.subtitle, size=22)
        tag(d, (MAIN[0] - 160, 30), "第一视角", size=22)
        tag(d, (MAIN[0] + 18, 16), "跟随视角", size=20)
        tag(d, (MAIN[0] + 18, SIDE[1] + 16), "俯视轨迹", size=20)

        driven = pose.get("driven", 0.0)
        line = (f"t {pose['t']:6.1f} s     已行驶 {driven:5.1f} / {planned:.1f} m     "
                f"距目标 {pose.get('to_goal', 0.0):5.1f} m     {speed:4.2f} m/s")
        tag(d, (38, CANVAS[1] - 92), line, size=26)
        # Progress along the planned distance.
        x0, x1, yb = 38, MAIN[0] - 38, CANVAS[1] - 34
        d.rounded_rectangle([x0, yb, x1, yb + 10], radius=5, fill=(255, 255, 255, 60))
        frac = max(0.0, min(1.0, driven / planned)) if planned else 0.0
        d.rounded_rectangle([x0, yb, x0 + (x1 - x0) * frac, yb + 10], radius=5,
                            fill=TRAIL + (255,))
        if pose["t"] >= arrive_t - 1e-6 and meta.get("arrived"):
            ct = meta["cross_track_m"]
            tag(d, (MAIN[0] // 2 - 330, CANVAS[1] // 2 - 60),
                f"到达目标   行驶 {meta['driven_m']:.1f} m   平均偏离 {ct['mean'] * 100:.1f} cm",
                size=34, bold=True, pad=(24, 14), fill=(255, 236, 200))
        frame = Image.alpha_composite(canvas.convert("RGBA"), hud).convert("RGB")
        ffmpeg.stdin.write(frame.tobytes())
        if n % 250 == 0:
            print(f"  frame {n}/{len(poses)}  t={pose['t']:.1f}s")

    ffmpeg.stdin.close()
    if ffmpeg.wait() != 0:
        raise SystemExit("ffmpeg failed")
    size = os.path.getsize(args.out) / 1e6
    print(f"wrote {args.out}: {len(poses)} frames at {fps:g} fps "
          f"({len(poses) / fps:.1f} s), {size:.0f} MB")


if __name__ == "__main__":
    main()
