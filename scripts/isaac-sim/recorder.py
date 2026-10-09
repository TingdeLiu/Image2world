"""Film a navigation run: what the robot sees, the robot from behind, and a plan view.

`navigate.py --record DIR` drives exactly the run it always drives and, on the
side, writes three things for `compose_video.py` to put together:

    first/NNNNNN.jpg   a camera on the robot's nose, looking where it walks
    chase/NNNNNN.jpg   a camera following behind it
    topdown.png        one orthographic render of the route, cut below the ceiling
    poses.jsonl        the robot's pose for every frame, and meta.json

Two choices are worth knowing before changing anything here.

The chase camera sits on the robot's own trail, a fixed distance back along the
path it has walked, not at a fixed offset behind its heading. A fixed offset
swings through walls on every corner of a corridor; the trail is, by
construction, space the robot has just been standing in.

The plan view is a camera looking straight down from just under the ceiling,
not the occupancy grid. Its near clipping plane is what removes the ceiling, so
the picture is a section through the building at that height -- the floor, the
shopfronts and the atrium, rendered from the same splats as the other two views.
"""
from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

H_APERTURE = 20.955          # USD's default filmback, mm


def _look_at(eye, target):
    """A USD camera transform (row-vector) at `eye` looking at `target`, Z up.

    USD cameras look down their local -Z with +Y up, so the rows are the world
    images of the camera's X (right), Y (up) and Z (backwards) axes.
    """
    from pxr import Gf

    eye, target = np.asarray(eye, float), np.asarray(target, float)
    forward = target - eye
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, (0.0, 0.0, 1.0))
    if np.linalg.norm(right) < 1e-6:            # looking straight down or up
        right = np.array([1.0, 0.0, 0.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    rows = [list(right) + [0.0], list(up) + [0.0], list(-forward) + [0.0],
            list(eye) + [1.0]]
    return Gf.Matrix4d(*[float(v) for row in rows for v in row])


def _smooth_angle(previous, new, alpha):
    if previous is None:
        return new
    return previous + alpha * math.atan2(math.sin(new - previous), math.cos(new - previous))


class Recorder:
    def __init__(self, stage, out_dir, every=2, first_res=(1920, 1080),
                 chase_res=(1280, 1080), first_hfov=80.0, chase_hfov=65.0,
                 chase_back=2.0, chase_height=0.85, nose=(0.50, 0.0, 0.12),
                 pitch_deg=-8.0, fill_light=1000.0):
        import omni.replicator.core as rep

        self.rep = rep
        self.stage = stage
        self.out_dir = out_dir
        self.every = max(1, int(every))
        self.chase_back = chase_back
        self.chase_height = chase_height
        self.nose = nose
        self.pitch = math.radians(pitch_deg)
        for sub in ("first", "chase"):
            os.makedirs(os.path.join(out_dir, sub), exist_ok=True)
        if fill_light:
            # The exported stage carries one distant light at intensity 1, which
            # is nothing: the splats are radiance and need no light, but the
            # robot is an ordinary mesh and came out a black silhouette.
            from pxr import UsdLux

            UsdLux.DomeLight.Define(stage, "/World/Record/Fill").CreateIntensityAttr(fill_light)

        self.first_op = self._camera("/World/Record/FirstPerson", first_res, first_hfov)
        self.chase_op = self._camera("/World/Record/Chase", chase_res, chase_hfov)
        self.annotators = {}
        for name, path, res in (("first", "/World/Record/FirstPerson", first_res),
                                ("chase", "/World/Record/Chase", chase_res)):
            product = rep.create.render_product(path, res)
            annotator = rep.AnnotatorRegistry.get_annotator("rgb")
            annotator.attach([product])
            self.annotators[name] = annotator

        self.pool = ThreadPoolExecutor(max_workers=6)
        self.poses = open(os.path.join(out_dir, "poses.jsonl"), "w", encoding="utf-8")
        self.trail = []            # (x, y, z) every frame, for the chase camera
        self.yaw = None
        self.chase_eye = None
        self.chase_target = None
        self.pending = None        # the pose the cameras were placed for
        self.saved = 0
        self.frame = 0

    def _camera(self, path, res, hfov_deg):
        from pxr import Gf, UsdGeom

        cam = UsdGeom.Camera.Define(self.stage, path)
        cam.CreateHorizontalApertureAttr(H_APERTURE)
        cam.CreateVerticalApertureAttr(H_APERTURE * res[1] / res[0])
        cam.CreateFocalLengthAttr(H_APERTURE / (2.0 * math.tan(math.radians(hfov_deg) / 2.0)))
        cam.CreateClippingRangeAttr(Gf.Vec2f(0.05, 400.0))
        return cam.MakeMatrixXform()

    # --- the plan view ---------------------------------------------------
    def render_topdown(self, app, bounds, cut_z, px_per_m=40, warmup=40):
        """One orthographic render of `bounds` (xmin, xmax, ymin, ymax), looking down
        from `cut_z`. USD's orthographic aperture is in tenths of a scene unit."""
        from pxr import Gf, UsdGeom

        xmin, xmax, ymin, ymax = bounds
        width_m, height_m = xmax - xmin, ymax - ymin
        res = (int(round(width_m * px_per_m)), int(round(height_m * px_per_m)))
        cam = UsdGeom.Camera.Define(self.stage, "/World/Record/TopDown")
        cam.CreateProjectionAttr(UsdGeom.Tokens.orthographic)
        cam.CreateHorizontalApertureAttr(width_m * 10.0)
        cam.CreateVerticalApertureAttr(height_m * 10.0)
        cam.CreateClippingRangeAttr(Gf.Vec2f(0.01, 60.0))
        # Looking straight down needs no rotation at all: the camera's -Z is the
        # world's -Z, and its +Y (the top of the picture) is the world's +Y.
        placed = Gf.Matrix4d(1.0)
        placed.SetTranslateOnly(Gf.Vec3d((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, cut_z))
        cam.MakeMatrixXform().Set(placed)
        product = self.rep.create.render_product("/World/Record/TopDown", res)
        annotator = self.rep.AnnotatorRegistry.get_annotator("rgb")
        annotator.attach([product])
        # The timeline is not playing yet, and replicator only captures on its
        # own while it is: app.update() alone left this annotator empty. Ask for
        # the frame explicitly, with subframes so the path tracer converges.
        for _ in range(4):
            app.update()
        self.rep.orchestrator.step(rt_subframes=warmup)
        data = np.asarray(annotator.get_data())
        annotator.detach()
        product.destroy()
        UsdGeom.Imageable(cam).MakeInvisible()
        if data.size == 0:
            print("record: !! the plan view came back empty; the video will use the map instead")
            self.topdown = None
            return
        from PIL import Image

        path = os.path.join(self.out_dir, "topdown.png")
        Image.fromarray(data[..., :3]).save(path)
        self.topdown = {"image": "topdown.png", "bounds": [xmin, xmax, ymin, ymax],
                        "cut_z": cut_z, "px_per_m": px_per_m, "size": list(res)}
        print(f"record: plan view {res[0]}x{res[1]} px of {width_m:.1f} x {height_m:.1f} m, "
              f"cut at z={cut_z:.2f}, mean {data[..., :3].mean():.0f}/255 -> {path}")

    # --- every frame -----------------------------------------------------
    def _place(self, here):
        x, y, z, yaw = here
        self.yaw = _smooth_angle(self.yaw, yaw, 0.15)
        c, s = math.cos(self.yaw), math.sin(self.yaw)

        # First person: on the nose, heading smoothed so the gait's sway does
        # not shake the picture, pitched a little down to see the floor ahead.
        fx, fy, fz = self.nose
        eye = (x + c * fx - s * fy, y + s * fx + c * fy, z + fz)
        reach = 10.0
        target = (eye[0] + c * reach, eye[1] + s * reach, eye[2] + reach * math.tan(self.pitch))
        self.first_op.Set(_look_at(eye, target))

        # Chase: a point `chase_back` metres back along the trail.
        self.trail.append((x, y, z))
        back, i = 0.0, len(self.trail) - 1
        while i > 0 and back < self.chase_back:
            ax, ay, _ = self.trail[i]
            bx, by, _ = self.trail[i - 1]
            back += math.hypot(ax - bx, ay - by)
            i -= 1
        if back >= self.chase_back:
            px, py, _ = self.trail[i]
        else:   # not walked far enough yet: straight behind the heading
            px, py = x - c * self.chase_back, y - s * self.chase_back
        eye = np.array([px, py, z + self.chase_height])
        target = np.array([x + c * 1.2, y + s * 1.2, z - 0.1])
        alpha = 0.12
        self.chase_eye = eye if self.chase_eye is None else self.chase_eye + alpha * (eye - self.chase_eye)
        self.chase_target = (target if self.chase_target is None
                             else self.chase_target + alpha * (target - self.chase_target))
        self.chase_op.Set(_look_at(self.chase_eye, self.chase_target))

    def step(self, here, sim_seconds, info=None):
        """Call once per rendered frame, right after the app update.

        Saves what the update just rendered -- the cameras placed on the previous
        call -- then places them for the pose the robot is in now.
        """
        if self.pending is not None and self.frame % self.every == 0:
            images = {name: np.asarray(a.get_data()) for name, a in self.annotators.items()}
            if all(img.size for img in images.values()):
                index = self.saved
                for name, img in images.items():
                    path = os.path.join(self.out_dir, name, f"{index:06d}.jpg")
                    self.pool.submit(_save_jpeg, img[..., :3].copy(), path)
                record = dict(self.pending)
                record["index"] = index
                self.poses.write(json.dumps(record) + "\n")
                self.saved += 1
        self._place(here)
        self.pending = {"t": round(sim_seconds, 3),
                        "x": round(float(here[0]), 4), "y": round(float(here[1]), 4),
                        "z": round(float(here[2]), 4), "yaw": round(float(here[3]), 4),
                        **(info or {})}
        self.frame += 1

    def close(self, meta):
        self.pool.shutdown(wait=True)
        self.poses.close()
        meta = dict(meta)
        meta["frames"] = self.saved
        meta["every"] = self.every
        meta["topdown"] = getattr(self, "topdown", None)
        with open(os.path.join(self.out_dir, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        print(f"record: {self.saved} frames -> {self.out_dir}")


def _save_jpeg(rgb, path):
    from PIL import Image

    Image.fromarray(rgb).save(path, quality=95, subsampling=0)
