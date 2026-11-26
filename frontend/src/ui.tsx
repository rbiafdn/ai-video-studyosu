import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { api, fmtUsd, MODE_LABEL, STATUS_LABEL } from './api'

export function cx(...a: (string | false | null | undefined)[]) {
  return a.filter(Boolean).join(' ')
}

// ---------------------------------------------------------------- bildirimler
type Toast = { id: number; text: string; kind: 'ok' | 'bad' | 'info' }
const ToastCtx = createContext<(text: string, kind?: Toast['kind']) => void>(() => {})
export const useToast = () => useContext(ToastCtx)

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const push = useCallback((text: string, kind: Toast['kind'] = 'info') => {
    const id = Date.now() + Math.random()
    setItems((x) => [...x.slice(-4), { id, text, kind }])
    window.setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), kind === 'bad' ? 9000 : 4500)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex w-[420px] flex-col gap-2">
        {items.map((t) => (
          <div key={t.id} role="status"
            className={cx('rounded-lg border px-3 py-2 text-[13px] shadow-xl whitespace-pre-line',
              t.kind === 'bad' ? 'border-bad/50 bg-[#2a1519] text-[#ffc9ce]' : t.kind === 'ok' ? 'border-ok/40 bg-[#12251d] text-[#c5f5df]' : 'border-line bg-panel2')}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

/** Bir işlemi çalıştırır; hata olursa bildirim gösterir. */
export function useAction() {
  const toast = useToast()
  return useCallback(async <T,>(fn: () => Promise<T>, okText?: string): Promise<T | undefined> => {
    try {
      const r = await fn()
      if (okText) toast(okText, 'ok')
      return r
    } catch (e: any) {
      toast(e.message || 'Bilinmeyen hata', 'bad')
      return undefined
    }
  }, [toast])
}

// ---------------------------------------------------------------- temel parçalar
export function Btn({ children, kind = 'ghost', small, className, ...p }:
  React.ButtonHTMLAttributes<HTMLButtonElement> & { kind?: 'primary' | 'ghost' | 'ok' | 'bad'; small?: boolean }) {
  const [busy, setBusy] = useState(false)
  const { onClick, disabled, ...rest } = p
  return (
    <button {...rest} disabled={disabled || busy}
      onClick={async (e) => {
        if (!onClick) return
        const r: any = onClick(e)
        if (r && typeof r.then === 'function') { setBusy(true); try { await r } finally { setBusy(false) } }
      }}
      className={cx('inline-flex items-center justify-center gap-1.5 rounded-md border font-medium whitespace-nowrap transition-colors disabled:opacity-45 disabled:cursor-not-allowed',
        small ? 'px-2 py-[3px] text-[12px]' : 'px-3 py-1.5 text-[13px]',
        kind === 'primary' && 'border-accent bg-accent text-white hover:bg-[#6a59f5]',
        kind === 'ghost' && 'border-line bg-panel2 text-ink hover:border-[#3a4156] hover:bg-[#222738]',
        kind === 'ok' && 'border-ok/50 bg-ok/15 text-ok hover:bg-ok/25',
        kind === 'bad' && 'border-bad/50 bg-bad/10 text-bad hover:bg-bad/20', className)}>
      {children}
    </button>
  )
}

const TONE: Record<string, string> = {
  ok: 'bg-ok/15 text-ok border-ok/30', warn: 'bg-warn/15 text-warn border-warn/30', bad: 'bg-bad/15 text-bad border-bad/30',
  info: 'bg-info/15 text-info border-info/30', mute: 'bg-white/5 text-mute border-line', accent: 'bg-accent/20 text-[#b9b0ff] border-accent/40',
}
export function Badge({ tone = 'mute', children, title }: { tone?: keyof typeof TONE | string; children: ReactNode; title?: string }) {
  return <span title={title} className={cx('inline-flex items-center rounded border px-1.5 py-px text-[11px] font-medium whitespace-nowrap', TONE[tone] || TONE.mute)}>{children}</span>
}

const STATUS_TONE: Record<string, string> = {
  approved: 'ok', succeeded: 'ok', draft: 'warn', rejected: 'bad', failed: 'bad', running: 'info', submitting: 'info',
  queued: 'mute', waiting_manual: 'accent', canceled: 'mute', blocked: 'warn',
}
export const StatusBadge = ({ status }: { status: string }) => <Badge tone={STATUS_TONE[status] || 'mute'}>{STATUS_LABEL[status] || status}</Badge>
export const ModeBadge = ({ mode }: { mode: string }) =>
  <Badge tone={mode === 'real' ? 'ok' : mode === 'manual' ? 'accent' : 'mute'}>{MODE_LABEL[mode] || mode}</Badge>

export function Progress({ value, live, tone = 'accent' }: { value: number; live?: boolean; tone?: 'accent' | 'ok' | 'bad' }) {
  const v = Math.max(0, Math.min(1, value || 0))
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/8" role="progressbar" aria-valuenow={Math.round(v * 100)}>
      <div className={cx('h-full rounded-full transition-all duration-500', tone === 'ok' ? 'bg-ok' : tone === 'bad' ? 'bg-bad' : 'bg-accent', live && 'bar-live')}
        style={{ width: `${Math.max(v * 100, live ? 6 : 0)}%` }} />
    </div>
  )
}

export function Panel({ title, right, children, className }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cx('rounded-xl border border-line bg-panel', className)}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h2 className="text-[13px] font-semibold tracking-wide text-ink">{title}</h2>
          <div className="flex items-center gap-2">{right}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Modal({ title, onClose, children, wide }: { title: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/65 p-6" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={cx('w-full rounded-xl border border-line bg-panel shadow-2xl', wide ? 'max-w-[1280px]' : 'max-w-[640px]')}>
        <header className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="text-[15px] font-semibold">{title}</h2>
          <button onClick={onClose} aria-label="Kapat" className="rounded px-2 py-0.5 text-lg text-mute hover:bg-white/10 hover:text-ink">×</button>
        </header>
        <div className="p-5">{children}</div>
      </div>
    </div>
  )
}

export function Field({ label, children, hint, className }: { label: string; children: ReactNode; hint?: string; className?: string }) {
  return (
    <div className={cx('flex flex-col gap-1', className)}>
      <label>{label}</label>
      {children}
      {hint && <span className="text-[11px] text-mute">{hint}</span>}
    </div>
  )
}

export function FileBtn({ onFile, accept, children, small = true }: { onFile: (f: File) => any; accept?: string; children: ReactNode; small?: boolean }) {
  const ref = useRef<HTMLInputElement>(null)
  return (
    <>
      <input ref={ref} type="file" accept={accept} className="hidden"
        onChange={async (e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) await onFile(f) }} />
      <Btn small={small} onClick={() => ref.current?.click()}>{children}</Btn>
    </>
  )
}

export const Empty = ({ children }: { children: ReactNode }) =>
  <div className="rounded-lg border border-dashed border-line px-4 py-8 text-center text-[13px] text-mute">{children}</div>

export const Cost = ({ cost }: { cost: any }) => (
  <span className="text-[12px] text-mute" title="Tamamlanan işlerin tahmini maliyeti">
    Gerçek <b className="text-ink">{fmtUsd(cost?.real_usd || 0)}</b>
    {cost?.pending_usd > 0 && <> · bekleyen {fmtUsd(cost.pending_usd)}</>}
    {cost?.mock_usd > 0 && <> · mock karşılığı {fmtUsd(cost.mock_usd)}</>}
  </span>
)

// ---------------------------------------------------------------- medya
export function Media({ asset, className, controls = true }: { asset: any; className?: string; controls?: boolean }) {
  if (!asset) return null
  const url = `${asset.url}?v=${asset.id}`
  if (asset.kind === 'image') return <a href={url} target="_blank" rel="noreferrer"><img src={url} alt="" loading="lazy" className={cx('rounded-md bg-black object-cover', className)} /></a>
  if (asset.kind === 'video') return <video src={`${url}#t=0.4`} controls={controls} preload="metadata" className={cx('rounded-md bg-black', className)} />
  if (asset.kind === 'audio') return <audio src={url} controls preload="none" className={className} />
  return <a href={url} className="text-info underline" target="_blank" rel="noreferrer">Dosyayı aç</a>
}

/** İş satırı: durum, ilerleme, hata ve varsa 'elle adım' paneli. */
export function JobLine({ job, compact }: { job: any; compact?: boolean }) {
  const act = useAction()
  const active = ['queued', 'submitting', 'running'].includes(job.status)
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2 text-[12px]">
        <StatusBadge status={job.status} />
        {!compact && <span className="font-medium text-ink">{job.title}{job.scene_no ? ` · Sahne ${job.scene_no}` : ''}</span>}
        <span className="text-mute">{job.provider_label}</span>
        <ModeBadge mode={job.mode} />
        {job.attempts > 0 && <Badge tone="warn" title="Geçici hata sonrası yeniden deneme sayısı">{job.attempts}. deneme</Badge>}
        <span className="ml-auto flex gap-1.5">
          {(active || job.status === 'waiting_manual') && <Btn small kind="bad" onClick={() => act(() => api.post(`/jobs/${job.id}/cancel`))}>İptal</Btn>}
          {['failed', 'canceled', 'blocked'].includes(job.status) && <Btn small onClick={() => act(() => api.post(`/jobs/${job.id}/retry`), 'Yeniden kuyruğa alındı')}>Yeniden dene</Btn>}
        </span>
      </div>
      {active && <Progress value={job.progress} live />}
      {job.error && <div className={cx('rounded border px-2 py-1 text-[12px] whitespace-pre-wrap break-words', job.status === 'failed' ? 'border-bad/40 bg-bad/10 text-[#ffc9ce]' : 'border-warn/30 bg-warn/10 text-warn')}>{job.error}</div>}
      {job.manual && <ManualStep job={job} />}
    </div>
  )
}

export function ManualStep({ job }: { job: any }) {
  const act = useAction()
  const m = job.manual
  return (
    <div className="rounded-lg border border-accent/40 bg-accent/10 p-3 text-[13px]">
      <div className="mb-1.5 font-semibold text-[#cfc8ff]">{m.title}</div>
      <ol className="mb-2 list-decimal space-y-0.5 pl-5 text-ink/90">{m.steps.map((s: string, i: number) => <li key={i}>{s}</li>)}</ol>
      {m.instruction && <div className="mb-2 rounded bg-black/30 px-2 py-1.5"><span className="text-mute">Düzeltme talimatı: </span>{m.instruction}</div>}
      <div className="flex flex-wrap items-center gap-2">
        {m.inputs.map((i: any) => <a key={i.url} href={i.url} download className="rounded-md border border-line bg-panel2 px-2 py-[3px] text-[12px] hover:border-accent">⬇ {i.label}</a>)}
        <FileBtn accept={m.accept} onFile={(f) => act(() => api.upload(`/jobs/${job.id}/manual-upload`, f), 'Sonuç yüklendi, doğrulanıyor')}>⬆ Sonucu yükle</FileBtn>
      </div>
    </div>
  )
}
