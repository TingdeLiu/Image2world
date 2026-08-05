import { readWorlds } from '../utils/worldsScanner'
import { HomeLanding } from '../components/HomeLanding'

export const dynamic = 'force-dynamic'

export default function Home() {
  const worlds = readWorlds()
  const featured = worlds[0]
  const latestWorldVersion = featured?.worldVersions[featured.worldVersions.length - 1]

  return (
    <HomeLanding
      featuredWorld={featured ? {
        slug: featured.slug,
        name: featured.project.display_name || featured.slug,
        imageUrl: latestWorldVersion?.plateImageUrl || featured.world?.assets.thumbnail_url || featured.sourceImageUrl,
      } : undefined}
    />
  )
}
