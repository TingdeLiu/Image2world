import fs from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'

// usd-convert-gsplat needs Python >=3.11,<3.13, which the backend conda
// environment (3.10) cannot provide -- see README.md for the venv setup.
const scriptDirectory = path.join(process.cwd(), 'scripts', 'usd-export')
const exporter = path.join(scriptDirectory, 'export_world_usd.py')

const venvInterpreters = [
  path.join(scriptDirectory, '.venv', 'Scripts', 'python.exe'),
  path.join(scriptDirectory, '.venv', 'bin', 'python'),
]

const interpreter =
  venvInterpreters.find((candidate) => fs.existsSync(candidate)) ??
  process.env.IMAGEWORLD_USD_PYTHON ??
  'python'

const args = process.argv.slice(2)

if (args[0] === '--pytest') {
  const test = spawnSync(interpreter, ['-m', 'pytest', scriptDirectory, '-q', ...args.slice(1)], {
    stdio: 'inherit',
  })
  if (test.error) {
    console.error(`Failed to run ${interpreter}: ${test.error.message}`)
    console.error('See scripts/usd-export/README.md for environment setup.')
    process.exit(1)
  }
  process.exit(test.status ?? 1)
}

if (!args.length) {
  console.error('Usage: npm run export:usd -- <slug> [--lod 500k] [--format usdz|usda]')
  console.error('Available worlds:')
  const worldsDirectory = path.join(process.cwd(), 'public', 'worlds')
  if (fs.existsSync(worldsDirectory)) {
    for (const slug of fs.readdirSync(worldsDirectory)) {
      if (!slug.startsWith('.')) console.error(`  ${slug}`)
    }
  }
  process.exit(1)
}

const result = spawnSync(interpreter, [exporter, ...args], { stdio: 'inherit' })

if (result.error) {
  console.error(`Failed to run ${interpreter}: ${result.error.message}`)
  console.error('See scripts/usd-export/README.md for environment setup.')
  process.exit(1)
}

process.exit(result.status ?? 1)
