'use client'

import { useState } from 'react'
import { GlobeHemisphereWest as GlobeHemisphereWestIcon, Plus } from '@phosphor-icons/react'
import { AppButton } from './AppButton'
import { CreateWorldModal } from './CreateWorldModal'

/**
 * Shown when `public/worlds/` holds no projects — a fresh clone, or after the
 * example worlds are removed. Without a world there is no `/[slug]` route to
 * render the sidebar, so this screen carries its own entry point into the
 * generation flow.
 */
export function EmptyWorldsScreen() {
  const [createModalOpen, setCreateModalOpen] = useState(false)

  return (
    <div className="flex h-screen items-center justify-center bg-black p-6 font-mono text-white">
      <div className="flex w-full max-w-md flex-col items-center gap-5 text-center">
        <GlobeHemisphereWestIcon size={40} className="text-white/40" />

        <div className="flex flex-col gap-2">
          <h1 className="text-2xl font-bold">No worlds yet</h1>
          <p className="text-sm leading-relaxed text-white/50">
            Generate one from a single image, or drop an existing project
            directory into <code className="rounded bg-white/10 px-1 py-0.5">public/worlds/</code>.
          </p>
        </div>

        <AppButton
          onClick={() => setCreateModalOpen(true)}
          className="flex h-9 items-center justify-center gap-2 rounded border border-white/10 bg-white/5 px-4 font-mono text-xs text-white transition-all hover:border-white/30 hover:bg-white/10"
        >
          <Plus size={14} weight="bold" />
          <span>Create New World</span>
        </AppButton>

        <p className="text-[10px] leading-relaxed text-white/30">
          Generation needs the local AI backend running on port 8000.
          See backend/README.md for setup.
        </p>
      </div>

      <CreateWorldModal open={createModalOpen} onClose={() => setCreateModalOpen(false)} />
    </div>
  )
}
