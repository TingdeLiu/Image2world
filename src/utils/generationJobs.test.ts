import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { after, describe, it } from 'node:test'

/**
 * A generation is no longer owned by the request that started it, so the job
 * file is the only record of what happened. Two things matter more than the
 * bookkeeping:
 *
 *   A cancelled job stays cancelled. The pipeline notices the abort somewhere
 *   inside a backend call and unwinds through the failure path, so "failed"
 *   arrives *after* "canceled" every time -- and must not overwrite it.
 *
 *   Liveness comes from the heartbeat in the file, not from memory. Next bundles
 *   each route handler separately, so the module state that started a job is not
 *   the module state that reads or cancels it -- an in-memory registry reported
 *   live jobs as interrupted and failed to abort them, and did so in practice.
 */

const JOB_ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'imageworld-jobs-'))
process.env.IMAGEWORLD_GENERATION_JOB_ROOT = JOB_ROOT

const {
  activeJob,
  cancelJob,
  completeJob,
  createJob,
  failJob,
  isTerminal,
  listJobs,
  readJob,
  reportProgress,
  trackJob,
} = await import('./generationJobs.ts')

after(() => fs.rmSync(JOB_ROOT, { recursive: true, force: true }))

/** A job the registry believes this process is running. */
function startedJob(name: string, backend: 'marble' | 'sharp' = 'sharp') {
  const job = createJob(name, backend)
  trackJob(job.id)
  return job
}

describe('createJob', () => {
  it('starts running, with a distinct id', () => {
    const first = startedJob('Kitchen')
    const second = startedJob('Studio', 'marble')

    assert.equal(first.status, 'running')
    assert.equal(first.name, 'Kitchen')
    assert.equal(second.backend, 'marble')
    assert.notEqual(first.id, second.id)
  })

  it('never records the Marble key, only which backend ran', () => {
    const job = startedJob('Loft', 'marble')
    const raw = fs.readFileSync(path.join(JOB_ROOT, job.id, 'job.json'), 'utf8')
    assert.equal(JSON.parse(raw).backend, 'marble')
    assert.equal(/key/i.test(raw), false, `job file mentions a key: ${raw}`)
  })
})

describe('reportProgress', () => {
  it('moves the job along', () => {
    const job = startedJob('Atrium')
    reportProgress(job.id, { stage: 'splat', progress: 40, message: 'Reconstructing', detail: 'SHARP' })

    const updated = readJob(job.id)
    assert.equal(updated?.stage, 'splat')
    assert.equal(updated?.progress, 40)
    assert.equal(updated?.detail, 'SHARP')
  })

  it('cannot revive a finished job', () => {
    const job = startedJob('Attic')
    completeJob(job.id, 'attic-a1b2c3')
    reportProgress(job.id, { stage: 'splat', progress: 40, message: 'Reconstructing' })

    assert.equal(readJob(job.id)?.status, 'complete')
    assert.equal(readJob(job.id)?.progress, 100)
  })
})

describe('outcomes', () => {
  it('records the published slug', () => {
    const job = startedJob('Hallway')
    completeJob(job.id, 'hallway-x1y2z3')

    const done = readJob(job.id)
    assert.equal(done?.status, 'complete')
    assert.equal(done?.slug, 'hallway-x1y2z3')
    assert.equal(isTerminal(done!), true)
  })

  it('records why a generation failed', () => {
    const job = startedJob('Basement')
    failJob(job.id, 'World reconstruction timed out after 240 seconds.', 'backend_timeout', true)

    const failed = readJob(job.id)
    assert.equal(failed?.status, 'failed')
    assert.equal(failed?.errorCode, 'backend_timeout')
    assert.equal(failed?.retryable, true)
    assert.match(failed?.error ?? '', /timed out/)
  })

  it('keeps a cancellation even though the pipeline fails afterwards', () => {
    const job = startedJob('Garage')
    cancelJob(job.id)
    // What the unwinding pipeline reports on its way out.
    failJob(job.id, 'World reconstruction could not reach the local AI backend.')

    const canceled = readJob(job.id)
    assert.equal(canceled?.status, 'canceled')
    assert.match(canceled?.error ?? '', /canceled/i)
  })

  it('treats cancelling a finished job as a no-op, not an error', () => {
    const job = startedJob('Porch')
    completeJob(job.id, 'porch-p1q2r3')

    assert.equal(cancelJob(job.id)?.status, 'complete')
  })

  it('has nothing to say about a job that does not exist', () => {
    assert.equal(readJob('0123456789ab'), undefined)
    assert.equal(cancelJob('0123456789ab'), undefined)
  })

  it('refuses an id that could climb out of the job directory', () => {
    assert.throws(() => readJob('../../etc'), /Invalid generation job id/)
    assert.throws(() => cancelJob('..'), /Invalid generation job id/)
  })
})

describe('after a crash', () => {
  /** Age a job's heartbeat, the way a stopped process would. */
  function silence(id: string, secondsAgo: number) {
    const file = path.join(JOB_ROOT, id, 'job.json')
    const job = JSON.parse(fs.readFileSync(file, 'utf8'))
    const stamp = new Date(Date.now() - secondsAgo * 1000).toISOString()
    fs.writeFileSync(file, JSON.stringify({ ...job, heartbeatAt: stamp, updatedAt: stamp }))
  }

  it('reports a job whose heartbeat stopped as interrupted', () => {
    const orphan = createJob('Rooftop', 'sharp')
    silence(orphan.id, 300)

    const found = readJob(orphan.id)
    assert.equal(found?.status, 'interrupted')
    assert.match(found?.error ?? '', /stopped/)
  })

  it('leaves a job that is still beating alone, even from another module', () => {
    // No trackJob: this is what the job looks like to a different route
    // bundle, which has its own module state and only the file to go on.
    const job = createJob('Cellar', 'sharp')
    silence(job.id, 3)

    assert.equal(readJob(job.id)?.status, 'running')
  })

  it('leaves a job this instance is running alone regardless of the file', () => {
    const job = startedJob('Pantry')
    silence(job.id, 300)

    assert.equal(readJob(job.id)?.status, 'running')
    cancelJob(job.id)
  })
})

describe('activeJob', () => {
  it('finds the generation still in flight', () => {
    const job = startedJob('Conservatory')
    const active = activeJob()

    assert.equal(active?.id, job.id)
    assert.equal(active?.name, 'Conservatory')
  })

  it('is empty once everything has finished', () => {
    for (const job of listJobs()) {
      if (!isTerminal(job)) cancelJob(job.id)
    }
    assert.equal(activeJob(), undefined)
  })

  it('lists newest first', () => {
    const names = listJobs().map((job) => job.name)
    assert.ok(names.includes('Conservatory'))
    const timestamps = listJobs().map((job) => job.createdAt)
    assert.deepEqual(timestamps, [...timestamps].sort((a, b) => b.localeCompare(a)))
  })
})
