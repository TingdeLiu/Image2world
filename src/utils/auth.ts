/**
 * Who is asking, as distinct from where the request came from.
 *
 * `isAppRequest` in `sameOrigin.ts` answers "did this come from one of our own
 * pages" -- that is CSRF, not identity. It stops another site from driving this
 * API inside a visitor's browser, and it does nothing whatsoever about someone
 * pointing curl at the host. Until this module existed that was the entire
 * access control: every route that occupies the GPU, spends a Marble key or
 * writes into `public/worlds` was reachable by anyone who could reach the port.
 *
 * The two are complementary and both are needed. The cookie below carries
 * identity and is sent automatically by the browser -- which is exactly what
 * makes cookies forgeable across sites -- so it is the custom-header check that
 * keeps it safe. Removing either one weakens the other.
 *
 * ## Web Crypto, not node:crypto
 *
 * The same verification has to run in `middleware.ts`, which gates the pages
 * and the world assets under `public/worlds`. Middleware runs on the Edge
 * runtime, where `node:crypto` and `Buffer` do not exist. Web Crypto exists in
 * both runtimes, so there is one implementation rather than two that can drift
 * -- at the cost of `verifySession` being async, which is why the route guard
 * is too.
 *
 * ## Off unless configured
 *
 * With no secret set this module reports "no authentication configured" and
 * every guard passes, which is the behaviour this app has always had and the
 * one a laptop wants. Turning it on is two environment variables, and they are
 * unprefixed on purpose: `NEXT_PUBLIC_*` is inlined at build time, so a gate
 * that read one could be baked open by whichever machine ran the build. Same
 * reasoning as `localTools.ts`.
 */

const SECRET = process.env.IMAGEWORLD_AUTH_SECRET || ''
const PASSWORD = process.env.IMAGEWORLD_ACCESS_PASSWORD || ''

/**
 * Both halves are required, and the reason is the failure mode of getting it
 * half-configured: a secret with no password would lock the operator out of
 * their own instance, and a password with no secret would issue cookies nothing
 * could verify. Neither is a state worth supporting, so neither turns auth on.
 */
export const AUTH_ENABLED = Boolean(SECRET && PASSWORD)

export const SESSION_COOKIE = 'imageworld_session'
export const SIGN_IN_PATH = '/sign-in'
const SESSION_TTL_SECONDS = 60 * 60 * 24 * 30

/** The only account this provider knows about. See the module comment. */
export const OPERATOR_USER_ID = 'operator'

export interface Session {
  userId: string
  /** Seconds since the epoch. */
  expiresAt: number
}

const encoder = new TextEncoder()

function base64url(bytes: Uint8Array) {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function fromBase64url(value: string) {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/')
    .padEnd(Math.ceil(value.length / 4) * 4, '=')
  const binary = atob(padded)
  return Uint8Array.from(binary, (character) => character.charCodeAt(0))
}

async function sha256(value: string) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value)))
}

/**
 * Compare without leaking where two values first differ.
 *
 * Both sides are hashed to a fixed width first, so a length mismatch cannot
 * short-circuit the loop and leak the length either.
 */
async function equals(a: string, b: string) {
  const [left, right] = await Promise.all([sha256(a), sha256(b)])
  let difference = 0
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ right[index]
  }
  return difference === 0
}

async function signingKey() {
  return crypto.subtle.importKey(
    'raw',
    encoder.encode(SECRET),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
}

async function sign(payload: string) {
  const signature = await crypto.subtle.sign('HMAC', await signingKey(), encoder.encode(payload))
  return base64url(new Uint8Array(signature))
}

export async function checkPassword(candidate: string) {
  if (!AUTH_ENABLED) return false
  return equals(candidate, PASSWORD)
}

/** A signed, self-contained session token. No server-side store to keep. */
export async function issueSession(userId = OPERATOR_USER_ID) {
  const session: Session = {
    userId,
    expiresAt: Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS,
  }
  const payload = base64url(encoder.encode(JSON.stringify(session)))
  return { token: `${payload}.${await sign(payload)}`, session }
}

/**
 * Read a token back, rejecting anything tampered with or past its expiry.
 *
 * Resolves to `undefined` rather than throwing: an unreadable cookie means "not
 * signed in", which is a normal state, not an error to surface.
 */
export async function verifySession(token: string | undefined): Promise<Session | undefined> {
  if (!AUTH_ENABLED || !token) return undefined
  const [payload, signature] = token.split('.')
  if (!payload || !signature) return undefined
  if (!(await equals(signature, await sign(payload)))) return undefined

  try {
    const session = JSON.parse(new TextDecoder().decode(fromBase64url(payload))) as Session
    if (typeof session.userId !== 'string' || typeof session.expiresAt !== 'number') return undefined
    if (session.expiresAt * 1000 <= Date.now()) return undefined
    return session
  } catch {
    return undefined
  }
}

export function sessionCookieValue(request: Request) {
  const header = request.headers.get('cookie')
  if (!header) return undefined
  for (const part of header.split(';')) {
    const [key, ...rest] = part.trim().split('=')
    if (key === SESSION_COOKIE) return decodeURIComponent(rest.join('='))
  }
  return undefined
}

export async function getSession(request: Request) {
  return verifySession(sessionCookieValue(request))
}

/**
 * The identity a route should attribute its work to.
 *
 * With authentication off there is exactly one user of a machine-local app, and
 * calling them `OPERATOR_USER_ID` keeps ownership meaningful either way: worlds
 * made before auth was switched on stay owned by the same name afterwards,
 * rather than becoming orphans the moment it is.
 */
export async function currentUserId(request: Request) {
  if (!AUTH_ENABLED) return OPERATOR_USER_ID
  return (await getSession(request))?.userId
}

export interface SessionCookieOptions {
  secure: boolean
}

function cookie(value: string, maxAge: number, { secure }: SessionCookieOptions) {
  return [
    `${SESSION_COOKIE}=${value}`,
    'Path=/',
    'HttpOnly',
    'SameSite=Lax',
    `Max-Age=${maxAge}`,
    ...(secure ? ['Secure'] : []),
  ].join('; ')
}

export function sessionCookie(token: string, options: SessionCookieOptions) {
  return cookie(encodeURIComponent(token), SESSION_TTL_SECONDS, options)
}

export function clearedSessionCookie(options: SessionCookieOptions) {
  return cookie('', 0, options)
}

/** Whether the connection can carry a `Secure` cookie back. */
export function isSecureRequest(request: Request) {
  if (request.headers.get('x-forwarded-proto') === 'https') return true
  try {
    return new URL(request.url).protocol === 'https:'
  } catch {
    return false
  }
}
