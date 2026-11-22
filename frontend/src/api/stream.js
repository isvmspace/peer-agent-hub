import { getToken } from './client'

// Server-sent events over fetch, so the bearer token goes in a header instead of the URL.
// Reconnects with backoff until close() is called; `url` may be a function to resume from a cursor.
export function openStream(url, onEvent, onError) {
  const controller = new AbortController()
  let closed = false
  let delay = 1000

  async function run() {
    while (!closed) {
      try {
        const target = typeof url === 'function' ? url() : url
        const response = await fetch(target, {
          headers: { Authorization: `Bearer ${getToken()}`, Accept: 'text/event-stream' },
          signal: controller.signal,
        })
        if (!response.ok) {
          const text = await response.text()
          let error = text
          try { error = JSON.parse(text).error || text } catch { /* plain text */ }
          throw Object.assign(new Error(error || `连接失败（${response.status}）`), { fatal: response.status < 500 })
        }
        delay = 1000
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (!closed) {
          const { value, done } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          let index
          while ((index = buffer.indexOf('\n\n')) >= 0) {
            const chunk = buffer.slice(0, index)
            buffer = buffer.slice(index + 2)
            let name = 'message'
            const data = []
            for (const line of chunk.split('\n')) {
              if (line.startsWith('event:')) name = line.slice(6).trim()
              else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
            }
            if (!data.length) continue
            const payload = JSON.parse(data.join('\n'))
            // access was withdrawn while the stream was open: stop instead of reconnecting
            if (name === 'denied') throw Object.assign(new Error(payload.error || '已没有权限'), { fatal: true })
            onEvent(name, payload)
          }
        }
      } catch (error) {
        if (closed) return
        onError?.(error)
        if (error.fatal) return
      }
      await new Promise(resolve => setTimeout(resolve, delay))
      delay = Math.min(delay * 2, 15000)
    }
  }

  run()
  return () => { closed = true; controller.abort() }
}
