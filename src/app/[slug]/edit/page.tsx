import { notFound } from 'next/navigation'
import { readWorlds } from '../../../utils/worldsScanner'
import { currentViewer } from '../../../utils/serverSession'
import { LoadedApp } from '../../../components/LoadedApp'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ slug: string }>
}

export default async function Page({ params }: PageProps) {
  const { slug } = await params
  const worlds = readWorlds(await currentViewer())
  
  const entry = worlds.find((w) => w.slug === slug)
  if (!entry) {
    notFound()
  }

  return (
    <LoadedApp
      worlds={worlds}
      slug={slug}
      editing={true}
    />
  )
}
