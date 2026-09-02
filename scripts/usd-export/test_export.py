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

import atexit
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


def build_synthetic_room(root: Path, slug: str) -> str | None:
    """A world tree with a real collision mesh, for a machine that has no world.

    Generated worlds are local data and are not checked in, so every CI run had
    `FIXTURE_WORLD is None` and skipped the seven tests that open an exported
    stage -- exactly the ones guarding the mistakes this project has actually
    made: the floor drifting off zero, the ground plane picking up an offset it
    must not have, the height failing to move to Z. They only ever ran on the
    one laptop that had generated a world.

    The room is a box 3 m tall, subdivided until it carries enough vertices to
    satisfy the same assertions a real collider does. Its `ground_plane_offset`
    is measured from the mesh on disk rather than assumed, so the test still
    checks what it always checked -- that the exporter applies the offset with
    the right sign, on the right prim, after the flip -- without depending on
    trimesh keeping glTF's axes on a round trip.
    """
    try:
        import trimesh
    except ImportError:
        return None

    room = trimesh.creation.box(extents=(4.0, 3.0, 5.0))
    room.apply_translation((0.0, 1.5, 0.0))  # floor on y=0, ceiling on y=3
    while len(room.vertices) < 1200:  # test_collider_is_present_and_collidable
        room = room.subdivide()

    world_dir = root / slug / "output" / "world"
    world_dir.mkdir(parents=True, exist_ok=True)
    glb = world_dir / "0-world.glb"
    room.export(glb)

    reloaded = trimesh.load(glb, process=False)
    ceiling = float(reloaded.bounds[1][1])
    # flip_y turns the room over: y -> -y, so its lowest point lands at -ceiling
    # and the offset that puts the floor back on zero is the ceiling height.
    (world_dir / "0-world-500k.spz").write_bytes(b"")  # found, never opened
    (world_dir / "0-world.json").write_text(
        json.dumps({
            "display_name": "Synthetic Room",
            "assets": {"splats": {"semantics_metadata": {
                "flip_y": True,
                "metric_scale_factor": 1.0,
                "ground_plane_offset": ceiling,
            }}},
        }),
        encoding="utf-8",
    )
    return slug


FIXTURE_WORLD = find_fixture_world()
SYNTHETIC_ROOT: Path | None = None
if FIXTURE_WORLD is None:
    SYNTHETIC_ROOT = Path(tempfile.mkdtemp(prefix="imageworld-usd-test-"))
    FIXTURE_WORLD = build_synthetic_room(SYNTHETIC_ROOT, "synthetic-room")
    if FIXTURE_WORLD is None:
        shutil.rmtree(SYNTHETIC_ROOT, ignore_errors=True)
        SYNTHETIC_ROOT = None
    else:
        atexit.register(shutil.rmtree, SYNTHETIC_ROOT, True)
        # In-process lookups (resolve_world) read this; the export subprocess is
        # told through IMAGEWORLD_WORLDS_DIR in run_export().
        exporter.WORLDS_DIR = SYNTHETIC_ROOT

def missing_exporter_dependencies() -> list[str]:
    """What the exporter would refuse to start without, in this interpreter.

    The stage tests re-run the exporter through `sys.executable`, so the answer
    has to be about this interpreter and about the exporter's whole list -- not
    just OpenUSD. Checking only `pxr` was enough to turn six clean skips into
    six errors on a machine that had OpenUSD and nothing else.
    """
    missing = []
    for module, hint in exporter.REQUIRED_MODULES:
        try:
            __import__(module)
        except ImportError:
            missing.append(hint)
    return missing


MISSING_DEPENDENCIES = missing_exporter_dependencies()

# Two separate reasons to skip, and both are worth naming. CI has no generated
# worlds -- they are local data -- and not the exporter's heavy dependencies,
# because the contract tests that matter most there read TypeScript source and
# need neither.
needs_world = pytest.mark.skipif(
    FIXTURE_WORLD is None or bool(MISSING_DEPENDENCIES),
    reason=(f"needs a generated world in public/worlds/ (have: {FIXTURE_WORLD}) and "
            f"pip install {' '.join(MISSING_DEPENDENCIES) or '-r scripts/usd-export/requirements.txt'}"),
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


def _synthetic_world(root, slug, semantics):
    """The least on disk that `resolve_world` will accept.

    It never opens the splat, only finds it, so an empty file is enough. This
    exists because no world in `public/worlds` carries `rotation_x` -- the ones
    that would are video reconstructions, which are not checked in -- and the
    path from `semantics_metadata` to the exporter's field had no test at all.
    """
    world_dir = root / slug / "output" / "world"
    world_dir.mkdir(parents=True)
    (world_dir / "0-world-500k.spz").write_bytes(b"")
    (world_dir / "0-world.json").write_text(
        json.dumps({"assets": {"splats": {"semantics_metadata": semantics}}}),
        encoding="utf-8",
    )
    return root


def test_a_world_with_no_splat_exports_under_no_splat(tmp_path, monkeypatch):
    """`--no-splat` has to mean it. It did not.

    `resolve_world` failed with "no background splat" whatever the flag said, so
    a world that has collision geometry and no appearance could not be exported
    at all -- which is exactly what a synthetic fixture is, and what
    `build_navmap.py` needs, since it never looks at the splat. The flag's own
    comment already said these runs "never touch it".
    """
    root = tmp_path / "bare"
    world_dir = root / "rooms" / "output" / "world"
    world_dir.mkdir(parents=True)
    (world_dir / "0-world.glb").write_bytes(b"")
    (world_dir / "0-world.json").write_text(
        json.dumps({"assets": {"splats": {"semantics_metadata": {}}}}),
        encoding="utf-8")
    monkeypatch.setattr(exporter, "WORLDS_DIR", root)

    world = exporter.resolve_world("rooms", "500k", need_splat=False)
    assert world.splat_path is None
    assert world.collider_path is not None


def test_a_world_with_no_splat_still_fails_when_one_is_wanted(tmp_path, monkeypatch):
    """The other half, so the fix does not quietly disarm the check.

    Without `--no-splat` the splat is the point of the export, and a missing one
    is still a refusal rather than a stage that succeeds and writes nothing.
    """
    root = tmp_path / "bare"
    world_dir = root / "rooms" / "output" / "world"
    world_dir.mkdir(parents=True)
    (world_dir / "0-world.json").write_text(
        json.dumps({"assets": {"splats": {"semantics_metadata": {}}}}),
        encoding="utf-8")
    monkeypatch.setattr(exporter, "WORLDS_DIR", root)

    with pytest.raises(SystemExit):
        exporter.resolve_world("rooms", "500k")


def test_resolve_world_reads_rotation_x(tmp_path, monkeypatch):
    """A Nerfstudio world arrives as flip_y false plus an explicit rotation."""
    monkeypatch.setattr(
        exporter, "WORLDS_DIR",
        _synthetic_world(tmp_path, "zup", {"flip_y": False, "rotation_x": -math.pi / 2}),
    )
    world = exporter.resolve_world("zup", "500k")
    assert world.flip_y is False
    assert world.rotation_x == pytest.approx(-math.pi / 2)
    # The value the exporter would actually author on /World/Background.
    assert exporter.background_rotation_degrees(
        world.flip_y, world.rotation_x
    ) == pytest.approx(-90.0)


def test_resolve_world_without_rotation_x_falls_back_to_flip_y(tmp_path, monkeypatch):
    """A Marble world says nothing about rotation, and must keep the old 180."""
    monkeypatch.setattr(
        exporter, "WORLDS_DIR", _synthetic_world(tmp_path, "marble", {"flip_y": True}),
    )
    world = exporter.resolve_world("marble", "500k")
    assert world.rotation_x is None
    assert exporter.background_rotation_degrees(world.flip_y, world.rotation_x) == 180.0


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
    environment = dict(os.environ)
    if SYNTHETIC_ROOT is not None:
        environment["IMAGEWORLD_WORLDS_DIR"] = str(SYNTHETIC_ROOT)
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
        env=environment,
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
def test_collider_meshes_state_their_approximation(y_up_stage):
    """Every collision mesh must ask for the triangle mesh in writing.

    Left unauthored it works by schema default, and that default is doing real
    work: a convex hull would swallow the doorways, the gaps under furniture
    and the holes the reconstruction left, and the navigation map built from
    PhysX raycasts would come back as one solid block of floor. Relying on a
    default for that is not the same as asking for it.
    """
    from pxr import UsdGeom, UsdPhysics

    collider = y_up_stage.GetPrimAtPath("/World/Background/Collider")
    meshes = [p for p in iter_descendants(collider) if p.IsA(UsdGeom.Mesh)]
    assert meshes, "collider has no mesh geometry"
    for mesh in meshes:
        assert mesh.HasAPI(UsdPhysics.MeshCollisionAPI), (
            f"{mesh.GetPath()} carries no MeshCollisionAPI"
        )
        approximation = UsdPhysics.MeshCollisionAPI(mesh).GetApproximationAttr().Get()
        assert approximation == UsdPhysics.Tokens.none, (
            f"{mesh.GetPath()} would be cooked as '{approximation}', not the source mesh"
        )
    # A triangle mesh can only be static. If the background ever becomes a rigid
    # body, PhysX quietly swaps in a convex hull and the map goes solid.
    assert not collider.HasAPI(UsdPhysics.RigidBodyAPI), (
        "a rigid-body background would force PhysX off the triangle mesh"
    )


@needs_world
def test_floor_sits_near_origin(y_up_stage):
    """The manifest's calibrated offset must land the floor on zero.

    Sign and order both matter and neither is visible in the viewer: the offset
    is applied after the flip, and it belongs to the background alone. It once
    went onto the ground plane as well, which put an invisible floor a metre
    above the real one and every dropped rigid body on top of it.
    """
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
