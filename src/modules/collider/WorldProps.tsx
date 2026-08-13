'use client'

import { useEffect, useMemo } from 'react'
import { RigidBody } from '@react-three/rapier'
import { useGLTF } from '@react-three/drei'
import * as THREE from 'three'
import { useAssetMaterials } from '../scene/useAssetMaterials'
import { retainGltfResource } from '../../utils/threeResourceLifecycle'
import { ObjectRenderMode } from '../../types/world'
import type { WorldProps as WorldPropsData } from '../../types/world'

interface Props {
  data: WorldPropsData
  flipY?: boolean
  rotationX?: number
  groundPlaneOffset?: number
  metricScaleFactor?: number
  renderMode: ObjectRenderMode
}

/**
 * Movable pieces of the room, as dynamic rigid bodies.
 *
 * Each prop is one node in the props GLB, carved out of the world's own
 * gaussians rather than modelled — so a chair here is the reconstruction of
 * that chair, not a guess at one.
 *
 * The colliders are convex hulls, not trimeshes. Rapier's dynamic trimesh has
 * no interior, which makes its mass properties meaningless and lets fast
 * contacts tunnel straight through; a hull is the standard proxy and is what
 * the extractor's silhouette is good for anyway.
 */
export function WorldProps({
  data,
  flipY,
  rotationX,
  groundPlaneOffset,
  metricScaleFactor,
  renderMode,
}: Props) {
  const { scene: rawScene } = useGLTF(data.url)
  const { wireframeMaterial, shadedMaterial } = useAssetMaterials()
  const normalizedRotation = rotationX ?? (flipY ? Math.PI : 0)
  const normalizedGroundPlaneOffset = groundPlaneOffset ?? 0
  const normalizedMetricScaleFactor = metricScaleFactor ?? 1

  useEffect(
    () => retainGltfResource(`props:${data.url}`, rawScene, () => useGLTF.clear(data.url)),
    [rawScene, data.url],
  )

  /**
   * Pair each declared prop with its node. A prop with no node would be an
   * invisible thing to trip over, and a node with no prop entry has no mass,
   * so anything unmatched is dropped rather than guessed at.
   */
  const bodies = useMemo(() => {
    rawScene.updateWorldMatrix(true, true)
    const meshes = new Map<string, THREE.Mesh>()
    rawScene.traverse((child) => {
      if (child instanceof THREE.Mesh && child.name) meshes.set(child.name, child)
    })

    return data.props.flatMap((prop) => {
      const source = meshes.get(prop.id)
      if (!source) return []
      const mesh = new THREE.Mesh(source.geometry)
      mesh.name = prop.id
      mesh.applyMatrix4(source.matrixWorld)
      mesh.castShadow = true
      mesh.receiveShadow = true
      return [{ prop, mesh }]
    })
  }, [data.props, rawScene])

  useEffect(() => {
    const material = renderMode === ObjectRenderMode.Wireframe ? wireframeMaterial : shadedMaterial
    for (const { mesh } of bodies) {
      mesh.material = material
      mesh.material.needsUpdate = true
    }
  }, [bodies, renderMode, shadedMaterial, wireframeMaterial])

  return (
    <>
      {bodies.map(({ prop, mesh }) => (
        <RigidBody
          key={prop.id}
          type="dynamic"
          colliders="hull"
          mass={prop.mass}
          // Props are furniture: they should settle rather than skitter, and
          // come to rest instead of drifting after a shove.
          friction={0.8}
          restitution={0}
          linearDamping={0.4}
          angularDamping={0.6}
          rotation={[normalizedRotation, 0, 0]}
          position={[0, normalizedGroundPlaneOffset, 0]}
          scale={[normalizedMetricScaleFactor, normalizedMetricScaleFactor, normalizedMetricScaleFactor]}
        >
          <primitive object={mesh} />
        </RigidBody>
      ))}
    </>
  )
}
