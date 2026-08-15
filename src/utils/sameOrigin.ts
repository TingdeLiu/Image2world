/**
 * The header this app puts on its own API calls, and the gate every
 * side-effectful route checks before doing anything.
 *
 * Next's built-in cross-site protection covers `/_next` and `/__nextjs`, not
 * `/api`, so these routes are reachable from any page the user happens to have
 * open in another tab. A `POST` with no headers, or with `text/plain`, is a
 * CORS "simple request": the browser sends it without a preflight, and
 * `Request.json()` never looks at Content-Type. The attacker cannot read the
 * reply — but by then the write has happened. A wiped `scene.json`, an occupied
 * GPU, a file manager opening on the host.
 *
 * A custom header is what actually closes this. Any cross-origin `fetch`
 * carrying one stops being a simple request, so the browser must preflight it
 * first — and nothing here answers a preflight with `Access-Control-Allow-*`,
 * so the check fails and the real request is never sent. (Next does reply to
 * the `OPTIONS` itself, with a 204; it is the missing CORS headers, not the
 * status, that stops the request.)
 *
 * Which means: **adding CORS response headers to any of these routes, or a
 * global `headers()` entry in `next.config.ts`, disables this guard.** If a
 * route ever genuinely needs to be callable cross-origin, give it its own
 * allow-list rather than opening the door for all of them.
 *
 * `Sec-Fetch-Site` is only a fast path. It is absent on older browsers, on
 * non-browser clients, and on non-secure origins — Chrome omits the whole
 * `Sec-Fetch-*` family for `http://192.168.x.x`, which is exactly how a dev
 * server gets reached from a phone. So it may reject a request but must never
 * be what admits one — hence both conditions, not either.
 */
export const APP_REQUEST_HEADER = 'x-imageworld-app'

/** Spread into a `fetch` init so the request passes {@link isAppRequest}. */
export const APP_REQUEST_HEADERS: Readonly<Record<string, string>> = {
  [APP_REQUEST_HEADER]: '1',
}

/** Whether a request came from this app's own pages rather than another site. */
export function isAppRequest(request: Request): boolean {
  const site = request.headers.get('sec-fetch-site')
  if (site !== null && site !== 'same-origin' && site !== 'none') return false
  return request.headers.get(APP_REQUEST_HEADER) === '1'
}
