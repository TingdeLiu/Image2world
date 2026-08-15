'use client'

import { useMemo, useEffect } from 'react'
import { RigidBody } from '@react-three/rapier'
import { useGLTF } from '@react-three/drei'
import { clone as cloneSkeleton } from 'three/examples/jsm/utils/SkeletonUtils.js'
import * as THREE from 'three'
import { useDebugStore } from '../../store/debug'
import { ObjectRenderMode, WorldRenderMode } from '../../types/world'
import { useAssetMaterials } from '../scene/useAssetMaterials'
import { DROP_TARGET_LAYER, markColliderDropTarget } from '../scene/dropTargets'
import { shadowCatcherColor, shadowCatcherOpacity } from '../scene/shadows'
import { retainGltfResource } from '../../utils/threeResourceLifecycle'
import { SURFACE_PHYSICS, surfaceMeshes, type SurfaceMesh } from './surfaces'
import { worldTransform } from './worldTransform'

interface Props {
  url: string
  flipY?: boolean
  rotationX?: number
  groundPlaneOffset?: number
  metricScaleFactor?: number
  shadowOpacity?: number
  shadowColor?: string
}

const ignoreRaycast: THREE.Object3D['raycast'] = () => {}

export function WorldCollider({ url, flipY, rotationX, groundPlaneOffset, metricScaleFactor, shadowOpacity, shadowColor }: Props) {
  const { scene: rawScene } = useGLTF(url)
  const objectRenderMode = useDebugStore((s) => s.objectRenderMode)
  const worldRenderMode = useDebugStore((s) => s.worldRenderMode)
  const { wireframeMaterial, shadedMaterial, wireframeOverlayMaterial } = useAssetMaterials()
  const normalizedGroundPlaneOffset = groundPlaneOffset ?? 0
  const normalizedMetricScaleFactor = metricScaleFactor ?? 1
  const normalizedRotation = rotationX ?? (flipY ? Math.PI : 0)
  const colliderTransformKey = `${url}:${normalizedRotation}:${normalizedGroundPlaneOffset}:${normalizedMetricScaleFactor}`

  useEffect(
    () => retainGltfResource(
      `collider:${url}`,
      rawScene,
      () => useGLTF.clear(url),
    ),
    [rawScene, url],
  )

  // Own shadow material instance — not shared, so shader compiles correctly per-mesh
  const shadowMat = useMemo(() => new THREE.ShadowMaterial({
    color: shadowCatcherColor(shadowColor),
    opacity: shadowCatcherOpacity(shadowOpacity),
    transparent: true,
    depthWrite: false,
  }), [shadowColor, shadowOpacity])
  useEffect(() => () => shadowMat.dispose(), [shadowMat])

  useEffect(() => {
    shadowMat.color.set(shadowCatcherColor(shadowColor))
    shadowMat.opacity = shadowCatcherOpacity(shadowOpacity)
    shadowMat.needsUpdate = true
  }, [shadowColor, shadowMat, shadowOpacity])

  /**
   * Worlds built after the surface split carry one GLB node per class, and each
   * becomes its own rigid body so the floor can grip while walls stay slippery.
   * Older worlds are a single unnamed mesh; `surfaces` comes back empty and the
   * whole thing goes into one body with Rapier's defaults, exactly as before.
   */
  const { surfaces, legacyScene, overlayScene, dropTargetScene, collisionMeshes } = useMemo(() => {
    const dropTargetScene = cloneSkeleton(rawScene)
    dropTargetScene.traverse((child) => {
      child.layers.set(DROP_TARGET_LAYER)
      // So a "what did I click in the room" raycast can tell this apart from
      // GroundPlane's invisible helper, which shares the layer.
      markColliderDropTarget(child)
    })
    const overlayScene = cloneSkeleton(rawScene)
    const surfaces = surfaceMeshes(rawScene)

    if (surfaces.length) {
      return {
        surfaces,
        legacyScene: null,
        overlayScene,
        dropTargetScene,
        collisionMeshes: surfaces.map((surface) => surface.mesh),
      }
    }

    const legacyScene = cloneSkeleton(rawScene)
    const collisionMeshes: THREE.Mesh[] = []
    legacyScene.traverse((child) => {
      if (child instanceof THREE.Mesh) collisionMeshes.push(child)
    })
    return { surfaces: [] as SurfaceMesh[], legacyScene, overlayScene, dropTargetScene, collisionMeshes }
  }, [rawScene])

  const showMesh = worldRenderMode !== WorldRenderMode.ObjectOnly

  useEffect(() => {
    const isShadowCatcher = objectRenderMode === ObjectRenderMode.Lit
    for (const child of collisionMeshes) {
      child.visible = showMesh
      child.raycast = ignoreRaycast
      child.receiveShadow = isShadowCatcher
      if (child.material !== wireframeMaterial && child.material !== shadedMaterial && child.material !== shadowMat) {
        const old = Array.isArray(child.material) ? child.material : [child.material]
        old.forEach((m) => m?.dispose?.())
      }
      child.material = isShadowCatcher ? shadowMat
        : objectRenderMode === ObjectRenderMode.ShadedWireframe ? shadedMaterial
        : wireframeMaterial
      child.material.needsUpdate = true
    }
  }, [collisionMeshes, showMesh, objectRenderMode, wireframeMaterial, shadedMaterial, shadowMat])

  useEffect(() => {
    overlayScene.traverse((child) => {
      if (!(child instanceof THREE.Mesh)) return
      child.material = wireframeOverlayMaterial
      child.renderOrder = 1
      child.raycast = ignoreRaycast
    })
  }, [overlayScene, wireframeOverlayMaterial])

  // Spread rather than copied field by field: `SplatRenderer` spreads the same
  // object, so the two cannot drift apart into placing the same world in two
  // different places. See `worldTransform.ts`.
  const transform = worldTransform({ rotationX: normalizedRotation, groundPlaneOffset, metricScaleFactor })

  return (
    <>
      {surfaces.map(({ surface, mesh }) => {
        const physics = SURFACE_PHYSICS[surface]
        return (
          <RigidBody
            key={`${colliderTransformKey}:${surface}`}
            type="fixed"
            colliders="trimesh"
            friction={physics.friction}
            frictionCombineRule={physics.frictionCombineRule}
            restitution={physics.restitution}
            {...transform}
          >
            <primitive object={mesh} />
          </RigidBody>
        )
      })}
      {legacyScene && (
        <RigidBody
          key={colliderTransformKey}
          type="fixed"
          colliders="trimesh"
          {...transform}
        >
          <primitive object={legacyScene} />
        </RigidBody>
      )}
      {objectRenderMode === ObjectRenderMode.ShadedWireframe && showMesh && (
        <primitive object={overlayScene} {...transform} />
      )}
      <primitive object={dropTargetScene} {...transform} />
    </>
  )
}
