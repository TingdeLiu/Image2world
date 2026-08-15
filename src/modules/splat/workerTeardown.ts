/**
 * The message Spark rejects in-flight worker requests with when it shuts down.
 *
 * From `@sparkjsdev/spark`'s `_SplatWorker.dispose`:
 *
 *     dispose() {
 *       this.worker.terminate();
 *       ...
 *       for (const message of messages) message.reject(new Error("Worker terminate"));
 *     }
 *
 * Every pending request is rejected, and none of Spark's own call sites catch —
 * `await worker.call("sortSplats32", …)` and `await worker.call("traverseLodTrees", …)`
 * both let it propagate. With LOD on, one of those is in flight almost all the
 * time, so tearing a splat scene down — switching worlds, changing viewer
 * quality, deleting the world you are standing in — reliably produces an
 * unhandled rejection. In production that is a console line; in `next dev` the
 * error overlay covers the screen and reads as a crash.
 *
 * Nothing is wrong when this happens. The renderer asked the worker to stop and
 * the worker stopped.
 */
const TEARDOWN_MESSAGE = 'Worker terminate'

/** Whether a rejection is Spark cancelling its own work on the way out. */
export function isSplatWorkerTeardown(reason: unknown): boolean {
  return reason instanceof Error && reason.message === TEARDOWN_MESSAGE
}

let installed = false

/**
 * Stop that one rejection from surfacing as a crash.
 *
 * Deliberately a window-level handler rather than something wrapped around
 * Spark's internals: the promises are created and rejected entirely inside the
 * library, and the alternatives — opting out of React Three Fiber's automatic
 * disposal, or reaching into `sortWorker.messages` to blunt the rejections
 * before they fire — both depend on private details that would fail silently if
 * either library moved. This matches one exact message and leaves every other
 * rejection alone.
 *
 * Idempotent: several splat scenes can mount over a session and only one
 * listener is ever added.
 */
export function silenceSplatWorkerTeardown(): void {
  if (installed || typeof window === 'undefined') return
  installed = true
  window.addEventListener('unhandledrejection', (event) => {
    if (isSplatWorkerTeardown(event.reason)) event.preventDefault()
  })
}
