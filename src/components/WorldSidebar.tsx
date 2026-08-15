'use client'

import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Tooltip } from '@radix-ui/themes'
import {
  ArrowCounterClockwise,
  Butterfly as ButterflyIcon,
  Check,
  CheckSquare as CheckSquareIcon,
  FileText as FileTextIcon,
  FolderOpen as FolderOpenIcon,
  GlobeHemisphereWest as GlobeHemisphereWestIcon,
  ImageSquare as ImageSquareIcon,
  List as ListIcon,
  PencilSimple as PencilSimpleIcon,
  Person as PersonIcon,
  QuestionMark as QuestionMarkIcon,
  ShareNetwork,
  SpeakerHigh,
  SpeakerSlash,
  Square as SquareIcon,
  TerminalWindow as TerminalWindowIcon,
  Plus,
  Trash as TrashIcon,
} from '@phosphor-icons/react'
import { useRouter } from 'next/navigation'
import type { WorldEntry, WorldHoverPreview, WorldObjectAsset, WorldSceneProject } from '../types/world'
import { type ControllerMode, useDebugStore } from '../store/debug'
import { useAudioStore } from '../store/audio'
import { LOCAL_TOOLS_VISIBLE } from '../utils/localTools'
import { APP_REQUEST_HEADERS } from '../utils/sameOrigin'
import { REPO_URL } from '../utils/links'
import { ViewerQuality } from '../types/world'
import { AppButton } from './AppButton'
import { ChromeThumbnail, chrome } from './AppChrome'
import { CreateWorldModal } from './CreateWorldModal'

const CONTROLLER_MODES: readonly { mode: ControllerMode; label: string }[] = [
  { mode: 'fly', label: 'Fly' },
  { mode: 'fps', label: 'FPS' },
]

const QUALITY_MODES = [
  { mode: ViewerQuality.Low, label: 'Low' },
  { mode: ViewerQuality.High, label: 'High' },
] as const

function nextMode<T>(items: readonly { mode: T }[], current: T) {
  const index = items.findIndex((item) => item.mode === current)
  return items[(index + 1) % items.length].mode
}

interface Props {
  worlds: WorldEntry[]
  activeSlug: string
  compact?: boolean
  activeSceneProject?: WorldSceneProject
  activeSceneProjectEnabled: boolean
  onActiveSceneProjectToggle: () => void
  activeWorldVersionIndex?: number
  hoveredObjectAssetId?: string | null
  hoveredObjectInstanceId?: string | null
  onObjectHover?: (asset: WorldObjectAsset, hovering: boolean, instanceId?: string) => void
  onWorldHover?: (preview: WorldHoverPreview, hovering: boolean) => void
  onActiveWorldVersionChange: (index: number) => void
  /** Re-read the library after it changes on disk. */
  onWorldsChanged?: () => void | Promise<void>
}


function worldAssetExtension(world: WorldEntry['world']) {
  const assetUrl = world && Object.values(world.assets.splats.spz_urls).find(Boolean)
  const extension = assetUrl?.split(/[?#]/, 1)[0].match(/\.[a-z0-9]+$/i)?.[0]
  return extension?.toLowerCase() || '.splat'
}


export function WorldSidebar({
  worlds,
  activeSlug,
  compact = false,
  activeSceneProject,
  activeSceneProjectEnabled,
  onActiveSceneProjectToggle,
  activeWorldVersionIndex,
  hoveredObjectAssetId,
  hoveredObjectInstanceId,
  onObjectHover,
  onWorldHover,
  onActiveWorldVersionChange,
  onWorldsChanged,
}: Props) {
  const router = useRouter()
  const [menuOpen, setMenuOpen] = useState(false)
  const [shareComplete, setShareComplete] = useState(false)
  const butterfliesEnabled = useDebugStore((s) => s.butterfliesEnabled)
  const setButterfliesEnabled = useDebugStore((s) => s.setButterfliesEnabled)
  // These open a file manager or terminal on whichever machine runs the server,
  // so they follow the same gate as the routes behind them.
  const canOpenLocalFolders = LOCAL_TOOLS_VISIBLE

  const [createModalOpen, setCreateModalOpen] = useState(false)

  /**
   * Right-clicking a world offers to delete it.
   *
   * Two steps rather than one, because this is not undoable and a world is
   * hundreds of megabytes that may have cost real money to generate. The
   * confirm names the world, so a mis-aimed right click has to be confirmed
   * against something the user can read.
   */
  const [worldMenu, setWorldMenu] = useState<{ slug: string; x: number; y: number } | null>(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [menuBusy, setMenuBusy] = useState(false)
  const [menuError, setMenuError] = useState<string | null>(null)

  const closeWorldMenu = () => {
    setWorldMenu(null)
    setConfirmingDelete(false)
    setRenaming(false)
    setMenuError(null)
  }

  useEffect(() => {
    if (!worldMenu) return
    const onKeyDown = (event: KeyboardEvent) => {
      // Same reason the backdrop ignores clicks mid-flight: closing here would
      // hide a delete that is still running, and its error if it fails.
      if (event.key === 'Escape' && !menuBusy) closeWorldMenu()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [worldMenu, menuBusy])

  const deleteWorld = async (slug: string) => {
    if (menuBusy) return
    setMenuBusy(true)
    setMenuError(null)
    try {
      const response = await fetch(`/api/worlds?slug=${encodeURIComponent(slug)}`, {
        method: 'DELETE',
        headers: APP_REQUEST_HEADERS,
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.error || `Could not delete this world (HTTP ${response.status}).`)
      }
      closeWorldMenu()
      // Leaving the user standing in a world that no longer exists would load a
      // broken page, so move to a neighbour first and only then re-read.
      if (slug === activeSlug) {
        const next = worlds.find((world) => world.slug !== slug)
        router.push(next ? `/${next.slug}` : '/')
      }
      // Not `router.refresh()`: the library lives in `LoadedApp`'s state, seeded
      // from props once, so refreshing the server payload leaves the list alone.
      await onWorldsChanged?.()
    } catch (error) {
      setMenuError(error instanceof Error ? error.message : 'Could not delete this world.')
    } finally {
      setMenuBusy(false)
    }
  }

  const renameWorld = async (slug: string, name: string) => {
    const wanted = name.trim()
    if (!wanted || menuBusy) return
    setMenuBusy(true)
    setMenuError(null)
    try {
      const response = await fetch(`/api/worlds?slug=${encodeURIComponent(slug)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...APP_REQUEST_HEADERS },
        body: JSON.stringify({ name: wanted }),
      })
      const body = await response.json().catch(() => ({})) as { slug?: string; error?: string }
      if (!response.ok) {
        throw new Error(body.error || `Could not rename this world (HTTP ${response.status}).`)
      }
      closeWorldMenu()
      // The slug is the URL, so renaming the world the user is standing in
      // moves them; staying put would ask the router for a world that is gone.
      if (slug === activeSlug && body.slug && body.slug !== slug) {
        router.push(`/${body.slug}`)
      }
      await onWorldsChanged?.()
    } catch (error) {
      setMenuError(error instanceof Error ? error.message : 'Could not rename this world.')
    } finally {
      setMenuBusy(false)
    }
  }

  const shareWorld = async () => {
    const activeWorld = worlds.find((world) => world.slug === activeSlug)
    const title = activeWorld?.project.display_name || activeSlug
    const shareData = {
      title: `${title} · Image2World`,
      text: `Explore “${title}”, an interactive 3D world made from a single image.`,
      url: window.location.href,
    }

    try {
      if (navigator.share) {
        await navigator.share(shareData)
      } else {
        await navigator.clipboard.writeText(window.location.href)
      }
      setShareComplete(true)
      window.setTimeout(() => setShareComplete(false), 1800)
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      console.warn('Could not share this world.', error)
    }
  }

  const menuWorldName = worldMenu
    ? worlds.find((world) => world.slug === worldMenu.slug)?.project.display_name ?? worldMenu.slug
    : ''

  const selectWorld = (slug: string) => {
    router.push(`/${slug}`)
    setMenuOpen(false)
  }

  const openClaudeTerminal = () => {
    fetch('/api/open-claude-terminal', { headers: APP_REQUEST_HEADERS }).catch((error) => {
      console.warn('Could not open Claude terminal.', error)
    })
  }

  const openWorldFolder = (slug: string) => {
    fetch(`/api/open-world-folder?slug=${encodeURIComponent(slug)}`, { headers: APP_REQUEST_HEADERS }).catch((error) => {
      console.warn(`Could not open world folder for "${slug}".`, error)
    })
  }

  const openAssetFolder = (slug: string, target: 'scene' | 'world-asset' | 'object-asset', asset?: string) => {
    const params = new URLSearchParams({ slug, target })
    if (asset) params.set('asset', asset)
    fetch(`/api/open-world-folder?${params.toString()}`, { headers: APP_REQUEST_HEADERS }).catch((error) => {
      console.warn(`Could not open ${target} folder for "${slug}".`, error)
    })
  }

  return (
    <aside className={`${chrome.enter} ${compact ? 'w-64 max-w-[calc(100vw-2rem)]' : 'w-full sm:w-64 max-h-[80vh]'} flex flex-col gap-1 whitespace-nowrap text-sm`}>
      <div className={`${chrome.bar} flex flex-shrink-0 items-center justify-between px-2 py-1 text-sm font-medium font-mono`}>
        <AppButton
          onClick={() => {
            if (!compact) setMenuOpen((open) => !open)
          }}
          className="min-w-0 flex-1 gap-2 px-1 truncate font-mono text-white opacity-100 hover:bg-transparent"
          aria-expanded={compact ? undefined : menuOpen}
        >
          {!compact && (
            <span className="text-white/60 sm:hidden flex items-center">
              <ListIcon size={16} weight="regular" />
            </span>
          )}
          <span>Image2World</span>{activeSlug && <span className="text-white/40 sm:hidden md:hidden">/ {activeSlug}</span>}
        </AppButton>
        <AppButton
          onClick={() => setButterfliesEnabled(!butterfliesEnabled)}
          active={butterfliesEnabled}
          className={`h-7 w-7 justify-center p-1 text-white ${butterfliesEnabled ? 'bg-white/15' : ''}`}
          aria-label={butterfliesEnabled ? 'Hide butterflies' : 'Show butterflies'}
          aria-pressed={butterfliesEnabled}
          title={butterfliesEnabled ? 'Hide butterflies' : 'Show butterflies'}
        >
          <ButterflyIcon size={16} weight={butterfliesEnabled ? 'fill' : 'regular'} />
        </AppButton>
        {!compact && (
          <AppButton
            onClick={shareWorld}
            className="h-7 w-7 justify-center p-1 text-white"
            aria-label={shareComplete ? 'World link copied' : 'Share this world'}
            title={shareComplete ? 'Link copied' : 'Share world'}
          >
            {shareComplete ? <Check size={16} weight="bold" className="text-[#e5ff63]" /> : <ShareNetwork size={16} />}
          </AppButton>
        )}
        <a
          href={REPO_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex h-7 w-7 items-center justify-center rounded p-1 text-white opacity-80 transition-[background-color,opacity] hover:bg-white/10 hover:opacity-100"
          aria-label="Open documentation"
        >
          <span className="text-sm leading-none"><QuestionMarkIcon size={16} weight="regular" /></span>
        </a>
        {canOpenLocalFolders && (
          <Tooltip
            content="Open new Claude terminal"
            delayDuration={0}
            side="right"
          >
            <AppButton
              onClick={openClaudeTerminal}
              className="h-7 w-7 justify-center p-1 text-white"
              aria-label="Open new Claude terminal"
              title="Open Claude terminal"
            >
              <TerminalWindowIcon size={16} weight="regular" />
            </AppButton>
          </Tooltip>
        )}
      </div>

      <SidebarViewerControls />

      {!compact && <div
        className={`
          ${chrome.panel} min-h-0 flex flex-1 flex-col gap-1 overflow-hidden p-1.5
          transition-[opacity,transform,max-height] duration-200 ease-out sm:max-h-[calc(90vh-3rem)] sm:translate-y-0 sm:opacity-100
          ${menuOpen ? 'max-h-[calc(90vh-3rem)] translate-y-0 opacity-100' : 'max-h-0 -translate-y-2 opacity-0 pointer-events-none sm:pointer-events-auto'}
        `}
      >
        <div className="w-full min-w-0 flex-1 overflow-y-auto overflow-x-hidden">
          <div className="flex min-w-0 flex-col gap-0 pr-1">
            <div className="mb-2 px-1">
              <AppButton
                onClick={() => setCreateModalOpen(true)}
                className="w-full justify-center gap-2 border border-white/10 hover:border-white/30 text-white font-mono bg-white/5 hover:bg-white/10 py-1.5 rounded transition-all flex items-center h-8"
              >
                <Plus size={14} weight="bold" />
                <span>Create New World</span>
              </AppButton>
            </div>
            {worlds.map(({ slug, project, world, worldVersions, objectAssets, sceneProject, sourceImageUrl }) => {
              const isActive = slug === activeSlug
              const name = project.display_name || slug
              const projectLoaded = isActive ? activeSceneProject : sceneProject
              const latestVersion = worldVersions[worldVersions.length - 1]
              const selectedVersionIndex = isActive ? activeWorldVersionIndex : latestVersion?.index
              const selectedVersion = worldVersions.find((version) => version.index === selectedVersionIndex) ?? latestVersion
              const displayWorld = selectedVersion?.world ?? world
              const hasSplatFile = Boolean(displayWorld && Object.values(displayWorld.assets.splats.spz_urls).some(Boolean))
              const splatExtension = worldAssetExtension(displayWorld)
              const hasWorldRow = Boolean(selectedVersion || hasSplatFile)
              const worldLoading = Boolean(selectedVersion && !selectedVersion.complete && isLoadingStatus(selectedVersion.status))
              const sourcePreview: WorldHoverPreview = {
                slug,
                imageUrl: sourceImageUrl,
                alt: `${name} source image`,
              }
              const activeWorldPreview: WorldHoverPreview = {
                slug,
                imageUrl: selectedVersion?.plateImageUrl,
                alt: `${name} world generation image`,
              }
              return (
                <div key={slug} className="rounded">
                  <div
                    className={`
                      min-w-0 flex items-center gap-1 rounded
                      ${isActive ? 'border-white/50 bg-white/20' : ''}
                    `}
                    onMouseEnter={() => {
                      if (!isActive) onWorldHover?.(sourcePreview, true)
                    }}
                    onMouseLeave={() => {
                      if (!isActive) onWorldHover?.(sourcePreview, false)
                    }}
                    onContextMenu={(event) => {
                      if (!canOpenLocalFolders) return
                      event.preventDefault()
                      setConfirmingDelete(false)
                      setRenaming(false)
                      setRenameValue(slug)
                      setMenuError(null)
                      setWorldMenu({ slug, x: event.clientX, y: event.clientY })
                    }}
                  >
                    <AppButton
                      onClick={() => selectWorld(slug)}
                      active={isActive}
                      className={`
                        min-w-0 flex flex-1 items-center rounded py-2 text-left
                        ${isActive ? 'hover:bg-transparent' : ''}
                      `}
                    >
                      <span className="block min-w-0 flex-1 truncate text-sm font-medium leading-tight text-white">{slug}</span>
                    </AppButton>
                    {isActive && (
                      <AppButton
                        onClick={() => {
                          router.push(`/${slug}/edit`)
                          setMenuOpen(false)
                        }}
                        className="h-8 w-8 flex-shrink-0 justify-center text-white"
                        aria-label={`Create or edit scene.json for ${name}`}
                        title={`Create or edit scene.json for ${name}`}
                      >
                        <PencilSimpleIcon size={15} weight="regular" />
                      </AppButton>
                    )}
                    {canOpenLocalFolders && isActive && (
                      <Tooltip
                        content={`Open the local folder for ${name}`}
                        delayDuration={0}
                        side="right"
                      >
                        <AppButton
                          onClick={() => openWorldFolder(slug)}
                          className="h-8 w-8 flex-shrink-0 justify-center text-white"
                          aria-label={`Open local folder for ${name}`}
                          title={`Open local folder for ${name}`}
                        >
                          <FolderOpenIcon size={15} weight="regular" />
                        </AppButton>
                      </Tooltip>
                    )}
                  </div>

                  <div
                    className={`
                      overflow-hidden transition-all duration-300 ease-in-out
                      ${isActive ? 'max-h-[32rem]' : 'max-h-0'}
                    `}
                  >
                    <div className="mt-1 flex min-w-0 flex-col">
                      {isActive && projectLoaded && (
                        <div className="group flex min-w-0 items-center gap-1 rounded px-2 py-1 text-left text-white/80">
                          <span className="relative flex h-7 w-7 flex-shrink-0 items-center justify-center rounded bg-white/10 text-white/45 ring-1 ring-white/10">
                            <FileTextIcon size={14} weight="regular" />
                            <FileExtensionBadge extension=".json" />
                          </span>
                          <span className="min-w-0 flex-1 truncate text-xs font-medium leading-tight text-white/80">
                            scene.json
                          </span>
                          {canOpenLocalFolders && (
                            <AppButton
                              onClick={() => openAssetFolder(slug, 'scene')}
                              className="h-7 w-7 flex-shrink-0 justify-center p-1 text-white opacity-0 transition-opacity group-hover:opacity-90 focus-visible:opacity-100 hover:opacity-100"
                              aria-label={`Open world folder for ${name}`}
                              title={`Open world folder for ${name}`}
                            >
                              <FolderOpenIcon size={14} weight="regular" />
                            </AppButton>
                          )}
                          <AppButton
                            onClick={onActiveSceneProjectToggle}
                            active={activeSceneProjectEnabled}
                            className="h-6 w-6 flex-shrink-0 justify-center p-1 text-white/70"
                            aria-label={activeSceneProjectEnabled ? 'Disable scene.json' : 'Enable scene.json'}
                            aria-pressed={activeSceneProjectEnabled}
                            title={activeSceneProjectEnabled ? 'Disable scene.json' : 'Enable scene.json'}
                          >
                            {activeSceneProjectEnabled ? (
                              <CheckSquareIcon size={14} weight="bold" />
                            ) : (
                              <SquareIcon size={14} weight="regular" />
                            )}
                          </AppButton>
                        </div>
                      )}
                      {hasWorldRow && (
                        <div
                          className="group flex min-w-0 items-center gap-1 rounded"
                          onMouseEnter={() => onWorldHover?.(activeWorldPreview, true)}
                          onMouseLeave={() => onWorldHover?.(activeWorldPreview, false)}
                        >
                          <div className="min-w-0 flex flex-1 items-center gap-2 rounded px-2 py-1 text-left text-white opacity-80">
                            <span className="relative flex h-7 w-7 flex-shrink-0">
                              {activeWorldPreview.imageUrl ? (
                                <ChromeThumbnail thumbnailUrl={activeWorldPreview.imageUrl} alt={activeWorldPreview.alt} />
                              ) : (
                                <span className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded bg-white/10 text-white/45 ring-1 ring-white/10">
                                  <GlobeHemisphereWestIcon size={14} weight="regular" />
                                </span>
                              )}
                              {hasSplatFile && <FileExtensionBadge extension={splatExtension} />}
                            </span>
                            <span className="min-w-0 flex-1 text-white/85 text-xs font-semibold leading-tight truncate">
                              {slug}
                            </span>
                            {worldLoading && <LoadingSpinner />}
                            {isActive && worldVersions.length > 1 && selectedVersion && (
                              <select
                                value={selectedVersion.index}
                                className="h-5 flex-shrink-0 rounded border border-white/10 bg-white/5 px-1 text-[10px] leading-none text-white/60"
                                aria-label={`Select world version for ${name}`}
                                onChange={(event) => onActiveWorldVersionChange(Number(event.target.value))}
                              >
                                {worldVersions.map((version) => (
                                  <option key={version.index} value={version.index}>
                                    {version.label}
                                  </option>
                                ))}
                              </select>
                            )}
                            {!isActive && worldVersions.length > 1 && (
                              <span className="flex-shrink-0 rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[10px] leading-none text-white/45">
                                {worldVersions.length}
                              </span>
                            )}
                          </div>
                          {canOpenLocalFolders && (
                            <AppButton
                              onClick={() => openAssetFolder(slug, 'world-asset')}
                              className="h-7 w-7 flex-shrink-0 justify-center p-1 text-white opacity-0 transition-opacity group-hover:opacity-90 focus-visible:opacity-100 hover:opacity-100"
                              aria-label={`Open world asset folder for ${name}`}
                              title={`Open world asset folder for ${name}`}
                            >
                              <FolderOpenIcon size={14} weight="regular" />
                            </AppButton>
                          )}
                        </div>
                      )}
                      {objectAssets.map((obj) => {
                        const objectLoading = !obj.complete && isLoadingStatus(obj.status)
                        const objectPreview: WorldHoverPreview = {
                          slug: obj.assetId,
                          imageUrl: obj.referenceImageUrl ?? obj.thumbnailUrl,
                          alt: `${obj.name} reference image`,
                        }
                        return (
                          <div
                            key={obj.assetId}
                            className={`flex min-w-0 items-center gap-2 rounded px-2 py-1 text-left group ${
                              hoveredObjectAssetId === obj.assetId && !hoveredObjectInstanceId ? 'bg-white/10 opacity-100' : ''
                            } ${obj.complete ? '' : 'opacity-80'}`}
                            onMouseEnter={() => {
                              if (obj.complete) {
                                onObjectHover?.(obj, true)
                              } else {
                                onWorldHover?.(objectPreview, true)
                              }
                            }}
                            onMouseLeave={() => {
                              if (obj.complete) {
                                onObjectHover?.(obj, false)
                              } else {
                                onWorldHover?.(objectPreview, false)
                              }
                            }}
                          >
                            <span className="relative flex h-7 w-7 flex-shrink-0">
                              <ChromeThumbnail thumbnailUrl={obj.thumbnailUrl ?? obj.referenceImageUrl} alt={obj.name} />
                              {obj.complete && <FileExtensionBadge extension=".obj" />}
                            </span>
                            <span className="min-w-0 flex-1">
                              <span className="block text-white/80 text-xs font-medium leading-tight truncate">
                                {obj.name}
                              </span>
                            </span>
                            {objectLoading ? (
                              <LoadingSpinner />
                            ) : obj.index !== undefined && obj.index > 0 && obj.variantLabel && (
                              <span className="flex-shrink-0 rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[10px] leading-none text-white/50">
                                {obj.variantLabel}
                              </span>
                            )}
                            {canOpenLocalFolders && (
                              <AppButton
                                onClick={() => openAssetFolder(slug, 'object-asset', obj.baseObjectId)}
                                className="h-7 w-7 flex-shrink-0 justify-center p-1 text-white opacity-0 transition-opacity group-hover:opacity-90 focus-visible:opacity-100 hover:opacity-100"
                                aria-label={`Open asset folder for ${obj.name}`}
                                title={`Open asset folder for ${obj.name}`}
                              >
                                <FolderOpenIcon size={14} weight="regular" />
                              </AppButton>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      </div>}

      <CreateWorldModal open={createModalOpen} onClose={() => setCreateModalOpen(false)} />
      {worldMenu && createPortal((
        <div
          className="fixed inset-0 z-[60]"
          onPointerDown={() => {
            // Dismissing mid-flight would take the spinner and any error
            // message with it, leaving a delete or rename running unseen.
            if (!menuBusy) closeWorldMenu()
          }}
          onContextMenu={(event) => {
            event.preventDefault()
            if (!menuBusy) closeWorldMenu()
          }}
        >
          <div
            role="menu"
            aria-label={`Actions for ${menuWorldName}`}
            className={`${chrome.panel} absolute w-60 p-1 text-sm`}
            style={{
              left: Math.min(worldMenu.x, (typeof window === 'undefined' ? 0 : window.innerWidth) - 252),
              top: Math.min(worldMenu.y, (typeof window === 'undefined' ? 0 : window.innerHeight) - 210),
            }}
            onPointerDown={(event) => event.stopPropagation()}
          >
            <div className="truncate px-2 py-1 font-mono text-xs text-white/50">{menuWorldName}</div>

            {renaming ? (
              <div className="flex flex-col gap-1 px-2 py-1">
                <input
                  autoFocus
                  value={renameValue}
                  onChange={(event) => setRenameValue(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== 'Enter' || menuBusy) return
                    event.preventDefault()
                    void renameWorld(worldMenu.slug, renameValue)
                  }}
                  aria-label="New world name"
                  className="w-full rounded border border-white/20 bg-black/40 px-2 py-1 text-sm text-white outline-none focus:border-white/50"
                />
                <p className="whitespace-normal text-[11px] leading-snug text-white/45">
                  This is the folder name and the address, so the link to this world changes.
                </p>
                <div className="mt-1 flex gap-1">
                  <AppButton
                    onClick={() => void renameWorld(worldMenu.slug, renameValue)}
                    disabled={menuBusy}
                    className="flex-1 justify-center rounded bg-white/15 py-1.5 text-white hover:bg-white/25 disabled:opacity-60"
                  >
                    {menuBusy ? <LoadingSpinner /> : 'Rename'}
                  </AppButton>
                  <AppButton onClick={closeWorldMenu} disabled={menuBusy} className="flex-1 justify-center rounded py-1.5 text-white disabled:opacity-60">
                    Cancel
                  </AppButton>
                </div>
              </div>
            ) : confirmingDelete ? (
              <div className="flex flex-col gap-1 px-2 py-1">
                <p className="whitespace-normal text-xs leading-snug text-white/75">
                  Delete this world and everything in it? The files are removed from disk and
                  cannot be recovered.
                </p>
                <div className="mt-1 flex gap-1">
                  <AppButton
                    onClick={() => void deleteWorld(worldMenu.slug)}
                    disabled={menuBusy}
                    className="flex-1 justify-center rounded bg-red-500/25 py-1.5 text-white hover:bg-red-500/40 disabled:opacity-60"
                  >
                    {menuBusy ? <LoadingSpinner /> : 'Delete'}
                  </AppButton>
                  <AppButton onClick={closeWorldMenu} disabled={menuBusy} className="flex-1 justify-center rounded py-1.5 text-white disabled:opacity-60">
                    Cancel
                  </AppButton>
                </div>
              </div>
            ) : (
              <>
                <AppButton
                  onClick={() => {
                    setRenameValue(worldMenu.slug)
                    setRenaming(true)
                  }}
                  className="w-full justify-start gap-2 rounded px-2 py-2 text-left text-white hover:bg-white/10"
                >
                  <PencilSimpleIcon size={15} weight="regular" />
                  <span>Rename</span>
                </AppButton>
                <AppButton
                  onClick={() => setConfirmingDelete(true)}
                  className="w-full justify-start gap-2 rounded px-2 py-2 text-left text-white hover:bg-red-500/25"
                >
                  <TrashIcon size={15} weight="regular" />
                  <span>Delete world</span>
                </AppButton>
              </>
            )}

            {menuError && (
              <p className="whitespace-normal px-2 py-1 text-xs leading-snug text-red-300">{menuError}</p>
            )}
          </div>
        </div>
      ), document.body)}

    </aside>
  )
}

function SidebarViewerControls() {
  const muted = useAudioStore((s) => s.muted)
  const toggleMuted = useAudioStore((s) => s.toggleMuted)
  const resetObjects = useDebugStore((s) => s.resetObjects)
  const controllerMode = useDebugStore((s) => s.controllerMode)
  const setControllerMode = useDebugStore((s) => s.setControllerMode)
  const viewerQuality = useDebugStore((s) => s.viewerQuality)
  const setViewerQuality = useDebugStore((s) => s.setViewerQuality)
  const currentControllerMode = CONTROLLER_MODES.find((item) => item.mode === controllerMode) ?? CONTROLLER_MODES[0]
  const currentQuality = QUALITY_MODES.find((item) => item.mode === viewerQuality) ?? QUALITY_MODES[0]

  return (
    <div className={`${chrome.bar} flex flex-shrink-0 items-center gap-1 text-sm font-medium font-mono`}>
      <Tooltip content="Reset" delayDuration={0} side="right">
        <AppButton
          onClick={resetObjects}
          className="h-7 w-7 justify-center p-1 text-white"
          aria-label="Reset"
          title="Reset"
        >
          <ArrowCounterClockwise size={16} weight="bold" />
        </AppButton>
      </Tooltip>
      <Tooltip content={muted ? 'Unmute' : 'Mute'} delayDuration={0} side="right">
        <AppButton
          onClick={toggleMuted}
          className="h-7 w-7 justify-center p-1 text-white"
          aria-label={muted ? 'Unmute' : 'Mute'}
          title={muted ? 'Unmute' : 'Mute'}
        >
          {muted ? <SpeakerSlash size={16} weight="fill" /> : <SpeakerHigh size={16} weight="fill" />}
        </AppButton>
      </Tooltip>
      <Tooltip content="Change controller" delayDuration={0} side="right">
        <AppButton
          onClick={() => setControllerMode(nextMode(CONTROLLER_MODES, controllerMode))}
          className="h-7 w-20 justify-center gap-1 flex border flex-1 border-white/20 text-white"
          aria-label="Change controller"
          title="Change controller"
        >
          <span className="flex-shrink-0 text-white/45 flex items-center">
            <PersonIcon size={15} weight="fill" />
          </span>
          <span>{currentControllerMode.label}</span>
        </AppButton>
      </Tooltip>
      <Tooltip content="Change quality" delayDuration={0} side="right">
        <AppButton
          onClick={() => setViewerQuality(nextMode(QUALITY_MODES, viewerQuality))}
          className="h-7 w-16 justify-center gap-1 flex border flex-1 border-white/20 text-white"
          aria-label="Change quality"
          title="Change quality"
        >
          <span className="flex-shrink-0 text-white/45 flex items-center">
            <ImageSquareIcon size={15} weight="fill" />
          </span>
          <span>{currentQuality.label}</span>
        </AppButton>
      </Tooltip>
    </div>
  )
}

function FileExtensionBadge({ extension }: { extension: string }) {
  return (
    <span className="pointer-events-none absolute bottom-0 right-0 rounded-sm bg-black/70 px-px font-mono text-[6px] font-semibold leading-[7px] text-white/70 ring-1 ring-white/10">
      {extension}
    </span>
  )
}

function LoadingSpinner() {
  return (
    <span className="pointer-events-none h-3 w-3 flex-shrink-0 rounded-full border border-white/25 border-t-white/90 animate-spin" />
  )
}

function isLoadingStatus(status?: string) {
  if (!status) return true
  return ['queued', 'submitted', 'running', 'processing', 'in_progress'].includes(status.toLowerCase())
}
