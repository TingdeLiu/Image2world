import { cookies } from 'next/headers'
import { AUTH_ENABLED, OPERATOR_USER_ID, SESSION_COOKIE, verifySession } from './auth'

/**
 * The viewer, for server components.
 *
 * Separate from `auth.ts` because that module is also imported by
 * `middleware.ts`, which runs on the Edge runtime -- pulling `next/headers` in
 * there would break it. The verification itself is shared; only the way the
 * cookie is reached differs.
 *
 * Pages are already behind the middleware, so this is not the gate. It is what
 * decides *whose* worlds a page renders, and it matters the moment there is
 * more than one account.
 */
export async function currentViewer(): Promise<string | undefined> {
  if (!AUTH_ENABLED) return OPERATOR_USER_ID
  const token = (await cookies()).get(SESSION_COOKIE)?.value
  return (await verifySession(token))?.userId
}
