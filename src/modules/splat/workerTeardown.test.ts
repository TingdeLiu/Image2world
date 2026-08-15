import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { isSplatWorkerTeardown } from './workerTeardown.ts'

/**
 * This predicate decides which rejections get swallowed, so it is the whole
 * safety margin of the handler that uses it. Too loose and a real failure
 * disappears; too tight and the overlay comes back on every world switch.
 */
describe('isSplatWorkerTeardown', () => {
  it('matches the error Spark rejects pending work with', () => {
    assert.equal(isSplatWorkerTeardown(new Error('Worker terminate')), true)
  })

  it('leaves every other rejection alone', () => {
    for (const reason of [
      new Error('Worker terminated'),
      new Error('worker terminate'),
      new Error('Worker terminate: out of memory'),
      new Error('Failed to fetch'),
      new TypeError('Cannot read properties of undefined'),
    ]) {
      assert.equal(isSplatWorkerTeardown(reason), false, `${String(reason)} should not be swallowed`)
    }
  })

  it('does not match a bare string, which is not what Spark throws', () => {
    // `dispose` rejects with `new Error(...)`. A string with the same text is
    // something else, and something else is not ours to hide.
    assert.equal(isSplatWorkerTeardown('Worker terminate'), false)
    assert.equal(isSplatWorkerTeardown({ message: 'Worker terminate' }), false)
    assert.equal(isSplatWorkerTeardown(undefined), false)
    assert.equal(isSplatWorkerTeardown(null), false)
  })
})
