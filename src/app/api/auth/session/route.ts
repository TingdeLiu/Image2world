import { NextRequest, NextResponse } from 'next/server'
import {
  AUTH_ENABLED,
  checkPassword,
  clearedSessionCookie,
  getSession,
  isSecureRequest,
  issueSession,
  sessionCookie,
} from '../../../../utils/auth'
import { isAppRequest } from '../../../../utils/sameOrigin'

export const dynamic = 'force-dynamic'

/**
 * Sign in, sign out, and ask whether either is necessary.
 *
 * Deliberately not behind `guardRoute`: this is the route you call *before* you
 * have a session, so requiring one would be a closed loop. It keeps the
 * same-origin check, which is the half that still applies.
 */

/** Slow down guessing. Per process, which is all a single-operator app has. */
const ATTEMPT_WINDOW_MS = 60_000
const MAX_ATTEMPTS = 8
let attempts: { count: number; since: number } = { count: 0, since: 0 }

function tooManyAttempts() {
  const now = Date.now()
  if (now - attempts.since > ATTEMPT_WINDOW_MS) attempts = { count: 0, since: now }
  return attempts.count >= MAX_ATTEMPTS
}

export async function GET(request: NextRequest) {
  if (!isAppRequest(request)) {
    return NextResponse.json({ error: 'Forbidden' }, { status: 403 })
  }
  const session = await getSession(request)
  return NextResponse.json({
    authRequired: AUTH_ENABLED,
    signedIn: !AUTH_ENABLED || Boolean(session),
    userId: session?.userId ?? null,
  })
}

export async function POST(request: NextRequest) {
  if (!isAppRequest(request)) {
    return NextResponse.json({ error: 'Forbidden' }, { status: 403 })
  }
  if (!AUTH_ENABLED) {
    // Answering "signed in" would be a lie, and answering 401 would strand a UI
    // that has no password to offer. This instance simply has no door.
    return NextResponse.json(
      { error: 'This instance has no authentication configured.' },
      { status: 409 },
    )
  }
  if (tooManyAttempts()) {
    return NextResponse.json(
      { error: 'Too many attempts. Wait a minute and try again.' },
      { status: 429 },
    )
  }

  let password: string
  try {
    password = ((await request.json()) as { password?: string }).password ?? ''
  } catch {
    return NextResponse.json({ error: 'Invalid request.' }, { status: 400 })
  }

  if (!(await checkPassword(password))) {
    attempts.count += 1
    if (!attempts.since) attempts.since = Date.now()
    return NextResponse.json({ error: 'Incorrect password.' }, { status: 401 })
  }

  attempts = { count: 0, since: 0 }
  const { token, session } = await issueSession()
  const response = NextResponse.json({ signedIn: true, userId: session.userId })
  response.headers.set('set-cookie', sessionCookie(token, { secure: isSecureRequest(request) }))
  return response
}

export async function DELETE(request: NextRequest) {
  if (!isAppRequest(request)) {
    return NextResponse.json({ error: 'Forbidden' }, { status: 403 })
  }
  const response = NextResponse.json({ signedIn: false })
  response.headers.set('set-cookie', clearedSessionCookie({ secure: isSecureRequest(request) }))
  return response
}
