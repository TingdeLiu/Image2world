'use client'

import { useEffect, useState } from 'react'
import { chrome } from './AppChrome'

const SEEN_KEY = 'imageworld:controls-seen'
const DISMISS_AFTER_MS = 9000

/**
 * What to do once the world finishes loading.
 *
 * Nothing on screen said so before: on a phone the joystick only draws itself
 * *after* you already guessed to drag the left half, and on a desktop the
 * first-person controls were undiscoverable unless you tried WASD on a hunch.
 *
 * It shows once per browser and gets out of the way — on a timer, on the first
 * real input, or on a click.
 */
export function ControlsHint({ visible }: { visible: boolean }) {
  const [dismissed, setDismissed] = useState(true)
  const [touch, setTouch] = useState(false)

  useEffect(() => {
    if (!visible) return
    // Reading localStorage during render would desync hydration, so the hint
    // starts hidden and only appears once the client has had its say.
    try {
      if (window.localStorage.getItem(SEEN_KEY)) return
    } catch {
      // Private browsing or blocked storage: show the hint, just do not
      // remember that we did.
    }
    setTouch(window.matchMedia?.('(pointer: coarse)').matches ?? false)
    setDismissed(false)
  }, [visible])

  useEffect(() => {
    if (dismissed) return

    const dismiss = () => {
      setDismissed(true)
      try {
        window.localStorage.setItem(SEEN_KEY, '1')
      } catch {
        // Not remembering is better than throwing on the way out.
      }
    }

    const timer = window.setTimeout(dismiss, DISMISS_AFTER_MS)
    // Anyone who has started moving does not need to be told how.
    window.addEventListener('keydown', dismiss, { once: true })
    window.addEventListener('pointerdown', dismiss, { once: true })
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('keydown', dismiss)
      window.removeEventListener('pointerdown', dismiss)
    }
  }, [dismissed])

  if (dismissed || !visible) return null

  const controls = touch
    ? [
      ['Left half', 'move'],
      ['Right half', 'look around'],
    ]
    : [
      ['W A S D', 'move'],
      ['Drag', 'look around'],
      ['Space', 'jump'],
      ['Scroll', 'step forward or back'],
    ]

  return (
    <div
      className={`${chrome.enter} pointer-events-none fixed bottom-24 left-1/2 z-30 -translate-x-1/2 md:bottom-20`}
      role="status"
    >
      <div className={`${chrome.bar} flex flex-wrap items-center justify-center gap-x-4 gap-y-1.5 px-4 py-2.5`}>
        {controls.map(([key, action]) => (
          <span key={key} className="flex items-center gap-1.5 text-xs text-white/70">
            <kbd className="rounded border border-white/20 bg-white/10 px-1.5 py-0.5 font-sans text-[11px] font-medium text-white/90">
              {key}
            </kbd>
            {action}
          </span>
        ))}
      </div>
    </div>
  )
}
