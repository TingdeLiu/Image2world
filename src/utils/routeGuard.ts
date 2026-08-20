import { AUTH_ENABLED, getSession, type Session } from './auth.ts'
import { isAppRequest } from './sameOrigin.ts'

/**
 * The one call every side-effectful route makes before doing anything.
 *
 * There are two independent questions and both have to be answered: *where*
 * the request came from (`isAppRequest`, which stops another site driving this
 * API in a visitor's browser) and *who* is asking (`getSession`, which stops
 * anyone who can reach the port from occupying the GPU or spending a Marble
 * key). They used to be one question, because only the first was being asked.
 *
 * Combining them into a single guard is the point. Eight routes each remembered
 * to call `isAppRequest`; asking them all to remember a second, differently
 * shaped check is how a ninth route ends up with one and not the other.
 *
 * The error shape matches what the routes already returned, so callers that
 * read `error.message` keep working.
 *
 * A plain `Response`, not a `NextResponse`: route handlers accept either, and
 * not importing `next/server` is what lets this module be tested outside the
 * bundler -- which, for the module that decides who gets to use the GPU, is
 * worth more than the convenience methods.
 */

export interface GuardResult {
  denied?: Response
  session?: Session
}

function refuse(message: string, status: number, code: string) {
  return Response.json(
    { error: { code, message, stage: 'initializing', retryable: false } },
    { status },
  )
}

/**
 * `denied` is a response to return immediately; its absence means proceed.
 *
 * Deliberately not a thrown exception: a route that forgets to catch would
 * turn a refusal into a 500, and a 500 on an auth check reads as a bug in the
 * app rather than as a closed door.
 */
export async function guardRoute(request: Request): Promise<GuardResult> {
  if (!isAppRequest(request)) {
    return { denied: refuse('Forbidden', 403, 'forbidden') }
  }
  if (!AUTH_ENABLED) return {}

  const session = await getSession(request)
  if (!session) {
    return { denied: refuse('Sign in to continue.', 401, 'unauthenticated') }
  }
  return { session }
}
