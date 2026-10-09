"""Tests for the CPU half of `render_viewpoints.py`: reading the PLY and moving
the gaussians into the viewer frame. The rasteriser itself needs CUDA and is
checked by rendering (docs/ISAAC_SIM.md); what goes into it is checked here,
because a wrong quaternion renders a plausible-looking but smeared world.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from backend.tools.render_viewpoints import gaussians_in_viewer, local_path, read_ply, quaternion_of


def write_ply(path, count=3, extra_element=True, rest=0):
    names = (["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2"] + [f"f_rest_{i}" for i in range(rest)]
             + ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"])
    data = np.zeros(count, dtype=[(n, "<f4") for n in names])
    data["x"] = np.arange(count)
    data["y"] = 1.0
    data["rot_0"] = 1.0
    data["scale_0"] = data["scale_1"] = data["scale_2"] = math.log(0.1)
    header = ["ply", "format binary_little_endian 1.0", f"element vertex {count}"]
    header += [f"property float {n}" for n in names]
    if extra_element:
        # SHARP appends the source camera after the vertices; it must be ignored.
        header += ["element intrinsic 9", "property float intrinsic"]
    header.append("end_header")
    with open(path, "wb") as handle:
        handle.write(("\n".join(header) + "\n").encode("ascii"))
        handle.write(data.tobytes())
        if extra_element:
            handle.write(np.arange(9, dtype="<f4").tobytes())
    return data


def test_reads_only_the_vertex_element(tmp_path):
    write_ply(tmp_path / "a.ply")
    ply = read_ply(tmp_path / "a.ply")
    np.testing.assert_allclose(ply["x"], [0, 1, 2])
    assert len(ply["opacity"]) == 3


def test_half_turn_about_x_moves_means_rotations_and_scales_together(tmp_path):
    write_ply(tmp_path / "a.ply")
    # flip_y with a metric scale of 2 and a 1.5 m ground offset.
    matrix = np.array([[2, 0, 0, 0], [0, -2, 0, 1.5], [0, 0, -2, 0], [0, 0, 0, 1]], dtype=np.float64)
    means, quats, scales, opacities, sh, degree = gaussians_in_viewer(read_ply(tmp_path / "a.ply"), matrix)
    np.testing.assert_allclose(means[1], [2, -0.5, 0])
    # Identity orientation becomes the half turn itself: w=0, x=+-1.
    np.testing.assert_allclose(np.abs(quats[0]), [0, 1, 0, 0], atol=1e-12)
    np.testing.assert_allclose(scales, 0.2)
    np.testing.assert_allclose(opacities, 0.5)
    assert sh.shape == (3, 1, 3) and degree == 0


def test_view_dependent_colour_is_dropped_rather_than_mis_rotated(tmp_path):
    write_ply(tmp_path / "a.ply", rest=45)
    ply = read_ply(tmp_path / "a.ply")
    _, _, _, _, sh, degree = gaussians_in_viewer(ply, np.eye(4))
    assert sh.shape == (3, 16, 3) and degree == 3
    rotate = np.eye(4)
    rotate[1:3, 1:3] = [[0, -1], [1, 0]]
    _, _, _, _, sh, degree = gaussians_in_viewer(ply, rotate)
    assert sh.shape == (3, 1, 3) and degree == 0


@pytest.mark.parametrize("angle", [0.3, math.pi / 2, math.pi, -2.5])
def test_quaternion_of_round_trips_a_rotation(angle):
    c, s = math.cos(angle), math.sin(angle)
    rotation = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    w, x, y, z = quaternion_of(rotation)
    rebuilt = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    np.testing.assert_allclose(rebuilt, rotation, atol=1e-12)


def test_windows_paths_reach_wsl(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert str(local_path("D:/Tyndall Labs/x.ply")).replace("\\", "/") == "/mnt/d/Tyndall Labs/x.ply"
    assert str(local_path("/already/posix")).replace("\\", "/") == "/already/posix"
