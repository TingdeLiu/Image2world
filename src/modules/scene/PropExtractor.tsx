'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import { Cursor as CursorIcon, Hand as HandIcon, SpinnerGap as SpinnerIcon } from '@phosphor-icons/react'
import { AppButton } from '../../components/AppButton'
import { chrome } from '../../components/AppChrome'
import { markObjectInteraction } from '../interaction/pointerGuards'
import { APP_REQUEST_HEADERS } from '../../utils/sameOrigin'
import { DROP_TARGET_LAYER, isColliderDropTarget } from './dropTargets'

export interface PropExtractionController {
  active: boolean
  busy: boolean
  error: string | null
  lastPropId: string | null
  setActive: (active: boolean) => void
  extract: (point: [number, number, number]) => void
  reportMiss: () => void
}

interface Options {
  slug: string
  /** Called after a successful cut so the viewer picks the new prop up. */
  onExtracted?: () => void | Promise<void>
}

/**
 * Turn a click in the room into a prop you can push around.
 *
 * The click is all the browser contributes: the route on the other end has the
 * gaussians and the source photo, and SAM 2 decides where the object's edges
 * are. What has to happen here is the coordinate hand-off — see
 * `PropExtractorScene` for why the point has to leave in splat space.
 */
export function usePropExtraction({ slug, onExtracted }: Options): PropExtractionController {
  const [active, setActive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastPropId, setLastPropId] = useState<string | null>(null)

  // Leaving the world mid-extraction should not carry the mode over to the next.
  useEffect(() => {
    setActive(false)
    setError(null)
    setLastPropId(null)
  }, [slug])

  const extract = useCallback((point: [number, number, number]) => {
    setBusy(true)
    setError(null)
    void (async () => {
      try {
        const response = await fetch(`/api/extract-prop?slug=${encodeURIComponent(slug)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...APP_REQUEST_HEADERS },
          body: JSON.stringify({ point }),
        })
        const payload = await response.json().catch(() => ({})) as { error?: string; propId?: string }
        if (!response.ok) throw new Error(payload.error || `Extraction failed (HTTP ${response.status}).`)
        setLastPropId(payload.propId ?? null)
        await onExtracted?.()
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : 'Extraction failed.')
      } finally {
        setBusy(false)
      }
    })()
  }, [onExtracted, slug])

  const reportMiss = useCallback(() => {
    setError('Nothing solid there. Aim at a surface the room actually reconstructed.')
  }, [])

  return { active, busy, error, lastPropId, setActive, extract, reportMiss }
}

/**
 * Canvas-side half: turn a pointer event into a splat-space coordinate.
 *
 * The ray is cast against the drop-target layer, which already holds a copy of
 * the collider carrying the same transform as the visible one. Undoing that
 * transform with `worldToLocal` is what hands the backend a point in the
 * splat's own space — the space SHARP's camera can project, and the only one
 * where "which pixel did this come from" has an answer.
 */
export function PropExtractorScene({ controller }: { controller: PropExtractionController }) {
  const { camera, gl, scene } = useThree()
  const raycaster = useMemo(() => new THREE.Raycaster(), [])

  useEffect(() => {
    if (!controller.active) return
    const canvas = gl.domElement

    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0 || controller.busy) return
      // Keep the click from also grabbing the pointer for camera look.
      markObjectInteraction()
      event.preventDefault()

      const rect = canvas.getBoundingClientRect()
      if (!rect.width || !rect.height) return
      raycaster.layers.set(DROP_TARGET_LAYER)
      raycaster.setFromCamera(
        new THREE.Vector2(
          ((event.clientX - rect.left) / rect.width) * 2 - 1,
          -((event.clientY - rect.top) / rect.height) * 2 + 1,
        ),
        camera,
      )

      // Nearest hit *on the room*. Taking the nearest hit of any kind would
      // usually mean GroundPlane's invisible 200 m helper, which shares this
      // layer, sits at y=0 next to the floor, and would send a click with no
      // depth to the extractor -- coming back as an unreadable 502.
      const hit = raycaster
        .intersectObjects(scene.children, true)
        .find((candidate) => isColliderDropTarget(candidate.object))
      if (!hit) {
        /**
         * Missing is routine, not exceptional. A ray straight down the middle
         * of the screen is horizontal, and single-view reconstruction leaves
         * whole directions with no geometry at body height -- measured here,
         * dead centre hits nothing at all while a ray 0.3 down the screen hits
         * four surfaces. A click that silently does nothing reads as broken.
         */
        controller.reportMiss()
        return
      }
      const local = hit.object.worldToLocal(hit.point.clone())
      controller.extract([local.x, local.y, local.z])
    }

    canvas.addEventListener('pointerdown', onPointerDown)
    return () => canvas.removeEventListener('pointerdown', onPointerDown)
  }, [camera, controller, gl.domElement, raycaster, scene.children])

  return null
}

export function PropExtractorOverlay({ controller }: { controller: PropExtractionController }) {
  const { active, busy, error, lastPropId, setActive } = controller

  return (
    <div className={`${chrome.enter} pointer-events-none fixed left-1/2 top-3 z-30 flex -translate-x-1/2 flex-col items-center gap-1.5`}>
      <AppButton
        onClick={() => setActive(!active)}
        active={active}
        className={`pointer-events-auto flex h-9 items-center gap-2 rounded-full px-3.5 text-sm ${chrome.bar} ${
          active ? 'text-white' : 'text-white/80'
        }`}
        aria-pressed={active}
      >
        {busy ? <SpinnerIcon size={16} className="animate-spin" /> : active ? <CursorIcon size={16} weight="fill" /> : <HandIcon size={16} />}
        {busy ? 'Cutting it loose…' : active ? 'Click something to free it' : 'Free an object'}
      </AppButton>
      {active && !busy && !error && (
        <p className="rounded bg-black/70 px-2 py-1 text-xs text-white/70 backdrop-blur-md">
          {lastPropId ? `Freed ${lastPropId} — walk into it, or pick another.` : 'Pick a chair, a box, anything loose.'}
        </p>
      )}
      {error && (
        <p className="max-w-sm rounded bg-red-950/80 px-2 py-1 text-center text-xs text-red-200 backdrop-blur-md">
          {error}
        </p>
      )}
    </div>
  )
}
