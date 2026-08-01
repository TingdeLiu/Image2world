import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface AudioStore {
  muted: boolean
  setMuted: (v: boolean) => void
  toggleMuted: () => void
}

export const useAudioStore = create<AudioStore>()(
  persist(
    (set, get) => ({
      muted: false,
      setMuted: (muted) => set({ muted }),
      toggleMuted: () => set({ muted: !get().muted }),
    }),
    { name: 'imageworld-audio', skipHydration: true },
  ),
)

let audioHydration: Promise<void> | undefined

export function hydrateAudioStore() {
  if (typeof window === 'undefined') return Promise.resolve()
  audioHydration ??= Promise.resolve(useAudioStore.persist.rehydrate())
  return audioHydration
}
