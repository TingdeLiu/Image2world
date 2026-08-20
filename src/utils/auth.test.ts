import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

/**
 * A session token is the only thing standing between the internet and a GPU, so
 * what matters here is not that a valid token works -- it is that everything
 * else fails: a tampered payload, a swapped signature, an expired stamp, or a
 * token minted under a different secret.
 *
 * The module reads its configuration once at import, so each scenario imports
 * it fresh with a cache-busting query. That is also the only way to exercise
 * the unconfigured case, which is the default the app ships with.
 */

let instance = 0
async function loadAuth(env: Record<string, string | undefined>) {
  const keys = ['IMAGEWORLD_AUTH_SECRET', 'IMAGEWORLD_ACCESS_PASSWORD'] as const
  const previous = Object.fromEntries(keys.map((key) => [key, process.env[key]]))

  for (const key of keys) {
    // Assigning `undefined` to process.env stores the *string* "undefined",
    // which is truthy -- so "not configured" has to mean deleted, not unset.
    const value = env[key]
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }

  try {
    instance += 1
    return await import(`./auth.ts?case=${instance}`)
  } finally {
    for (const key of keys) {
      const value = previous[key]
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
}

const CONFIGURED = {
  IMAGEWORLD_AUTH_SECRET: 'a-secret-that-is-long-enough-to-be-real',
  IMAGEWORLD_ACCESS_PASSWORD: 'correct horse battery staple',
}

describe('with no secret configured', () => {
  it('reports authentication as off', async () => {
    const auth = await loadAuth({
      IMAGEWORLD_AUTH_SECRET: undefined,
      IMAGEWORLD_ACCESS_PASSWORD: undefined,
    })
    assert.equal(auth.AUTH_ENABLED, false)
  })

  it('attributes work to the operator rather than to nobody', async () => {
    // Worlds generated before the gate goes up must not become orphans when it
    // does, so the unauthenticated identity is the same one the password issues.
    const auth = await loadAuth({
      IMAGEWORLD_AUTH_SECRET: undefined,
      IMAGEWORLD_ACCESS_PASSWORD: undefined,
    })
    const request = new Request('http://localhost:3000/api/worlds')
    assert.equal(await auth.currentUserId(request), auth.OPERATOR_USER_ID)
  })

  it('accepts no password and mints no session', async () => {
    const auth = await loadAuth({
      IMAGEWORLD_AUTH_SECRET: undefined,
      IMAGEWORLD_ACCESS_PASSWORD: undefined,
    })
    assert.equal(await auth.checkPassword(''), false)
    assert.equal(await auth.checkPassword('anything'), false)
  })
})

describe('half-configured', () => {
  it('stays off with a secret but no password', async () => {
    // Otherwise the operator locks themselves out of their own instance.
    const auth = await loadAuth({
      IMAGEWORLD_AUTH_SECRET: 'secret',
      IMAGEWORLD_ACCESS_PASSWORD: undefined,
    })
    assert.equal(auth.AUTH_ENABLED, false)
  })

  it('stays off with a password but no secret', async () => {
    // Otherwise it issues cookies nothing can verify.
    const auth = await loadAuth({
      IMAGEWORLD_AUTH_SECRET: undefined,
      IMAGEWORLD_ACCESS_PASSWORD: 'hunter2',
    })
    assert.equal(auth.AUTH_ENABLED, false)
  })
})

describe('passwords', () => {
  it('accepts the configured one and nothing near it', async () => {
    const auth = await loadAuth(CONFIGURED)
    assert.equal(await auth.checkPassword('correct horse battery staple'), true)
    assert.equal(await auth.checkPassword('correct horse battery stapl'), false)
    assert.equal(await auth.checkPassword('correct horse battery staple '), false)
    assert.equal(await auth.checkPassword(''), false)
  })
})

describe('session tokens', () => {
  it('round-trips a freshly issued session', async () => {
    const auth = await loadAuth(CONFIGURED)
    const { token, session } = await auth.issueSession()

    const verified = await auth.verifySession(token)
    assert.equal(verified?.userId, session.userId)
    assert.equal(verified?.expiresAt, session.expiresAt)
  })

  it('rejects a payload edited after signing', async () => {
    const auth = await loadAuth(CONFIGURED)
    const { token } = await auth.issueSession()
    const [payload, signature] = token.split('.')

    const forged = Buffer.from(JSON.stringify({ userId: 'someone-else', expiresAt: 2 ** 40 }))
      .toString('base64url')
    assert.notEqual(forged, payload)
    assert.equal(await auth.verifySession(`${forged}.${signature}`), undefined)
  })

  it('rejects a token signed with a different secret', async () => {
    const mint = await loadAuth({ ...CONFIGURED, IMAGEWORLD_AUTH_SECRET: 'the-other-secret' })
    const check = await loadAuth(CONFIGURED)
    const { token } = await mint.issueSession()

    assert.equal(await check.verifySession(token), undefined)
  })

  it('rejects an expired session', async () => {
    const auth = await loadAuth(CONFIGURED)
    const expired = Buffer.from(JSON.stringify({
      userId: auth.OPERATOR_USER_ID,
      expiresAt: Math.floor(Date.now() / 1000) - 1,
    })).toString('base64url')
    // Sign it properly: expiry has to be checked on its own, not incidentally
    // by the signature failing.
    const { token } = await auth.issueSession()
    const goodSignature = token.split('.')[1]
    assert.equal(await auth.verifySession(`${expired}.${goodSignature}`), undefined)
  })

  it('rejects malformed and missing tokens', async () => {
    const auth = await loadAuth(CONFIGURED)
    for (const bad of [undefined, '', 'no-dot', '.', 'a.b', '...']) {
      assert.equal(await auth.verifySession(bad), undefined, `accepted ${JSON.stringify(bad)}`)
    }
  })

  it('verifies nothing at all while authentication is off', async () => {
    const mint = await loadAuth(CONFIGURED)
    const { token } = await mint.issueSession()
    const off = await loadAuth({
      IMAGEWORLD_AUTH_SECRET: undefined,
      IMAGEWORLD_ACCESS_PASSWORD: undefined,
    })
    assert.equal(await off.verifySession(token), undefined)
  })
})

describe('the session cookie', () => {
  it('is http-only and same-site, and drops Secure on plain http', async () => {
    const auth = await loadAuth(CONFIGURED)
    const cookie = auth.sessionCookie('token-value', { secure: false })

    assert.match(cookie, /^imageworld_session=token-value/)
    assert.match(cookie, /HttpOnly/)
    assert.match(cookie, /SameSite=Lax/)
    assert.equal(/Secure/.test(cookie), false, 'a Secure cookie over http is never sent back')
  })

  it('marks Secure when the request arrived over https', async () => {
    const auth = await loadAuth(CONFIGURED)
    assert.match(auth.sessionCookie('t', { secure: true }), /Secure/)
  })

  it('expires immediately when cleared', async () => {
    const auth = await loadAuth(CONFIGURED)
    assert.match(auth.clearedSessionCookie({ secure: false }), /Max-Age=0/)
  })

  it('reads its own cookie out of a request, ignoring the others', async () => {
    const auth = await loadAuth(CONFIGURED)
    const { token } = await auth.issueSession()
    const request = new Request('http://localhost:3000/', {
      headers: { cookie: `theme=dark; ${auth.SESSION_COOKIE}=${encodeURIComponent(token)}; other=1` },
    })

    assert.equal((await auth.getSession(request))?.userId, auth.OPERATOR_USER_ID)
  })
})

describe('isSecureRequest', () => {
  it('trusts a proxy that says the hop was https', async () => {
    const auth = await loadAuth(CONFIGURED)
    const request = new Request('http://localhost:3000/', {
      headers: { 'x-forwarded-proto': 'https' },
    })
    assert.equal(auth.isSecureRequest(request), true)
  })

  it('reads the scheme when there is no proxy', async () => {
    const auth = await loadAuth(CONFIGURED)
    assert.equal(auth.isSecureRequest(new Request('https://example.com/')), true)
    assert.equal(auth.isSecureRequest(new Request('http://example.com/')), false)
  })
})
