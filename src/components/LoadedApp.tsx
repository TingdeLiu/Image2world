'use client'

import { Component, Suspense, lazy, useCallback, useEffect, useState, type ReactNode } from 'react'
import dynamic from 'next/dynamic'
import { usePathname } from 'next/navigation'
import type { AssetStreamState, WorldViewerProps } from './WorldViewer'
import { WorldSidebar } from './WorldSidebar'
import { BottomLeftControls, ViewerModeHotkeys } from './BottomLeftControls'
import { TouchControls } from './TouchControls'
import { useSceneProject } from '../modules/scene/useSceneProject'
import { hydrateDebugStore, useDebugStore } from '../store/debug'
import { hydrateAudioStore } from '../store/audio'
import { isEditableTarget } from '../utils/dom'
import type { WorldEntry, WorldHoverPreview, WorldObjectAsset } from '../types/world'
import { fetchWorlds } from '../utils/worldLoader'
import { TerminalWindow as TerminalWindowIcon } from '@phosphor-icons/react'

const WorldViewer = dynamic<WorldViewerProps>(
  () => import('./WorldViewer').then((module) => module.WorldViewer),
  { ssr: false, loading: () => null },
)
const LevaPanel = process.env.NODE_ENV === 'development'
  ? lazy(() => import('leva').then((module) => ({ default: module.Leva })))
  : null
const DebugPanel = process.env.NODE_ENV === 'development'
  ? lazy(() => import('./DebugPanel').then((module) => ({ default: module.DebugPanel })))
  : null

type ViewerLoadPhase = 'runtime' | 'scene' | 'ready' | 'error'

interface ViewerLoadState {
  phase: ViewerLoadPhase
  progress: number
  detail?: string
}

interface ViewerErrorBoundaryProps {
  resetKey: string
  onError: (error: unknown) => void
  children: ReactNode
}

class ViewerErrorBoundary extends Component<ViewerErrorBoundaryProps, { hasError: boolean }> {
  state = { hasError: false }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error: unknown) {
    this.props.onError(error)
  }

  componentDidUpdate(previousProps: ViewerErrorBoundaryProps) {
    if (previousProps.resetKey !== this.props.resetKey && this.state.hasError) {
      this.setState({ hasError: false })
    }
  }

  render() {
    return this.state.hasError ? null : this.props.children
  }
}

function ViewerLoadingOverlay({
  worldName,
  sourceImageUrl,
  loadState,
  onRetry,
}: {
  worldName: string
  sourceImageUrl?: string
  loadState: ViewerLoadState
  onRetry: () => void
}) {
  const ready = loadState.phase === 'ready'
  const failed = loadState.phase === 'error'
  const status = loadState.phase === 'runtime'
    ? 'Loading 3D runtime'
    : loadState.phase === 'scene'
      ? `Streaming scene · ${loadState.progress}%`
      : failed
        ? loadState.detail || 'Viewer could not be loaded'
        : 'Scene ready'

  return (
    <div
      role="status"
      aria-live="polite"
      aria-hidden={ready}
      className={`pointer-events-none fixed inset-0 z-[5] overflow-hidden bg-[#060707] transition-[opacity,visibility] duration-500 ${
        ready ? 'invisible opacity-0 delay-300' : 'visible opacity-100'
      }`}
    >
      {sourceImageUrl && (
        <div
          aria-hidden="true"
          className="absolute inset-[-8%] scale-110 bg-cover bg-center opacity-20 blur-2xl saturate-50"
          style={{ backgroundImage: `url(${sourceImageUrl})` }}
        />
      )}
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-[radial-gradient(circle_at_50%_42%,rgba(255,255,255,0.08),transparent_34%),linear-gradient(180deg,rgba(0,0,0,0.12),rgba(0,0,0,0.82))]"
      />
      <div className="absolute inset-0 opacity-[0.045] [background-image:linear-gradient(rgba(255,255,255,.8)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.8)_1px,transparent_1px)] [background-size:48px_48px]" />

      <div className="relative flex h-full items-center justify-center px-6">
        <div className="w-full max-w-sm">
          <div className="mb-8 flex items-center gap-3 font-mono text-[10px] uppercase tracking-[0.28em] text-white/45">
            <span className="h-1.5 w-1.5 rounded-full bg-white/60" />
            ImageWorld / scene loader
          </div>
          <p className="mb-2 font-mono text-[11px] uppercase tracking-[0.2em] text-white/35">
            Opening world
          </p>
          <h1 className="truncate text-2xl font-medium tracking-[-0.03em] text-white/90">
            {worldName}
          </h1>
          <div
            role="progressbar"
            aria-label="Scene loading progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={loadState.progress}
            className="mt-7 h-px overflow-hidden bg-white/15"
          >
            <div
              className={`h-full bg-white/80 transition-[width] duration-300 ${failed ? 'bg-red-300/80' : ''}`}
              style={{ width: `${Math.max(loadState.progress, failed ? 100 : 4)}%` }}
            />
          </div>
          <div className="mt-3 flex items-center justify-between gap-4 font-mono text-[10px] uppercase tracking-[0.14em] text-white/45">
            <span className={failed ? 'normal-case tracking-normal text-red-200/80' : ''}>{status}</span>
            {!failed && <span>{String(loadState.progress).padStart(2, '0')}</span>}
          </div>
          {failed && (
            <button
              type="button"
              onClick={onRetry}
              className="pointer-events-auto mt-6 border border-white/20 bg-white/5 px-4 py-2 font-mono text-[10px] uppercase tracking-[0.16em] text-white/75 transition hover:border-white/40 hover:bg-white/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
            >
              Retry viewer
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

function streamItemLabel(item: string) {
  if (!item) return 'Preparing scene resources'
  const cleanItem = item.split(/[?#]/, 1)[0]
  const fileName = cleanItem.split('/').filter(Boolean).at(-1)
  if (!fileName) return 'Streaming scene resources'
  try {
    return `Loading ${decodeURIComponent(fileName)}`
  } catch {
    return `Loading ${fileName}`
  }
}

function AssetStreamIndicator({
  state,
  visible,
}: {
  state: AssetStreamState
  visible: boolean
}) {
  return (
    <div
      aria-live="polite"
      aria-hidden={!visible}
      className={`pointer-events-none fixed right-4 top-28 z-10 w-[min(17rem,calc(100vw-2rem))] rounded border border-white/10 bg-[#111212]/90 px-3 py-2.5 shadow-[0_12px_40px_rgba(0,0,0,0.35)] backdrop-blur-md transition-[opacity,transform,visibility] duration-300 sm:top-4 ${
        visible ? 'visible translate-y-0 opacity-100' : 'invisible -translate-y-1 opacity-0'
      }`}
    >
      <div className="flex items-center gap-2">
        <span className="relative flex h-2 w-2 flex-none">
          <span className="absolute inset-0 animate-ping rounded-full bg-white/35" />
          <span className="relative m-0.5 h-1 w-1 rounded-full bg-white/75" />
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[10px] tracking-[0.02em] text-white/60">
          {streamItemLabel(state.item)}
        </span>
        <span className="font-mono text-[9px] tabular-nums text-white/35">
          {state.total > 0 ? `${state.loaded}/${state.total}` : `${state.progress}%`}
        </span>
      </div>
      <div className="mt-2 h-px overflow-hidden bg-white/10">
        <div
          className="h-full bg-white/55 transition-[width] duration-300"
          style={{ width: `${Math.min(100, Math.max(3, state.progress))}%` }}
        />
      </div>
    </div>
  )
}

export function LoadedApp({
  worlds: initialWorlds,
  slug,
  editing = false,
}: {
  worlds: WorldEntry[]
  slug: string
  editing?: boolean
}) {
  const [worlds, setWorlds] = useState<WorldEntry[]>(initialWorlds)
  const [refreshingWorlds, setRefreshingWorlds] = useState(false)
  const levaCollapsed = useDebugStore((s) => s.levaCollapsed)
  const setLevaCollapsed = useDebugStore((s) => s.setLevaCollapsed)
  const viewerQuality = useDebugStore((s) => s.viewerQuality)
  const pathname = usePathname()
  const [uiHidden, setUiHidden] = useState(false)
  const [sceneProjectEnabled, setSceneProjectEnabled] = useState(true)
  const [selectedWorldVersions, setSelectedWorldVersions] = useState<Record<string, number>>({})
  const [hoveredObjectAssetId, setHoveredObjectAssetId] = useState<string | null>(null)
  const [hoveredObjectInstanceId, setHoveredObjectInstanceId] = useState<string | null>(null)
  const [hoveredWorldPreview, setHoveredWorldPreview] = useState<WorldHoverPreview | null>(null)
  const [viewerRetryToken, setViewerRetryToken] = useState(0)
  const [viewerLoadState, setViewerLoadState] = useState<ViewerLoadState>({
    phase: 'runtime',
    progress: 6,
  })
  const [assetStreamState, setAssetStreamState] = useState<AssetStreamState>({
    active: false,
    progress: 0,
    loaded: 0,
    total: 0,
    item: '',
  })

  const refreshWorlds = useCallback(async () => {
    setRefreshingWorlds(true)
    try {
      const updated = await fetchWorlds()
      setWorlds(updated)
    } catch (error) {
      console.warn('Could not refresh local world assets.', error)
    } finally {
      setRefreshingWorlds(false)
    }
  }, [])

  // Sync state if initialWorlds changes (e.g., when refetched/refreshed)
  useEffect(() => {
    setWorlds(initialWorlds)
  }, [initialWorlds])

  useEffect(() => {
    void Promise.all([hydrateDebugStore(), hydrateAudioStore()])
  }, [])

  const entry = worlds.find((w) => w.slug === slug) ?? worlds[0]
  const showLeva = process.env.NEXT_PUBLIC_SHOW_LEVA === 'true' || process.env.NODE_ENV === 'development'
  const uiVisible = !uiHidden
  const defaultWorldVersionIndex = entry?.worldVersions[entry.worldVersions.length - 1]?.index
  const activeWorldVersionIndex = selectedWorldVersions[entry?.slug] ?? defaultWorldVersionIndex
  const activeWorldVersion = entry?.worldVersions.find((version) => version.index === activeWorldVersionIndex)
  const activeWorld = activeWorldVersion?.world ?? entry?.world
  const renderableObjectAssets = entry?.objectAssets.filter((asset) => asset.complete && asset.url) ?? []
  const renderableAllObjectAssets = entry?.allObjectAssets.filter((asset) => asset.complete && asset.url) ?? []
  const hasSidebarWorldRow = Boolean(activeWorldVersion || (activeWorld && Object.values(activeWorld.assets.splats.spz_urls).some(Boolean)))
  const emptyWorld = !hasSidebarWorldRow && !entry?.objectAssets.length
  const viewerLoadKey = `${entry?.slug ?? slug}:${activeWorldVersionIndex ?? 'default'}:${viewerQuality}:${viewerRetryToken}`
  
  // Use pathname as refreshKey to trigger re-fetch on route transitions
  const { sceneProject, sceneProjectReady, updateSceneProject } = useSceneProject(
    entry?.slug ?? slug,
    pathname || '',
    entry?.sceneProject
  )
  const sceneProjectActive = Boolean(sceneProject && sceneProjectEnabled)

  useEffect(() => {
    setSceneProjectEnabled(true)
    setHoveredObjectAssetId(null)
    setHoveredObjectInstanceId(null)
    setHoveredWorldPreview(null)
  }, [slug])

  useEffect(() => {
    setViewerLoadState({ phase: 'runtime', progress: 6 })
    setAssetStreamState({ active: false, progress: 0, loaded: 0, total: 0, item: '' })
  }, [viewerLoadKey])

  const handleObjectHover = useCallback((asset: WorldObjectAsset, hovering: boolean, instanceId?: string) => {
    setHoveredObjectAssetId((current) => {
      if (hovering) return asset.assetId
      return current === asset.assetId ? null : current
    })
    setHoveredObjectInstanceId((current) => {
      if (hovering) return instanceId ?? null
      return current === instanceId ? null : current
    })
  }, [])

  const handleWorldHover = useCallback((preview: WorldHoverPreview, hovering: boolean) => {
    setHoveredWorldPreview((current) => {
      if (hovering) return preview
      return current?.slug === preview.slug ? null : current
    })
  }, [])

  const handleCanvasReady = useCallback(() => {
    setViewerLoadState((current) => (
      current.phase === 'error'
        ? current
        : { phase: 'scene', progress: Math.max(18, current.progress) }
    ))
  }, [])

  const handleSceneProgress = useCallback((progress: number) => {
    const percent = Math.round(18 + progress * 78)
    setViewerLoadState((current) => (
      current.phase === 'error' || current.phase === 'ready'
        ? current
        : { phase: 'scene', progress: Math.max(current.progress, percent) }
    ))
  }, [])

  const handleSceneReady = useCallback(() => {
    setViewerLoadState({ phase: 'ready', progress: 100 })
  }, [])

  const handleViewerError = useCallback((error: unknown) => {
    console.error('The 3D viewer failed to load.', error)
    setViewerLoadState({
      phase: 'error',
      progress: 100,
      detail: error instanceof Error ? error.message : 'Viewer could not be loaded',
    })
  }, [])

  const retryViewer = useCallback(() => {
    setViewerLoadState({ phase: 'runtime', progress: 6 })
    setViewerRetryToken((token) => token + 1)
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target)) return
      if (event.code !== 'Backquote') return
      event.preventDefault()
      setUiHidden((hidden) => !hidden)
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  if (!entry) {
    return (
      <div className="flex items-center justify-center h-screen text-white bg-black">
        World not found.
      </div>
    )
  }

  return (
    <div className="relative w-screen h-screen bg-black overflow-hidden select-none">
      <ViewerLoadingOverlay
        worldName={entry.project.display_name || entry.slug}
        sourceImageUrl={entry.sourceImageUrl}
        loadState={viewerLoadState}
        onRetry={retryViewer}
      />
      <AssetStreamIndicator
        state={assetStreamState}
        visible={viewerLoadState.phase === 'ready' && assetStreamState.active}
      />
      <ViewerModeHotkeys />
      {!editing && LevaPanel && DebugPanel && showLeva && uiVisible && (
        <div className="hidden md:block">
          <Suspense fallback={null}>
            <LevaPanel
              collapsed={{ collapsed: levaCollapsed, onChange: setLevaCollapsed }}
              theme={{ sizes: { rootWidth: '380px', controlWidth: '180px' } }}
            />
            <DebugPanel />
          </Suspense>
        </div>
      )}
      <ViewerErrorBoundary resetKey={viewerLoadKey} onError={handleViewerError}>
        <WorldViewer
          key={viewerLoadKey}
          world={activeWorld}
          slug={entry.slug}
          sourceImageUrl={entry.sourceImageUrl}
          hoveredWorldPreview={hoveredWorldPreview}
          objectAssets={renderableObjectAssets}
          allObjectAssets={renderableAllObjectAssets}
          worldSfxUrls={entry.worldSfxUrls}
          sceneProject={editing || sceneProjectEnabled ? sceneProject : undefined}
          sceneProjectReady={sceneProjectReady}
          hoveredObjectAssetId={hoveredObjectAssetId}
          hoveredObjectInstanceId={hoveredObjectInstanceId}
          editing={editing}
          uiVisible={uiVisible}
          onObjectHover={handleObjectHover}
          onSceneProjectSaved={updateSceneProject}
          onRefreshWorlds={refreshWorlds}
          refreshingWorlds={refreshingWorlds}
          onCanvasReady={handleCanvasReady}
          onSceneProgress={handleSceneProgress}
          onSceneReady={handleSceneReady}
          onSceneError={handleViewerError}
          onAssetStreamChange={setAssetStreamState}
        />
      </ViewerErrorBoundary>
      {!editing && uiVisible && emptyWorld && (
        <div className="pointer-events-none fixed inset-0 z-10 flex items-center justify-center px-6 gap-2">
          <div className="bg-black/25 rounded px-2 py-1 flex items-center gap-2">
            <span className="animate-pulse flex items-center">
              <TerminalWindowIcon size={18} weight="regular" />
            </span>
            <span className="truncate text-center font-mono text-sm text-white/75">
              waiting for objects and environment...
            </span>
          </div>
        </div>
      )}
      {uiVisible && (
        <div className={`fixed inset-x-4 top-4 sm:left-4 sm:right-auto ${editing ? 'z-30' : 'z-10'}`}>
          <WorldSidebar
            worlds={worlds}
            activeSlug={entry.slug}
            compact={editing}
            activeSceneProject={sceneProject}
            activeSceneProjectEnabled={sceneProjectActive}
            onActiveSceneProjectToggle={() => setSceneProjectEnabled((enabled) => !enabled)}
            activeWorldVersionIndex={activeWorldVersionIndex}
            hoveredObjectAssetId={hoveredObjectAssetId}
            hoveredObjectInstanceId={hoveredObjectInstanceId}
            onObjectHover={handleObjectHover}
            onWorldHover={handleWorldHover}
            onActiveWorldVersionChange={(index) => setSelectedWorldVersions((versions) => ({
              ...versions,
              [entry.slug]: index,
            }))}
          />
        </div>
      )}
      {!editing && uiVisible && (
        <>
          <TouchControls />
        </>
      )}
      {uiVisible && (
        <div className="fixed inset-x-0 bottom-4 z-20 flex justify-center px-4 sm:left-4 sm:right-auto sm:justify-start sm:px-0">
          <BottomLeftControls />
        </div>
      )}
    </div>
  )
}
