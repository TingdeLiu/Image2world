'use client'

import { useEffect, useRef, useState } from 'react'
import { useFrame } from '@react-three/fiber'
import * as THREE from 'three'
import { chrome } from '../../components/AppChrome'
import type { WorldWalkable } from '../../types/world'

const SIZE = 132

export interface MinimapPose {
  x: number
  z: number
  yaw: number
}

export type MinimapPoseRef = { current: MinimapPose }

export function useMinimapPose(): MinimapPoseRef {
  return useRef<MinimapPose>({ x: 0, z: 0, yaw: 0 })
}

/**
 * Canvas-side half: report where the player is, without re-rendering React.
 *
 * A minimap updates every frame, and pushing that through state would rerender
 * the whole overlay 60 times a second. The pose goes into a ref that the DOM
 * side samples on its own animation frame.
 */
export function MinimapTracker({ poseRef }: { poseRef: MinimapPoseRef }) {
  const euler = useRef(new THREE.Euler(0, 0, 0, 'YXZ'))

  useFrame(({ camera }) => {
    euler.current.setFromQuaternion(camera.quaternion)
    poseRef.current.x = camera.position.x
    poseRef.current.z = camera.position.z
    poseRef.current.yaw = euler.current.y
  })

  return null
}

/**
 * A floor plan of where you can actually stand, with you on it.
 *
 * The mask arrives as a greyscale PNG; it is recoloured here rather than shown
 * raw so that unreachable ground reads as absent instead of as dark floor —
 * the whole point is to show the edge you would otherwise walk off.
 */
export function Minimap({
  data,
  poseRef,
  metricScaleFactor = 1,
}: {
  data: WorldWalkable
  poseRef: MinimapPoseRef
  metricScaleFactor?: number
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const markerRef = useRef<HTMLDivElement>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    let cancelled = false

    const image = new Image()
    image.onload = () => {
      if (cancelled) return
      canvas.width = data.cols
      canvas.height = data.rows
      const context = canvas.getContext('2d')
      if (!context) return

      context.drawImage(image, 0, 0, data.cols, data.rows)
      const pixels = context.getImageData(0, 0, data.cols, data.rows)
      const { data: rgba } = pixels
      for (let i = 0; i < rgba.length; i += 4) {
        const walkable = rgba[i] > 127
        rgba[i] = 125
        rgba[i + 1] = 211
        rgba[i + 2] = 252
        rgba[i + 3] = walkable ? 205 : 0
      }
      context.putImageData(pixels, 0, 0)
      setReady(true)
    }
    image.onerror = () => setReady(false)
    image.src = data.url

    return () => {
      cancelled = true
    }
  }, [data.cols, data.rows, data.url])

  useEffect(() => {
    if (!ready) return
    let frame = 0

    const tick = () => {
      frame = window.requestAnimationFrame(tick)
      const marker = markerRef.current
      if (!marker) return
      const { x, z, yaw } = poseRef.current
      // The grid is in metres, measured at the scale the world had when the
      // collider was built. The player is in metres at the scale the world has
      // now; the two are the same number unless somebody rescaled the world
      // since, so this ratio is 1 in every ordinary case. Maps written before
      // the grid was metric report a built scale of 1, which reproduces the
      // plain divide-by-scale those files were read with.
      const liveScale = metricScaleFactor || 1
      const builtScale = data.metricScaleFactor || 1
      const gridX = (x * builtScale) / liveScale
      const gridZ = (z * builtScale) / liveScale
      const col = (gridX - data.origin[0]) / data.cellSize
      const row = (gridZ - data.origin[1]) / data.cellSize

      /**
       * Being off the map is the normal opening state, not an edge case: a
       * single-view world puts you where the photo was taken, which is outside
       * the room it reconstructed. Pin the marker to the edge it left by and
       * fade it, so it reads as "you are off the map, this way" instead of
       * flying out of the panel — measured at top: 120%, past the viewport.
       */
      const clampedCol = Math.min(Math.max(col, 0), data.cols)
      const clampedRow = Math.min(Math.max(row, 0), data.rows)
      const offMap = clampedCol !== col || clampedRow !== row
      marker.style.left = `${(clampedCol / data.cols) * 100}%`
      marker.style.top = `${(clampedRow / data.rows) * 100}%`
      marker.style.opacity = offMap ? '0.45' : '1'
      // Screen-space north is -Z, and yaw grows counter-clockwise about +Y.
      marker.style.transform = `translate(-50%, -50%) rotate(${-yaw}rad)`
    }

    frame = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(frame)
  }, [data.cellSize, data.cols, data.metricScaleFactor, data.origin, data.rows, metricScaleFactor, poseRef, ready])

  // The canvas stays mounted even before the mask decodes -- the loader above
  // needs something to draw into -- so the wrapper hides it instead.
  const aspect = data.cols / Math.max(1, data.rows)
  const width = aspect >= 1 ? SIZE : SIZE * aspect
  const height = aspect >= 1 ? SIZE / aspect : SIZE

  return (
    /* Bottom left, not bottom right: the source-image panel owns that corner
       and is 384px square, which covered the minimap completely. Sitting above
       the mode buttons here depends on nothing but its own height. */
    <div className={`${chrome.enter} pointer-events-none fixed bottom-16 left-3 z-30 hidden md:block`}>
      <div
        className={`${chrome.bar} relative overflow-hidden p-1.5`}
        style={{ width: width + 12, height: height + 12, visibility: ready ? 'visible' : 'hidden' }}
      >
        <div className="relative h-full w-full">
          <canvas
            ref={canvasRef}
            className="h-full w-full"
            style={{ imageRendering: 'pixelated' }}
            aria-label={`Walkable area: ${data.walkableArea} square metres`}
          />
          <div ref={markerRef} className="absolute" style={{ left: '50%', top: '50%' }}>
            <div className="h-0 w-0 border-x-[5px] border-b-[9px] border-x-transparent border-b-white drop-shadow" />
          </div>
        </div>
      </div>
    </div>
  )
}
