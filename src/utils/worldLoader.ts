import { ViewerQuality, type World, type WorldEntry } from '../types/world'

export function loadWorlds(): WorldEntry[] {
  // In Next.js, we pass initial worlds from Server Components, so default to empty
  return []
}

export async function fetchWorlds(): Promise<WorldEntry[]> {
  const response = await fetch('/api/worlds', { cache: 'no-store' })
  if (!response.ok) throw new Error(await response.text())
  return response.json() as Promise<WorldEntry[]>
}

function localWorldAssetUrl(url: string | undefined): string {
  return url?.startsWith('/worlds/') ? url : ''
}

export function getSplatUrl(world: World, quality: ViewerQuality = ViewerQuality.High): string {
  const urls = world.assets.splats.spz_urls
  const selectedUrl = quality === ViewerQuality.Low
    ? urls['100k'] || urls['150k'] || urls['500k'] || urls.full_res
    : urls.full_res || urls['500k'] || urls['150k'] || urls['100k']
  return localWorldAssetUrl(selectedUrl)
}
