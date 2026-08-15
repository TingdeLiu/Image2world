export interface WorldAssets {
  mesh: { collider_mesh_url: string }
  imagery: { pano_url: string }
  splats: {
    spz_urls: {
      '500k'?: string
      '100k'?: string
      '150k'?: string
      full_res?: string
    }
    semantics_metadata: {
      metric_scale_factor: number
      ground_plane_offset: number
      flip_y?: boolean
      rotation_x?: number
    }
  }
  thumbnail_url: string
  caption: string
  /** Absent for worlds with nothing carved out of them yet. */
  props?: WorldProps
  /** Absent for worlds whose collider predates walkability analysis. */
  walkable?: WorldWalkable
}

/**
 * A piece of the room cut loose so it can be pushed around.
 *
 * Unlike the objects in `WorldObjectAsset`, a prop's geometry is the room's own
 * reconstruction rather than a single-view guess at what the object might look
 * like — it is carved out of the same gaussians, by a SAM mask or a box the
 * user dragged. `id` matches its node name in `<n>-world-props.glb`.
 */
export interface WorldProp {
  id: string
  label?: string
  /** Kilograms, derived from hull volume — plausible, not measured. */
  mass: number
  /** Splat space, like the collider GLB: the viewer applies the world transform. */
  centre: Vec3Tuple
}

export interface WorldProps {
  version: 1
  url: string
  props: WorldProp[]
}

/**
 * Where a person could actually stand, as a grid over the floor plan.
 *
 * The mask is a greyscale PNG (white = walkable) covering `cols` x `rows`
 * cells of `cellSize` metres, with cell [0,0] at `origin` in viewer-space
 * (x, z) — before the world's metric scale is applied.
 *
 * `standableArea` counts every surface flat enough and clear enough to stand
 * on; `walkableArea` counts only the part connected to where the player
 * starts. The gap between them is the reconstruction's stranded geometry.
 */
export interface WorldWalkable {
  url: string
  cellSize: number
  cols: number
  rows: number
  origin: [number, number]
  standableArea: number
  walkableArea: number
  /**
   * The world's metric scale at the moment the grid was measured.
   *
   * The grid is in metres. A player position is in metres too, but only while
   * the world's scale is what it was then — rescale the world in the editor and
   * the two drift apart by the ratio. Maps written before this field existed
   * were measured in unscaled splat units, which is the same thing as metres at
   * a scale of 1, so that is the fallback.
   */
  metricScaleFactor?: number
}

export interface World {
  world_id: string
  display_name: string
  assets: WorldAssets
  world_marble_url: string
  tags: string[] | null
  world_prompt: string | null
  created_at: string | null
  updated_at: string | null
}

export interface WorldProject {
  slug: string
  display_name?: string
  created_at?: string
  updated_at?: string
  notes?: string
}

export interface WorldObjectAsset {
  id: string
  assetId: string
  sourceWorldSlug: string
  baseObjectId: string
  index?: number
  variantLabel?: string
  fileName?: string
  name: string
  url: string
  referenceImageUrl?: string
  thumbnailUrl?: string
  sfxUrls: string[]
  complete: boolean
  status?: string
}

export type Vec3Tuple = [number, number, number]
export type WorldObjectPhysics = 'rigidbody' | 'static' | 'ghost'

export interface WorldObjectPlacement {
  instanceId: string
  objectId: string
  assetId?: string
  physics?: WorldObjectPhysics
  position: Vec3Tuple
  rotation: Vec3Tuple
  scale: Vec3Tuple
}

export interface WorldSceneSun {
  intensity: number
  rotation: Vec3Tuple
  environmentIntensity?: number
}

export interface WorldSceneProject {
  version: 1
  instances: WorldObjectPlacement[]
  sun?: WorldSceneSun
  metricScaleFactor?: number
  groundPlaneOffset?: number
  groundPlaneColliderEnabled?: boolean
  shadowCatcherOpacity?: number
  shadowCatcherColor?: string
  /**
   * Where the player starts, as [x, z] on the floor. Reconstruction puts the
   * camera where the photo was taken, which is usually outside the room, so
   * generated worlds record a point inside it instead. Absent on older worlds,
   * which fall back to the origin.
   */
  spawnPoint?: [number, number]
  /** Exact initial camera pose for reconstructed video worlds. */
  spawnPosition?: Vec3Tuple
  /** Initial camera orientation in Three.js YXZ Euler radians. */
  spawnYaw?: number
  spawnPitch?: number
  defaultController?: 'fly' | 'fps'
}

export interface WorldVersion {
  index: number
  label: string
  world?: World
  plateImageUrl?: string
  complete: boolean
  status?: string
}

export interface SourceImageVersion {
  url: string
  label: string
  fileName: string
  index?: number
}

export interface WorldHoverPreview {
  slug: string
  imageUrl?: string
  alt: string
}

export interface WorldEntry {
  slug: string
  project: WorldProject
  world?: World
  worldVersions: WorldVersion[]
  objectAssets: WorldObjectAsset[]
  allObjectAssets: WorldObjectAsset[]
  sourceImageUrl?: string
  sourceImageVersions: SourceImageVersion[]
  worldSfxUrls: string[]
  sceneProject?: WorldSceneProject
}

export enum WorldRenderMode {
  SplatOnly = 'splat-only',
  ObjectOnly = 'object-only',
  Combined = 'combined',
}

export enum ObjectRenderMode {
  Lit = 'lit',
  Wireframe = 'wireframe',
  ShadedWireframe = 'shaded-wireframe',
}

export enum ViewerQuality {
  Low = 'low',
  High = 'high',
}
