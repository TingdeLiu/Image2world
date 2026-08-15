import fs from 'fs'

/**
 * Turn a name a person typed into a slug.
 *
 * The slug is a directory name and a URL, so it has to survive both: lower
 * case, digits and hyphens, nothing else. `createWorldSlug` appends six random
 * characters on top of this because it is naming a world nobody asked to name;
 * renaming does not, because the user chose these words and would not
 * recognise them with a hash stapled on.
 *
 * Returns an empty string when nothing usable is left, which the caller has to
 * treat as "not a name" rather than as a directory called "".
 */
export function slugify(name: string) {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '')
}

const RETRY_DELAYS_MS = [80, 200, 500, 1200]

const TRANSIENT_CODES = new Set(['EPERM', 'EACCES', 'EBUSY', 'ENOTEMPTY'])

const isTransient = (error: unknown) =>
  TRANSIENT_CODES.has((error as NodeJS.ErrnoException)?.code ?? '')

export type MoveOutcome = 'renamed' | 'copied'

/**
 * Move a directory, falling back to copying when it cannot be renamed.
 *
 * A rename is atomic and free; a copy is neither, so it is the fallback rather
 * than the plan. But under `public/` a rename is not always available: the dev
 * server watches that tree, and Windows refuses to rename a directory another
 * process holds open, so every attempt there fails with `EPERM` for as long as
 * the watcher lives. Production has no watcher and renames normally, which is
 * why this retries first instead of copying outright — and why anyone testing
 * this in `next dev` will see the slow path every time.
 *
 * Throws only if both routes fail, leaving the source where it was.
 */
export async function moveDirectory(from: string, to: string): Promise<MoveOutcome> {
  for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt += 1) {
    try {
      fs.renameSync(from, to)
      return 'renamed'
    } catch (error) {
      // A non-transient failure will not improve by waiting, and neither will
      // copying help if the source is unreadable, so give up on both.
      if (!isTransient(error)) throw error
      if (attempt === RETRY_DELAYS_MS.length) break
      await new Promise((resolve) => setTimeout(resolve, RETRY_DELAYS_MS[attempt]))
    }
  }

  try {
    fs.cpSync(from, to, { recursive: true })
  } catch (error) {
    // A copy that stopped halfway leaves a partial world at the destination,
    // which the scanner would list as real and which permanently claims that
    // name. Take it back out before reporting the failure.
    fs.rmSync(to, { recursive: true, force: true })
    throw error
  }

  try {
    fs.rmSync(from, { recursive: true, force: true })
  } catch (error) {
    // The copy succeeded, so `to` is the good one and must be left alone --
    // whatever is holding `from` open would likely hold `to` too, and losing
    // both is the one outcome worse than a duplicate. Take the source out of
    // the scanner's view instead: `readWorlds` skips any directory without a
    // project.json, and unlinking one file is far more likely to succeed than
    // removing a tree.
    console.warn(`[Worlds] Copied ${from} to ${to} but could not remove the original:`, error)
    try {
      fs.rmSync(`${from}/project.json`, { force: true })
    } catch {
      // Then it stays visible as a duplicate, which the caller reports.
    }
  }
  return 'copied'
}
