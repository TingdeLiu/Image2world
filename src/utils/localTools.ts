/**
 * Whether the local developer tools are available.
 *
 * "Open world folder" and "open a terminal here" spawn processes on whichever
 * machine runs the server. On a laptop that is the point; on a deployed
 * instance it hands every visitor a way to launch processes on the host. The
 * BYOK path made that a real scenario -- with a Marble key, generation needs no
 * local GPU, so the app is deployable.
 *
 * Enabled in development, off in production unless the operator opts back in
 * with NEXT_PUBLIC_IMAGEWORLD_LOCAL_TOOLS=true (for running a production build
 * on your own machine). The NEXT_PUBLIC_ prefix is what lets one expression
 * gate both the API routes and the buttons that call them.
 */
export const LOCAL_TOOLS_ENABLED =
  process.env.NODE_ENV !== 'production' ||
  process.env.NEXT_PUBLIC_IMAGEWORLD_LOCAL_TOOLS === 'true'
