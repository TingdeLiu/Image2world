# USD export

Exports a generated world to an [OpenUSD](https://openusd.org) bundle so it can
be opened outside the browser viewer.

**Where this actually renders today.** `ParticleField3DGaussianSplat` landed in
OpenUSD 26.03 and tooling is still catching up, so check your target before
relying on it:

| Target | Splat | Meshes / lights / physics |
| --- | --- | --- |
| **Isaac Sim 5.1** | **no** — ships OpenUSD 24.05, verified: `UsdVol.ParticleField3DGaussianSplat` does not exist and no gsplat extension is bundled | yes |
| Omniverse RTX (Kit 108+ / OpenUSD 26.03+) | yes | yes |
| Houdini Solaris (via [houdini-gsplat](https://github.com/plattipus/houdini-gsplat)) | yes | yes |
| usdview | only with the `hdParticleField` reference renderer, which is not in the `usd-core` pip wheel | yes |
| Blender | no — native splat I/O is still an open design task ([#159470](https://projects.blender.org/blender/blender/issues/159470)) | yes |
| Apple Quick Look | no | yes |

A stage containing the splat still **opens without error** on OpenUSD 24.05 —
the prim keeps its type name, is treated as unknown, and is skipped. Verified by
opening an export with Isaac Sim 5.1's own USD build.

For simulation targets, the useful part of the background is the collision mesh
(`<n>-world.glb`), which every fully generated world carries. It is exported by
default under the same transform as the splat, so a `--no-splat` export is a
complete, walkable scene at a fraction of the size.

The background Gaussian splat is written as a `ParticleField3DGaussianSplat`
prim, OpenUSD's geometry type for radiance fields, using NVIDIA's
[`usd-convert-gsplat`](https://github.com/NVIDIA-Omniverse/usd-convert-gsplat)
(Apache-2.0). Foreground objects are converted from GLB to `UsdGeomMesh` with
`UsdPreviewSurface` materials and placed using the transforms in `scene.json`.

## Setup

`usd-convert-gsplat` requires **Python >=3.11,<3.13**, so it cannot be installed
into the `image2world` conda environment (Python 3.10). Create a separate venv:

```bash
python -m venv scripts/usd-export/.venv
```

Then install the dependencies:

```bash
scripts/usd-export/.venv/Scripts/python -m pip install -r scripts/usd-export/requirements.txt
```

On macOS/Linux the interpreter is at `scripts/usd-export/.venv/bin/python`.

## Usage

```bash
npm run export:usd -- home-room
```

The npm script prefers `scripts/usd-export/.venv`, then `$IMAGEWORLD_USD_PYTHON`,
then whatever `python` is on `PATH`. To call it directly:

```bash
scripts/usd-export/.venv/Scripts/python scripts/usd-export/export_world_usd.py home-room
```

Output lands in `public/worlds/<slug>/export/<slug>.usdz`.

### For Isaac Sim

Skip the splat it cannot render and switch to Z-up, which is what PhysX and the
Isaac Sim asset conventions assume (gravity along -Z). The collision mesh,
objects, and physics are what a simulator needs, and the file gets roughly 2x
smaller:

```bash
npm run export:usd -- home-room --no-splat --up-axis z
```

The exporter warns if the scene floor does not land near the origin. That
usually means `scene.json` overrode the calibrated `groundPlaneOffset` from the
world manifest — worth fixing before spawning a robot at the origin.

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--lod` | `500k` | Background splat LOD: `100k`, `150k`, `500k`, `full_res` |
| `--format` | `usdz` | `usdz` (single file) or `usda` (readable directory bundle) |
| `--up-axis` | `y` | Stage up-axis. `z` for Isaac Sim / PhysX |
| `--out` | — | Override the output path |
| `--no-objects` | off | Skip foreground objects |
| `--no-physics` | off | Skip `UsdPhysics` rigid-body and collision APIs |
| `--no-collider` | off | Skip the background collision mesh |
| `--no-splat` | off | Skip the Gaussian splat — use for OpenUSD <26.03 runtimes |
| `--keep-stage` | off | Keep the intermediate stage directory next to the `.usdz` |

Use `--format usda` when inspecting the result: the bundle stays a directory of
readable text plus the referenced layers and textures.

## Output structure

```text
/World                              # Xform, Y-up, metersPerUnit = 1
├── /World/Background               # translate(groundPlaneOffset) · rotateX(180 if flip_y) · scale(metricScaleFactor)
│   ├── /World/Background/Splat     # ParticleField3DGaussianSplat (visual)
│   └── /World/Background/Collider  # UsdGeomMesh from <n>-world.glb + CollisionAPI
├── /World/Objects
│   └── /World/Objects/<instanceId> # translate · rotateXYZ · scale from scene.json, + UsdPhysics APIs
│       └── ...                     # UsdGeomMesh referenced from meshes/<object>.usdc
├── /World/GroundPlane              # collision-only, follows groundPlaneOffset
└── /World/Sun                      # DistantLight aimed to match the viewer's sun
```

Each instance also carries `imageworld:objectId` and `imageworld:physics`
attributes so the placement can be traced back to `scene.json`.

The background transform mirrors `src/modules/splat/SplatRenderer.tsx`, and the
sun direction mirrors `sunPositionFromRotation()` in
`src/components/WorldViewer.tsx`. If either changes, update this exporter.

## Tests

```bash
npm run test:usd
```

Two halves. Half assert the TypeScript runtime still says what the exporter
assumes — the background transform in `SplatRenderer.tsx`, the collider
transform in `WorldCollider.tsx`, `sunPositionFromRotation()`, and the
`scene.json` precedence rules in `WorldViewer.tsx`. Half export the `home-room`
fixture and assert the resulting stage: the background matrix really is
`scale · rotateX(180) · translate`, the collider carries `CollisionAPI`, the
floor lands at the origin, and `--up-axis z` moves height onto Z.

The first half is deliberately literal about source formatting. If a
TypeScript change breaks it, that is the point: read the failure, decide whether
the exporter needs the same change, then update the pattern.

## Known limitations

- `ParticleField3DGaussianSplat` requires OpenUSD 26.03+ *and* a renderer that
  implements it — see the support table above. This is the main caveat.
- The export is one-way. There is no USD importer back into the viewer.
- The background transform, sun direction, and placement semantics are
  duplicated from the TypeScript runtime. `npm run test:usd` guards the
  duplication, but the guard is textual — a semantically equivalent rewrite of
  the TypeScript will fail it, and a change it does not pattern-match could
  still slip through.
- SHARP splats are DC-only (no `f_rest_*`), so degree-0 spherical harmonics are
  generated from RGB. The result is view-independent color — matching what the
  browser viewer shows.
- Physics is authored as collision plus rigid-body APIs with default mass. The
  browser runtime uses box colliders via Rapier; a USD consumer will derive its
  own approximation from the mesh.
- `full_res` splats produce large files (a 1.2M-splat scene is roughly 80 MB of
  USD). `500k` is the default for that reason.
- Only `baseColorTexture` is exported. TripoSR does not emit metallic/roughness
  maps, so those are set to constants.
