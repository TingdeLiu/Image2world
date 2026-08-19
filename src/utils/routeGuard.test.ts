import assert from 'node:assert/strict'
import { after, before, describe, it } from 'node:test'

/**
 * The guard answers two questions and both have to be able to say no.
 *
 * The origin half was the only one that existed, so the regression worth
 * catching is a request that looks perfectly like the app's own page -- correct
 * header, same-site -- and simply has no session. That is curl with one header
 * set, which is the entire threat this was added for.
 */

const SECRET = 'a-secret-that-is-long-enough-to-be-real'
const PASSWORD = 'correct horse battery staple'

const previous = {
  secret: process.env.IMAGEWORLD_AUTH_SECRET,
  password: process.env.IMAGEWORLD_ACCESS_PASSWORD,
}

before(() => {
  process.env.IMAGEWORLD_AUTH_SECRET = SECRET
  process.env.IMAGEWORLD_ACCESS_PASSWORD = PASSWORD
})
after(() => {
  if (previous.secret === undefined) delete process.env.IMAGEWORLD_AUTH_SECRET
  else process.env.IMAGEWORLD_AUTH_SECRET = previous.secret
  if (previous.password === undefined) delete process.env.IMAGEWORLD_ACCESS_PASSWORD
  else process.env.IMAGEWORLD_ACCESS_PASSWORD = previous.password
})

// Both modules read the environment at import, so this has to happen after the
// assignments above -- hence top-level await rather than an import statement.
process.env.IMAGEWORLD_AUTH_SECRET = SECRET
process.env.IMAGEWORLD_ACCESS_PASSWORD = PASSWORD
const { guardRoute } = await import('./routeGuard.ts')
const { issueSession, sessionCookie, SESSION_COOKIE, APP_HEADER } = {
  ...(await import('./auth.ts')),
  APP_HEADER: (await import('./sameOrigin.ts')).APP_REQUEST_HEADER,
}

function request(headers: Record<string, string>) {
  return new Request('http://localhost:3000/api/generate', { method: 'POST', headers })
}

const fromOurPage = { [APP_HEADER]: '1', 'sec-fetch-site': 'same-origin' }

async function signedInCookie() {
  const { token } = await issueSession()
  return sessionCookie(token, { secure: false }).split(';')[0]
}

describe('guardRoute', () => {
  it('refuses a request from another site outright', async () => {
    const { denied } = await guardRoute(request({ 'sec-fetch-site': 'cross-site' }))
    assert.equal(denied?.status, 403)
  })

  it('refuses a request with no app header, session or not', async () => {
    const { denied } = await guardRoute(request({ cookie: await signedInCookie() }))
    assert.equal(denied?.status, 403)
  })

  it('refuses a same-origin request carrying no session', async () => {
    // curl with one header set. Nothing about the origin check stops this.
    const { denied } = await guardRoute(request(fromOurPage))
    assert.equal(denied?.status, 401)

    const body = await denied!.json() as { error: { code: string } }
    assert.equal(body.error.code, 'unauthenticated')
  })

  it('refuses a forged session cookie', async () => {
    const { denied } = await guardRoute(request({
      ...fromOurPage,
      cookie: `${SESSION_COOKIE}=not-a-real-token`,
    }))
    assert.equal(denied?.status, 401)
  })

  it('admits a signed-in request and reports who it is', async () => {
    const { denied, session } = await guardRoute(request({
      ...fromOurPage,
      cookie: await signedInCookie(),
    }))
    assert.equal(denied, undefined)
    assert.equal(session?.userId, 'operator')
  })
})
