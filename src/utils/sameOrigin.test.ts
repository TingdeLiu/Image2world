import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { APP_REQUEST_HEADER, APP_REQUEST_HEADERS, isAppRequest } from './sameOrigin.ts'

/**
 * These pin the shape of the CSRF gate on every side-effectful API route.
 *
 * The failure mode this guards against is quiet: a route that admits a request
 * it should not still returns 200, and the damage — a wiped scene, an occupied
 * GPU — looks like something else entirely. So the cases below spell out both
 * halves, what must be let in and what must not.
 */

function request(headers: Record<string, string>) {
  return new Request('http://localhost:3000/api/scene-project?slug=demo', {
    method: 'POST',
    headers,
  })
}

describe('isAppRequest', () => {
  it('admits the app\'s own fetches', () => {
    assert.equal(isAppRequest(request({ ...APP_REQUEST_HEADERS })), true)
    assert.equal(
      isAppRequest(request({ ...APP_REQUEST_HEADERS, 'sec-fetch-site': 'same-origin' })),
      true,
    )
  })

  it('admits a request with no Sec-Fetch-Site at all', () => {
    // Older browsers and non-browser clients omit the header. It may reject a
    // request but must never be the thing that admits one, so its absence
    // leaves the custom header as the only gate — which is the design.
    assert.equal(isAppRequest(request({ ...APP_REQUEST_HEADERS })), true)
  })

  it('rejects a bare cross-site post', () => {
    // The attack: a form or a headerless fetch from another page. Both are CORS
    // simple requests, so the browser sends them without a preflight.
    assert.equal(isAppRequest(request({})), false)
    assert.equal(isAppRequest(request({ 'content-type': 'text/plain' })), false)
  })

  it('rejects a forged header when Sec-Fetch-Site says cross-site', () => {
    // A cross-origin fetch cannot actually set the custom header without
    // passing a preflight first, but if one ever arrives, the browser's own
    // account of where it came from wins.
    assert.equal(
      isAppRequest(request({ ...APP_REQUEST_HEADERS, 'sec-fetch-site': 'cross-site' })),
      false,
    )
    assert.equal(
      isAppRequest(request({ ...APP_REQUEST_HEADERS, 'sec-fetch-site': 'same-site' })),
      false,
    )
  })

  it('rejects a same-origin request that is missing the header', () => {
    // Belt and braces: same-origin alone is not enough, because Sec-Fetch-Site
    // is absent often enough that trusting it on its own would open the gate.
    assert.equal(isAppRequest(request({ 'sec-fetch-site': 'same-origin' })), false)
  })

  it('rejects a header with any value other than 1', () => {
    assert.equal(isAppRequest(request({ [APP_REQUEST_HEADER]: 'true' })), false)
    assert.equal(isAppRequest(request({ [APP_REQUEST_HEADER]: '' })), false)
  })
})
