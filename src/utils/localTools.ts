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
 * (for running a production build on their own machine).
 *
 * The two constants read different variables on purpose. Next inlines every
 * `NEXT_PUBLIC_*` reference at build time -- in the server bundle too -- so a
 * build machine that happened to have the flag set would bake `'true' ===
 * 'true'` into the route handlers, and no runtime environment could switch the
 * endpoints back off. The gate that actually protects the host therefore reads
 * an unprefixed variable, which stays a real lookup at runtime; the prefixed
 * one only decides whether buttons are drawn.
 *
 * Running a production build locally means setting both.
 */
export const LOCAL_TOOLS_ENABLED =
  process.env.NODE_ENV !== 'production' ||
  process.env.IMAGEWORLD_LOCAL_TOOLS === 'true'

/**
 * Whether to render the buttons that call those routes.
 *
 * Client-side, so this one has to be `NEXT_PUBLIC_`. It is a display decision:
 * showing a button whose route is disabled costs a 404, not an exposure.
 */
export const LOCAL_TOOLS_VISIBLE =
  process.env.NODE_ENV !== 'production' ||
  process.env.NEXT_PUBLIC_IMAGEWORLD_LOCAL_TOOLS === 'true'
