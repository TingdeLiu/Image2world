import type { WorldObjectAsset, WorldObjectPlacement } from '../../types/world'

const GRID_CELL_SIZE = 1
const GRID_MAX_COLUMNS = 3
export const OBJECT_RESET_ORIGIN: [number, number, number] = [0, 0, -2]

// Every object mesh is rendered at this factor before its per-instance scale.
// The generation pipeline divides measured metric sizes by it, so viewer and
// pipeline must agree -- keep it here, where neither pulls in the other's deps.
export const OBJECT_SCALE = 0.5

export function gridPosition(index: number, total: number): [number, number, number] {
  const columns = Math.min(GRID_MAX_COLUMNS, Math.max(1, total))
  const column = index % columns
  const row = Math.floor(index / columns)

  return [
    OBJECT_RESET_ORIGIN[0] + (column - (columns - 1) / 2) * GRID_CELL_SIZE,
    OBJECT_RESET_ORIGIN[1],
    OBJECT_RESET_ORIGIN[2] + row * -GRID_CELL_SIZE,
  ]
}

export function createDefaultPlacements(objects: WorldObjectAsset[]): WorldObjectPlacement[] {
  return objects.map((object, index) => ({
    instanceId: object.id,
    objectId: object.id,
    assetId: object.assetId,
    physics: 'rigidbody',
    position: gridPosition(index, objects.length),
    rotation: [0, 0, 0],
    scale: [1, 1, 1],
  }))
}

export function getInitialPlacements(
  objects: WorldObjectAsset[],
  savedPlacements?: WorldObjectPlacement[],
): WorldObjectPlacement[] {
  return savedPlacements ?? createDefaultPlacements(objects)
}
