'use client'

import { useEffect, useMemo } from 'react'
import { RigidBody } from '@react-three/rapier'
import { useGLTF } from '@react-three/drei'
import * as THREE from 'three'
import { useAssetMaterials } from '../scene/useAssetMaterials'
import { shadowCatcherColor, shadowCatcherOpacity } from '../scene/shadows'
import { retainGltfResource } from '../../utils/threeResourceLifecycle'
import { ObjectRenderMode } from '../../types/world'
import type { WorldProps as WorldPropsData } from '../../types/world'
import { worldTransform } from './worldTransform'

interface Props {
  data: WorldPropsData
  flipY?: boolean
  rotationX?: number
  groundPlaneOffset?: number
  metricScaleFactor?: number
  renderMode: ObjectRenderMode
  shadowOpacity?: number
  shadowColor?: string
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
  shadowOpacity,
  shadowColor,
}: Props) {
  const { scene: rawScene } = useGLTF(data.url)
  const { wireframeMaterial, shadedMaterial } = useAssetMaterials()
  // The same object `WorldCollider` and `SplatRenderer` spread, so a prop
  // cannot end up in a different place from the room it was carved out of.
  const transform = worldTransform({ flipY, rotationX, groundPlaneOffset, metricScaleFactor })

  /**
   * Lit mode has to be invisible, exactly as it is for the collision mesh.
   *
   * A prop is a hull cut from the room's own gaussians, and those gaussians are
   * still being drawn. Painting the hull opaque on top of them puts a grey shell
   * over the very object it was carved from -- the reason this component draws
   * nothing in the default view and only shows up in the debug modes. Casting
   * and receiving shadows still works, so a prop that moves takes its shadow
   * with it.
   */
  const shadowMat = useMemo(() => new THREE.ShadowMaterial({
    color: shadowCatcherColor(shadowColor),
    opacity: shadowCatcherOpacity(shadowOpacity),
    transparent: true,
    depthWrite: false,
  }), [shadowColor, shadowOpacity])
  useEffect(() => () => shadowMat.dispose(), [shadowMat])

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
    const material = renderMode === ObjectRenderMode.Lit ? shadowMat
      : renderMode === ObjectRenderMode.ShadedWireframe ? shadedMaterial
      : wireframeMaterial
    for (const { mesh } of bodies) {
      mesh.material = material
      mesh.material.needsUpdate = true
    }
  }, [bodies, renderMode, shadedMaterial, shadowMat, wireframeMaterial])

  return (
    <>
      {bodies.map(({ prop, mesh }) => (
        <RigidBody
          // The scale belongs in the key: a hull collider is computed once at
          // mount from the geometry as it stands then, so rescaling the world
          // would otherwise leave the prop's physics shape behind its mesh.
          key={`${prop.id}:${transform.scale[0]}`}
          type="dynamic"
          colliders="hull"
          mass={prop.mass}
          // Props are furniture: they should settle rather than skitter, and
          // come to rest instead of drifting after a shove.
          friction={0.8}
          restitution={0}
          linearDamping={0.4}
          angularDamping={0.6}
          {...transform}
        >
          <primitive object={mesh} />
        </RigidBody>
      ))}
    </>
  )
}
