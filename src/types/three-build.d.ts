// three-compat.ts imports Three.js directly from its build file (bypassing the
// `three$` webpack/turbopack alias to avoid a self-referential resolution loop).
// TypeScript has no declarations for that raw path, so we map it to the official
// `three` package types here. Keeps `tsc --noEmit` clean without `ignoreBuildErrors`.
declare module '*/three/build/three.module.js' {
  export * from 'three'
}
