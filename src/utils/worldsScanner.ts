import fs from 'fs'
import path from 'path'
import { spawn } from 'child_process'
import type {
  World,
  WorldEntry,
  WorldProject,
  WorldObjectAsset,
  WorldSceneProject,
  WorldVersion,
  SourceImageVersion,
  WorldObjectPlacement,
  WorldSceneSun
} from '../types/world'

type WorldManifest = Record<string, unknown> & {
  assets?: Record<string, unknown> & {
    imagery?: Record<string, unknown>
    mesh?: Record<string, unknown>
    splats?: Record<string, unknown> & {
      spz_urls?: Record<string, string | undefined>
    }
  }
}

type ProjectManifest = Record<string, unknown> & {
  slug?: string
  display_name?: string
  created_at?: string
  updated_at?: string
  notes?: string
}

type RequestManifest = Record<string, unknown> & {
  status?: string
  input_files?: unknown
  request?: Record<string, unknown> & {
    world_prompt?: Record<string, unknown> & {
      image_prompt?: Record<string, unknown> & {
        uri?: unknown
      }
    }
  }
}

type FileWithName = { name: string }

export interface IndexedName {
  index: number
  slug: string
  scope?: string
  extension: string
  hidden?: boolean
  name: string
}

export interface IndexedArtifact<T extends FileWithName = FileWithName> {
  file: T
  name: string
  slug: string
  extension: string
  indexed?: IndexedName
  index?: number
}

interface IndexedFileOptions {
  extensions?: ReadonlySet<string>
  slugs?: ReadonlySet<string>
}

const worldsDir = path.join(process.cwd(), 'public', 'worlds')
const repoRoot = process.cwd()
const RESERVED_OUTPUT_DIRS = new Set(['world', 'sfx'])
const MODEL_EXTENSIONS = new Set(['.glb'])
const AUDIO_EXTENSIONS = new Set(['.mp3', '.ogg', '.wav', '.m4a', '.opus'])
const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.avif'])
const PROJECT_VERSION = 1
const WORLD_SPZ_KEYS = new Set(['100k', '150k', '500k', 'full_res'])

function visibleFiles(dir: string): fs.Dirent[] {
  if (!fs.existsSync(dir)) return []
  return fs.readdirSync(dir, { withFileTypes: true })
    .filter((file) => file.isFile() && !file.name.startsWith('.'))
    .sort((a, b) => a.name.localeCompare(b.name))
}

export function parseIndexedName(fileName: string): IndexedName | undefined {
  const requestMatch = fileName.match(/^\.(\d+)-(.+?)(?:__([a-z0-9._-]+))?-request\.json$/i)
  if (requestMatch) {
    return {
      index: Number(requestMatch[1]),
      slug: requestMatch[2],
      ...(requestMatch[3] ? { scope: requestMatch[3] } : {}),
      extension: '.json',
      hidden: true,
      name: fileName,
    }
  }

  const match = fileName.match(/^(\d+)-(.+?)(\.[^.]+)$/)
  if (!match) return undefined
  return {
    index: Number(match[1]),
    slug: match[2],
    extension: match[3].toLowerCase(),
    name: fileName,
  }
}

export function indexedFiles<T extends FileWithName>(
  files: T[],
  options: IndexedFileOptions = {},
): Array<IndexedArtifact<T>> {
  return files
    .map((file) => {
      const indexed = parseIndexedName(file.name)
      const extension = path.extname(file.name).toLowerCase()
      return {
        file,
        name: file.name,
        slug: indexed?.slug ?? path.basename(file.name, extension),
        extension,
        ...(indexed ? { indexed, index: indexed.index } : {}),
      }
    })
    .filter((entry) => !options.extensions || options.extensions.has(entry.extension))
    .filter((entry) => !options.slugs || options.slugs.has(entry.slug))
    .sort((a, b) => {
      const aIndex = a.index ?? Number.MAX_SAFE_INTEGER
      const bIndex = b.index ?? Number.MAX_SAFE_INTEGER
      return aIndex - bIndex || a.name.localeCompare(b.name)
    })
}

export function versionLabel(file: IndexedArtifact) {
  return file.index === undefined ? path.basename(file.name, file.extension) : `v${file.index}`
}

export function firstIndexed<T extends IndexedArtifact>(files: T[]) {
  return files[0]
}

export function latestIndexed<T extends IndexedArtifact>(files: T[]) {
  return [...files].sort((a, b) => {
    const aIndex = a.index ?? Number.MIN_SAFE_INTEGER
    const bIndex = b.index ?? Number.MIN_SAFE_INTEGER
    return bIndex - aIndex || b.name.localeCompare(a.name)
  })[0]
}

export function byIndex<T extends IndexedArtifact>(files: T[], index: number) {
  return files.find((file) => file.index === index)
}

export function worldsUrl(slug: string, relativePath: string) {
  return `/worlds/${slug}/${relativePath.split(path.sep).join('/')}`
}

function readSourceImageVersions(slug: string): SourceImageVersion[] {
  const sourceDir = path.join(worldsDir, slug, 'source')
  return indexedFiles(visibleFiles(sourceDir), { extensions: IMAGE_EXTENSIONS })
    .map((image) => ({
      url: worldsUrl(slug, path.join('source', image.name)),
      label: versionLabel(image),
      fileName: image.name,
      ...(image.index === undefined ? {} : { index: image.index }),
    }))
}

function readSourceImageUrl(slug: string): string | undefined {
  const versions = readSourceImageVersions(slug)
  return versions.find((version) => version.index === 0)?.url ?? versions[0]?.url
}

function statusText(value: unknown) {
  return String(value || '').toLowerCase()
}

function requestMetadataFiles(dir: string, slug: string, scope?: string) {
  if (!fs.existsSync(dir)) return []

  return fs.readdirSync(dir, { withFileTypes: true })
    .flatMap((entry) => {
      if (!entry.isFile()) return []
      const parsed = parseIndexedName(entry.name)
      if (!parsed?.hidden || parsed.slug !== slug || parsed.scope !== scope) return []

      try {
        return [{
          ...parsed,
          data: JSON.parse(fs.readFileSync(path.join(dir, entry.name), 'utf-8')) as RequestManifest,
        }]
      } catch {
        return []
      }
    })
    .sort((a, b) => a.index - b.index)
}

function readObjectAssets(slug: string): WorldObjectAsset[] {
  const outputDir = path.join(worldsDir, slug, 'output')
  if (!fs.existsSync(outputDir)) return []

  return fs.readdirSync(outputDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !RESERVED_OUTPUT_DIRS.has(entry.name))
    .flatMap((entry) => {
      const objectDir = path.join(outputDir, entry.name)
      const files = visibleFiles(objectDir)
      const models = indexedFiles(files, { extensions: MODEL_EXTENSIONS })
      const images = indexedFiles(files, { extensions: IMAGE_EXTENSIONS })
      const objectJsonPath = path.join(objectDir, 'object.json')
      let displayName = entry.name
      if (fs.existsSync(objectJsonPath) && fs.statSync(objectJsonPath).isFile()) {
        try {
          const json = JSON.parse(fs.readFileSync(objectJsonPath, 'utf-8'))
          displayName = json.object?.name ?? json.name ?? displayName
        } catch {
          displayName = entry.name
        }
      }

      const thumbnailFor = (index?: number) => {
        const sameIndexImages = images.filter((image) => index === undefined || image.index === index)
        return sameIndexImages.find((image) => image.name.includes('thumbnail')) ?? firstIndexed(sameIndexImages)
      }

      const referenceImageFor = (model: IndexedArtifact) => {
        const sameIndexImages = images.filter((image) => model.index === undefined || image.index === model.index)
        return sameIndexImages.find((image) => image.slug === model.slug)
          ?? sameIndexImages.find((image) => !image.name.includes('thumbnail'))
          ?? firstIndexed(sameIndexImages)
      }

      const imageRequests = requestMetadataFiles(objectDir, entry.name, 'image')
      const modelRequests = requestMetadataFiles(objectDir, entry.name, 'model')
      const indexes = new Set<number>()
      for (const model of models) if (model.index !== undefined) indexes.add(model.index)
      for (const request of imageRequests) indexes.add(request.index)
      for (const request of modelRequests) indexes.add(request.index)

      if (!indexes.size && models.some((model) => model.index === undefined)) {
        indexes.add(Number.MAX_SAFE_INTEGER)
      }

      return [...indexes].sort((a, b) => a - b).flatMap((indexValue) => {
        const index = indexValue === Number.MAX_SAFE_INTEGER ? undefined : indexValue
        const model = models.find((candidate) => candidate.index === index)
        const imageRequest = imageRequests.find((request) => request.index === index)
        const modelRequest = modelRequests.find((request) => request.index === index)
        const thumbnail = thumbnailFor(index)
        const referenceImage = model ? referenceImageFor(model) : thumbnail
        const referenceImageUrl = referenceImage
          ? worldsUrl(slug, path.join('output', entry.name, referenceImage.name))
          : undefined
        const requestStatus = statusText(modelRequest?.data.status ?? imageRequest?.data.status)
        const complete = Boolean(model)
        if (!complete && !imageRequest && !modelRequest) return []

        return {
          id: index === undefined ? entry.name : `${entry.name}-${index}`,
          assetId: index === undefined ? `${slug}/${entry.name}` : `${slug}/${entry.name}/${index}`,
          sourceWorldSlug: slug,
          baseObjectId: entry.name,
          ...(index === undefined ? {} : { index }),
          variantLabel: model ? versionLabel(model) : `v${index}`,
          fileName: model?.name,
          name: displayName,
          url: model ? worldsUrl(slug, path.join('output', entry.name, model.name)) : '',
          referenceImageUrl,
          thumbnailUrl: thumbnail ? worldsUrl(slug, path.join('output', entry.name, thumbnail.name)) : referenceImageUrl,
          sfxUrls: readSfxUrls(slug, path.join('output', entry.name, 'sfx')),
          complete,
          ...(!complete && requestStatus ? { status: requestStatus } : {}),
        }
      })
    })
}

function readSfxUrls(slug: string, relativeDir: string): string[] {
  return indexedFiles(visibleFiles(path.join(worldsDir, slug, relativeDir)), { extensions: AUDIO_EXTENSIONS })
    .map((file) => worldsUrl(slug, path.join(relativeDir, file.name)))
}

function readWorldSfxUrls(slug: string): string[] {
  return readSfxUrls(slug, path.join('output', 'sfx'))
}

function worldAssetUrl(slug: string, filename?: string): string {
  return filename ? worldsUrl(slug, path.join('output', 'world', filename)) : ''
}

function httpImageUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:' ? value : undefined
  } catch {
    return undefined
  }
}

function localWorldsFileUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || httpImageUrl(value)) return undefined

  const resolvedPath = path.resolve(repoRoot, value)
  const relativePath = path.relative(worldsDir, resolvedPath)
  if (relativePath.startsWith('..') || path.isAbsolute(relativePath)) return undefined
  if (!fs.existsSync(resolvedPath) || !fs.statSync(resolvedPath).isFile()) return undefined

  const [worldSlug, ...worldRelativeParts] = relativePath.split(path.sep)
  if (!worldSlug || !worldRelativeParts.length) return undefined
  return worldsUrl(worldSlug, worldRelativeParts.join(path.sep))
}

function requestPlateImageUrl(request?: RequestManifest): string | undefined {
  const inputFiles = Array.isArray(request?.input_files) ? request.input_files : []
  for (const inputFile of inputFiles) {
    const imageUrl = localWorldsFileUrl(inputFile) ?? httpImageUrl(inputFile)
    if (imageUrl) return imageUrl
  }

  return httpImageUrl(request?.request?.world_prompt?.image_prompt?.uri)
}

function assetKeyForFilename(key: string): string {
  return key.replace(/[^a-z0-9_-]/gi, '_')
}

function latestIndexedFile(files: fs.Dirent[], slug: string, extension?: string) {
  const matches = indexedFiles(files, {
    slugs: new Set([slug]),
    ...(extension ? { extensions: new Set([extension.toLowerCase()]) } : {}),
  }).filter((file) => file.index !== undefined)
  return latestIndexed(matches)?.indexed
}

function worldAssetFilename(files: fs.Dirent[], index: number | undefined, slug: string, extensions?: ReadonlySet<string>) {
  const matches = indexedFiles(files, {
    slugs: new Set([slug]),
    ...(extensions ? { extensions } : {}),
  }).filter((file) => file.index !== undefined)
  if (index === undefined) return latestIndexed(matches)?.name
  return byIndex(matches, index)?.name
}

function readWorldManifest(slug: string) {
  const worldDir = path.join(worldsDir, slug, 'output', 'world')
  const files = visibleFiles(worldDir)
  const latestWorld = latestIndexedFile(files, 'world', '.json')

  if (latestWorld) {
    try {
      const raw = fs.readFileSync(path.join(worldDir, latestWorld.name), 'utf-8')
      return {
        world: JSON.parse(raw) as WorldManifest,
        index: latestWorld.index,
      }
    } catch {
      return undefined
    }
  }

  return undefined
}

function readWorldManifestForIndex(slug: string, index: number): WorldManifest | undefined {
  const worldDir = path.join(worldsDir, slug, 'output', 'world')
  const indexedPath = path.join(worldDir, `${index}-world.json`)
  if (fs.existsSync(indexedPath) && fs.statSync(indexedPath).isFile()) {
    try {
      return JSON.parse(fs.readFileSync(indexedPath, 'utf-8')) as WorldManifest
    } catch {
      return undefined
    }
  }

  return undefined
}

function displayNameFromSlug(slug: string): string {
  return slug
    .split('-')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

function readProjectManifest(slug: string): ProjectManifest | undefined {
  const projectPath = path.join(worldsDir, slug, 'project.json')
  if (!fs.existsSync(projectPath) || !fs.statSync(projectPath).isFile()) return undefined
  try {
    return JSON.parse(fs.readFileSync(projectPath, 'utf-8')) as ProjectManifest
  } catch {
    return undefined
  }
}

function withLocalWorldAssets(slug: string, world: WorldManifest, index?: number): World {
  const files = visibleFiles(path.join(worldsDir, slug, 'output', 'world'))
  const existingSpzUrls = world.assets?.splats?.spz_urls ?? {}
  const spzUrls: Record<string, string> = {}

  // Background splats may be .spz (World Labs) or .ply (locally generated by SHARP);
  // Spark's SplatMesh auto-detects either from the file extension.
  const splatExtensions = new Set(['.spz', '.ply'])
  for (const key of Object.keys(existingSpzUrls)) {
    const assetKey = assetKeyForFilename(key)
    const filename = worldAssetFilename(files, index, `world-${assetKey}`, splatExtensions)
    if (filename) spzUrls[key] = worldAssetUrl(slug, filename)
  }

  for (const file of indexedFiles(files, { extensions: splatExtensions })) {
    const match = file.slug.match(/^world-(100k|150k|500k|full_res)$/)
    if (!match || !WORLD_SPZ_KEYS.has(match[1])) continue

    const key = match[1]
    if (index === undefined || file.index === index) {
      spzUrls[key] = worldAssetUrl(slug, file.name)
    }
  }

  const collider = worldAssetFilename(files, index, 'world', MODEL_EXTENSIONS)
  const pano = worldAssetFilename(files, index, 'world-pano', IMAGE_EXTENSIONS)
  const thumbnail = worldAssetFilename(files, index, 'world-thumbnail', IMAGE_EXTENSIONS)

  return {
    world_id: (world.world_id as string) ?? slug,
    display_name: (world.display_name as string) ?? displayNameFromSlug(slug),
    world_marble_url: (world.world_marble_url as string) ?? '',
    tags: (world.tags as string[]) ?? null,
    world_prompt: (world.world_prompt as string) ?? null,
    created_at: (world.created_at as string) ?? null,
    updated_at: (world.updated_at as string) ?? null,
    assets: {
      caption: (world.assets?.caption as string) ?? '',
      thumbnail_url: worldAssetUrl(slug, thumbnail) || (world.assets?.thumbnail_url as string) || '',
      mesh: {
        collider_mesh_url: worldAssetUrl(slug, collider),
      },
      imagery: {
        pano_url: worldAssetUrl(slug, pano),
      },
      splats: {
        spz_urls: spzUrls,
        semantics_metadata: {
          metric_scale_factor: 1,
          ground_plane_offset: 0,
          flip_y: true,
          ...((world.assets?.splats?.semantics_metadata ?? {}) as Record<string, unknown>),
        },
      },
    },
  }
}

function worldAssetIndexes(slug: string): number[] {
  const files = visibleFiles(path.join(worldsDir, slug, 'output', 'world'))
  const indexes = new Set<number>()
  for (const file of indexedFiles(files)) {
    if (file.index === undefined) continue
    if (
      file.slug === 'world' && file.extension === '.json'
    ) {
      indexes.add(file.index)
    }
  }
  return [...indexes].sort((a, b) => a - b)
}

function worldRequestIndexes(slug: string): number[] {
  const worldDir = path.join(worldsDir, slug, 'output', 'world')
  return requestMetadataFiles(worldDir, 'world').map((request) => request.index)
}

function readWorldVersions(slug: string): WorldVersion[] {
  const indexes = [...new Set([
    ...worldAssetIndexes(slug),
    ...worldRequestIndexes(slug),
  ])].sort((a, b) => a - b)

  return indexes.flatMap((index) => {
    const files = visibleFiles(path.join(worldsDir, slug, 'output', 'world'))
    const request = requestMetadataFiles(path.join(worldsDir, slug, 'output', 'world'), 'world')
      .find((candidate) => candidate.index === index)
    const manifest = readWorldManifestForIndex(slug, index)
    if (!manifest && !request) return []
    const world = manifest ? withLocalWorldAssets(slug, manifest, index) : undefined
    const colliderUrl = String(world?.assets?.mesh?.collider_mesh_url || '')
    const spzUrls = world?.assets?.splats?.spz_urls ?? {}
    const plate = worldAssetFilename(files, index, 'world-plate', IMAGE_EXTENSIONS)
    const plateImageUrl = plate ? worldAssetUrl(slug, plate) : requestPlateImageUrl(request?.data)
    const requestStatus = statusText(request?.data.status)
    const complete = Boolean(world && colliderUrl && Object.keys(spzUrls).length)
    return {
      index,
      label: `v${index}`,
      ...(world ? { world } : {}),
      ...(plateImageUrl ? { plateImageUrl } : {}),
      complete,
      ...(!complete && requestStatus ? { status: requestStatus } : {}),
    }
  })
}

export function sceneProjectPath(slug: string): string | null {
  const worldDir = path.resolve(worldsDir, slug)
  const isInsideWorlds = worldDir !== worldsDir && worldDir.startsWith(`${worldsDir}${path.sep}`)
  if (!isInsideWorlds) return null
  return path.join(worldDir, 'scene.json')
}

export function sanitizePlacementProject(input: unknown): WorldSceneProject | undefined {
  if (!input || typeof input !== 'object') return undefined
  const record = input as Record<string, unknown>
  if (record.version !== PROJECT_VERSION || !Array.isArray(record.instances)) return undefined
  const isVec3 = (value: unknown): value is [number, number, number] => (
    Array.isArray(value) &&
    value.length === 3 &&
    value.every((part) => typeof part === 'number' && Number.isFinite(part))
  )

  const instances = record.instances.flatMap((instance): Array<WorldObjectPlacement> => {
    if (!instance || typeof instance !== 'object') return []
    const item = instance as Record<string, unknown>
    const { instanceId, objectId, assetId, physics, position, rotation, scale } = item

    if (typeof instanceId !== 'string' || typeof objectId !== 'string') return []
    if (assetId !== undefined && typeof assetId !== 'string') return []
    if (
      physics !== undefined &&
      physics !== 'rigidbody' &&
      physics !== 'static' &&
      physics !== 'ghost'
    ) return []
    if (!isVec3(position) || !isVec3(rotation) || !isVec3(scale)) return []
    return [{
      instanceId,
      objectId,
      ...(assetId ? { assetId } : {}),
      physics: (physics as 'rigidbody' | 'static' | 'ghost') ?? 'rigidbody',
      position,
      rotation,
      scale
    }]
  })
  const sun = (() => {
    if (!record.sun || typeof record.sun !== 'object') return undefined
    const candidate = record.sun as Record<string, unknown>
    if (typeof candidate.intensity !== 'number' || !Number.isFinite(candidate.intensity)) return undefined
    if (!isVec3(candidate.rotation)) return undefined
    const environmentIntensity = candidate.environmentIntensity
    return {
      intensity: candidate.intensity,
      rotation: candidate.rotation,
      ...(typeof environmentIntensity === 'number' && Number.isFinite(environmentIntensity) ? { environmentIntensity } : {}),
    } as WorldSceneSun
  })()
  const metricScaleFactor = record.metricScaleFactor
  const groundPlaneOffset = record.groundPlaneOffset
  const groundPlaneColliderEnabled = record.groundPlaneColliderEnabled
  const shadowCatcherOpacity = record.shadowCatcherOpacity
  const normalizedShadowCatcherOpacity = typeof shadowCatcherOpacity === 'number' && Number.isFinite(shadowCatcherOpacity)
    ? Math.min(Math.max(shadowCatcherOpacity, 0), 1)
    : undefined
  const shadowCatcherColor = record.shadowCatcherColor
  const normalizedShadowCatcherColor = typeof shadowCatcherColor === 'string' && /^#[0-9a-f]{6}$/i.test(shadowCatcherColor)
    ? shadowCatcherColor.toLowerCase()
    : undefined

  return {
    version: PROJECT_VERSION,
    instances,
    ...(sun ? { sun } : {}),
    ...(typeof metricScaleFactor === 'number' && Number.isFinite(metricScaleFactor) ? { metricScaleFactor } : {}),
    ...(typeof groundPlaneOffset === 'number' && Number.isFinite(groundPlaneOffset) ? { groundPlaneOffset } : {}),
    ...(typeof groundPlaneColliderEnabled === 'boolean' ? { groundPlaneColliderEnabled } : {}),
    ...(normalizedShadowCatcherOpacity !== undefined ? { shadowCatcherOpacity: normalizedShadowCatcherOpacity } : {}),
    ...(normalizedShadowCatcherColor !== undefined ? { shadowCatcherColor: normalizedShadowCatcherColor } : {}),
  }
}

export function readSceneProject(slug: string): WorldSceneProject | undefined {
  const filePath = sceneProjectPath(slug)
  if (!filePath || !fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) return undefined
  try {
    return sanitizePlacementProject(JSON.parse(fs.readFileSync(filePath, 'utf-8')))
  } catch {
    return undefined
  }
}

export function readWorlds(): WorldEntry[] {
  if (!fs.existsSync(worldsDir)) return []
  const entries = fs.readdirSync(worldsDir)
    .flatMap((slug) => {
      const worldDir = path.join(worldsDir, slug)
      if (!fs.statSync(worldDir).isDirectory()) return []
      const project = readProjectManifest(slug)
      if (!project) return []
      const manifest = readWorldManifest(slug)
      const worldVersions = readWorldVersions(slug)
      const defaultWorld = worldVersions[worldVersions.length - 1]?.world
        ?? (manifest ? withLocalWorldAssets(slug, manifest.world, manifest.index) : undefined)
      return [{
        slug,
        project: {
          slug: project.slug ?? slug,
          display_name: project.display_name ?? displayNameFromSlug(slug),
          ...(project.created_at ? { created_at: project.created_at } : {}),
          ...(project.updated_at ? { updated_at: project.updated_at } : {}),
          ...(project.notes ? { notes: project.notes } : {}),
        } as WorldProject,
        ...(defaultWorld ? { world: defaultWorld } : {}),
        worldVersions,
        objectAssets: readObjectAssets(slug),
        allObjectAssets: [] as WorldObjectAsset[],
        sourceImageUrl: readSourceImageUrl(slug),
        sourceImageVersions: readSourceImageVersions(slug),
        worldSfxUrls: readWorldSfxUrls(slug),
        sceneProject: readSceneProject(slug),
      } as WorldEntry]
    })
  const allObjectAssets = entries.flatMap((entry) => entry.objectAssets)
  return entries.map((entry) => ({ ...entry, allObjectAssets }))
}

export function openFolder(folderPath: string) {
  const command = process.platform === 'darwin'
    ? 'open'
    : process.platform === 'win32'
      ? 'cmd'
      : 'xdg-open'
  const args = process.platform === 'win32'
    ? ['/c', 'start', '', folderPath]
    : [folderPath]
  const child = spawn(command, args, { detached: true, stdio: 'ignore' })
  child.unref()
}

export function openClaudeTerminal() {
  if (process.platform !== 'darwin') return false

  const shellQuote = (value: string) => `'${value.replace(/'/g, `'"'"'`)}'`
  const appleScriptString = (value: string) => value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
  const command = `cd ${shellQuote(repoRoot)} && claude`
  const child = spawn('osascript', [
    '-e',
    'tell application "Terminal"',
    '-e',
    `do script "${appleScriptString(command)}"`,
    '-e',
    'activate',
    '-e',
    'end tell',
  ], { detached: true, stdio: 'ignore' })
  child.unref()
  return true
}
