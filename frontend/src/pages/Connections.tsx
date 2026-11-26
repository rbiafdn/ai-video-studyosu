import { useState } from 'react'
import { api } from '../api'
import { Badge, Btn, cx, Field, useToast } from '../ui'

export const MODE_TEXT: Record<string, [string, string]> = {
  connected: ['ok', 'Bağlı'], shared: ['info', 'Ortak anahtar'], demo: ['warn', 'Deneme modu'],
}

function statusOf(c: any): [string, string] {
  if (c.set && c.status === 'error') return ['bad', 'Hatalı']
  if (c.set && c.status === 'untested') return ['mute', 'Sınanmadı']
  return MODE_TEXT[c.mode] as [string, string]
}

/** Sınama sonucunda gelen bilgi: kalan kredi, ses kütüphanesi, erişilebilen özellikler, modeller. */
function Info({ c }: { c: any }) {
  const i = c.info || {}
  if (!c.set || c.status !== 'connected') return null
  return (
    <div className="mt-3 space-y-2 rounded-lg border border-line bg-bg/60 p-3 text-[12px]">
      {i.credit && <div><span className="text-mute">Kalan kredi / bakiye: </span><b>{i.credit}</b></div>}
      {c.service === 'anthropic' && i.models?.length > 0 && (
        <div><span className="text-mute">Erişilebilen modeller: </span>{i.models.slice(0, 8).join(', ')}{i.models.length > 8 && ` +${i.models.length - 8}`}
          {i.selected_model_available === false && <div className="mt-1 text-warn">Seçili yönetmen modeli bu hesapta görünmüyor; Tercihler’den başka model seçin.</div>}</div>)}
      {c.service === 'elevenlabs' && (
        <div><div className="text-mute">Ses kütüphanesi ({i.voice_count ?? 0} ses):</div>
          <div className="mt-1 flex max-h-24 flex-wrap gap-1 overflow-y-auto">{(i.voices || []).map((v: any) => <Badge key={v.voice_id} title={v.voice_id}>{v.name}</Badge>)}</div></div>)}
      {c.service === 'kling' && i.features && (
        <div><div className="text-mute">Özellik erişimi:</div>
          <div className="mt-1 flex flex-wrap gap-1.5">{Object.entries<any>(i.features).map(([k, f]) =>
            <Badge key={k} tone={f.state === 'paket var' ? 'ok' : f.state === 'belirsiz' ? 'warn' : 'mute'} title={f.state}>{f.label}: {f.state}</Badge>)}</div>
          <div className="mt-1.5 text-mute">Webhook imzası: {i.webhook_signed ? 'doğrulanıyor' : 'tanımlı değil (isteğe bağlı)'}</div></div>)}
      {c.service === 'heygen' && (
        <div><span className="text-mute">Avatar motorları: </span>{(i.models || []).map((m: any) => m.label).join(', ')}
          {i.looks?.length > 0 && <div className="mt-1"><span className="text-mute">Hazır avatar görünümleri: </span>{i.looks.map((l: any) => l.name).join(', ')}</div>}</div>)}
      {i.note && <div className="text-mute">{i.note}</div>}
    </div>
  )
}

/** Tek bir servis bağlantısı: ne işe yarar, anahtar nereden alınır, giriş alanları, test düğmesi, sonuç. */
export function ConnectionCard({ c, base = '/connections', onChange, shared }: { c: any; base?: string; onChange: (payload: any) => void; shared?: boolean }) {
  const toast = useToast()
  const [vals, setVals] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')
  const [tone, label] = statusOf(c)
  const run = async (kind: string, fn: () => Promise<any>) => {
    setBusy(kind)
    try {
      const r = await fn()
      if (r?.result) toast(`${c.label}: ${r.result.message}`, r.result.ok ? 'ok' : 'bad')
      onChange(r); setVals({})
    } catch (e: any) { toast(e.message, 'bad') }
    setBusy('')
  }
  const required = c.fields.filter((f: any) => f.required)
  const canSave = required.every((f: any) => (vals[f.name] || '').trim())
  return (
    <div className="rounded-xl border border-line bg-panel p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-[14px] font-semibold">{c.label}{c.required && !shared && <Badge tone="accent">zorunlu</Badge>}</div>
          <p className="mt-0.5 text-[12px] text-mute">{c.purpose}</p>
        </div>
        <Badge tone={tone}>{label}</Badge>
      </div>
      <a href={c.url} target="_blank" rel="noreferrer" className="mt-2 inline-block text-[12px] text-info hover:underline">Anahtarı buradan alın ↗</a>
      <div className="mt-3 space-y-2">
        {c.fields.map((f: any, idx: number) => (
          <Field key={f.name} label={f.label + (idx === 0 && c.set ? ` · kayıtlı: •••• ${c.last4}` : '')}>
            <input type="password" autoComplete="off" spellCheck={false} value={vals[f.name] ?? ''} onChange={(e) => setVals({ ...vals, [f.name]: e.target.value })}
              placeholder={c.set ? (idx === 0 ? 'Değiştirmek için yeni anahtarı yapıştırın' : 'Değiştirmek için yapıştırın (anahtarla birlikte)') : 'Yapıştırın'} />
          </Field>))}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Btn kind="primary" disabled={!canSave || !!busy} onClick={() => run('save', () => api.put(`${base}/${c.service}`, { values: vals }))}>{busy === 'save' ? 'Sınanıyor…' : 'Kaydet ve test et'}</Btn>
        {!shared && <Btn disabled={!c.set || !!busy} onClick={() => run('test', () => api.post(`${base}/${c.service}/test`))}>{busy === 'test' ? 'Sınanıyor…' : 'Bağlantıyı test et'}</Btn>}
        <Btn kind="bad" disabled={!c.set || !!busy} onClick={() => confirm(`${c.label} anahtarı silinsin mi?`) && run('del', () => api.del(`${base}/${c.service}`))}>Sil</Btn>
      </div>
      {c.set && c.message && <p className={cx('mt-2 text-[12px]', c.status === 'error' ? 'text-bad' : 'text-ok')}>{c.message}</p>}
      {!c.set && !shared && (
        <p className="mt-2 text-[12px] text-mute">{c.mode === 'shared' ? 'Kendi anahtarınız yok: kurumun ortak anahtarı kullanılır (aylık sınırınız içinde).'
          : c.required ? 'Anahtar olmadan sohbet çalışmaz (yönetici demo modunu açmadıysa).' : 'Anahtar girilmezse bu servis deneme modunda (sahte çıktıyla) çalışır.'}</p>)}
      <Info c={c} />
    </div>
  )
}
