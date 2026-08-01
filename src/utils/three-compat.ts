// Re-export everything from the real three module
export * from '../../node_modules/three/build/three.module.js'

// Re-inject deprecated constants that were removed in Three.js v168+
// but are still imported by libraries like postprocessing
export const LuminanceFormat = 1018
export const LuminanceAlphaFormat = 1019
export const AlphaFormat = 1021
