'use client'

import Image from 'next/image'
import Link from 'next/link'
import { useState } from 'react'
import {
  ArrowRight,
  Cube,
  CursorClick,
  GlobeHemisphereWest,
  ImageSquare,
  Plus,
  Sparkle,
  SpeakerHigh,
} from '@phosphor-icons/react'
import { CreateWorldModal } from './CreateWorldModal'

export interface FeaturedWorld {
  slug: string
  name: string
  imageUrl?: string
}

export function HomeLanding({ featuredWorld }: { featuredWorld?: FeaturedWorld }) {
  const [createModalOpen, setCreateModalOpen] = useState(false)

  return (
    <main className="landing-scroll relative h-screen overflow-x-hidden overflow-y-auto bg-[#0a0b0a] text-[#f2f0e9] selection:bg-[#e5ff63] selection:text-black">
      <div className="landing-grain pointer-events-none fixed inset-0 z-50 opacity-[0.035]" />

      <nav className="relative z-20 mx-auto flex w-full max-w-[1480px] items-center justify-between px-5 py-5 sm:px-8 lg:px-12">
        <Link href="/" className="group flex items-center gap-3" aria-label="Image2World home">
          <span className="grid h-9 w-9 place-items-center rounded-full border border-white/15 bg-white/[0.04] transition group-hover:rotate-[-8deg] group-hover:border-[#e5ff63]/60">
            <GlobeHemisphereWest size={18} weight="duotone" />
          </span>
          <span className="font-display text-lg tracking-[-0.03em]">Image2World</span>
          <span className="hidden rounded-full border border-white/10 px-2 py-1 font-mono text-[9px] uppercase tracking-[0.18em] text-white/40 sm:inline-flex">
            local ai studio
          </span>
        </Link>

        <div className="flex items-center gap-2">
          {featuredWorld && (
            <Link
              href={`/${featuredWorld.slug}`}
              className="hidden h-9 items-center gap-2 rounded-full px-4 text-xs text-white/65 transition hover:bg-white/[0.06] hover:text-white sm:inline-flex"
            >
              Open studio
              <ArrowRight size={13} />
            </Link>
          )}
          <button
            type="button"
            onClick={() => setCreateModalOpen(true)}
            className="inline-flex h-9 items-center gap-2 rounded-full bg-[#e5ff63] px-4 text-xs font-semibold text-[#10110c] transition hover:scale-[1.02] hover:bg-[#efff91] active:scale-[0.98]"
          >
            <Plus size={13} weight="bold" />
            Create world
          </button>
        </div>
      </nav>

      <section className="relative mx-auto grid min-h-[calc(100vh-80px)] w-full max-w-[1480px] items-center gap-12 px-5 pb-16 pt-8 sm:px-8 lg:grid-cols-[0.82fr_1.18fr] lg:px-12 lg:pb-20 lg:pt-4">
        <div className="relative z-10 max-w-2xl landing-reveal">
          <div className="mb-7 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.035] px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-white/55">
            <span className="h-1.5 w-1.5 rounded-full bg-[#e5ff63] shadow-[0_0_12px_#e5ff63]" />
            One image in · a world out
          </div>

          <h1 className="font-display max-w-[12ch] text-[clamp(3.7rem,8vw,8.6rem)] leading-[0.84] tracking-[-0.075em] text-[#f5f1e8]">
            Step inside your image.
          </h1>

          <p className="mt-8 max-w-lg text-base leading-7 text-white/52 sm:text-lg sm:leading-8">
            Turn a single frame into a space you can walk through—a Gaussian splat you can see,
            a collision mesh you can bump into, and a scene editor. Your machine. Your worlds.
          </p>

          <div className="mt-9 flex flex-col gap-3 sm:flex-row">
            <button
              type="button"
              onClick={() => setCreateModalOpen(true)}
              className="group inline-flex h-13 items-center justify-center gap-3 rounded-full bg-[#f2f0e9] px-6 text-sm font-semibold text-[#11120e] transition hover:bg-[#e5ff63]"
            >
              Build from an image
              <ArrowRight size={16} className="transition-transform group-hover:translate-x-1" />
            </button>
            {featuredWorld && (
              <Link
                href={`/${featuredWorld.slug}`}
                className="inline-flex h-13 items-center justify-center gap-3 rounded-full border border-white/14 bg-white/[0.035] px-6 text-sm text-white/75 transition hover:border-white/30 hover:bg-white/[0.07] hover:text-white"
              >
                Explore {featuredWorld.name}
              </Link>
            )}
          </div>

          <div className="mt-12 grid max-w-xl grid-cols-3 border-y border-white/10 py-5">
            {[
              ['01', 'Upload'],
              ['02', 'Generate'],
              ['03', 'Explore'],
            ].map(([index, label]) => (
              <div key={index} className="border-r border-white/10 px-4 first:pl-0 last:border-r-0 last:pr-0">
                <span className="block font-mono text-[9px] text-[#e5ff63]/70">{index}</span>
                <span className="mt-1 block text-xs uppercase tracking-[0.14em] text-white/48">{label}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="relative min-h-[440px] landing-reveal landing-reveal-delay sm:min-h-[570px] lg:min-h-[680px]">
          <div className="absolute inset-[3%_0_6%_8%] rotate-[1.4deg] overflow-hidden rounded-[1.75rem] border border-white/15 bg-[#171914] shadow-[0_40px_120px_rgba(0,0,0,0.55)] sm:inset-[2%_2%_7%_7%]">
            {featuredWorld?.imageUrl ? (
              <Image
                src={featuredWorld.imageUrl}
                alt={`${featuredWorld.name} generated scene`}
                fill
                priority
                unoptimized
                sizes="(max-width: 1024px) 92vw, 58vw"
                className="scale-[1.03] object-cover object-center"
              />
            ) : (
              <div className="absolute inset-0 bg-[radial-gradient(circle_at_62%_40%,#405044,transparent_27%),linear-gradient(145deg,#232a24,#0d0f0d_68%)]" />
            )}
            <div className="absolute inset-0 bg-[linear-gradient(180deg,rgba(5,7,5,0.08),transparent_50%,rgba(5,7,5,0.62))]" />
            <div className="absolute inset-0 opacity-25 [background-image:linear-gradient(rgba(255,255,255,.12)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.12)_1px,transparent_1px)] [background-size:56px_56px] [mask-image:linear-gradient(to_bottom,black,transparent_75%)]" />

            <div className="absolute left-5 right-5 top-5 flex items-center justify-between">
              <div className="flex items-center gap-2 rounded-full border border-white/15 bg-black/40 px-3 py-2 backdrop-blur-xl">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#e5ff63]" />
                <span className="font-mono text-[9px] uppercase tracking-[0.16em] text-white/68">Live world</span>
              </div>
              <div className="rounded-full border border-white/15 bg-black/40 px-3 py-2 font-mono text-[9px] uppercase tracking-[0.14em] text-white/60 backdrop-blur-xl">
                WebGL · 60 fps
              </div>
            </div>

            <div className="absolute bottom-5 left-5 right-5 flex items-end justify-between gap-3">
              <div className="max-w-[70%] rounded-2xl border border-white/15 bg-black/50 p-4 backdrop-blur-xl">
                <p className="font-mono text-[9px] uppercase tracking-[0.16em] text-[#e5ff63]/75">Generated scene</p>
                <p className="font-display mt-1 truncate text-xl tracking-[-0.03em]">
                  {featuredWorld?.name ?? 'Your next world'}
                </p>
                <p className="mt-2 text-[11px] text-white/45">
                  Walk it in first person · physics ready
                </p>
              </div>
              {featuredWorld && (
                <Link
                  href={`/${featuredWorld.slug}`}
                  aria-label={`Open ${featuredWorld.name}`}
                  className="grid h-12 w-12 shrink-0 place-items-center rounded-full bg-[#e5ff63] text-black transition hover:rotate-[-8deg] hover:scale-105"
                >
                  <ArrowRight size={18} weight="bold" />
                </Link>
              )}
            </div>
          </div>

          <div className="absolute left-0 top-[18%] hidden -rotate-6 rounded-2xl border border-white/15 bg-[#11130f]/88 p-4 shadow-2xl backdrop-blur-xl sm:block">
            <ImageSquare size={18} className="text-[#e5ff63]" />
            <p className="mt-8 font-mono text-[9px] uppercase tracking-[0.14em] text-white/38">Source</p>
            <p className="mt-1 text-xs text-white/76">One ordinary image</p>
          </div>

          <div className="absolute bottom-0 right-0 flex rotate-3 items-center gap-3 rounded-2xl border border-white/15 bg-[#ece9df] p-4 text-[#161712] shadow-2xl sm:right-[1%]">
            <div className="grid h-9 w-9 place-items-center rounded-full bg-[#c9de4e]">
              <Sparkle size={17} weight="fill" />
            </div>
            <div>
              <p className="font-mono text-[8px] uppercase tracking-[0.15em] text-black/45">Pipeline complete</p>
              <p className="mt-0.5 text-xs font-semibold">Scene ready to explore</p>
            </div>
          </div>
        </div>
      </section>

      <section className="border-t border-white/10 bg-[#e9e5da] text-[#171813]">
        <div className="mx-auto w-full max-w-[1480px] px-5 py-20 sm:px-8 lg:px-12 lg:py-28">
          <div className="grid gap-10 lg:grid-cols-[0.75fr_1.25fr]">
            <div>
              <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-black/45">Not another image generator</p>
              <h2 className="font-display mt-4 max-w-[9ch] text-5xl leading-[0.94] tracking-[-0.055em] sm:text-7xl">
                Make images inhabitable.
              </h2>
            </div>

            <div className="grid gap-px overflow-hidden rounded-2xl border border-black/10 bg-black/10 sm:grid-cols-2">
              {[
                { Icon: Cube, title: 'A space, not an effect', body: 'The photo becomes a Gaussian splat with a collision mesh, so the room is solid rather than merely visible.' },
                { Icon: CursorClick, title: 'Directable scenes', body: 'Move, rotate, scale, duplicate, and rebuild the composition in-browser.' },
                { Icon: SpeakerHigh, title: 'A world with sound', body: 'Generated ambience gives every scene physical presence.' },
                { Icon: GlobeHemisphereWest, title: 'Local by design', body: 'Run the open pipeline on your own GPU. Keep your images and worlds private.' },
              ].map(({ Icon, title, body }, index) => (
                <article key={title} className="min-h-64 bg-[#e9e5da] p-7 sm:p-8">
                  <div className="flex items-start justify-between">
                    <Icon size={24} weight="duotone" />
                    <span className="font-mono text-[9px] text-black/35">0{index + 1}</span>
                  </div>
                  <h3 className="font-display mt-16 text-2xl tracking-[-0.035em]">{title}</h3>
                  <p className="mt-3 max-w-xs text-sm leading-6 text-black/55">{body}</p>
                </article>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="relative overflow-hidden border-t border-white/10 px-5 py-24 text-center sm:px-8 lg:py-36">
        <div className="absolute left-1/2 top-1/2 h-96 w-96 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[#e5ff63]/10 blur-[110px]" />
        <div className="relative mx-auto max-w-4xl">
          <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[#e5ff63]/70">Your image is already a place</p>
          <h2 className="font-display mt-5 text-5xl leading-[0.94] tracking-[-0.06em] sm:text-7xl lg:text-8xl">
            Go see what&apos;s inside.
          </h2>
          <button
            type="button"
            onClick={() => setCreateModalOpen(true)}
            className="group mx-auto mt-9 inline-flex h-13 items-center gap-3 rounded-full bg-[#e5ff63] px-7 text-sm font-semibold text-[#11120d] transition hover:scale-[1.025] hover:bg-[#efff91]"
          >
            <Plus size={15} weight="bold" />
            Create your first world
          </button>
        </div>
      </section>

      <footer className="border-t border-white/10 px-5 py-6 sm:px-8 lg:px-12">
        <div className="mx-auto flex w-full max-w-[1480px] flex-col gap-3 font-mono text-[9px] uppercase tracking-[0.14em] text-white/32 sm:flex-row sm:items-center sm:justify-between">
          <span>Image2World · Image to interactive 3D</span>
          <span>Built for creators who want control</span>
        </div>
      </footer>

      <CreateWorldModal open={createModalOpen} onClose={() => setCreateModalOpen(false)} />
    </main>
  )
}
