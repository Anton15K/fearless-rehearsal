type Reply = { id: number; data?: unknown; detail?: string }
type Handler = (reply: Reply) => void

let worker: Worker | undefined
let nextId = 0
const pending = new Map<number, Handler>()

function getWorker() {
  if (!worker) {
    worker = new Worker(new URL('./demo.worker.ts', import.meta.url), { type: 'module' })
    worker.onmessage = ({ data }: MessageEvent<Reply>) => pending.get(data.id)?.(data)
    worker.onerror = () => {
      worker?.terminate()
      worker = undefined
      for (const [id, respond] of pending) {
        respond({ id, detail: 'The browser rehearsal could not start. Reload the page to retry.' })
      }
    }
  }
  return worker
}

export function requestDemo<T>(path: string, data?: unknown, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason)
      return
    }

    const id = ++nextId
    const abort = () => {
      pending.delete(id)
      reject(signal?.reason)
    }
    const base = new URL(import.meta.env.BASE_URL, document.baseURI).href
    const target = getWorker()
    pending.set(id, reply => {
      pending.delete(id)
      signal?.removeEventListener('abort', abort)
      if (reply.detail) reject(new Error(reply.detail))
      else resolve(reply.data as T)
    })
    signal?.addEventListener('abort', abort, { once: true })
    target.postMessage({ id, base, path, body: data === undefined ? null : JSON.stringify(data) })
  })
}
