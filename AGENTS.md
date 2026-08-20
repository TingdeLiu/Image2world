<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Check what is actually installed before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

## Checking what is actually installed

The block above once pointed at `node_modules/next/dist/docs/`. **That directory
does not exist in this install** (Next 15.5.19), so nobody should go looking for
it. What is there:

```bash
node -p "require('next/package.json').version"   # the version you are targeting
```

Signatures live in `node_modules/next/dist/**/*.d.ts`. Behaviour that types
cannot express — runtimes, bundling, caching — is worth confirming against a
running server rather than from memory.

Four things in this version that have already cost time here:

- **`params` and `searchParams` are Promises.** `const { slug } = await params`.
- **`cookies()` and `headers()` are async.**
- **Route handlers are bundled separately from one another.** A module-level
  `Map` or singleton in `src/utils/…` is *not* shared between `/api/a` and
  `/api/b` — each gets its own instance. Cross-route coordination has to go
  through a file or a real store. Unit tests cannot catch this, because they
  import the module once; see `src/utils/generationJobs.ts` for the shape that
  works, and PROJECT_REPORT §3.25 for how it failed first.
- **`middleware.ts` runs on the Edge runtime**, where `node:crypto` and `Buffer`
  do not exist. `src/utils/auth.ts` is on Web Crypto for exactly this reason
  (PROJECT_REPORT §3.26), and must stay that way as long as the middleware
  imports it.
