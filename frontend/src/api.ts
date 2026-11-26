import { useCallback, useEffect, useRef, useState } from 'react'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// CSRF belirteci oturum açılınca sunucudan gelir ve yalnızca bellekte tutulur
let csrf = ''
export const setCsrf = (v: string) => { csrf = v }
/** Oturum düştüğünde (401) uygulamanın giriş ekranına dönmesi için */
export const authLost = { handler: () => {} }

async function request<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: 'same-origin' }
  const headers: Record<string, string> = {}
  if (method !== 'GET' && csrf) headers['X-CSRF-Token'] = csrf
  if (body instanceof FormData) init.body = body
  else if (body !== undefined) {
    init.body = JSON.stringify(body)
    headers['Content-Type'] = 'application/json'
  }
  init.headers = headers
  let res: Response
  try {
    res = await fetch('/api' + path, init)
  } catch {
    throw new ApiError(0, 'Sunucuya ulaşılamıyor. Backend çalışıyor mu?')
  }
  if (!res.ok) {
    let msg = `Hata ${res.status}`
    try {
      const j = await res.json()
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j)
    } catch { /* gövde yok */ }
    if (res.status === 401 && !path.startsWith('/auth/')) authLost.handler()
    throw new ApiError(res.status, msg)
  }
  return res.json()
}

export const api = {
  get: <T = any>(p: string) => request<T>('GET', p),
  post: <T = any>(p: string, b?: unknown) => request<T>('POST', p, b ?? {}),
  put: <T = any>(p: string, b?: unknown) => request<T>('PUT', p, b),
  patch: <T = any>(p: string, b?: unknown) => request<T>('PATCH', p, b),
  del: <T = any>(p: string) => request<T>('DELETE', p),
  upload: <T = any>(p: string, file: File, fields: Record<string, string> = {}) => {
    const fd = new FormData()
    Object.entries(fields).forEach(([k, v]) => fd.append(k, v))
    fd.append('file', file)
    return request<T>('POST', p, fd)
  },
}

// ---- canlı olaylar (SSE): tek bağlantı, tüm sayfalar dinler
type Listener = (e: any) => void
const listeners = new Set<Listener>()
let source: EventSource | null = null
export const live = { connected: false }

/** Çıkışta canlı bağlantıyı kapatır (bir sonraki kullanıcı kendi olaylarını alır). */
export function closeLive() {
  source?.close()
  source = null
  live.connected = false
}

function ensureSource() {
  if (source) return
  source = new EventSource('/api/events')
  source.onopen = () => { live.connected = true; listeners.forEach((l) => l({ type: 'open' })) }
  source.onerror = () => { live.connected = false; listeners.forEach((l) => l({ type: 'closed' })) }
  source.onmessage = (m) => {
    try { const e = JSON.parse(m.data); listeners.forEach((l) => l(e)) } catch { /* yoksay */ }
  }
}

export function useEvents(fn: Listener) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    ensureSource()
    const l: Listener = (e) => ref.current(e)
    listeners.add(l)
    return () => { listeners.delete(l) }
  }, [])
}

/** Veriyi getirir; sunucudan olay geldikçe (kısa gecikmeyle birleştirerek) yeniler. */
export function useLive<T = any>(path: string | null, match: (e: any) => boolean = () => true) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const timer = useRef<number | undefined>(undefined)
  const seq = useRef(0)

  const reload = useCallback(async () => {
    if (!path) return
    const mine = ++seq.current
    try {
      const d = await api.get<T>(path)
      if (mine === seq.current) { setData(d); setError('') }
    } catch (e: any) {
      if (mine === seq.current) setError(e.message)
    }
  }, [path])

  useEffect(() => { setData(null); reload() }, [reload])
  useEvents((e) => {
    if (e.type !== 'open' && e.type !== 'closed' && !match(e)) return
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(reload, 200)
  })
  return { data, error, reload, setData }
}

export const fmtUsd = (v: number) => '$' + (v ?? 0).toFixed(v >= 10 ? 2 : 3)
export const fmtSec = (v?: number) => (v || v === 0 ? `${v.toFixed(1)} sn` : '')
export const fmtTime = (iso?: string | null) => (iso ? new Date(iso).toLocaleTimeString('tr-TR') : '')

export const TYPE_LABEL: Record<string, string> = { talking: 'Konuşan', silent: 'Konuşmasız', narrator: 'Anlatıcı' }
export const STATUS_LABEL: Record<string, string> = {
  queued: 'Sırada', blocked: 'Bağlantı bekliyor', submitting: 'Gönderiliyor', running: 'Çalışıyor', waiting_manual: 'Elle adım bekliyor',
  succeeded: 'Tamamlandı', failed: 'Hata', canceled: 'İptal', draft: 'Taslak', approved: 'Onaylı', rejected: 'Reddedildi',
}
export const MODE_LABEL: Record<string, string> = { real: 'Gerçek API', mock: 'Deneme modu', none: 'Bağlı değil', manual: 'Elle', local: 'Yerel' }
