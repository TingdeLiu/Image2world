import { NextResponse, type NextRequest } from 'next/server'
import { AUTH_ENABLED, SIGN_IN_PATH, getSession } from './utils/auth'

/**
 * The gate for everything that is not an API route.
 *
 * Guarding `/api/*` alone would have been security theatre. The pages are
 * server components that read the same worlds off disk and render them, and the
 * splats themselves sit in `public/worlds`, where Next serves them as static
 * files -- so anyone holding a slug could fetch a world's geometry directly
 * without touching a single guarded route.
 *
 * API routes are excluded here on purpose. They answer with JSON that the app's
 * own fetch calls already know how to read; redirecting them to an HTML sign-in
 * page would turn a 401 into a parse error. `routeGuard.ts` covers them.
 *
 * When authentication is not configured this returns immediately, which is the
 * default and the behaviour a laptop has always had.
 */
export async function middleware(request: NextRequest) {
  if (!AUTH_ENABLED) return NextResponse.next()
  if (await getSession(request)) return NextResponse.next()

  const { pathname, search } = request.nextUrl

  // A world asset is fetched by the renderer, not navigated to. Redirecting it
  // would hand a WebGL loader an HTML page and produce a decode error instead
  // of a refusal, so say no in a way the caller can actually read.
  if (pathname.startsWith('/worlds/')) {
    return new NextResponse('Unauthorized', { status: 401 })
  }

  const signIn = new URL(SIGN_IN_PATH, request.url)
  // Come back to where they were headed once they are through.
  if (pathname !== '/') signIn.searchParams.set('next', `${pathname}${search}`)
  return NextResponse.redirect(signIn)
}

export const config = {
  matcher: [
    /*
     * Everything except:
     *   api          -- guarded by routeGuard, and answers in JSON
     *   sign-in      -- the door itself
     *   _next        -- the app's own build output
     *   assets       -- shared ambience, not per-world data
     *   favicon/etc  -- fetched before any session exists
     */
    '/((?!api|sign-in|_next/static|_next/image|assets/|favicon.ico|robots.txt).*)',
  ],
}
