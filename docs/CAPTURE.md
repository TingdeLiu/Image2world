# Capturing demo footage

Two different deliverables, and conflating them is the usual mistake:

| | README loop | Full demo video |
| --- | --- | --- |
| Length | 7–8 s | 90–120 s |
| Sound | none | voiceover or captions |
| Plays | automatically, forever | on click |
| Job | stop the scroll | explain the project |
| Lives in | `README.md`, social posts | YouTube, Devpost, HN comments |

The README loop matters more. It is the first thing a stranger sees, it has no
narrator, and it has about two seconds to break their assumption that this is a
photo with a parallax effect.

## The one idea both must land

Turning around shows a room the source photo never contained. Every other
feature is secondary — a viewer who does not grasp this thinks the project is a
depth-map toy.

So the turn is never the opening shot. Let the viewer build the assumption
first, then break it. The payoff has to come last.

## Source worlds

| World | Pipeline | Use it for |
| --- | --- | --- |
| `marble-from-photo-7y19ti` | Marble | **The hero shots.** Full-res variant of the bundled `demo-office`, so footage matches the live demo while looking its best |
| `marble-office` | Marble | Alternate hero. This is the world measured at 0/24 open directions and 211 m² |
| `sunlit-office-k7hil8` | SHARP | The "problem" shot — walking out into nothing, streaking |
| `bounded-t3nfxd` | SHARP | Backup problem shot |

Quote measurements only against the world they were measured on. The 0/24 and
211 m² figures belong to `marble-office`.

## Shot list — README loop (7.5 s)

As shipped in `docs/media/demo-loop.webp` (8.2 s):

| Time | Shot | Why |
| --- | --- | --- |
| 0.0–1.0 s | The source photo, held still, filling the frame | Viewer files it as "a photo" |
| 1.0–1.6 s | Crossfade into the 3D scene | The room appears to gain depth rather than be replaced |
| 1.6–3.7 s | Walk forward. Parallax opens up | "That was not a photo" |
| 3.7–4.2 s | Stop | Lets the turn read as deliberate |
| 4.2–7.6 s | Turn 180° slowly | The room continues where the photo ended |
| 7.6–8.2 s | Hold on the reveal, loop back to frame 1 | Reads as finished, not truncated |

**Crossfade, not a hard cut** — this was tried the other way first. A hard cut
only works if the 3D opening frame matches the photo, and it does not: the
spawn point is derived from the collider bounding box, not the original camera
pose, so the viewer starts further into the room with a narrower field of view.
Cut hard and it reads as two unrelated shots. Dissolved, the furniture roughly
aligns and the transition sells the actual claim — that the photo *became* the
space.

Turn *slowly*. A fast whip-pan reads as a camera trick; a slow turn lets the
viewer verify the geometry is really there. This is the whole video.

## Shot list — full video (~100 s)

| Time | Beat |
| --- | --- |
| 0:00–0:12 | The loop above, unhurried |
| 0:12–0:30 | **The problem.** In a SHARP world, walk past what the camera saw and fall out. Show the streaking when you leave the capture pose. Caption: single-view reconstruction, 8 of 24 directions open |
| 0:30–0:50 | **The fix.** Same interaction in a Marble world. Caption: 0 of 24, 211 m² |
| 0:50–1:10 | **The product.** Drag a photo into the dialog, streamed progress, world opens |
| 1:10–1:30 | **Depth.** Scene editor moving an object, physics, USD export for Isaac Sim |
| 1:30–1:45 | MIT, GitHub URL, live demo URL |

The 0:12–0:50 comparison is the most persuasive stretch, because the claim is
measured rather than asserted. Do not cut it for time.

## Before recording

1. Run a production build, not the dev server — HMR and dev overlays cost
   frames. Stop the dev server first, they share `.next`:
   ```bash
   npm run build
   ```
2. `npm start`, then open the world.
3. Set quality to **High** (the sidebar toggle) so the full-res splat loads.
4. Wait for the splat to fully resolve before rolling. It sharpens
   progressively and an early start records the blurry phase.
5. Press `` ` `` to hide the interface.
6. `F11` for browser fullscreen, so no tabs or URL bar are in frame.
7. Silence notifications.

## Driving the camera

Record the camera with a script rather than your hand. A hand-held mouse turn
wobbles, and the turn is the one shot that has to look effortless.

This works without modifying the app: the controllers read synthetic events.
Movement keys are matched on `event.code` with no `isTrusted` check, and
pixel-mode wheel deltas are routed to the tumble handler
(`src/modules/camera/useCameraGestures.ts`).

Paste into the browser console:

```js
const cam = (() => {
  const canvas = document.querySelector('canvas')
  if (!canvas) throw new Error('No canvas — is a world open?')

  const frame = () => new Promise(requestAnimationFrame)
  const easeInOutSine = (t) => -(Math.cos(Math.PI * t) - 1) / 2
  const linear = (t) => t

  // Pixel-mode wheel deltas reach onTumblePixels(-deltaX, -deltaY), so the
  // sign is flipped here: look({x: +n}) turns right.
  async function look({ x = 0, y = 0, ms = 1000, ease = easeInOutSine } = {}) {
    const start = performance.now()
    let done = 0
    for (;;) {
      const t = Math.min(1, (performance.now() - start) / ms)
      const p = ease(t)
      const step = p - done
      done = p
      if (step !== 0) {
        canvas.dispatchEvent(new WheelEvent('wheel', {
          deltaX: -x * step,
          deltaY: -y * step,
          deltaMode: 0,
          bubbles: true,
          cancelable: true,
        }))
      }
      if (t >= 1) return
      await frame()
    }
  }

  async function walk({ key = 'KeyW', ms = 1000 } = {}) {
    window.dispatchEvent(new KeyboardEvent('keydown', { code: key, bubbles: true }))
    await wait(ms)
    window.dispatchEvent(new KeyboardEvent('keyup', { code: key, bubbles: true }))
  }

  const wait = (ms) => new Promise((r) => setTimeout(r, ms))

  return { look, walk, wait, linear, easeInOutSine }
})()
```

### Calibrate the turn first

Pixels-per-degree depends on the sensitivity setting, so measure once:

```js
await cam.look({ x: 200, ms: 1000 })
```

Eyeball how far that turned, then scale. **At the default sensitivity, 1400 px
is very close to 180°** — that is the value the shipped loop uses. 800 px only
reached about 100°, which lands on a blank wall and wastes the reveal. Store the
number: every take needs the same value or the loop will not match.

### The README loop, as a sequence

Start recording, then run this. The leading `wait` gives you room to trim.

```js
await cam.wait(1500)
await cam.walk({ key: 'KeyW', ms: 2100 })
await cam.wait(500)
await cam.look({ x: 800, ms: 3400 })   // replace 800 with your calibration
await cam.wait(1200)
```

Keep the tab focused — `requestAnimationFrame` throttles in background tabs and
the motion will stutter.

For a shot that walks and turns at once, run them together:

```js
await Promise.all([
  cam.walk({ key: 'KeyW', ms: 3000 }),
  cam.look({ x: 300, ms: 3000, ease: cam.linear }),
])
```

Use `linear` for moves that continue past the cut, `easeInOutSine` for moves
that start and stop on camera.

## Screen recording

OBS Studio, Display or Window capture:

- 1920×1080, **60 fps** — splat rendering looks materially worse at 30
- Output: MP4, CQP/CRF **18** (visually lossless; the file is an intermediate)
- No webcam, no overlays, no mic for the loop

Record more than you need at both ends. Trimming is free; a re-take is not.

## Post

Trim first, convert second. `-ss` before `-i` seeks fast:

```bash
ffmpeg -ss 00:00:01.5 -i raw.mp4 -t 7.5 -c copy loop-trimmed.mp4
```

**Use WebP, not GIF.** This was measured on the real loop, and it is not close:

| Format | Settings | Size |
| --- | --- | --- |
| GIF | 24 fps, 960 px | 31 MB |
| GIF | 20 fps, 800 px | 19 MB |
| **WebP** | **24 fps, 960 px, q60** | **2.2 MB** |

A moving splat scene defeats GIF entirely. Every pixel changes every frame, so
inter-frame compression finds nothing to reuse, and 256 colours cannot hold a
photographic interior — even the degraded 19 MB variant is unshippable.

```bash
ffmpeg -i loop-trimmed.mp4 -vf "fps=24,scale=960:-1:flags=lanczos" -loop 0 -q:v 60 -compression_level 6 -y docs/media/demo-loop.webp
```

GitHub serves animated WebP untouched — verified: `content-type: image/webp`,
full byte count, autoplaying because of `-loop 0`. Reference it with an `<img>`
tag so the width can be pinned:

```html
<img src="./docs/media/demo-loop.webp" alt="…" width="820">
```

`ffprobe` reports nothing useful for animated WebP (`image data not found`,
`width=0`). That is a demuxer limitation, not a broken file. Check frames with
Pillow instead:

```bash
python -c "from PIL import Image; im=Image.open('docs/media/demo-loop.webp'); print(im.n_frames, im.size, im.is_animated)"
```

Also export an MP4. X, LinkedIn, and Devpost want a video file, and some
markdown renderers outside GitHub will not animate WebP:

```bash
ffmpeg -i loop-trimmed.mp4 -c:v libx264 -crf 23 -preset slow -pix_fmt yuv420p -movflags +faststart -y docs/media/demo-loop.mp4
```

For the long video, keep MP4 and upload it; never turn 100 seconds into a GIF.

## Placing it

The loop goes directly under the title in `README.md`, above the badges. A
reader deciding whether to keep scrolling should not have to get past a table of
contents first.
