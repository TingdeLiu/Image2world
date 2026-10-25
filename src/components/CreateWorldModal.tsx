'use client'

import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import NextImage from 'next/image'
import {
  ArrowSquareOut,
  CheckCircle,
  GlobeHemisphereWest as GlobeHemisphereWestIcon,
  Spinner,
  UploadSimple,
  WarningCircle,
  X,
} from '@phosphor-icons/react'
import { useRouter } from 'next/navigation'
import { AppButton } from './AppButton'
import { MARBLE_KEYS_URL, REPO_URL } from '../utils/links'
import { APP_REQUEST_HEADERS } from '../utils/sameOrigin'
import { summarizeAppearanceQuality } from '../utils/reconstructionQuality'

type BackendStatus = 'checking' | 'ready' | 'offline'
type GenerationStage = 'initializing' | 'uploading' | 'sfm' | 'training' | 'exporting' | 'evaluating' | 'splat' | 'finalizing'
type VideoQuality = 'preview' | 'standard' | 'quality' | 'navigation'

interface GenerationUiProgress {
  stage: GenerationStage
  progress: number
  message: string
  detail?: string
}

/** `/api/generate/jobs/<id>`. Mirrors `src/utils/generationJobs.ts`. */
interface GenerationJob {
  id: string
  name: string
  status: 'running' | 'complete' | 'failed' | 'canceled' | 'interrupted'
  stage: GenerationStage
  progress: number
  message: string
  detail?: string
  slug?: string
  error?: string
}

const INITIAL_GENERATION_PROGRESS: GenerationUiProgress = {
  stage: 'initializing',
  progress: 0,
  message: 'Preparing generation',
  detail: 'Waiting for the local pipeline',
}
const GENERATION_STAGE_ORDER: GenerationStage[] = [
  'initializing',
  'uploading',
  'sfm',
  'training',
  'exporting',
  'evaluating',
  'splat',
  'finalizing',
]

const MAX_IMAGE_BYTES = 10 * 1024 * 1024
const MAX_VIDEO_BYTES = 5 * 1024 * 1024 * 1024
const ACCEPTED_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp'])
const ACCEPTED_VIDEO_TYPES = new Set([
  'video/mp4',
  'video/quicktime',
  'video/x-matroska',
  'video/webm',
  'video/x-msvideo',
  'video/x-m4v',
])
const VIDEO_EXTENSIONS = new Set(['mp4', 'mov', 'mkv', 'webm', 'avi', 'm4v'])
const LOCAL_BACKEND_URL = (
  process.env.NEXT_PUBLIC_IMAGEWORLD_BACKEND_URL || 'http://localhost:8000'
).replace(/\/+$/, '')

function isVideoFile(file: File) {
  const extension = file.name.split('.').pop()?.toLowerCase() || ''
  return ACCEPTED_VIDEO_TYPES.has(file.type) || file.type.startsWith('video/') || VIDEO_EXTENSIONS.has(extension)
}

function validateSource(file: File) {
  if (isVideoFile(file)) {
    if (file.size > MAX_VIDEO_BYTES) return 'Video must be smaller than 5 GB.'
    return ''
  }
  if (!ACCEPTED_IMAGE_TYPES.has(file.type)) return 'Choose a PNG, JPG, WEBP, MP4, MOV, MKV, or WEBM file.'
  if (file.size > MAX_IMAGE_BYTES) return 'Image must be smaller than 10 MB.'
  return ''
}

async function responseErrorMessage(response: Response) {
  const contentType = response.headers.get('content-type') || ''
  if (contentType.includes('application/json')) {
    const payload = await response.json() as { error?: { message?: string } | string; detail?: string }
    return (typeof payload.error === 'string' ? payload.error : payload.error?.message)
      || payload.detail
      || `Generation request failed with HTTP ${response.status}.`
  }
  const message = (await response.text()).trim()
  return message || `Generation request failed with HTTP ${response.status}.`
}

interface ReconstructionJob {
  id: string
  status: 'uploading' | 'queued' | 'running' | 'awaiting_review' | 'complete' | 'failed' | 'canceled' | 'canceling' | 'interrupted'
  name?: string
  quality_report?: unknown
  stage: string
  progress: number
  message: string
  detail?: string
  error?: string
}

function abortableDelay(milliseconds: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const timeout = window.setTimeout(() => {
      signal.removeEventListener('abort', abort)
      resolve()
    }, milliseconds)
    const abort = () => {
      window.clearTimeout(timeout)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    signal.addEventListener('abort', abort, { once: true })
  })
}

function videoProgress(job: ReconstructionJob): GenerationUiProgress {
  const supported = new Set<GenerationStage>(['uploading', 'sfm', 'training', 'exporting', 'evaluating', 'finalizing'])
  const stage = supported.has(job.stage as GenerationStage)
    ? job.stage as GenerationStage
    : job.status === 'queued'
      ? 'initializing'
      : 'training'
  return {
    stage,
    progress: Math.min(98, Math.max(1, Math.round(job.progress || 1))),
    message: job.message || 'Reconstructing video',
    detail: job.detail,
  }
}

/**
 * Follow a generation to its end by polling.
 *
 * The pipeline used to stream progress down the POST that started it, which
 * made the connection load-bearing: closing the tab tore the work down. Now the
 * work is a job on disk and this is only a view of it, so a lost connection --
 * or a lost tab -- costs nothing but the progress bar.
 */
async function followGenerationJob(
  jobId: string,
  onProgress: (progress: GenerationUiProgress) => void,
  signal: AbortSignal,
) {
  let job = await readGenerationJob(jobId, signal)

  while (job.status === 'running') {
    onProgress(jobProgress(job))
    await abortableDelay(1_500, signal)
    job = await readGenerationJob(jobId, signal)
  }

  if (job.status === 'complete' && job.slug) return job.slug
  throw new Error(job.error || job.message || `Generation ${job.status}.`)
}

async function readGenerationJob(jobId: string, signal: AbortSignal) {
  const response = await fetch(`/api/generate/jobs/${jobId}`, {
    cache: 'no-store',
    headers: APP_REQUEST_HEADERS,
    signal,
  })
  if (!response.ok) throw new Error(await responseErrorMessage(response))
  return (await response.json() as { job: GenerationJob }).job
}

function jobProgress(job: GenerationJob): GenerationUiProgress {
  return {
    stage: job.stage,
    // The job reaches 100 only when it is complete; holding the bar just short
    // of the end keeps "done" meaning the world is actually openable.
    progress: Math.min(98, Math.max(1, Math.round(job.progress || 1))),
    message: job.message,
    detail: job.detail,
  }
}

function friendlyFileName(file: File) {
  const nameWithoutExt = file.name.substring(0, file.name.lastIndexOf('.')) || file.name
  return nameWithoutExt
    .split(/[-_]+/)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

// The key is the user's own and stays on their machine: it is kept in
// localStorage and attached per request, never persisted server-side.
// Storage key keeps the old "imageworld" spelling on purpose: renaming it would
// silently drop the API key users already saved. Display names use Image2World.
const MARBLE_KEY_STORAGE = 'imageworld:marble-api-key'
const VIDEO_JOB_STORAGE = 'imageworld:active-video-job'

function rememberVideoJob(id: string | null) {
  try {
    if (id) window.localStorage.setItem(VIDEO_JOB_STORAGE, id)
    else window.localStorage.removeItem(VIDEO_JOB_STORAGE)
  } catch { /* Reattaching is optional when browser storage is unavailable. */ }
}
// The public demo serves a prebuilt world and has neither a GPU backend nor a
// function timeout long enough for Marble, which takes about eight minutes
// against a 60-300 s serverless ceiling. Offering the form there would let a
// visitor spend ~$1.20 of their own credits on a request that cannot finish, so
// the deployment explains itself instead of failing.
const DEMO_MODE = process.env.NEXT_PUBLIC_IMAGEWORLD_DEMO_MODE === 'true'

function readStoredMarbleKey() {
  if (typeof window === 'undefined') return ''
  try {
    return window.localStorage.getItem(MARBLE_KEY_STORAGE) ?? ''
  } catch {
    return ''
  }
}

function storeMarbleKey(key: string) {
  try {
    if (key) window.localStorage.setItem(MARBLE_KEY_STORAGE, key)
    else window.localStorage.removeItem(MARBLE_KEY_STORAGE)
  } catch {
    // Private browsing or a blocked origin -- the key simply won't persist.
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
  const [marbleKey, setMarbleKey] = useState('')
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [dragActive, setDragActive] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [generationProgress, setGenerationProgress] = useState<GenerationUiProgress>(INITIAL_GENERATION_PROGRESS)
  const [generationError, setGenerationError] = useState('')
  const [fileError, setFileError] = useState('')
  const [backendStatus, setBackendStatus] = useState<BackendStatus>('checking')
  const [backendMessage, setBackendMessage] = useState('')
  const [selectedFileUrl, setSelectedFileUrl] = useState('')
  const [videoQuality, setVideoQuality] = useState<VideoQuality>('standard')
  const [cameraHeight, setCameraHeight] = useState(1.6)
  const [previewFirst, setPreviewFirst] = useState(true)
  const [previewJob, setPreviewJob] = useState<ReconstructionJob | null>(null)
  const generationAbortRef = useRef<AbortController | null>(null)
  const activeVideoJobRef = useRef<string | null>(null)
  const activeGenerationJobRef = useRef<string | null>(null)
  const generatingRef = useRef(false)
  const dialogRef = useRef<HTMLDivElement>(null)
  const modalTitleId = useId()

  // The key is deliberately left out of the reset: it is a setting, not part of
  // the form the user refills for each world.
  const resetCreateForm = useCallback(() => {
    setSelectedFile(null)
    setWorldName('')
    setGenerationError('')
    setGenerationProgress(INITIAL_GENERATION_PROGRESS)
    setFileError('')
    setVideoQuality('standard')
    setCameraHeight(1.6)
    setPreviewFirst(true)
    setPreviewJob(null)
    activeVideoJobRef.current = null
  }, [])

  const selectedVideo = selectedFile ? isVideoFile(selectedFile) : false
  const usingMarble = marbleKey.trim().length > 0 && !selectedVideo

  const closeCreateModal = useCallback(() => {
    if (generating) return
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

  /**
   * Reattach to a generation that is already running.
   *
   * Reloading the page no longer stops the work, so without this the modal
   * would offer to start a second one and be told the GPU is busy. Called when
   * the modal opens, and after a 409 says one is already in flight.
   */
  const watchGenerationJob = useCallback(async (jobId: string, jobName: string) => {
    if (generatingRef.current) return
    generatingRef.current = true
    setGenerating(true)
    setGenerationError('')
    setWorldName((current) => current || jobName)

    const controller = new AbortController()
    generationAbortRef.current = controller
    activeGenerationJobRef.current = jobId

    try {
      const slug = await followGenerationJob(jobId, setGenerationProgress, controller.signal)
      closeCreateModal()
      resetCreateForm()
      router.push(`/${slug}`)
    } catch (error: unknown) {
      if (controller.signal.aborted) return
      setGenerationError(error instanceof Error ? error.message : String(error))
    } finally {
      generatingRef.current = false
      if (generationAbortRef.current === controller) generationAbortRef.current = null
      activeGenerationJobRef.current = null
      setGenerating(false)
    }
  }, [closeCreateModal, resetCreateForm, router])

  const resumeActiveGeneration = useCallback(async () => {
    try {
      const response = await fetch('/api/generate/jobs', {
        cache: 'no-store',
        headers: APP_REQUEST_HEADERS,
      })
      if (!response.ok) return
      const { active } = await response.json() as { active: GenerationJob | null }
      if (active) void watchGenerationJob(active.id, active.name)
    } catch {
      // Nothing to reattach to is the normal case, not an error worth showing.
    }
  }, [watchGenerationJob])

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
    const dialog = dialogRef.current
    if (!dialog) return
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const focusableElements = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])',
    )).filter((element) => element.tabIndex >= 0 && element.getClientRects().length > 0)

    const focusInside = () => {
      if (!dialog.contains(document.activeElement)) {
        const target = dialog.querySelector<HTMLElement>('#world-name') || focusableElements()[0] || dialog
        target.focus({ preventScroll: true })
      }
    }
    focusInside()

    const trapTab = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return
      const elements = focusableElements()
      const first = elements[0] || dialog
      const last = elements[elements.length - 1] || dialog
      if (!dialog.contains(document.activeElement)
        || (event.shiftKey ? document.activeElement === first : document.activeElement === last)
        || document.activeElement === dialog) {
        event.preventDefault()
        const target = event.shiftKey ? last : first
        target.focus({ preventScroll: true })
      }
    }
    document.addEventListener('keydown', trapTab, true)
    document.addEventListener('focusin', focusInside)
    return () => {
      document.removeEventListener('keydown', trapTab, true)
      document.removeEventListener('focusin', focusInside)
      document.body.style.overflow = previousOverflow
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true })
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    setGenerationError('')
    if (!DEMO_MODE) {
      setMarbleKey(readStoredMarbleKey())
      void checkBackend()
      void resumeActiveGeneration()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeCreateModal()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [checkBackend, closeCreateModal, open, resumeActiveGeneration])

  useEffect(() => () => {
    generationAbortRef.current?.abort()
  }, [])

  const chooseSourceFile = (file: File) => {
    const error = validateSource(file)
    setFileError(error)
    setGenerationError('')
    if (error) {
      setSelectedFile(null)
      return
    }
    setSelectedFile(file)
    if (!worldName) setWorldName(friendlyFileName(file))
  }

  const followVideoReconstruction = useCallback(async (
    initialJob: ReconstructionJob,
    name: string,
    signal: AbortSignal,
  ): Promise<string | null> => {
    let job = initialJob
    activeVideoJobRef.current = job.id
    rememberVideoJob(job.id)
    setGenerationProgress(videoProgress(job))

    while (job.status !== 'complete' && job.status !== 'awaiting_review') {
      if (['failed', 'canceled', 'interrupted'].includes(job.status)) {
        throw new Error(job.error || job.message || `Reconstruction ${job.status}.`)
      }
      await abortableDelay(2_000, signal)
      const statusResponse = await fetch(`${LOCAL_BACKEND_URL}/api/reconstruction-jobs/${job.id}`, {
        cache: 'no-store',
        signal,
      })
      if (!statusResponse.ok) throw new Error(await responseErrorMessage(statusResponse))
      job = await statusResponse.json() as ReconstructionJob
      setGenerationProgress(videoProgress(job))
    }
    if (job.status === 'awaiting_review') {
      setPreviewJob(job)
      return null
    }
    // Keep a completed GPU result available if asset publication fails. A
    // retry must reuse it rather than uploading or training the video again.
    setPreviewJob(job)

    setGenerationProgress({
      stage: 'finalizing',
      progress: 98,
      message: 'Publishing reconstructed world',
      detail: 'Building LODs, collision geometry, and scene metadata',
    })
    const finalResponse = await fetch('/api/video-reconstruction/finalize', {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...APP_REQUEST_HEADERS },
      body: JSON.stringify({ jobId: job.id, name }),
      signal,
    })
    if (!finalResponse.ok) throw new Error(await responseErrorMessage(finalResponse))
    const finalized = await finalResponse.json() as { slug?: string }
    if (!finalized.slug) throw new Error('World publisher returned no world identifier.')
    rememberVideoJob(null)
    return finalized.slug
  }, [])

  const watchVideoReconstruction = useCallback(async (job: ReconstructionJob, action?: 'refine' | 'accept') => {
    if (generatingRef.current) return
    generatingRef.current = true
    setGenerating(true)
    setGenerationError('')
    const controller = new AbortController()
    generationAbortRef.current = controller
    activeVideoJobRef.current = job.id
    try {
      if (action) {
        const response = await fetch(`${LOCAL_BACKEND_URL}/api/reconstruction-jobs/${job.id}/${action}`, {
          method: 'POST', signal: controller.signal,
        })
        if (!response.ok) throw new Error(await responseErrorMessage(response))
        job = await response.json() as ReconstructionJob
        setPreviewJob(null)
      }
      const slug = await followVideoReconstruction(job, job.name || 'Video world', controller.signal)
      if (slug) {
        onClose()
        resetCreateForm()
        router.push(`/${slug}`)
      }
    } catch (error) {
      setGenerationError(error instanceof Error ? error.message : String(error))
    } finally {
      generatingRef.current = false
      setGenerating(false)
      activeVideoJobRef.current = null
      if (generationAbortRef.current === controller) generationAbortRef.current = null
    }
  }, [followVideoReconstruction, onClose, resetCreateForm, router])

  useEffect(() => {
    if (!open || DEMO_MODE || generatingRef.current) return
    const controller = new AbortController()
    void (async () => {
      try {
        let id: string | null = null
        try { id = window.localStorage.getItem(VIDEO_JOB_STORAGE) } catch { /* optional storage */ }
        const response = await fetch(`${LOCAL_BACKEND_URL}/api/reconstruction-jobs${id ? `/${id}` : ''}`, {
          cache: 'no-store', signal: controller.signal,
        })
        if (!response.ok || generatingRef.current) return
        const payload = await response.json() as ReconstructionJob & { jobs?: ReconstructionJob[] }
        const job = id ? payload : payload.jobs?.find((candidate) => candidate.status === 'awaiting_review')
        if (!job || controller.signal.aborted) return
        if (job.status === 'awaiting_review') setPreviewJob(job)
        else if (['queued', 'running', 'complete'].includes(job.status)) void watchVideoReconstruction(job)
      } catch { /* An unavailable backend must not prevent creating an image world. */ }
    })()
    return () => controller.abort()
  }, [open, watchVideoReconstruction])

  const runVideoReconstruction = async (file: File, name: string, signal: AbortSignal) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('name', name)
    formData.append('quality', videoQuality)
    formData.append('camera_height', String(cameraHeight))
    formData.append('preview_first', String(previewFirst))
    const response = await fetch(`${LOCAL_BACKEND_URL}/api/reconstruction-jobs`, { method: 'POST', body: formData, signal })
    if (!response.ok) throw new Error(await responseErrorMessage(response))
    return followVideoReconstruction(await response.json() as ReconstructionJob, name, signal)
  }

  const handleCreateWorld = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedFile || !worldName.trim()) return

    setGenerationError('')
    // Marble runs entirely in the cloud, so the local GPU backend is only
    // required when no key is supplied.
    if (!usingMarble) {
      const backendReady = backendStatus === 'ready' || await checkBackend()
      if (!backendReady) {
        setGenerationError('Start the local AI backend, or add a Marble API key to generate in the cloud.')
        return
      }
    }

    storeMarbleKey(marbleKey.trim())
    generatingRef.current = true
    setGenerating(true)
    setGenerationProgress({
      stage: 'initializing',
      progress: 1,
      message: selectedVideo ? 'Uploading source video' : 'Uploading source image',
      detail: selectedVideo
        ? 'Streaming the video to the persistent local reconstruction queue'
        : usingMarble
        ? 'Sending the photo to World Labs Marble'
        : 'Opening a live connection to the local pipeline',
    })
    const generationController = new AbortController()
    generationAbortRef.current = generationController

    try {
      let slug: string | null
      if (selectedVideo) {
        slug = await runVideoReconstruction(
          selectedFile,
          worldName.trim(),
          generationController.signal,
        )
      } else {
        const formData = new FormData()
        formData.append('file', selectedFile)
        formData.append('name', worldName)
        if (usingMarble) formData.append('marbleApiKey', marbleKey.trim())

        // Only the upload is tied to this request; the generation it starts
        // outlives it, so the abort signal stops here and cancellation goes
        // through the job instead.
        const res = await fetch('/api/generate', {
          method: 'POST',
          headers: APP_REQUEST_HEADERS,
          body: formData,
          signal: generationController.signal,
        })
        if (res.status === 409) {
          // Another generation is already running -- most likely this one,
          // started before a reload. Watch it instead of reporting a clash.
          const { jobId } = await res.json() as { jobId?: string }
          if (jobId) {
            generatingRef.current = false
            void watchGenerationJob(jobId, worldName.trim())
            return
          }
        }
        if (!res.ok) throw new Error(await responseErrorMessage(res))
        const { job } = await res.json() as { job: GenerationJob }
        activeGenerationJobRef.current = job.id
        setGenerationProgress(jobProgress(job))
        slug = await followGenerationJob(job.id, setGenerationProgress, generationController.signal)
      }
      if (!slug) return
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
      generatingRef.current = false
      activeVideoJobRef.current = null
      activeGenerationJobRef.current = null
      setGenerating(false)
    }
  }

  const cancelGeneration = () => {
    setGenerationProgress((current) => ({
      ...current,
      message: 'Canceling generation',
      detail: 'Stopping the active backend stage and cleaning temporary files',
    }))
    const videoJobId = activeVideoJobRef.current
    const generationJobId = activeGenerationJobRef.current
    generationAbortRef.current?.abort()
    if (videoJobId) {
      void fetch(`${LOCAL_BACKEND_URL}/api/reconstruction-jobs/${videoJobId}`, { method: 'DELETE' })
      rememberVideoJob(null)
    }
    // Aborting the poller only stops watching; the job has to be told.
    if (generationJobId) {
      void fetch(`/api/generate/jobs/${generationJobId}`, {
        method: 'DELETE',
        headers: APP_REQUEST_HEADERS,
      })
    }
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
      chooseSourceFile(e.dataTransfer.files[0])
    }
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      chooseSourceFile(e.target.files[0])
    }
  }

  if (!open) return null
  const previewQuality = summarizeAppearanceQuality(previewJob?.quality_report)

  return createPortal((
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md p-4">
      <div
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
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
            <span>{DEMO_MODE ? 'Generate Your Own' : 'Create New World'}</span>
          </h2>
          <p className="mt-1.5 text-xs leading-relaxed text-white/45">
            Turn an image or a walkthrough video into a navigable Gaussian world.
          </p>
        </div>

        {DEMO_MODE ? (
          <div className="flex flex-col gap-4">
            <div className="rounded-lg border border-sky-400/25 bg-sky-400/5 px-3 py-2.5 text-xs leading-relaxed text-sky-100/85">
              This is a read-only demo. Generating a world writes several hundred
              megabytes of splats and meshes to disk, so it runs on your own
              machine rather than here.
            </div>

            <div className="flex flex-col gap-3">
              <span className="font-mono text-[10px] uppercase tracking-wider text-white/40">
                Run it yourself
              </span>
              <div className="flex flex-col gap-2.5 text-xs leading-relaxed text-white/60">
                <p>
                  <span className="font-semibold text-white/80">On your GPU.</span>{' '}
                  Clone the repository and start the local pipeline — free, about
                  16–43 seconds per world, and it reconstructs what the camera saw.
                </p>
                <p>
                  <span className="font-semibold text-white/80">Or in the cloud.</span>{' '}
                  Add your own Marble API key to get a world that is closed on
                  every side, like the one you are standing in. Billed to your key,
                  roughly $1.20 each.
                </p>
              </div>
            </div>

            <a
              href={REPO_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1 flex items-center justify-center gap-1.5 rounded bg-white px-4 py-2.5 font-mono text-xs font-semibold text-black transition hover:bg-white/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
            >
              Get it on GitHub
              <ArrowSquareOut size={13} weight="bold" aria-hidden />
            </a>

            <p className="text-center text-[10px] font-mono leading-relaxed text-white/35">
              Close this and press <span className="text-white/55">W</span>{' '}
              <span className="text-white/55">A</span>{' '}
              <span className="text-white/55">S</span>{' '}
              <span className="text-white/55">D</span> to keep exploring.
            </p>
          </div>
        ) : generating ? (
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
              <div className="mt-3 grid grid-cols-7 gap-1">
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
        ) : previewJob ? (
          <div className="space-y-4 py-2">
            <div>
              <h3 className="text-sm font-semibold text-white">{previewJob.status === 'complete' ? 'Ready to publish' : 'Review your preview'}</h3>
              <p className="mt-1 text-xs leading-relaxed text-white/55">{previewJob.name || 'Video world'}. Compare the recorded view on the left with the reconstruction on the right.</p>
            </div>
            {previewQuality.status === 'measured' && previewQuality.metrics ? (
              <>
                <a href={`${LOCAL_BACKEND_URL}/api/reconstruction-jobs/${previewJob.id}/preview/0`} target="_blank" rel="noreferrer">
                  <NextImage src={`${LOCAL_BACKEND_URL}/api/reconstruction-jobs/${previewJob.id}/preview/0`} alt="Recorded view on the left and preview reconstruction on the right" unoptimized width={768} height={216} className="h-auto w-full rounded border border-white/10" />
                </a>
                <p className="text-xs text-white/65">{previewJob.status === 'complete' ? 'Final test' : 'Validation'}: PSNR {previewQuality.metrics.psnr.toFixed(1)} dB · SSIM {previewQuality.metrics.ssim.toFixed(3)} · LPIPS {previewQuality.metrics.lpips.toFixed(3)}</p>
              </>
            ) : <p className="text-xs text-amber-100/70">Preview evaluation unavailable. {previewQuality.reason}</p>}
            <p className="text-xs leading-relaxed text-white/50">{previewJob.status === 'complete'
              ? 'The reconstruction is saved. Retry publishing without uploading or training again.'
              : 'Continue refining from this checkpoint, or use the preview as it is. Final test views are checked only after your choice.'}</p>
            {generationError && <p role="alert" className="text-xs text-red-300">{generationError}</p>}
            <div className="flex flex-wrap gap-2">
              {previewJob.status === 'complete' ? (
                <AppButton onClick={() => void watchVideoReconstruction(previewJob)} className="rounded bg-white px-3 py-2 text-xs font-semibold text-black">Retry publishing</AppButton>
              ) : <>
                <AppButton onClick={() => void watchVideoReconstruction(previewJob, 'refine')} className="rounded bg-white px-3 py-2 text-xs font-semibold text-black">Continue refining</AppButton>
                <AppButton onClick={() => void watchVideoReconstruction(previewJob, 'accept')} className="rounded border border-white/20 px-3 py-2 text-xs text-white">Use preview</AppButton>
              </>}
              <AppButton onClick={async () => {
                try {
                  const response = await fetch(`${LOCAL_BACKEND_URL}/api/reconstruction-jobs/${previewJob.id}`, { method: 'DELETE' })
                  if (!response.ok) { setGenerationError(await responseErrorMessage(response)); return }
                  rememberVideoJob(null)
                  setPreviewJob(null)
                } catch (error) {
                  setGenerationError(error instanceof Error ? error.message : 'Could not discard this preview.')
                }
              }} className="rounded px-3 py-2 text-xs text-white/55">Discard</AppButton>
            </div>
          </div>
        ) : (
          <form onSubmit={handleCreateWorld} className="flex flex-col gap-4">
            {usingMarble ? (
              <div
                className="flex items-start gap-2 rounded-lg border border-sky-400/25 bg-sky-400/5 px-3 py-2 text-xs leading-relaxed text-sky-100/80"
                role="status"
              >
                <GlobeHemisphereWestIcon size={16} className="mt-0.5 flex-shrink-0" weight="fill" />
                <span className="min-w-0 flex-1">
                  Generating with World Labs Marble — a complete, enclosed world. Billed to your own key.
                </span>
              </div>
            ) : (
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
            )}

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
                maxLength={80}
                value={worldName}
                onChange={(e) => setWorldName(e.target.value)}
                placeholder="e.g. Dream Bedroom"
                className="bg-zinc-900 border border-white/10 focus:border-white/30 rounded px-3 py-2 text-white placeholder-white/20 font-mono text-sm outline-none transition-colors"
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <div className="flex items-baseline justify-between gap-2">
                <label htmlFor="marble-key" className="text-xs font-mono font-semibold text-white/60 uppercase tracking-wider">
                  Marble API Key <span className="text-white/30 normal-case">(optional)</span>
                </label>
                <a
                  href={MARBLE_KEYS_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex flex-shrink-0 items-center gap-1 rounded border border-sky-400/30 bg-sky-400/[0.07] px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-sky-200/90 transition hover:border-sky-300/50 hover:bg-sky-400/15 hover:text-sky-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-300"
                >
                  Get a key
                  <ArrowSquareOut size={11} weight="bold" aria-hidden />
                </a>
              </div>
              <input
                id="marble-key"
                type="password"
                disabled={selectedVideo}
                autoComplete="off"
                spellCheck={false}
                value={marbleKey}
                onChange={(e) => setMarbleKey(e.target.value)}
                placeholder="Leave empty to use the local GPU pipeline"
                className="bg-zinc-900 border border-white/10 focus:border-white/30 rounded px-3 py-2 text-white placeholder-white/20 font-mono text-sm outline-none transition-colors disabled:cursor-not-allowed disabled:opacity-40"
              />
              <span className="text-[10px] font-mono leading-relaxed text-white/40">
                {selectedVideo
                  ? 'Walkthrough videos always use the local COLMAP + Nerfstudio Splatfacto pipeline.'
                  : usingMarble
                  ? 'Stored in this browser only. Marble builds a fully enclosed world you can walk around in — billed to your account (~1500 credits per world).'
                  : 'Without a key, generation runs on your local GPU: free and fast, but only reconstructs what the camera saw, so the space is open behind you.'}
              </span>
            </div>

            <div className="flex flex-col gap-1.5">
              <span className="text-xs font-mono font-semibold text-white/60 uppercase tracking-wider">
                Source Image or Video
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
                aria-label={selectedFile ? `Change source media, currently ${selectedFile.name}` : 'Choose source image or video'}
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
                  accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime,video/x-matroska,video/webm,video/x-msvideo,video/x-m4v,.mkv,.m4v"
                  className="hidden"
                  onChange={handleFileChange}
                />
                {selectedFile ? (
                  <div className="flex flex-col items-center gap-1.5 w-full">
                    <div className="relative w-16 h-16 rounded overflow-hidden border border-white/10 bg-zinc-900 flex items-center justify-center">
                      {selectedVideo ? (
                        <video
                          src={selectedFileUrl}
                          muted
                          playsInline
                          preload="metadata"
                          aria-label="Video preview"
                          className="h-full w-full object-cover"
                        />
                      ) : (
                        <NextImage
                          src={selectedFileUrl}
                          alt="Preview"
                          fill
                          sizes="64px"
                          unoptimized
                          className="object-cover"
                        />
                      )}
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
                      Drag & drop an image or walkthrough video
                    </span>
                    <span className="text-[10px] font-mono text-white/30">
                      Images up to 10 MB · Videos up to 5 GB
                    </span>
                  </>
                )}
              </div>
              {fileError && <span className="text-[10px] font-mono text-red-300/80" role="alert">{fileError}</span>}
              {selectedFile && !selectedVideo && (
                <span className="font-mono text-[10px] leading-relaxed text-amber-200/60">
                  For navigation training, use a walkthrough video with connected camera motion. A single image can make a visual preview, but cannot expose unseen space reliably.
                </span>
              )}
            </div>

            {selectedVideo && (
              <div className="grid grid-cols-1 gap-3 rounded-lg border border-white/10 bg-white/[0.025] p-3 sm:grid-cols-[1fr_8rem]">
                <div className="flex flex-col gap-1.5">
                  <span className="font-mono text-[10px] font-semibold uppercase tracking-wider text-white/50">
                    Reconstruction quality
                  </span>
                  <div className="grid grid-cols-4 gap-1 rounded-md bg-black/25 p-1">
                    {([
                      ['preview', 'Preview'],
                      ['standard', 'Standard'],
                      ['quality', 'Quality'],
                      ['navigation', 'Navigation'],
                    ] as const).map(([value, label]) => (
                      <button
                        key={value}
                        type="button"
                        onClick={() => setVideoQuality(value)}
                        className={`rounded px-2 py-1.5 font-mono text-[10px] transition ${
                          videoQuality === value
                            ? 'bg-white text-black'
                            : 'text-white/45 hover:bg-white/[0.06] hover:text-white/75'
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <span className="font-mono text-[9px] leading-relaxed text-white/30">
                    {videoQuality === 'preview'
                      ? '140 frames · 7k steps'
                      : videoQuality === 'standard'
                        ? '280 frames · 20k steps'
                        : videoQuality === 'quality'
                          ? '400 frames · 30k steps · denser coverage'
                          : '480 frames · 45k steps · tracking + coverage checks'}
                  </span>
                  <label className="flex items-start gap-2 text-xs leading-relaxed text-white/65">
                    <input type="checkbox" checked={previewFirst} onChange={(event) => setPreviewFirst(event.target.checked)} className="mt-1" />
                    <span>Preview first. Review the result before the longer refinement.</span>
                  </label>
                  {videoQuality === 'navigation' && (
                    <span className="font-mono text-[9px] leading-relaxed text-cyan-200/55">
                      Recommended for navigation training. Walk slowly through connected spaces and return near the start; incomplete tracks are rejected before training.
                    </span>
                  )}
                </div>
                <label className="flex flex-col gap-1.5 font-mono text-[10px] uppercase tracking-wider text-white/50">
                  Camera height
                  <div className="flex items-center rounded-md border border-white/10 bg-black/25 px-2">
                    <input
                      type="number"
                      min={0.5}
                      max={3}
                      step={0.05}
                      value={cameraHeight}
                      onChange={(event) => setCameraHeight(Number(event.target.value))}
                      className="min-w-0 flex-1 bg-transparent py-1.5 text-right text-xs text-white outline-none"
                    />
                    <span className="ml-1 text-white/30">m</span>
                  </div>
                </label>
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
                disabled={!selectedFile || !worldName.trim() || (!usingMarble && backendStatus !== 'ready')}
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
