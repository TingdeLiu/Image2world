import crypto from 'crypto'
import fs from 'fs'
import path from 'path'

/**
 * Durable state for a single-image generation.
 *
 * The pipeline used to be owned by the HTTP request that started it: progress
 * streamed back over that connection, and `request.signal` aborting tore the
 * work down and deleted the staged world. Which meant a refresh, a closed
 * laptop, or a flaky tunnel discarded a Marble generation eight minutes in --
 * and charged for it.
 *
 * So the request no longer owns the work. It creates a job, starts the pipeline
 * detached from itself, and returns an id. Progress lands in a file the browser
 * polls, which is how the video path has always worked
 * (`backend/reconstruction_jobs.py`); this is the same shape on the Node side,
 * because the image pipeline lives here -- it holds the Marble key, which never
 * touches the backend or the disk.
 */

const JOB_ROOT = path.resolve(
  process.env.IMAGEWORLD_GENERATION_JOB_ROOT
    || path.join(process.cwd(), '.artifacts', 'generation-jobs'),
)

export const GENERATION_STAGES = ['initializing', 'splat', 'finalizing'] as const
export type GenerationStage = (typeof GENERATION_STAGES)[number]

/** `running` is the only state a job can leave; the rest are terminal. */
export const TERMINAL_STATUSES = ['complete', 'failed', 'canceled', 'interrupted'] as const
export type GenerationStatus = 'running' | (typeof TERMINAL_STATUSES)[number]

export interface GenerationJob {
  id: string
  name: string
  /** Which backend is doing the work. The key itself is never recorded. */
  backend: 'marble' | 'sharp'
  status: GenerationStatus
  stage: GenerationStage
  progress: number
  message: string
  detail?: string
  slug?: string
  error?: string
  errorCode?: string
  retryable?: boolean
  createdAt: string
  updatedAt: string
  /** Last sign of life from the pipeline. See `trackJob`. */
  heartbeatAt?: string
}

export interface GenerationProgressPatch {
  stage: GenerationStage
  progress: number
  message: string
  detail?: string
}

const JOB_ID_PATTERN = /^[a-f0-9]{12}$/

/**
 * How a running job proves it is still alive, and how it learns it was told to
 * stop.
 *
 * Both have to go through the file. Next bundles each route handler separately,
 * so the module-level state in `/api/generate` is not the module-level state in
 * `/api/generate/jobs/[id]` -- an in-memory registry would report a live job as
 * interrupted from one route and fail to abort it from another. It did both
 * before this existed.
 */
const HEARTBEAT_INTERVAL_MS = 2_000
/** Generous next to the interval: a busy event loop must not look like death. */
const HEARTBEAT_TIMEOUT_MS = 60_000

/** Controllers for jobs started by *this* module instance. */
const running = new Map<string, AbortController>()
const heartbeats = new Map<string, ReturnType<typeof setInterval>>()

function isAlive(job: GenerationJob, now = Date.now()) {
  if (running.has(job.id)) return true
  const beat = Date.parse(job.heartbeatAt ?? job.updatedAt)
  return Number.isFinite(beat) && now - beat < HEARTBEAT_TIMEOUT_MS
}

function jobDir(id: string) {
  const resolved = path.resolve(JOB_ROOT, id)
  const relative = path.relative(JOB_ROOT, resolved)
  if (!JOB_ID_PATTERN.test(id) || relative !== id) {
    throw new Error('Invalid generation job id')
  }
  return resolved
}

function jobFile(id: string) {
  return path.join(jobDir(id), 'job.json')
}

/** Write through a temporary file so a reader never sees a half-written job. */
function writeJob(job: GenerationJob): GenerationJob {
  const target = jobFile(job.id)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  const temporary = `${target}.tmp`
  fs.writeFileSync(temporary, `${JSON.stringify(job, null, 2)}\n`, 'utf8')
  fs.renameSync(temporary, target)
  return job
}

export function isTerminal(job: GenerationJob) {
  return (TERMINAL_STATUSES as readonly string[]).includes(job.status)
}

/**
 * Read a job, reporting one whose heartbeat has stopped as interrupted.
 *
 * A generation cannot be resumed the way a reconstruction can -- there are no
 * checkpoints to pick up, and the staged world was deleted on the way down --
 * so the honest answer is that it stopped, not that it is still going. Without
 * this a crash leaves a job that polls at 40% forever.
 */
export function readJob(id: string): GenerationJob | undefined {
  // Resolve outside the try: a job that is simply absent is `undefined`, but an
  // id that tries to climb out of the job directory is a caller bug, and
  // swallowing it here would leave the guard in `jobDir` untested and easy to
  // lose in a later refactor.
  const file = jobFile(id)

  let job: GenerationJob
  try {
    job = JSON.parse(fs.readFileSync(file, 'utf8')) as GenerationJob
  } catch {
    return undefined
  }

  if (job.status !== 'running' || isAlive(job)) return job
  return writeJob({
    ...job,
    status: 'interrupted',
    error: 'The server stopped before this generation finished. Nothing was published.',
    updatedAt: new Date().toISOString(),
  })
}

/** Newest first. */
export function listJobs(): GenerationJob[] {
  if (!fs.existsSync(JOB_ROOT)) return []
  return fs.readdirSync(JOB_ROOT, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && JOB_ID_PATTERN.test(entry.name))
    .flatMap((entry) => readJob(entry.name) ?? [])
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
}

/**
 * The generation still in flight, if there is one.
 *
 * One at a time is not a limitation to work around: the backend serialises GPU
 * work anyway, and it is what lets a reloaded page find its way back to a
 * generation without having to remember an id.
 */
export function activeJob(): GenerationJob | undefined {
  return listJobs().find((job) => !isTerminal(job))
}

export function createJob(name: string, backend: GenerationJob['backend']): GenerationJob {
  const now = new Date().toISOString()
  return writeJob({
    id: crypto.randomBytes(6).toString('hex'),
    name,
    backend,
    status: 'running',
    stage: 'initializing',
    progress: 1,
    message: 'Queued',
    detail: 'Preparing the generation workspace',
    createdAt: now,
    updatedAt: now,
  })
}

/**
 * Start the heartbeat for a job and return the signal its pipeline should watch.
 *
 * The same tick does both halves of the file-based contract: it stamps
 * `heartbeatAt` so other routes can tell this job apart from an abandoned one,
 * and it re-reads the status so a cancel written by another route -- another
 * bundle, with its own module state -- actually reaches the pipeline.
 */
export function trackJob(id: string): AbortSignal {
  const controller = new AbortController()
  running.set(id, controller)

  const beat = setInterval(() => {
    let job: GenerationJob
    try {
      job = JSON.parse(fs.readFileSync(jobFile(id), 'utf8')) as GenerationJob
    } catch {
      return
    }
    if (job.status !== 'running') {
      stopHeartbeat(id)
      controller.abort()
      return
    }
    writeJob({ ...job, heartbeatAt: new Date().toISOString() })
  }, HEARTBEAT_INTERVAL_MS)

  // Node keeps the process alive for pending timers; a heartbeat is not a
  // reason to hold a server open.
  beat.unref?.()
  heartbeats.set(id, beat)
  return controller.signal
}

function stopHeartbeat(id: string) {
  const beat = heartbeats.get(id)
  if (beat) clearInterval(beat)
  heartbeats.delete(id)
}

export function reportProgress(id: string, patch: GenerationProgressPatch): void {
  const job = readJob(id)
  // A canceled job keeps its last words; late progress from a pipeline that has
  // not noticed the abort yet must not overwrite them.
  if (!job || isTerminal(job)) return
  writeJob({ ...job, ...patch, updatedAt: new Date().toISOString() })
}

function finish(id: string, patch: Partial<GenerationJob> & { status: GenerationStatus }) {
  const job = readJob(id)
  running.delete(id)
  stopHeartbeat(id)
  if (!job) return undefined
  // Cancellation wins: the pipeline reporting failure afterwards is a symptom
  // of the cancel, not a second outcome.
  if (isTerminal(job)) return job
  return writeJob({ ...job, ...patch, updatedAt: new Date().toISOString() })
}

export function completeJob(id: string, slug: string) {
  return finish(id, {
    status: 'complete',
    stage: 'finalizing',
    progress: 100,
    message: 'World generated',
    detail: 'Opening the finished scene',
    slug,
  })
}

export function failJob(id: string, error: string, errorCode?: string, retryable = true) {
  return finish(id, {
    status: 'failed',
    message: 'Generation failed',
    detail: error,
    error,
    ...(errorCode ? { errorCode } : {}),
    retryable,
  })
}

/**
 * Ask a job to stop, and record that immediately.
 *
 * The status is written before the pipeline unwinds, so a poll that lands
 * during cleanup reports "canceled" rather than the stage it happened to be in.
 */
export function cancelJob(id: string): GenerationJob | undefined {
  const job = readJob(id)
  if (!job) return undefined
  if (isTerminal(job)) return job

  const canceled = writeJob({
    ...job,
    status: 'canceled',
    message: 'Generation canceled',
    detail: 'No world was published',
    error: 'Generation canceled. No world was published.',
    updatedAt: new Date().toISOString(),
  })
  // Only this instance's own jobs can be aborted directly. A job owned by
  // another bundle picks the cancel up from the file on its next heartbeat.
  running.get(id)?.abort()
  running.delete(id)
  stopHeartbeat(id)
  return canceled
}
