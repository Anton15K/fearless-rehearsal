import { existsSync } from 'node:fs'
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const project = fileURLToPath(new URL('../..', import.meta.url))
const output = join(project, 'web', 'dist-demo', 'demo')
const runtime = join(output, 'pyodide')
const distribution = dirname(fileURLToPath(import.meta.resolve('pyodide')))

const runtimeFiles = [
  'pyodide.mjs', 'pyodide.asm.js', 'pyodide.asm.wasm', 'pyodide-lock.json', 'python_stdlib.zip',
]
const sourceFiles = [
  'fearless/__init__.py',
  'fearless/app_catalog.py',
  'fearless/app_directory.py',
  'fearless/app_error.py',
  'fearless/app_forecast.py',
  'fearless/browser.py',
  'fearless/outlook.py',
  'fearless/rehearsal.py',
  'fearless/rules.py',
  'fearless/assets/champions.json',
  'fearless/assets/public_games.jsonl',
]
const publicForecast = 'fearless/assets/public_forecast.json'
if (existsSync(join(project, publicForecast))) {
  sourceFiles.push('fearless/public_model.py', publicForecast)
}
const source = Object.fromEntries(await Promise.all(sourceFiles.map(async file => [
  file, await readFile(join(project, file), 'utf8'),
])))

await mkdir(runtime, { recursive: true })
await Promise.all([
  ...runtimeFiles.map(file => copyFile(join(distribution, file), join(runtime, file))),
  writeFile(join(output, 'source.json'), JSON.stringify(source)),
  copyFile(join(project, 'fearless/assets/NOTICE.md'), join(output, 'data-notice.md')),
  copyFile(join(project, 'web/scripts/demo-runtime-notice.txt'), join(output, 'runtime-notice.txt')),
])
