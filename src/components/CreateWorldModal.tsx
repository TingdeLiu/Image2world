'use client'

import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import NextImage from 'next/image'
import {
  CheckCircle,
  GlobeHemisphereWest as GlobeHemisphereWestIcon,
  Spinner,
  UploadSimple,
  WarningCircle,
  X,
} from '@phosphor-icons/react'
import { useRouter } from 'next/navigation'
import { AppButton } from './AppButton'

type SelectedMask = { mask: string; thumb: string }
type BackendStatus = 'checking' | 'ready' | 'offline'
type GenerationStage = 'initializing' | 'segmenting' | 'objects' | 'inpainting' | 'splat' | 'finalizing'

interface GenerationUiProgress {
  stage: GenerationStage
  progress: number
  message: string
  detail?: string
}

type GenerationStreamEvent =
  | ({ type: 'progress' } & GenerationUiProgress)
  | { type: 'complete'; success: true; slug: string }
  | {
      type: 'error'
      error: {
        code: string
        message: string
        stage: GenerationStage
        retryable: boolean
      }
    }

const INITIAL_GENERATION_PROGRESS: GenerationUiProgress = {
  stage: 'initializing',
  progress: 0,
  message: 'Preparing generation',
  detail: 'Waiting for the local pipeline',
}
const GENERATION_STAGE_ORDER: GenerationStage[] = [
  'initializing',
  'segmenting',
  'objects',
  'inpainting',
  'splat',
  'finalizing',
]

const MAX_IMAGE_BYTES = 10 * 1024 * 1024
const ACCEPTED_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp'])

function validateImage(file: File) {
  if (!ACCEPTED_IMAGE_TYPES.has(file.type)) return 'Choose a PNG, JPG, or WEBP image.'
  if (file.size > MAX_IMAGE_BYTES) return 'Image must be smaller than 10 MB.'
  return ''
}

async function responseErrorMessage(response: Response) {
  const contentType = response.headers.get('content-type') || ''
  if (contentType.includes('application/json')) {
    const payload = await response.json() as { error?: { message?: string } }
    return payload.error?.message || `Generation request failed with HTTP ${response.status}.`
  }
  const message = (await response.text()).trim()
  return message || `Generation request failed with HTTP ${response.status}.`
}

async function readGenerationStream(
  response: Response,
  onProgress: (progress: GenerationUiProgress) => void,
) {
  if (!response.body) throw new Error('The generation server did not return a progress stream.')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let completedSlug = ''

  const processLine = (line: string) => {
    if (!line.trim()) return
    const event = JSON.parse(line) as GenerationStreamEvent
    if (event.type === 'progress') {
      onProgress(event)
      return
    }
    if (event.type === 'complete') {
      completedSlug = event.slug
      return
    }
    throw new Error(event.error.message)
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      for (const line of lines) processLine(line)
      if (done) break
    }
    if (buffer.trim()) processLine(buffer)
  } finally {
    reader.releaseLock()
  }

  if (!completedSlug) throw new Error('The generation stream ended before the world was published.')
  return completedSlug
}

function friendlyFileName(file: File) {
  const nameWithoutExt = file.name.substring(0, file.name.lastIndexOf('.')) || file.name
  return nameWithoutExt
    .split(/[-_]+/)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

// Crop a 64x64 preview of a clicked object's bounding box from the source file.
async function cropThumb(file: File, bbox: number[]): Promise<string> {
  const [bx, by, bw, bh] = bbox
  const url = URL.createObjectURL(file)
  try {
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const im = new Image()
      im.onload = () => resolve(im)
      im.onerror = reject
      im.src = url
    })
    const size = 64
    const canvas = document.createElement('canvas')
    canvas.width = size
    canvas.height = size
    const ctx = canvas.getContext('2d')
    if (!ctx) return url
    const scale = Math.min(size / bw, size / bh)
    const dw = bw * scale
    const dh = bh * scale
    ctx.drawImage(img, bx, by, bw, bh, (size - dw) / 2, (size - dh) / 2, dw, dh)
    return canvas.toDataURL('image/png')
  } finally {
    URL.revokeObjectURL(url)
  }
}

interface Props {
  open: boolean
  /** Called once the modal is allowed to close; generation in flight blocks it. */
  onClose: () => void
}

export function CreateWorldModal({ open, onClose }: Props) {
  const router = useRouter()
  const [worldName, setWorldName] = useState('')
  const [concepts, setConcepts] = useState('')
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [selectedMasks, setSelectedMasks] = useState<SelectedMask[]>([])
  const [pointBusy, setPointBusy] = useState(false)
  const imgRef = useRef<HTMLImageElement>(null)
  const [dragActive, setDragActive] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [generationProgress, setGenerationProgress] = useState<GenerationUiProgress>(INITIAL_GENERATION_PROGRESS)
  const [generationError, setGenerationError] = useState('')
  const [fileError, setFileError] = useState('')
  const [pointError, setPointError] = useState('')
  const [backendStatus, setBackendStatus] = useState<BackendStatus>('checking')
  const [backendMessage, setBackendMessage] = useState('')
  const [selectedFileUrl, setSelectedFileUrl] = useState('')
  const generationAbortRef = useRef<AbortController | null>(null)
  const pointAbortRef = useRef<AbortController | null>(null)
  const modalTitleId = useId()

  const resetCreateForm = useCallback(() => {
    setSelectedFile(null)
    setWorldName('')
    setConcepts('')
    setSelectedMasks([])
    setGenerationError('')
    setGenerationProgress(INITIAL_GENERATION_PROGRESS)
    setFileError('')
    setPointError('')
  }, [])

  const closeCreateModal = useCallback(() => {
    if (generating) return
    pointAbortRef.current?.abort()
    onClose()
    resetCreateForm()
  }, [generating, onClose, resetCreateForm])

  const checkBackend = useCallback(async () => {
    setBackendStatus('checking')
    setBackendMessage('Checking local AI backend…')
    try {
      const response = await fetch('/api/generate', { cache: 'no-store' })
      const data = await response.json()
      if (!response.ok || !data.ready) {
        throw new Error(data.message || 'The local AI backend is not ready.')
      }
      setBackendStatus('ready')
      setBackendMessage('Local AI backend ready')
      return true
    } catch (error) {
      setBackendStatus('offline')
      setBackendMessage(error instanceof Error ? error.message : 'The local AI backend is unavailable.')
      return false
    }
  }, [])

  useEffect(() => {
    if (!selectedFile) {
      setSelectedFileUrl('')
      return
    }
    const url = URL.createObjectURL(selectedFile)
    setSelectedFileUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [selectedFile])

  useEffect(() => {
    if (!open) return
    setGenerationError('')
    void checkBackend()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeCreateModal()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [checkBackend, closeCreateModal, open])

  useEffect(() => () => {
    generationAbortRef.current?.abort()
    pointAbortRef.current?.abort()
  }, [])

  const chooseImageFile = (file: File) => {
    pointAbortRef.current?.abort()
    const error = validateImage(file)
    setFileError(error)
    setGenerationError('')
    setPointError('')
    setSelectedMasks([])
    if (error) {
      setSelectedFile(null)
      return
    }
    setSelectedFile(file)
    if (!worldName) setWorldName(friendlyFileName(file))
  }

  // Click-to-segment: a click on the source image picks the object at that point.
  const handleImageClick = async (e: React.MouseEvent<HTMLImageElement>) => {
    if (!selectedFile || pointBusy || generating) return
    const img = imgRef.current
    if (!img || !img.naturalWidth) return
    const rect = img.getBoundingClientRect()
    const x = Math.round((e.clientX - rect.left) * (img.naturalWidth / rect.width))
    const y = Math.round((e.clientY - rect.top) * (img.naturalHeight / rect.height))
    setPointError('')
    setPointBusy(true)
    const pointController = new AbortController()
    pointAbortRef.current = pointController
    try {
      const fd = new FormData()
      fd.append('file', selectedFile)
      fd.append('x', String(x))
      fd.append('y', String(y))
      const res = await fetch('/api/segment-point', {
        method: 'POST',
        body: fd,
        signal: pointController.signal,
      })
      if (!res.ok) throw new Error(await responseErrorMessage(res))
      const data = await res.json()
      const thumb = await cropThumb(selectedFile, data.bbox)
      setSelectedMasks((prev) => [...prev, { mask: data.mask, thumb }])
    } catch (err) {
      if (pointController.signal.aborted) return
      console.error('Point segmentation failed:', err)
      setPointError(err instanceof Error ? err.message : 'Could not segment that point.')
    } finally {
      if (pointAbortRef.current === pointController) {
        pointAbortRef.current = null
        setPointBusy(false)
      }
    }
  }

  const handleCreateWorld = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedFile || !worldName.trim()) return

    setGenerationError('')
    const backendReady = backendStatus === 'ready' || await checkBackend()
    if (!backendReady) {
      setGenerationError('Start the local AI backend, then retry the connection.')
      return
    }

    setGenerating(true)
    setGenerationProgress({
      stage: 'initializing',
      progress: 1,
      message: 'Uploading source image',
      detail: 'Opening a live connection to the generation pipeline',
    })
    const generationController = new AbortController()
    generationAbortRef.current = generationController

    try {
      const formData = new FormData()
      formData.append('file', selectedFile)
      formData.append('name', worldName)
      // Guided selection priority: click-selected masks > typed concepts > auto.
      if (selectedMasks.length > 0) {
        formData.append('masks', JSON.stringify(selectedMasks.map((m) => ({ mask: m.mask }))))
      } else if (concepts.trim()) {
        formData.append('concepts', concepts.trim())
      }

      const res = await fetch('/api/generate', {
        method: 'POST',
        body: formData,
        signal: generationController.signal,
      })

      if (!res.ok) {
        throw new Error(await responseErrorMessage(res))
      }

      const slug = await readGenerationStream(res, setGenerationProgress)
      setGenerationProgress({
        stage: 'finalizing',
        progress: 100,
        message: 'World generated',
        detail: 'Opening the finished scene',
      })
      onClose()
      resetCreateForm()
      router.push(`/${slug}`)
    } catch (error: unknown) {
      console.error(error)
      const message = generationController.signal.aborted
        ? 'Generation canceled. No world was published.'
        : error instanceof Error
          ? error.message
          : String(error)
      setGenerationError(message)
    } finally {
      if (generationAbortRef.current === generationController) {
        generationAbortRef.current = null
      }
      setGenerating(false)
    }
  }

  const cancelGeneration = () => {
    setGenerationProgress((current) => ({
      ...current,
      message: 'Canceling generation',
      detail: 'Stopping the active backend stage and cleaning temporary files',
    }))
    generationAbortRef.current?.abort()
  }

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === "dragenter" || e.type === "dragover") {
      setDragActive(true)
    } else if (e.type === "dragleave") {
      setDragActive(false)
    }
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      chooseImageFile(e.dataTransfer.files[0])
    }
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      chooseImageFile(e.target.files[0])
    }
  }

  if (!open) return null

  return createPortal((
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={modalTitleId}
        className="relative flex max-h-[calc(100dvh-2rem)] w-full max-w-md flex-col gap-4 overflow-y-auto rounded-2xl border border-white/12 bg-zinc-950/95 p-5 shadow-2xl shadow-black/70 sm:p-6"
      >
        <button
          onClick={closeCreateModal}
          disabled={generating}
          className="absolute top-4 right-4 rounded text-white/50 transition-colors hover:text-white disabled:opacity-35"
          aria-label="Close modal"
        >
          <X size={20} />
        </button>

        <div className="pr-8">
          <h2 id={modalTitleId} className="flex items-center gap-2 font-mono text-xl font-bold text-white">
            <GlobeHemisphereWestIcon className={generating ? "animate-spin text-white/80" : "text-white/60"} size={22} />
            <span>Create New World</span>
          </h2>
          <p className="mt-1.5 text-xs leading-relaxed text-white/45">
            Turn one image into a navigable splat environment with interactive 3D objects.
          </p>
        </div>

        {generating ? (
          <div className="flex flex-col gap-6 py-5" aria-live="polite">
            <div className="flex items-center justify-between gap-4 font-mono text-[10px] uppercase tracking-[0.18em] text-white/35">
              <span>Pipeline / {generationProgress.stage}</span>
              <span className="tabular-nums text-white/60">{generationProgress.progress}%</span>
            </div>

            <div className="flex items-start gap-4">
              <span className="mt-0.5 flex h-9 w-9 flex-none items-center justify-center rounded-full border border-white/10 bg-white/[0.035]">
                <Spinner size={18} className="animate-spin text-white/75" />
              </span>
              <div className="min-w-0">
                <p className="text-sm font-medium tracking-[-0.01em] text-white/90">
                  {generationProgress.message}
                </p>
                {generationProgress.detail && (
                  <p className="mt-1 text-xs leading-relaxed text-white/45">
                    {generationProgress.detail}
                  </p>
                )}
              </div>
            </div>

            <div>
              <div
                role="progressbar"
                aria-label="World generation progress"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={generationProgress.progress}
                className="h-1 overflow-hidden rounded-full bg-white/[0.06]"
              >
                <div
                  className="h-full rounded-full bg-white/80 transition-[width] duration-500"
                  style={{ width: `${generationProgress.progress}%` }}
                />
              </div>
              <div className="mt-3 grid grid-cols-6 gap-1">
                {GENERATION_STAGE_ORDER.map((stage, index) => {
                  const activeIndex = GENERATION_STAGE_ORDER.indexOf(generationProgress.stage)
                  const reached = index <= activeIndex
                  return (
                    <span
                      key={stage}
                      className={`h-0.5 rounded-full transition-colors ${reached ? 'bg-white/45' : 'bg-white/[0.07]'}`}
                      title={stage}
                    />
                  )
                })}
              </div>
            </div>

            <button
              type="button"
              onClick={cancelGeneration}
              className="self-start rounded border border-white/15 bg-white/[0.025] px-3 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-white/55 transition hover:border-red-300/30 hover:bg-red-300/[0.05] hover:text-red-100/80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
            >
              Cancel generation
            </button>
          </div>
        ) : (
          <form onSubmit={handleCreateWorld} className="flex flex-col gap-4">
            <div
              className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-xs leading-relaxed ${
                backendStatus === 'ready'
                  ? 'border-emerald-400/20 bg-emerald-400/5 text-emerald-200/80'
                  : backendStatus === 'offline'
                    ? 'border-amber-400/25 bg-amber-400/5 text-amber-100/80'
                    : 'border-white/10 bg-white/[0.03] text-white/50'
              }`}
              role="status"
            >
              {backendStatus === 'ready' ? (
                <CheckCircle size={16} className="mt-0.5 flex-shrink-0" weight="fill" />
              ) : backendStatus === 'offline' ? (
                <WarningCircle size={16} className="mt-0.5 flex-shrink-0" weight="fill" />
              ) : (
                <Spinner size={16} className="mt-0.5 flex-shrink-0 animate-spin" />
              )}
              <span className="min-w-0 flex-1">{backendMessage}</span>
              {backendStatus === 'offline' && (
                <button
                  type="button"
                  onClick={() => void checkBackend()}
                  className="flex-shrink-0 rounded border border-current/20 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider hover:bg-white/5"
                >
                  Retry
                </button>
              )}
            </div>

            {generationError && (
              <div
                role="alert"
                className="rounded-lg border border-red-400/25 bg-red-400/[0.06] px-3 py-2 text-xs leading-relaxed text-red-100/85"
              >
                <span className="font-semibold">Generation failed.</span> {generationError}
              </div>
            )}

            <div className="flex flex-col gap-1.5">
              <label htmlFor="world-name" className="text-xs font-mono font-semibold text-white/60 uppercase tracking-wider">
                World Name
              </label>
              <input
                id="world-name"
                type="text"
                required
                autoFocus
                maxLength={80}
                value={worldName}
                onChange={(e) => setWorldName(e.target.value)}
                placeholder="e.g. Dream Bedroom"
                className="bg-zinc-900 border border-white/10 focus:border-white/30 rounded px-3 py-2 text-white placeholder-white/20 font-mono text-sm outline-none transition-colors"
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <label htmlFor="world-concepts" className="text-xs font-mono font-semibold text-white/60 uppercase tracking-wider">
                Objects to Extract <span className="text-white/30 normal-case">(optional, SAM 3)</span>
              </label>
              <input
                id="world-concepts"
                type="text"
                value={concepts}
                onChange={(e) => setConcepts(e.target.value)}
                placeholder="e.g. chair, monitor, lamp"
                className="bg-zinc-900 border border-white/10 focus:border-white/30 rounded px-3 py-2 text-white placeholder-white/20 font-mono text-sm outline-none transition-colors"
              />
              <span className="text-[10px] font-mono text-white/40">
                Comma-separated. Leave blank to auto-detect the largest objects.
              </span>
            </div>

            <div className="flex flex-col gap-1.5">
              <span className="text-xs font-mono font-semibold text-white/60 uppercase tracking-wider">
                Source Image
              </span>
              <div
                onDragEnter={handleDrag}
                onDragOver={handleDrag}
                onDragLeave={handleDrag}
                onDrop={handleDrop}
                onClick={() => document.getElementById('file-upload')?.click()}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    document.getElementById('file-upload')?.click()
                  }
                }}
                role="button"
                tabIndex={0}
                aria-label={selectedFile ? `Change source image, currently ${selectedFile.name}` : 'Choose source image'}
                className={`border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-all flex flex-col items-center justify-center gap-2 min-h-[140px] ${
                  dragActive
                    ? 'border-white bg-white/5'
                    : selectedFile
                      ? 'border-white/30 bg-white/2 hover:border-white/50'
                      : 'border-white/10 bg-white/1 hover:border-white/20'
                }`}
              >
                <input
                  id="file-upload"
                  type="file"
                  accept="image/*"
                  className="hidden"
                  onChange={handleFileChange}
                />
                {selectedFile ? (
                  <div className="flex flex-col items-center gap-1.5 w-full">
                    <div className="relative w-16 h-16 rounded overflow-hidden border border-white/10 bg-zinc-900 flex items-center justify-center">
                      <NextImage
                        src={selectedFileUrl}
                        alt="Preview"
                        fill
                        sizes="64px"
                        unoptimized
                        className="object-cover"
                      />
                    </div>
                    <span className="text-xs font-mono text-white/70 truncate max-w-[240px]">
                      {selectedFile.name}
                    </span>
                    <span className="text-[10px] font-mono text-white/40">
                      Click or drag to change
                    </span>
                  </div>
                ) : (
                  <>
                    <UploadSimple size={24} className="text-white/45" />
                    <span className="text-xs font-mono text-white/60">
                      Drag & drop image here, or click to browse
                    </span>
                    <span className="text-[10px] font-mono text-white/30">
                      Supports PNG, JPG, WEBP (Max 10MB)
                    </span>
                  </>
                )}
              </div>
              {fileError && <span className="text-[10px] font-mono text-red-300/80" role="alert">{fileError}</span>}
            </div>

            {selectedFile && selectedFileUrl && (
              <div className="flex flex-col gap-1.5">
                <span className="text-xs font-mono font-semibold text-white/60 uppercase tracking-wider">
                  Click Objects to Extract <span className="text-white/30 normal-case">(optional, SAM 2)</span>
                </span>
                <div className="relative h-64 rounded-lg overflow-hidden border border-white/10 bg-zinc-900">
                  <NextImage
                    ref={imgRef}
                    src={selectedFileUrl}
                    alt="Click to segment"
                    fill
                    sizes="(max-width: 480px) calc(100vw - 4rem), 400px"
                    unoptimized
                    onClick={handleImageClick}
                    className={`object-contain ${pointBusy ? 'cursor-wait opacity-70' : 'cursor-crosshair'}`}
                  />
                  {pointBusy && (
                    <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                      <Spinner size={28} className="animate-spin text-white" />
                    </div>
                  )}
                </div>
                {pointError && (
                  <span className="text-[10px] font-mono leading-relaxed text-red-300/80" role="alert">
                    {pointError}
                  </span>
                )}
                {selectedMasks.length > 0 ? (
                  <div className="flex flex-wrap gap-2 mt-1">
                    {selectedMasks.map((m, i) => (
                      <div key={i} className="relative w-14 h-14 rounded overflow-hidden border border-white/15 bg-zinc-800">
                        <NextImage
                          src={m.thumb}
                          alt={`Object ${i + 1}`}
                          fill
                          sizes="56px"
                          unoptimized
                          className="object-cover"
                        />
                        <button
                          type="button"
                          onClick={() => setSelectedMasks((prev) => prev.filter((_, j) => j !== i))}
                          className="absolute top-0 right-0 bg-black/70 hover:bg-red-600 text-white w-4 h-4 flex items-center justify-center"
                          aria-label={`Remove selected object ${i + 1}`}
                        >
                          <X size={10} />
                        </button>
                      </div>
                    ))}
                  </div>
                ) : (
                  <span className="text-[10px] font-mono text-white/40">
                    Click objects in the image to hand-pick them. Overrides categories above.
                  </span>
                )}
              </div>
            )}

            <div className="flex justify-end gap-2 mt-2">
              <AppButton
                type="button"
                onClick={closeCreateModal}
                className="border border-white/10 hover:border-white/20 hover:bg-white/5 text-white/80 rounded px-4 py-2 font-mono text-xs h-9"
              >
                Cancel
              </AppButton>
              <AppButton
                type="submit"
                disabled={!selectedFile || !worldName.trim() || backendStatus !== 'ready'}
                className="bg-white text-black font-semibold hover:bg-white/90 disabled:opacity-40 disabled:hover:bg-white rounded px-4 py-2 font-mono text-xs h-9 flex items-center justify-center"
              >
                Generate World
              </AppButton>
            </div>
          </form>
        )}
      </div>
    </div>
  ), document.body)
}
