export const CHARACTER_HEIGHT = 1.6
export const CAMERA_EYE_OFFSET = CHARACTER_HEIGHT / 2
export const CHARACTER_SPAWN_CLEARANCE = 0.25

export const CHARACTER_SPAWN = { x: 0, y: CHARACTER_HEIGHT + CHARACTER_SPAWN_CLEARANCE, z: -0.5 }
export const CHARACTER_SPAWN_POSITION: [number, number, number] = [
  CHARACTER_SPAWN.x,
  CHARACTER_SPAWN.y,
  CHARACTER_SPAWN.z,
]

export const CHARACTER_BODY_SPAWN = {
  x: CHARACTER_SPAWN.x,
  y: CHARACTER_SPAWN.y - CAMERA_EYE_OFFSET,
  z: CHARACTER_SPAWN.z,
}
export const CHARACTER_BODY_SPAWN_POSITION: [number, number, number] = [
  CHARACTER_BODY_SPAWN.x,
  CHARACTER_BODY_SPAWN.y,
  CHARACTER_BODY_SPAWN.z,
]

/**
 * Resolve where the body starts, preferring the scene's own spawn point.
 *
 * Reconstruction places the camera where the photo was taken, which is normally
 * outside the room, so the origin leaves the player standing in the void behind
 * all the geometry. Generated worlds record a point inside the room; worlds made
 * before that fall back to the origin.
 */
export function characterBodySpawn(spawnPoint?: [number, number]) {
  if (!spawnPoint) return CHARACTER_BODY_SPAWN
  return { x: spawnPoint[0], y: CHARACTER_BODY_SPAWN.y, z: spawnPoint[1] }
}

export function characterBodySpawnPosition(spawnPoint?: [number, number]): [number, number, number] {
  const spawn = characterBodySpawn(spawnPoint)
  return [spawn.x, spawn.y, spawn.z]
}
