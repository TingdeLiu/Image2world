"""Guards against the exporter drifting from the browser runtime.

The USD exporter re-implements transform semantics that live in TypeScript:
the background transform, the sun direction, and the scene.json precedence
rules. Nothing links the two, so these tests do the linking -- half of them
assert the TypeScript source still says what the exporter assumes, and half
assert the exporter actually produces those transforms.

Run:
    scripts/usd-export/.venv/Scripts/python -m pytest scripts/usd-export -q
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import export_world_usd as exporter  # noqa: E402

REPO_ROOT = exporter.REPO_ROOT
SRC = REPO_ROOT / "src"
WORLDS_DIR = exporter.WORLDS_DIR


def find_fixture_world() -> str | None:
    """Any locally generated world carrying a background collision mesh.

    Generated worlds are local data, not fixtures checked into the repo, so the
    behaviour tests adapt to whatever is present and skip when nothing is.
    """
    if not WORLDS_DIR.is_dir():
        return None
    for candidate in sorted(WORLDS_DIR.iterdir()):
        if candidate.name.startswith("."):
            continue
        world_dir = candidate / "output" / "world"
        if world_dir.is_dir() and any(world_dir.glob("*-world.glb")):
            return candidate.name
    return None


FIXTURE_WORLD = find_fixture_world()
needs_world = pytest.mark.skipif(
    FIXTURE_WORLD is None,
    reason="no generated world with a collision mesh in public/worlds/",
)


def read_source(relative: str) -> str:
    path = SRC / relative
    assert path.is_file(), f"missing {path} -- did the file move?"
    return path.read_text(encoding="utf-8")


def assert_contains(source: str, pattern: str, what: str, owner: str) -> None:
    if re.search(pattern, source) is None:
        pytest.fail(
            f"{what} changed in {owner}.\n"
            f"  expected pattern: {pattern}\n"
            f"  scripts/usd-export/export_world_usd.py mirrors this -- update both, "
            f"then update this test."
        )


# --------------------------------------------------------------------------
# Contract: the TypeScript runtime still means what the exporter assumes
# --------------------------------------------------------------------------


def test_placement_contract():
    """`worldTransform()` is the placement, and `build_stage()` mirrors it.

    This used to grep the two components for the arithmetic written inline in
    each. The arithmetic moved into one shared helper, both greps stopped
    matching, and the drift these tests exist to catch happened underneath
    them: the TypeScript grew a `rotationX` override for Z-up Nerfstudio worlds
    and the exporter kept reading only `flip_y`. Watch the single source
    instead.
    """
    source = read_source("modules/collider/worldTransform.ts")
    assert_contains(
        source,
        r"rotation: \[rotationX \?\? \(flipY \? Math\.PI : 0\), 0, 0\]",
        "Background rotation (X axis; explicit rotationX wins over flipY)",
        "worldTransform.ts",
    )
    assert_contains(
        source,
        r"position: \[0, groundPlaneOffset \?\? 0, 0\]",
        "Background ground offset",
        "worldTransform.ts",
    )
    assert_contains(
        source,
        r"const scale = metricScaleFactor \?\? 1",
        "Background metric scale default",
        "worldTransform.ts",
    )
    assert_contains(
        source,
        r"scale: \[scale, scale, scale\]",
        "Background metric scale, uniform on all three axes",
        "worldTransform.ts",
    )


def test_both_renderers_use_the_shared_transform():
    """Neither component may go back to computing the placement itself.

    The exporter mirrors one function. If a component stops calling it, the
    exporter is mirroring something that no longer governs what the viewer
    draws -- and the splat and the collider can drift apart from each other
    too, which is silent: walking still works, the collider is just somewhere
    the splat is not.
    """
    for relative in ("modules/splat/SplatRenderer.tsx", "modules/collider/WorldCollider.tsx"):
        assert_contains(
            read_source(relative),
            r"worldTransform\(\{",
            "Shared placement helper",
            relative,
        )


def test_background_rotation_mirrors_the_typescript():
    """The exporter's half of the contract above, exercised directly."""
    assert exporter.background_rotation_degrees(True, None) == 180.0
    assert exporter.background_rotation_degrees(False, None) == 0.0
    # Nerfstudio: Z-up, arrives as flip_y=False with rotation_x=-pi/2.
    assert exporter.background_rotation_degrees(False, -math.pi / 2) == pytest.approx(-90.0)
    # An explicit rotation wins even when flip_y would say otherwise ...
    assert exporter.background_rotation_degrees(True, -math.pi / 2) == pytest.approx(-90.0)
    # ... including an explicit zero, which `??` keeps and a truth test loses.
    assert exporter.background_rotation_degrees(True, 0.0) == 0.0


def test_sun_direction_contract():
    """sun_direction() mirrors sunPositionFromRotation()."""
    source = read_source("components/WorldViewer.tsx")
    assert_contains(
        source,
        r"function sunPositionFromRotation",
        "Sun position helper",
        "WorldViewer.tsx",
    )
    # Start point (0, 10, 0), then rotate about X, Y, Z in that order.
    assert_contains(
        source,
        r"let x = 0\s+let y = 10\s+let z = 0",
        "Sun start position (0, 10, 0)",
        "WorldViewer.tsx",
    )
    assert_contains(
        source,
        r"\[y, z\] = \[y \* cx - z \* sx, y \* sx \+ z \* cx\]",
        "Sun X-axis rotation",
        "WorldViewer.tsx",
    )


def test_semantics_precedence_contract():
    """scene.json overrides the world manifest for scale and ground offset."""
    source = read_source("components/WorldViewer.tsx")
    assert_contains(
        source,
        r"sceneProject\?\.metricScaleFactor \?\? baseMetricScaleFactor",
        "metricScaleFactor precedence",
        "WorldViewer.tsx",
    )
    assert_contains(
        source,
        r"sceneProject\?\.groundPlaneOffset \?\? defaultGroundPlaneOffset",
        "groundPlaneOffset precedence",
        "WorldViewer.tsx",
    )
    assert_contains(
        source,
        r"const flipY = flip_y \?\? true",
        "flip_y default",
        "WorldViewer.tsx",
    )


def test_placement_schema_contract():
    """scene.json instances still carry the fields the exporter reads."""
    source = read_source("types/world.ts")
    for field in ("instanceId", "objectId", "position", "rotation", "scale"):
        assert_contains(source, rf"\b{field}\b", f"WorldObjectPlacement.{field}", "types/world.ts")


# --------------------------------------------------------------------------
# Behaviour: sun_direction() numerics
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rotation, expected",
    [
        ((0.0, 0.0, 0.0), (0.0, 10.0, 0.0)),
        ((math.pi / 2, 0.0, 0.0), (0.0, 0.0, 10.0)),
        ((0.0, math.pi / 2, 0.0), (0.0, 10.0, 0.0)),
        ((math.pi / 4, 0.0, 0.0), (0.0, 7.0710678, 7.0710678)),
    ],
)
def test_sun_direction_values(rotation, expected):
    result = exporter.sun_direction(rotation)
    assert result == pytest.approx(expected, abs=1e-6)


# --------------------------------------------------------------------------
# Behaviour: the exported stage
# --------------------------------------------------------------------------


def run_export(tmp_path: Path, *flags: str) -> Path:
    out = tmp_path / "world.usda"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "export_world_usd.py"),
            FIXTURE_WORLD,
            "--format",
            "usda",
            "--no-splat",  # keeps the test fast; transforms are authored regardless
            "--out",
            str(out.parent / "bundle"),
            *flags,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"export failed:\n{result.stdout}\n{result.stderr}"
    stage_path = out.parent / "bundle" / "world.usda"
    assert stage_path.is_file(), f"no stage at {stage_path}"
    return stage_path


@pytest.fixture(scope="module")
def y_up_stage(tmp_path_factory):
    from pxr import Usd

    return Usd.Stage.Open(str(run_export(tmp_path_factory.mktemp("yup"))))


@pytest.fixture(scope="module")
def z_up_stage(tmp_path_factory):
    from pxr import Usd

    return Usd.Stage.Open(str(run_export(tmp_path_factory.mktemp("zup"), "--up-axis", "z")))


@needs_world
def test_stage_basics(y_up_stage):
    from pxr import UsdGeom

    assert y_up_stage.GetDefaultPrim().GetPath() == "/World"
    assert UsdGeom.GetStageUpAxis(y_up_stage) == UsdGeom.Tokens.y
    assert UsdGeom.GetStageMetersPerUnit(y_up_stage) == 1.0


@needs_world
def test_background_transform_is_flip_y(y_up_stage):
    """flip_y must land as a 180-degree rotation about X, not any other axis."""
    from pxr import Gf, Usd, UsdGeom

    background = y_up_stage.GetPrimAtPath("/World/Background")
    assert background.IsValid()
    matrix = UsdGeom.XformCache().GetLocalToWorldTransform(background)

    world = exporter.resolve_world(FIXTURE_WORLD, "100k")
    expected = (
        Gf.Matrix4d().SetScale(world.metric_scale_factor)
        * Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), 180.0 if world.flip_y else 0.0))
        * Gf.Matrix4d().SetTranslate(Gf.Vec3d(0, world.ground_plane_offset, 0))
    )
    # The scale op is authored as Vec3f, so compare at single-precision tolerance.
    for row in range(4):
        assert matrix.GetRow(row) == pytest.approx(tuple(expected.GetRow(row)), abs=1e-6)


def iter_descendants(prim):
    for child in prim.GetChildren():
        yield child
        yield from iter_descendants(child)


@needs_world
def test_collider_is_present_and_collidable(y_up_stage):
    from pxr import UsdGeom, UsdPhysics

    collider = y_up_stage.GetPrimAtPath("/World/Background/Collider")
    assert collider.IsValid(), "background collision mesh missing"
    assert collider.HasAPI(UsdPhysics.CollisionAPI)
    meshes = [p for p in iter_descendants(collider) if p.IsA(UsdGeom.Mesh)]
    assert meshes, "collider has no mesh geometry"
    points = UsdGeom.Mesh(meshes[0]).GetPointsAttr().Get()
    assert points and len(points) > 1000


@needs_world
def test_floor_sits_near_origin(y_up_stage):
    """home-room uses the manifest's calibrated offset, so the floor lands at 0."""
    from pxr import Usd, UsdGeom

    bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    collider = y_up_stage.GetPrimAtPath("/World/Background/Collider")
    floor = bounds.ComputeWorldBound(collider).GetRange().GetMin()[1]
    assert abs(floor) < 0.25, f"floor drifted to {floor:.3f} m"


@needs_world
def test_z_up_moves_height_to_z(z_up_stage):
    """Isaac Sim / PhysX assume gravity along -Z."""
    from pxr import Usd, UsdGeom

    assert UsdGeom.GetStageUpAxis(z_up_stage) == UsdGeom.Tokens.z

    bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    collider = z_up_stage.GetPrimAtPath("/World/Background/Collider")
    box = bounds.ComputeWorldBound(collider).GetRange()
    assert abs(box.GetMin()[2]) < 0.25, "floor is not on the Z=0 plane"
    # A room is taller in its up axis than it is deep in the former up axis.
    assert box.GetMax()[2] > 2.0, "room height did not land on Z"


@needs_world
def test_ground_plane_sits_at_the_origin(y_up_stage):
    """The plane must coincide with the floor, which the background already puts at 0.

    It used to carry `groundPlaneOffset` as well, which raised it a metre above
    the floor. Harmless in the viewer, which never uses it for anything the eye
    can see; in Isaac Sim every rigid body landed on it instead of on the room.
    """
    from pxr import Usd, UsdGeom, UsdPhysics

    ground = y_up_stage.GetPrimAtPath("/World/GroundPlane")
    assert ground.IsValid()
    assert ground.HasAPI(UsdPhysics.CollisionAPI)
    assert UsdGeom.Plane(ground).GetAxisAttr().Get() == UsdGeom.Tokens.y

    height = (
        UsdGeom.Xformable(ground)
        .ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        .ExtractTranslation()[1]
    )
    assert abs(height) < 1e-6, f"ground plane sits at {height:.3f} m, not on the floor"
