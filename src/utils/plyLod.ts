const PLY_SCALAR_BYTES: Record<string, number> = {
  char: 1,
  int8: 1,
  uchar: 1,
  uint8: 1,
  short: 2,
  int16: 2,
  ushort: 2,
  uint16: 2,
  int: 4,
  int32: 4,
  uint: 4,
  uint32: 4,
  float: 4,
  float32: 4,
  double: 8,
  float64: 8,
}

interface BinaryPlyLayout {
  header: string
  headerByteLength: number
  vertexCount: number
  vertexStride: number
}

function inspectBinaryPly(input: Buffer): BinaryPlyLayout | undefined {
  const unixMarker = Buffer.from('end_header\n')
  const windowsMarker = Buffer.from('end_header\r\n')
  const unixIndex = input.indexOf(unixMarker)
  const windowsIndex = input.indexOf(windowsMarker)
  const marker = windowsIndex >= 0 && (unixIndex < 0 || windowsIndex < unixIndex)
    ? windowsMarker
    : unixMarker
  const markerIndex = marker === windowsMarker ? windowsIndex : unixIndex

  if (markerIndex < 0) return undefined

  const headerByteLength = markerIndex + marker.length
  const header = input.subarray(0, headerByteLength).toString('ascii')
  if (!/^format binary_little_endian 1(?:\.0)?\s*$/m.test(header)) return undefined

  const lines = header.split(/\r?\n/)
  const vertexElementIndex = lines.findIndex((line) => /^element vertex \d+\s*$/.test(line))
  if (vertexElementIndex < 0) return undefined

  const vertexCountMatch = lines[vertexElementIndex].match(/^element vertex (\d+)\s*$/)
  const vertexCount = Number(vertexCountMatch?.[1])
  if (!Number.isSafeInteger(vertexCount) || vertexCount <= 0) return undefined

  let vertexStride = 0
  for (let index = vertexElementIndex + 1; index < lines.length; index += 1) {
    const line = lines[index].trim()
    if (line.startsWith('element ')) break
    if (!line.startsWith('property ')) continue
    if (line.startsWith('property list ')) return undefined

    const scalarType = line.match(/^property\s+(\S+)\s+\S+/)?.[1]
    const scalarBytes = scalarType ? PLY_SCALAR_BYTES[scalarType] : undefined
    if (!scalarBytes) return undefined
    vertexStride += scalarBytes
  }

  if (vertexStride <= 0) return undefined
  if (headerByteLength + vertexCount * vertexStride > input.length) return undefined

  return { header, headerByteLength, vertexCount, vertexStride }
}

/**
 * Produces a deterministic, evenly distributed point subset while preserving
 * any non-vertex payload following the vertex table (for example SHARP camera
 * metadata). Unsupported PLY variants are returned unchanged via `undefined`.
 */
export function createBinaryPlyLod(input: Buffer, requestedVertexCount: number): Buffer | undefined {
  const layout = inspectBinaryPly(input)
  if (!layout) return undefined

  const targetVertexCount = Math.min(
    layout.vertexCount,
    Math.max(1, Math.floor(requestedVertexCount)),
  )
  if (targetVertexCount >= layout.vertexCount) return Buffer.from(input)

  const header = layout.header.replace(
    /^element vertex \d+\s*$/m,
    `element vertex ${targetVertexCount}`,
  )
  const headerBuffer = Buffer.from(header, 'ascii')
  const sourceVertexEnd = layout.headerByteLength + layout.vertexCount * layout.vertexStride
  const trailingByteLength = input.length - sourceVertexEnd
  const output = Buffer.allocUnsafe(
    headerBuffer.length + targetVertexCount * layout.vertexStride + trailingByteLength,
  )

  headerBuffer.copy(output, 0)
  let outputOffset = headerBuffer.length
  for (let index = 0; index < targetVertexCount; index += 1) {
    const sourceIndex = Math.min(
      layout.vertexCount - 1,
      Math.floor(((index + 0.5) * layout.vertexCount) / targetVertexCount),
    )
    const sourceOffset = layout.headerByteLength + sourceIndex * layout.vertexStride
    input.copy(output, outputOffset, sourceOffset, sourceOffset + layout.vertexStride)
    outputOffset += layout.vertexStride
  }

  input.copy(output, outputOffset, sourceVertexEnd)
  return output
}
