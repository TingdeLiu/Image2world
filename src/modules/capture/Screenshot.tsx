'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { useThree } from '@react-three/fiber'
import { Camera as CameraIcon, Check as CheckIcon } from '@phosphor-icons/react'
import { Tooltip } from '@radix-ui/themes'
import { AppButton } from '../../components/AppButton'

type Capture = () => string | null

export interface ScreenshotController {
  take: () => void
  saved: boolean
  register: (capture: Capture | null) => void
}

function fileName(worldName: string) {
  const slug = worldName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') || 'world'
  // Local time, because the point is "the one I took just now".
  const now = new Date()
  const stamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
    String(now.getHours()).padStart(2, '0'),
    String(now.getMinutes()).padStart(2, '0'),
    String(now.getSeconds()).padStart(2, '0'),
  ]
  return `${slug}-${stamp.slice(0, 3).join('')}-${stamp.slice(3).join('')}.png`
}

export function useScreenshot(worldName: string): ScreenshotController {
  const captureRef = useRef<Capture | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!saved) return
    const timer = window.setTimeout(() => setSaved(false), 1600)
    return () => window.clearTimeout(timer)
  }, [saved])

  const take = useCallback(() => {
    const dataUrl = captureRef.current?.()
    if (!dataUrl) return
    const link = document.createElement('a')
    link.href = dataUrl
    link.download = fileName(worldName)
    link.click()
    setSaved(true)
  }, [worldName])

  const register = useCallback((capture: Capture | null) => {
    captureRef.current = capture
  }, [])

  return { take, saved, register }
}

/**
 * Canvas-side half of taking a screenshot.
 *
 * The canvas is created without `preserveDrawingBuffer`, so reading it after an
 * ordinary frame yields a blank image — the drawing buffer is cleared once it
 * has been presented. Rendering and reading inside the same synchronous block
 * is what makes this work, and it avoids paying for that flag on every frame of
 * a splat scene that is already GPU-bound.
 */
export function ScreenshotCapture({ controller }: { controller: ScreenshotController }) {
  const gl = useThree((state) => state.gl)
  const scene = useThree((state) => state.scene)
  const camera = useThree((state) => state.camera)
  const { register } = controller

  useEffect(() => {
    register(() => {
      gl.render(scene, camera)
      return gl.domElement.toDataURL('image/png')
    })
    return () => register(null)
  }, [camera, gl, register, scene])

  return null
}

export function ScreenshotButton({
  controller,
  className = '',
}: {
  controller: ScreenshotController
  className?: string
}) {
  return (
    <Tooltip content={controller.saved ? 'saved to downloads' : 'save a screenshot'} delayDuration={0} side="top">
      <AppButton
        onClick={controller.take}
        className={`h-6 w-6 justify-center rounded border border-white/15 bg-black/70 p-0 text-white shadow-lg backdrop-blur-md ${className}`}
        aria-label="Save a screenshot"
      >
        {controller.saved ? <CheckIcon size={12} weight="bold" /> : <CameraIcon size={12} />}
      </AppButton>
    </Tooltip>
  )
}
