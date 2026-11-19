/// <reference lib="webworker" />

import type { PyodideInterface } from 'pyodide'

type Invocation = { id: number; base: string; path: string; body: string | null }
type Result = { data?: unknown; detail?: string; plans?: unknown[] }
type PythonRequest = (path: string, body: string, plans: string) => string
type Engine = { request: PythonRequest; database: IDBDatabase }

const scope = self as DedicatedWorkerGlobalScope
let engine: Promise<Engine> | undefined

function openStorage(base: string): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(`fearless-rehearsal:${new URL(base).pathname}`, 1)
    open.onupgradeneeded = () => open.result.createObjectStore('workspace')
    open.onsuccess = () => resolve(open.result)
    open.onerror = () => reject(new Error('This browser could not open its rehearsal storage.'))
  })
}

async function initialize(base: string): Promise<Engine> {
  const indexURL = new URL('demo/pyodide/', base).href
  const [module, source, database] = await Promise.all([
    import(/* @vite-ignore */ new URL('pyodide.mjs', indexURL).href) as Promise<{
      loadPyodide: (settings: { indexURL: string }) => Promise<PyodideInterface>
    }>,
    fetch(new URL('demo/source.json', base)).then(response => {
      if (!response.ok) throw new Error('The public rehearsal files are unavailable.')
      return response.json() as Promise<Record<string, string>>
    }),
    openStorage(base),
  ])
  const python = await module.loadPyodide({ indexURL })
  for (const [path, text] of Object.entries(source)) {
    const target = `/demo/${path}`
    python.FS.mkdirTree(target.slice(0, target.lastIndexOf('/')))
    python.FS.writeFile(target, text)
  }
  const request = python.runPython([
    'import sys',
    'sys.path.insert(0, "/demo")',
    'from fearless.browser import open_demo',
    'open_demo()',
  ].join('\n')) as PythonRequest
  return { request, database }
}

function transact(engine: Engine, path: string, body: string | null): Promise<Result> {
  return new Promise((resolve, reject) => {
    const transaction = engine.database.transaction('workspace', body === null ? 'readonly' : 'readwrite')
    const workspace = transaction.objectStore('workspace')
    const read = workspace.get('plans')
    let result: Result
    transaction.oncomplete = () => resolve(result)
    transaction.onabort = () => reject(transaction.error ?? new Error('The rehearsal could not be saved.'))
    read.onsuccess = () => {
      try {
        result = JSON.parse(engine.request(path, body ?? 'null', read.result ?? '[]')) as Result
        if (result.plans) workspace.put(JSON.stringify(result.plans), 'plans')
      } catch (error) {
        reject(error)
        transaction.abort()
      }
    }
  })
}

scope.onmessage = ({ data }: MessageEvent<Invocation>) => {
  const { id, base, path, body } = data
  engine ??= initialize(base).catch(error => {
    engine = undefined
    throw error
  })
  void engine.then(app => transact(app, path, body)).then(
    result => scope.postMessage({ id, data: result.data, detail: result.detail }),
    error => {
      console.error(error)
      scope.postMessage({ id, detail: 'The browser rehearsal could not run. Reload the page to retry.' })
    },
  )
}
