import * as THREE from 'three'

interface RetainedResource {
  count: number
  dispose: () => void
  disposeTimer?: ReturnType<typeof setTimeout>
}

const retainedResources = new Map<string, RetainedResource>()

function disposeMaterial(
  material: THREE.Material,
  disposedMaterials: Set<THREE.Material>,
  disposedTextures: Set<THREE.Texture>,
) {
  if (disposedMaterials.has(material)) return
  disposedMaterials.add(material)

  for (const value of Object.values(material)) {
    if (value instanceof THREE.Texture && !disposedTextures.has(value)) {
      disposedTextures.add(value)
      value.dispose()
    }
  }
  material.dispose()
}

function disposeObjectTree(root: THREE.Object3D) {
  const disposedGeometries = new Set<THREE.BufferGeometry>()
  const disposedMaterials = new Set<THREE.Material>()
  const disposedTextures = new Set<THREE.Texture>()

  root.traverse((child) => {
    if (child instanceof THREE.Mesh) {
      if (!disposedGeometries.has(child.geometry)) {
        disposedGeometries.add(child.geometry)
        child.geometry.dispose()
      }
      const materials = Array.isArray(child.material) ? child.material : [child.material]
      for (const material of materials) {
        disposeMaterial(material, disposedMaterials, disposedTextures)
      }
    }

    if (child instanceof THREE.SkinnedMesh) {
      const boneTexture = child.skeleton.boneTexture
      if (boneTexture && !disposedTextures.has(boneTexture)) {
        disposedTextures.add(boneTexture)
        boneTexture.dispose()
      }
    }
  })
}

function retainResource(key: string, dispose: () => void) {
  let entry = retainedResources.get(key)
  if (!entry) {
    entry = { count: 0, dispose }
    retainedResources.set(key, entry)
  }

  if (entry.disposeTimer) {
    clearTimeout(entry.disposeTimer)
    entry.disposeTimer = undefined
  }
  entry.count += 1

  let released = false
  return () => {
    if (released) return
    released = true
    entry!.count = Math.max(0, entry!.count - 1)
    if (entry!.count > 0) return

    // Delay disposal to survive React's development-only effect replay and
    // synchronous world remounts that reuse the same loader cache entry.
    entry!.disposeTimer = setTimeout(() => {
      if (entry!.count > 0) return
      entry!.dispose()
      retainedResources.delete(key)
    }, 0)
  }
}

export function retainGltfResource(
  cacheKey: string,
  scene: THREE.Object3D,
  clearLoaderCache: () => void,
) {
  return retainResource(`gltf:${cacheKey}`, () => {
    clearLoaderCache()
    disposeObjectTree(scene)
  })
}

export function retainTextureResource(
  cacheKey: string,
  texture: THREE.Texture,
  clearLoaderCache: () => void,
) {
  return retainResource(`texture:${cacheKey}`, () => {
    clearLoaderCache()
    texture.dispose()
  })
}
