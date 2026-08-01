"""Export an ImageWorld project to a portable OpenUSD bundle.

The background Gaussian splat is written with NVIDIA's `usd-convert-gsplat`
(Apache-2.0) as a `ParticleField3DGaussianSplat` prim -- the OpenUSD geometry
type for radiance fields. Foreground GLB objects become `UsdGeomMesh` prims
with `UsdPreviewSurface` materials, placed with the transforms recorded in
`scene.json`, and optionally tagged with `UsdPhysics` APIs so the world opens
in Isaac Sim / Omniverse with its rigid bodies intact.

Usage:
    python scripts/usd-export/export_world_usd.py <slug> [--lod 500k] ...

See README.md for environment setup.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[2]
WORLDS_DIR = REPO_ROOT / "public" / "worlds"
LOD_CHOICES = ("100k", "150k", "500k", "full_res")
SPLAT_EXTENSIONS = (".ply", ".spz")

# The viewer's defaults, mirrored from src/components/WorldViewer.tsx.
DEFAULT_SEMANTICS = {"metric_scale_factor": 1.0, "ground_plane_offset": 0.0, "flip_y": True}


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def remove_tree(path: Path, attempts: int = 5) -> None:
    """`shutil.rmtree` with Windows retries.

    Antivirus and the search indexer briefly hold handles on freshly written
    files, which surfaces as PermissionError on the follow-up rmdir.
    """

    def on_error(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onerror=on_error)
            return
        except (PermissionError, OSError):
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def require_dependencies() -> None:
    if sys.version_info < (3, 11) or sys.version_info >= (3, 13):
        fail(
            f"Python {sys.version_info.major}.{sys.version_info.minor} is unsupported. "
            "usd-convert-gsplat requires >=3.11,<3.13; create a separate venv "
            "(see scripts/usd-export/README.md)."
        )
    missing = []
    for module, hint in (("pxr", "usd-core"), ("usd_convert_gsplat", "usd-convert-gsplat[usd]"), ("trimesh", "trimesh")):
        try:
            __import__(module)
        except ImportError:
            missing.append(hint)
    if missing:
        fail(
            "missing dependencies: " + ", ".join(missing) + "\n"
            "  python -m pip install -r scripts/usd-export/requirements.txt"
        )


# --------------------------------------------------------------------------
# Project discovery -- mirrors src/utils/worldsScanner.ts
# --------------------------------------------------------------------------

INDEXED_NAME = re.compile(r"^(\d+)-(.+?)(\.[^.]+)$")


@dataclass
class IndexedFile:
    path: Path
    index: int
    slug: str
    extension: str


def indexed_files(directory: Path) -> list[IndexedFile]:
    if not directory.is_dir():
        return []
    found: list[IndexedFile] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        match = INDEXED_NAME.match(entry.name)
        if not match:
            continue
        found.append(
            IndexedFile(
                path=entry,
                index=int(match.group(1)),
                slug=match.group(2),
                extension=match.group(3).lower(),
            )
        )
    return found


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@dataclass
class WorldSource:
    slug: str
    display_name: str
    version_index: int
    splat_path: Path
    splat_lod: str
    collider_path: Path | None
    flip_y: bool
    metric_scale_factor: float
    ground_plane_offset: float
    scene: dict[str, Any]


def resolve_world(slug: str, requested_lod: str) -> WorldSource:
    world_root = WORLDS_DIR / slug
    if not world_root.is_dir():
        fail(f"world '{slug}' not found under {WORLDS_DIR}")

    world_dir = world_root / "output" / "world"
    manifests = [f for f in indexed_files(world_dir) if f.slug == "world" and f.extension == ".json"]
    if not manifests:
        fail(f"no world manifest (<n>-world.json) in {world_dir}")
    latest = max(manifests, key=lambda f: f.index)
    manifest = read_json(latest.path) or {}

    splats = [
        f
        for f in indexed_files(world_dir)
        if f.index == latest.index and f.extension in SPLAT_EXTENSIONS and f.slug.startswith("world-")
    ]
    by_lod = {f.slug[len("world-") :]: f for f in splats}
    if not by_lod:
        fail(f"no background splat (<n>-world-<lod>.ply/.spz) for version {latest.index} in {world_dir}")

    lod = requested_lod
    if lod not in by_lod:
        fallback = next((candidate for candidate in reversed(LOD_CHOICES) if candidate in by_lod), None)
        if fallback is None:
            fallback = sorted(by_lod)[0]
        print(f"note: LOD '{requested_lod}' unavailable, using '{fallback}'")
        lod = fallback

    # The background collision mesh (<n>-world.glb) carries the walkable
    # geometry. It is the only form of the background that USD runtimes older
    # than 26.03 -- Isaac Sim 5.1 ships OpenUSD 24.05 -- can actually use.
    collider = next(
        (f.path for f in indexed_files(world_dir) if f.index == latest.index and f.slug == "world" and f.extension == ".glb"),
        None,
    )

    semantics = {**DEFAULT_SEMANTICS, **(manifest.get("assets", {}).get("splats", {}).get("semantics_metadata") or {})}
    project = read_json(world_root / "project.json") or {}
    scene = read_json(world_root / "scene.json") or {}

    # scene.json overrides the manifest, matching the viewer's precedence.
    base_scale = float(semantics.get("metric_scale_factor") or 1.0)
    base_offset = float(semantics.get("ground_plane_offset") or 0.0)
    scale = scene.get("metricScaleFactor")
    metric_scale_factor = float(scale) if isinstance(scale, (int, float)) else base_scale
    offset = scene.get("groundPlaneOffset")
    ground_plane_offset = (
        float(offset)
        if isinstance(offset, (int, float))
        else base_offset * (metric_scale_factor / base_scale if base_scale else 1.0)
    )

    return WorldSource(
        slug=slug,
        display_name=str(project.get("display_name") or manifest.get("display_name") or slug),
        version_index=latest.index,
        splat_path=by_lod[lod].path,
        splat_lod=lod,
        collider_path=collider,
        flip_y=bool(semantics.get("flip_y", True)),
        metric_scale_factor=metric_scale_factor,
        ground_plane_offset=ground_plane_offset,
        scene=scene,
    )


@dataclass
class Placement:
    instance_id: str
    object_id: str
    glb_path: Path
    physics: str
    position: tuple[float, float, float]
    rotation: tuple[float, float, float]  # Euler XYZ, radians (three.js default order)
    scale: tuple[float, float, float]


def _vec3(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    if not all(isinstance(part, (int, float)) and math.isfinite(part) for part in value):
        return None
    return (float(value[0]), float(value[1]), float(value[2]))


def resolve_glb(slug: str, object_id: str, asset_id: str | None) -> Path | None:
    """Resolve a placement to its GLB. `assetId` is `<slug>/<objectId>[/<index>]`."""
    source_slug, base_object_id, index = slug, object_id, None
    if asset_id:
        parts = asset_id.split("/")
        if len(parts) >= 2:
            source_slug, base_object_id = parts[0], parts[1]
        if len(parts) >= 3 and parts[2].isdigit():
            index = int(parts[2])

    object_dir = WORLDS_DIR / source_slug / "output" / base_object_id
    models = [f for f in indexed_files(object_dir) if f.extension == ".glb"]
    if not models:
        return None
    if index is not None:
        match = next((f for f in models if f.index == index), None)
        if match:
            return match.path
    return max(models, key=lambda f: f.index).path


def resolve_placements(world: WorldSource) -> list[Placement]:
    instances = world.scene.get("instances")
    if not isinstance(instances, list):
        return []

    placements: list[Placement] = []
    for raw in instances:
        if not isinstance(raw, dict):
            continue
        instance_id, object_id = raw.get("instanceId"), raw.get("objectId")
        if not isinstance(instance_id, str) or not isinstance(object_id, str):
            continue
        position, rotation, scale = _vec3(raw.get("position")), _vec3(raw.get("rotation")), _vec3(raw.get("scale"))
        if position is None or rotation is None or scale is None:
            continue
        asset_id = raw.get("assetId") if isinstance(raw.get("assetId"), str) else None
        glb_path = resolve_glb(world.slug, object_id, asset_id)
        if glb_path is None:
            print(f"warning: no GLB for instance '{instance_id}' (object '{object_id}'), skipping")
            continue
        physics = raw.get("physics")
        placements.append(
            Placement(
                instance_id=instance_id,
                object_id=object_id,
                glb_path=glb_path,
                physics=physics if physics in ("rigidbody", "static", "ghost") else "rigidbody",
                position=position,
                rotation=rotation,
                scale=scale,
            )
        )
    return placements


# --------------------------------------------------------------------------
# USD authoring
# --------------------------------------------------------------------------


def usd_name(value: str) -> str:
    from pxr import Tf

    return Tf.MakeValidIdentifier(value)


SPLAT_PRIM_NAME = "Splat"


def write_splat_layer(world: WorldSource, out_path: Path) -> None:
    from usd_convert_gsplat import convertPlyUSD, read_spz, write_gaussian_splat_usd

    if world.splat_path.suffix.lower() == ".spz":
        write_gaussian_splat_usd(
            read_spz(str(world.splat_path)),
            str(out_path),
            source_file=str(world.splat_path),
            prim_name=SPLAT_PRIM_NAME,
            up_axis="Y",
        )
    else:
        # SHARP writes DC-only splats (f_dc_* with no f_rest_*), so degree-0 SH
        # is generated from RGB. generateScales stays off: scale_0..2 are present.
        convertPlyUSD(
            str(world.splat_path),
            str(out_path),
            SPLAT_PRIM_NAME,
            generateSh=True,
            generateScales=False,
            up_axis="Y",
        )


def write_mesh_layer(glb_path: Path, out_path: Path, textures_dir: Path) -> bool:
    """Convert a GLB to a USD layer with a single Xform root named `Object`."""
    import numpy as np
    import trimesh
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

    loaded = trimesh.load(glb_path, process=False)
    if isinstance(loaded, trimesh.Trimesh):
        geometries = [("mesh", loaded, np.eye(4))]
    elif isinstance(loaded, trimesh.Scene):
        geometries = []
        for node_name in loaded.graph.nodes_geometry:
            transform, geometry_name = loaded.graph[node_name]
            geometry = loaded.geometry.get(geometry_name)
            if isinstance(geometry, trimesh.Trimesh):
                geometries.append((geometry_name, geometry, transform))
    else:
        print(f"warning: unsupported geometry in {glb_path.name}, skipping")
        return False

    if not geometries:
        print(f"warning: no meshes in {glb_path.name}, skipping")
        return False

    stage = Usd.Stage.CreateNew(str(out_path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, "/Object")
    stage.SetDefaultPrim(root.GetPrim())
    looks_path = "/Object/Looks"

    for order, (name, geometry, transform) in enumerate(geometries):
        mesh_path = f"/Object/{usd_name(name) or f'mesh_{order}'}"
        mesh = UsdGeom.Mesh.Define(stage, mesh_path)

        # glTF node transforms are baked in: the placement transform from
        # scene.json is authored separately on the referencing prim.
        vertices = np.asarray(geometry.vertices, dtype=np.float64)
        if not np.allclose(transform, np.eye(4)):
            vertices = trimesh.transform_points(vertices, transform)
        faces = np.asarray(geometry.faces, dtype=np.int32)

        mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(vertices, dtype=np.float32)))
        mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(faces.reshape(-1)))
        mesh.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(len(faces), 3, dtype=np.int32)))
        mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        lo, hi = vertices.min(axis=0), vertices.max(axis=0)
        mesh.CreateExtentAttr([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))])

        try:
            # trimesh computes these lazily and needs scipy; a mesh without
            # normals still renders, so degrade instead of failing the export.
            vertex_normals = geometry.vertex_normals
        except Exception as error:  # noqa: BLE001 - trimesh raises many types here
            print(f"warning: no vertex normals for {glb_path.name} ({error})")
            vertex_normals = None

        if vertex_normals is not None and len(vertex_normals) == len(vertices):
            normals = np.asarray(vertex_normals, dtype=np.float32)
            if not np.allclose(transform, np.eye(4)):
                rotation = np.asarray(transform)[:3, :3]
                normals = normals @ rotation.T
                lengths = np.linalg.norm(normals, axis=1, keepdims=True)
                normals = np.divide(normals, lengths, out=normals, where=lengths > 0)
            mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(normals, dtype=np.float32)))
            mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)

        visual = getattr(geometry, "visual", None)
        uv = getattr(visual, "uv", None)
        material_prim = getattr(visual, "material", None)
        texture_image = None
        if material_prim is not None:
            texture_image = getattr(material_prim, "baseColorTexture", None) or getattr(material_prim, "image", None)

        if uv is not None and len(uv) == len(vertices) and texture_image is not None:
            textures_dir.mkdir(parents=True, exist_ok=True)
            texture_name = f"{out_path.stem}_{usd_name(name) or order}.png"
            texture_path = textures_dir / texture_name
            texture_image.convert("RGB").save(texture_path)

            # glTF UVs are top-left origin; USD `st` is bottom-left.
            st = np.asarray(uv, dtype=np.float32).copy()
            st[:, 1] = 1.0 - st[:, 1]
            primvar = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
                "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex
            )
            primvar.Set(Vt.Vec2fArray.FromNumpy(np.ascontiguousarray(st, dtype=np.float32)))

            UsdGeom.Scope.Define(stage, looks_path)  # idempotent
            material_path = f"{looks_path}/{mesh_path.rsplit('/', 1)[-1]}_mat"
            material = UsdShade.Material.Define(stage, material_path)
            shader = UsdShade.Shader.Define(stage, f"{material_path}/Surface")
            shader.CreateIdAttr("UsdPreviewSurface")
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.9)
            shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)

            reader = UsdShade.Shader.Define(stage, f"{material_path}/stReader")
            reader.CreateIdAttr("UsdPrimvarReader_float2")
            reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")

            sampler = UsdShade.Shader.Define(stage, f"{material_path}/diffuseTexture")
            sampler.CreateIdAttr("UsdUVTexture")
            sampler.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(f"./textures/{texture_name}")
            sampler.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
                reader.ConnectableAPI(), "result"
            )
            sampler.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
                sampler.ConnectableAPI(), "rgb"
            )
            material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
            UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
        else:
            colors = getattr(visual, "vertex_colors", None)
            if colors is not None and len(colors) == len(vertices):
                rgb = np.ascontiguousarray(np.asarray(colors, dtype=np.float32)[:, :3] / 255.0, dtype=np.float32)
                mesh.CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex).Set(Vt.Vec3fArray.FromNumpy(rgb))

    stage.GetRootLayer().Save()
    return True


def apply_physics(prim: Any, physics: str) -> None:
    from pxr import UsdPhysics

    if physics == "ghost":
        return
    UsdPhysics.CollisionAPI.Apply(prim)
    if physics == "rigidbody":
        UsdPhysics.RigidBodyAPI.Apply(prim)
        UsdPhysics.MassAPI.Apply(prim)


def sun_direction(rotation: tuple[float, float, float]) -> tuple[float, float, float]:
    """Mirror sunPositionFromRotation() in src/components/WorldViewer.tsx."""
    x, y, z = 0.0, 10.0, 0.0
    rx, ry, rz = rotation
    y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
    x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
    x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
    return (x, y, z)


def build_stage(
    world: WorldSource,
    placements: list[Placement],
    stage_dir: Path,
    include_physics: bool,
    include_collider: bool,
    include_splat: bool,
    up_axis: str,
) -> Path:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux

    main_path = stage_dir / "world.usda"
    stage = Usd.Stage.CreateNew(str(main_path))
    z_up = up_axis == "z"
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z if z_up else UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)

    root = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(root.GetPrim())
    root.GetPrim().SetMetadata("comment", f"ImageWorld export of '{world.display_name}' (v{world.version_index})")
    if z_up:
        # Everything below is authored in the viewer's Y-up frame; rotate the
        # whole world so +Y becomes +Z for Z-up consumers such as Isaac Sim,
        # whose physics defaults assume gravity along -Z.
        root.AddRotateXOp().Set(90.0)

    # --- background -------------------------------------------------------
    background = UsdGeom.Xform.Define(stage, "/World/Background")
    # Matches <group position={[0, offset, 0]} rotation={[flipY ? PI : 0, 0, 0]}
    # scale={metricScaleFactor}> in src/modules/splat/SplatRenderer.tsx.
    background.AddTranslateOp().Set(Gf.Vec3d(0.0, world.ground_plane_offset, 0.0))
    background.AddRotateXOp().Set(180.0 if world.flip_y else 0.0)
    background.AddScaleOp().Set(Gf.Vec3f(*([world.metric_scale_factor] * 3)))

    if include_splat:
        splat_layer = stage_dir / "background.usdc"
        print(f"  splat: {world.splat_path.name} ({world.splat_lod})")
        write_splat_layer(world, splat_layer)
        # Reference onto a child prim, not the Xform itself: a reference composes
        # into the prim it is authored on, and a local `Xform` type declaration
        # would win over the referenced ParticleField3DGaussianSplat type.
        # usd-convert-gsplat authors no defaultPrim, so name the source prim path.
        splat = stage.DefinePrim(f"/World/Background/{SPLAT_PRIM_NAME}")
        splat.GetReferences().AddReference("./background.usdc", f"/{SPLAT_PRIM_NAME}")

    # The collision mesh shares the background transform -- see WorldCollider.tsx,
    # which applies the same rotation/offset/scale as SplatRenderer.tsx.
    if include_collider and world.collider_path is not None:
        print(f"  scene: {world.collider_path.name} (collision mesh)")
        collider_layer = stage_dir / "background_collider.usdc"
        if write_mesh_layer(world.collider_path, collider_layer, stage_dir / "textures"):
            collider = stage.DefinePrim("/World/Background/Collider")
            collider.GetReferences().AddReference("./background_collider.usdc", "/Object")
            if include_physics:
                from pxr import UsdPhysics

                UsdPhysics.CollisionAPI.Apply(collider)

    # --- foreground objects ----------------------------------------------
    if placements:
        UsdGeom.Scope.Define(stage, "/World/Objects")
    meshes_dir = stage_dir / "meshes"
    textures_dir = stage_dir / "textures"
    converted: dict[Path, str | None] = {}

    for placement in placements:
        if placement.glb_path not in converted:
            meshes_dir.mkdir(parents=True, exist_ok=True)
            layer_name = f"{usd_name(placement.object_id)}_{placement.glb_path.stem}.usdc"
            layer_path = meshes_dir / layer_name
            print(f"  mesh:  {placement.glb_path.name}")
            ok = write_mesh_layer(placement.glb_path, layer_path, textures_dir)
            converted[placement.glb_path] = f"./meshes/{layer_name}" if ok else None

        reference = converted[placement.glb_path]
        if reference is None:
            continue

        instance = UsdGeom.Xform.Define(stage, f"/World/Objects/{usd_name(placement.instance_id)}")
        instance.AddTranslateOp().Set(Gf.Vec3d(*placement.position))
        # three.js Euler default order is XYZ, applied as R = Rz * Ry * Rx.
        instance.AddRotateXYZOp().Set(Gf.Vec3f(*[math.degrees(angle) for angle in placement.rotation]))
        instance.AddScaleOp().Set(Gf.Vec3f(*placement.scale))
        instance.GetPrim().GetReferences().AddReference(reference)
        instance.GetPrim().CreateAttribute("imageworld:objectId", Sdf.ValueTypeNames.String).Set(placement.object_id)
        instance.GetPrim().CreateAttribute("imageworld:physics", Sdf.ValueTypeNames.String).Set(placement.physics)
        if include_physics:
            apply_physics(instance.GetPrim(), placement.physics)

    # --- ground plane -----------------------------------------------------
    if include_physics and world.scene.get("groundPlaneColliderEnabled", True):
        from pxr import UsdPhysics

        ground = UsdGeom.Plane.Define(stage, "/World/GroundPlane")
        ground.CreateAxisAttr(UsdGeom.Tokens.y)
        ground.CreatePurposeAttr(UsdGeom.Tokens.guide)
        ground_xform = UsdGeom.Xformable(ground.GetPrim())
        ground_xform.AddTranslateOp().Set(Gf.Vec3d(0.0, world.ground_plane_offset, 0.0))
        UsdPhysics.CollisionAPI.Apply(ground.GetPrim())

    # --- sun --------------------------------------------------------------
    sun_config = world.scene.get("sun")
    if isinstance(sun_config, dict):
        rotation = _vec3(sun_config.get("rotation")) or (0.0, 0.0, 0.0)
        intensity = sun_config.get("intensity")
        light = UsdLux.DistantLight.Define(stage, "/World/Sun")
        light.CreateIntensityAttr(float(intensity) if isinstance(intensity, (int, float)) else 1.0)
        light.CreateAngleAttr(0.53)
        # A UsdLux distant light shines along its local -Z. The viewer places a
        # three.js directional light at `sunPositionFromRotation()` aimed at the
        # origin, so the light travels along -normalize(position).
        position = Gf.Vec3d(*sun_direction(rotation))
        if position.GetLength() > 1e-6:
            travel = -position.GetNormalized()
            orient = Gf.Rotation(Gf.Vec3d(0, 0, -1), travel).GetQuat()
            UsdGeom.Xformable(light.GetPrim()).AddOrientOp().Set(
                Gf.Quatf(float(orient.GetReal()), Gf.Vec3f(*orient.GetImaginary()))
            )

    # A scene whose floor is far from the origin is legal but awkward to
    # simulate -- a robot spawned at the origin starts inside or above the
    # geometry. It usually means scene.json overrode the calibrated
    # groundPlaneOffset from the world manifest.
    collider_prim = stage.GetPrimAtPath("/World/Background/Collider")
    if collider_prim.IsValid():
        bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
        floor = bounds.ComputeWorldBound(collider_prim).GetRange().GetMin()[2 if z_up else 1]
        if abs(floor) > 0.25:
            print(
                f"warning: scene floor sits at {floor:.2f} m, not 0 - check groundPlaneOffset "
                "in scene.json (the world manifest may hold a calibrated value)"
            )

    stage.GetRootLayer().Save()
    return main_path


def package_usdz(main_path: Path, usdz_path: Path) -> None:
    from pxr import UsdUtils

    if usdz_path.exists():
        usdz_path.unlink()
    if not UsdUtils.CreateNewUsdzPackage(main_path.as_posix(), usdz_path.as_posix()):
        fail(f"failed to package {usdz_path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export an ImageWorld project to OpenUSD.")
    parser.add_argument("slug", help="World slug under public/worlds/")
    parser.add_argument("--lod", default="500k", choices=LOD_CHOICES, help="Background splat LOD (default: 500k)")
    parser.add_argument(
        "--out",
        type=lambda value: Path(value).expanduser().resolve(),
        help="Output path (default: public/worlds/<slug>/export/<slug>.usdz)",
    )
    parser.add_argument("--format", default="usdz", choices=("usdz", "usda"), help="Output format (default: usdz)")
    parser.add_argument(
        "--up-axis",
        default="y",
        choices=("y", "z"),
        help="Stage up-axis (default: y, matching the viewer). Use z for Isaac Sim / PhysX.",
    )
    parser.add_argument("--no-objects", action="store_true", help="Skip foreground objects")
    parser.add_argument("--no-physics", action="store_true", help="Skip UsdPhysics rigid-body/collision APIs")
    parser.add_argument(
        "--no-collider",
        action="store_true",
        help="Skip the background collision mesh (<n>-world.glb)",
    )
    parser.add_argument(
        "--no-splat",
        action="store_true",
        help="Skip the Gaussian splat. Use for OpenUSD <26.03 runtimes (Isaac Sim 5.1 "
        "ships 24.05) that cannot render it anyway -- cuts most of the file size.",
    )
    parser.add_argument("--keep-stage", action="store_true", help="Keep the intermediate .usda stage directory")
    args = parser.parse_args()

    require_dependencies()

    world = resolve_world(args.slug, args.lod)
    placements = [] if args.no_objects else resolve_placements(world)
    print(f"Exporting '{world.display_name}' (v{world.version_index}) with {len(placements)} object(s)")
    if not args.no_collider and world.collider_path is None:
        print("note: no background collision mesh (<n>-world.glb); the background will be splat-only")

    export_dir = WORLDS_DIR / world.slug / "export"
    stage_dir = export_dir / f"_stage_{world.slug}"
    if stage_dir.exists():
        remove_tree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    main_path = build_stage(
        world,
        placements,
        stage_dir,
        include_physics=not args.no_physics,
        include_collider=not args.no_collider,
        include_splat=not args.no_splat,
        up_axis=args.up_axis,
    )

    if args.format == "usdz":
        out_path = args.out or export_dir / f"{world.slug}.usdz"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        package_usdz(main_path, out_path)
        if not args.keep_stage:
            remove_tree(stage_dir)
        total_bytes = out_path.stat().st_size
    else:
        out_dir = args.out or export_dir / world.slug
        if out_dir.suffix:
            out_dir = out_dir.with_suffix("")
        if out_dir.exists():
            remove_tree(out_dir)
        shutil.move(str(stage_dir), str(out_dir))
        out_path = out_dir / main_path.name
        # The root layer only holds references; report the whole bundle.
        total_bytes = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())

    try:
        display_path = out_path.resolve().relative_to(REPO_ROOT)
    except ValueError:  # --out pointed outside the repo
        display_path = out_path.resolve()
    print(f"Wrote {display_path} ({total_bytes / (1024 * 1024):.1f} MB)")


if __name__ == "__main__":
    main()
