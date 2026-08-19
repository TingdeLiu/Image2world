'use client'

import { useState } from 'react'
import { GlobeHemisphereWest, Spinner } from '@phosphor-icons/react'
import { APP_REQUEST_HEADERS } from '../utils/sameOrigin'

/**
 * The whole sign-in surface.
 *
 * One password, because there is one operator: this instance authenticates the
 * person who deployed it, not a directory of users. Swapping in a real identity
 * provider later replaces this component and `auth.ts`, and nothing else --
 * every route already asks the same question through `guardRoute`.
 */
export function SignInForm({ next }: { next?: string }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError('')

    try {
      const response = await fetch('/api/auth/session', {
        method: 'POST',
        headers: { 'content-type': 'application/json', ...APP_REQUEST_HEADERS },
        body: JSON.stringify({ password }),
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as { error?: string }
        setError(payload.error || `Sign-in failed with HTTP ${response.status}.`)
        return
      }
      // A full navigation, not a client transition: the pages behind this were
      // never rendered for an unauthenticated visitor, so there is nothing
      // cached to reuse.
      window.location.assign(isSafeNext(next) ? next : '/')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="grid min-h-screen place-items-center bg-[#0a0b0a] px-6 text-[#f2f0e9]">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border border-white/10 bg-white/[0.03] p-8"
      >
        <div className="mb-6 flex items-center gap-3">
          <span className="grid h-9 w-9 place-items-center rounded-full border border-white/15 bg-white/[0.04]">
            <GlobeHemisphereWest size={18} weight="duotone" />
          </span>
          <span className="font-display text-lg tracking-[-0.03em]">Image2World</span>
        </div>

        <label htmlFor="access-password" className="mb-2 block font-mono text-[10px] uppercase tracking-[0.18em] text-white/40">
          Access password
        </label>
        <input
          id="access-password"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="w-full rounded-lg border border-white/10 bg-black/40 px-3 py-2 text-sm outline-none focus:border-[#e5ff63]/60"
        />

        {error && (
          <p role="alert" className="mt-3 text-xs text-red-300">{error}</p>
        )}

        <button
          type="submit"
          disabled={submitting || !password}
          className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-[#e5ff63] px-4 py-2 text-sm font-medium text-black transition disabled:opacity-40"
        >
          {submitting && <Spinner size={14} className="animate-spin" />}
          {submitting ? 'Signing in' : 'Sign in'}
        </button>

        <p className="mt-6 font-mono text-[10px] leading-relaxed text-white/30">
          This instance runs generation on its own GPU and can spend a Marble
          key, so it is not left open. Unset IMAGEWORLD_AUTH_SECRET to turn the
          gate off.
        </p>
      </form>
    </main>
  )
}

/**
 * Only ever return to a path on this site.
 *
 * `next` arrives in the URL, so it is attacker-controlled: without this, a
 * crafted sign-in link would send someone to another origin the moment they
 * authenticate, with the visit looking like it came from here.
 */
function isSafeNext(next: string | undefined): next is string {
  return Boolean(next) && next!.startsWith('/') && !next!.startsWith('//')
}
