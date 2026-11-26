import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, fmtUsd, useEvents, useLive } from '../api'
import { Badge, Btn, cx, Empty, Progress, useAction } from '../ui'

const TOOL_LABEL: Record<string, string> = {
  proje_olustur: 'Proje oluştur', bolum_olustur: 'Bölüm oluştur', karakter_olustur: 'Karakter oluştur', karakter_guncelle: 'Karakteri güncelle',
  ses_listele: 'Sesleri listele', karaktere_ses_ata: 'Ses ata', sahneleri_yaz: 'Sahneleri yaz', dil_ekle: 'Dil ekle', replik_cevir: 'Replikleri çevir',
  adim_calistir: 'Adımı çalıştır', isleri_bekle: 'İşleri bekle', durum_getir: 'Durumu getir', varlik_onayla: 'Onayla', varlik_reddet: 'Reddet',
  varlik_incele: 'İncele', kurgu_yap: 'Kurgu yap', kontrol_calistir: 'Kontrolü çalıştır', kontrol_raporu_getir: 'Kalite raporunu oku',
  duzeltme_baslat: 'Düzeltme başlat', maliyet_getir: 'Maliyeti getir', kullaniciya_sor: 'Kullanıcıya sor', plan_onayi_iste: 'Plan onayı iste',
  kurgu_ayarla: 'Kurgu ayarı', ek_kullan: 'Eki kullan', sahne_isaretle: 'Sahneyi işaretle',
}
const STEP = ['', 'Sesler', 'Görseller', 'Konuşan sahneler', 'Konuşmasız sahneler']
const RUN_LABEL: Record<string, [string, string]> = {
  queued: ['info', 'Başlıyor'], running: ['info', 'Çalışıyor'], waiting_jobs: ['info', 'Üretim sürüyor'], waiting_user: ['accent', 'Yanıtınızı bekliyor'],
  paused_budget: ['warn', 'Bütçe sınırında durdu'], waiting_connection: ['warn', 'Bağlantı bekliyor'], done: ['ok', 'Tamamlandı'], failed: ['bad', 'Hata'], stopped: ['mute', 'Durduruldu'],
}
const EXAMPLES = [
  'Terk edilmiş bir deniz fenerinin sırrını anlatan 5 sahnelik kısa bir bölüm yap',
  'İki kardeşin kaybolan kedilerini aradığı sıcak bir hikâye çek; İngilizce sürümünü de çıkar',
  'Make a short mystery episode about a clockmaker who hears a ticking sound inside the wall',
]

function summarize(name: string, i: any): string {
  if (!i) return ''
  if (name === 'adim_calistir') return `${i.adim}. ${STEP[i.adim] || ''} · ${i.kapsam}${i.sahne_no ? ` · sahne ${i.sahne_no}` : ''}${i.dil && i.dil !== '*' ? ` · ${i.dil}` : ''}`
  if (name === 'sahneleri_yaz') return `${i.sahneler?.length || 0} sahne`
  if (name === 'replik_cevir') return `${i.dil} · ${i.ceviriler?.length || 0} replik`
  if (name === 'varlik_onayla') return `${i.varlik_idleri?.length || 0} varlık`
  if (name === 'duzeltme_baslat') return `sahne ${i.sahne_no} · ${i.yontem}${i.talimat ? ` · “${String(i.talimat).slice(0, 60)}”` : ''}`
  if (name === 'karakter_olustur' || name === 'proje_olustur') return i.ad || ''
  if (name === 'kullaniciya_sor') return i.soru || ''
  return [i.dil, i.sahne_no && `sahne ${i.sahne_no}`, i.varlik_id && `#${i.varlik_id}`].filter(Boolean).join(' · ')
}

function MediaGrid({ media }: { media: any[] }) {
  if (!media?.length) return null
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {media.map((m) => (
        <div key={`${m.asset_id}-${m.label}`} className={cx('rounded-md border border-line bg-black/20 p-1.5', m.kind === 'audio' ? 'w-[250px]' : 'w-[200px]')}>
          {m.kind === 'image' && <a href={m.url} target="_blank" rel="noreferrer"><img src={m.url} alt="" loading="lazy" className="aspect-video w-full rounded object-cover" /></a>}
          {m.kind === 'video' && <video src={`${m.url}#t=0.4`} controls preload="metadata" className="aspect-video w-full rounded bg-black" />}
          {m.kind === 'audio' && <audio src={m.url} controls preload="none" />}
          <div className="mt-1 truncate text-[11px] text-mute">{m.label}{m.language ? ` · ${m.language}` : ''} · v{m.version}</div>
        </div>))}
    </div>
  )
}

function ToolRow({ b, result }: { b: any; result?: any }) {
  const [open, setOpen] = useState(false)
  const pending = !result
  return (
    <div className={cx('rounded-lg border text-[12px]', result?.is_error ? 'border-bad/40 bg-bad/5' : 'border-line bg-panel2/60')}>
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left" aria-expanded={open}>
        <span className={cx('h-1.5 w-1.5 shrink-0 rounded-full', pending ? 'animate-pulse bg-info' : result.is_error ? 'bg-bad' : 'bg-ok')} />
        <span className="font-medium text-ink">{TOOL_LABEL[b.name] || b.name}</span>
        <span className="truncate text-mute">{summarize(b.name, b.input)}</span>
        {result?.is_error && <span className="truncate text-[#ffb3ba]">{result.text.slice(0, 90)}</span>}
        <span className="ml-auto shrink-0 text-mute">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-line px-2.5 py-2">
          <div className="text-[11px] text-mute">Girdi</div>
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-black/30 p-2 font-mono text-[11px]">{JSON.stringify(b.input, null, 1)}</pre>
          {result && <><div className="text-[11px] text-mute">Sonuç{result.images ? ` (+${result.images} görsel modele gösterildi)` : ''}</div>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-black/30 p-2 font-mono text-[11px]">{result.text}</pre></>}
        </div>)}
      {result?.media?.length > 0 && <div className="px-2.5 pb-2"><MediaGrid media={result.media} /></div>}
    </div>
  )
}

function ResultCard({ r }: { r: any }) {
  return (
    <div className="rounded-xl border border-ok/40 bg-ok/5 p-4">
      <div className="mb-3 flex items-center gap-2"><span className="text-[14px] font-semibold text-ok">Bölüm hazır</span>
        <Link to={`/bolum/${r.episode_id}`} className="text-[12px] text-info hover:underline">Bölüm ekranında aç</Link>
        <span className="ml-auto text-[12px] text-mute">Bu çalışma {fmtUsd(r.cost.run_usd)} / bütçe {fmtUsd(r.cost.budget_usd)}{r.cost.claude_usd > 0 && ` · Claude ${fmtUsd(r.cost.claude_usd)}`}</span></div>
      <div className="grid grid-cols-2 gap-4">
        {r.outputs.map((o: any) => (
          <div key={o.language}>
            <video src={`${o.video}?v=${o.asset_id}#t=0.5`} controls preload="metadata" className="aspect-video w-full rounded-lg bg-black" />
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[12px]">
              <Badge tone="accent">{o.language_name}</Badge><span className="text-mute">v{o.version} · {o.duration} sn</span>
              {o.report && <Badge tone={o.report.errors ? 'bad' : o.report.warnings ? 'warn' : 'ok'}>Kalite: {o.report.errors ? `${o.report.errors} hata` : o.report.warnings ? `${o.report.warnings} uyarı` : 'temiz'}</Badge>}
              <a className="text-info hover:underline" href={o.video} download>⬇ mp4</a>
              {o.subtitles && <a className="text-info hover:underline" href={o.subtitles} download>⬇ altyazı</a>}
              {o.vertical && <a className="text-info hover:underline" href={o.vertical} download>⬇ dikey</a>}
            </div>
          </div>))}
      </div>
      {r.needs_review?.length > 0 && <div className="mt-3 rounded border border-warn/40 bg-warn/10 px-2 py-1.5 text-[12px] text-warn">Elle bakılmalı: {r.needs_review.map((x: any) => `sahne ${x.no}${x.note ? ` (${x.note})` : ''}`).join(', ')}</div>}
      <div className="mt-3 flex flex-wrap gap-x-5 text-[12px] text-mute">
        {r.cost.by_language.map((l: any) => <span key={l.language}>{l.label}: <b className="text-ink">{fmtUsd(l.real_usd)}</b>{l.mock_usd > 0 && ` (mock karşılığı ${fmtUsd(l.mock_usd)})`}</span>)}
      </div>
    </div>
  )
}

function Panel({ conv }: { conv: any }) {
  const p = conv.panel
  const run = conv.run
  if (!p) return (
    <div className="space-y-3 p-4 text-[13px] text-mute">
      <div className="text-[13px] font-semibold text-ink">Canlı üretim</div>
      <p>Bir fikir, senaryo ya da talimat yazın. Yönetmen karakterleri ve sahneleri çıkarır, sesleri seçer ve hattı 1’den 6’ya çalıştırır. Üretim başlayınca aşamalar ve sahneler burada canlı görünür.</p>
      <p>Araya girebilirsiniz: “3. sahnede kadın gülsün”, “İngilizce sürümünü de çıkar”, “durdur”.</p>
    </div>)
  const langs: string[] = p.languages
  return (
    <div className="space-y-4 p-4">
      <div><div className="flex items-center gap-2"><span className="text-[13px] font-semibold">{p.project.name}</span></div>
        <Link to={`/bolum/${p.episode.id}`} className="text-[12px] text-info hover:underline">Bölüm {p.episode.number}: {p.episode.title} →</Link></div>
      {run && run.budget_usd > 0 && (
        <div><div className="mb-1 flex justify-between text-[12px] text-mute"><span>Anlık maliyet</span><span><b className="text-ink">{fmtUsd(run.spent_usd)}</b> / {fmtUsd(run.budget_usd)}</span></div>
          <Progress value={run.spent_usd / run.budget_usd} tone={run.spent_usd / run.budget_usd > 0.9 ? 'bad' : 'accent'} />
          <div className="mt-1 text-[11px] text-mute">{conv.mode === 'mock' ? 'Mock: ücret kesilmez, tutarlar gerçek modun tahminidir.' : `Claude: ${fmtUsd(run.claude_usd)} · ${run.turns} tur`}</div></div>)}
      {langs.map((l) => {
        const v = p.per_language[l]
        return (
          <div key={l}>
            <div className="mb-1.5 flex items-center gap-2 text-[12px]"><Badge tone="accent">{l}</Badge>
              {v.final && <a href={v.final.url} target="_blank" rel="noreferrer" className="text-info hover:underline">kurgu v{v.final.version}</a>}
              {v.report && <Badge tone={v.report.errors ? 'bad' : v.report.warnings ? 'warn' : 'ok'}>{v.report.verdict}</Badge>}</div>
            <div className="space-y-1">
              {v.steps.filter((s: any) => s.n <= 6).map((s: any) => (
                <div key={s.n} className="flex items-center gap-2 text-[12px]">
                  <span className={cx('flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[10px] font-bold', s.complete ? 'bg-ok text-black' : s.running ? 'animate-pulse bg-info text-black' : 'bg-white/10')}>{s.complete ? '✓' : s.n}</span>
                  <span className={cx('flex-1 truncate', s.complete ? 'text-mute' : 'text-ink')}>{s.title}</span>
                  {s.failed ? <Badge tone="bad">{s.failed} hata</Badge> : s.draft ? <Badge tone="warn">{s.draft} onay</Badge> : null}
                  <span className="w-9 text-right text-mute">{s.n <= 4 ? s.done : ''}</span>
                </div>))}
            </div>
            {v.warnings?.length > 0 && <div className="mt-1.5 rounded border border-warn/30 bg-warn/10 px-2 py-1 text-[11px] text-warn">{v.warnings.join(' · ')}</div>}
          </div>)
      })}
      <div>
        <div className="mb-1.5 text-[12px] text-mute">Sahneler</div>
        <div className="grid grid-cols-3 gap-1.5">
          {p.thumbs.map((t: any) => (
            <Link key={t.id} to={`/bolum/${p.episode.id}/sahne/${t.id}`} title={t.text} className={cx('relative block overflow-hidden rounded-md border', t.needs_review ? 'border-warn' : 'border-line')}>
              {t.image ? <img src={t.image} alt="" loading="lazy" className="aspect-video w-full object-cover" /> : <div className="flex aspect-video items-center justify-center bg-white/5 text-[11px] text-mute">—</div>}
              <span className="absolute left-1 top-1 rounded bg-black/70 px-1 text-[10px] font-semibold">{t.no}</span>
              {t.has_video && <span className="absolute right-1 top-1 rounded bg-ok px-1 text-[10px] font-bold text-black">▶</span>}
              {t.busy && <span className="absolute inset-x-0 bottom-0 h-1 bg-info bar-live" />}
            </Link>))}
        </div>
      </div>
      {p.failed_jobs.length > 0 && <div className="rounded border border-bad/40 bg-bad/10 px-2 py-1.5 text-[12px] text-[#ffc9ce]">{p.failed_jobs.length} iş hata verdi. <Link to="/isler" className="underline">İşler ekranı</Link></div>}
      {p.active_jobs.some((j: any) => j.status === 'waiting_manual') && <div className="rounded border border-accent/40 bg-accent/10 px-2 py-1.5 text-[12px] text-[#cfc8ff]">Elle tamamlanacak adım var. <Link to="/isler" className="underline">İşler ekranında aç</Link></div>}
    </div>
  )
}

export default function Chat() {
  const { id } = useParams()
  const nav = useNavigate()
  const act = useAction()
  const list = useLive<any[]>('/conversations', (e) => e.type === 'chat' || e.type === 'refresh')
  const { data: conv, reload } = useLive<any>(id ? `/conversations/${id}` : null,
    (e) => (e.type === 'chat' && String(e.conversation_id) === id && e.event !== 'delta' && e.event !== 'typing' && e.event !== 'tool') || e.type === 'job' || e.type === 'refresh')
  const [text, setText] = useState('')
  const [files, setFiles] = useState<any[]>([])
  const [stream, setStream] = useState('')
  const [activity, setActivity] = useState('')
  const bottom = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEvents((e) => {
    if (e.type !== 'chat' || String(e.conversation_id) !== id) return
    if (e.event === 'delta') setStream((s) => s + e.text)
    else if (e.event === 'typing') { setStream(''); setActivity('Düşünüyor…') }
    else if (e.event === 'tool') setActivity(`${TOOL_LABEL[e.name] || e.name}…`)
    else if (e.event === 'message') { setStream(''); setActivity('') }
  })
  useEffect(() => { setStream(''); setActivity(''); setFiles([]) }, [id])
  const count = conv?.messages.length || 0
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [count, stream.length > 0, conv?.run?.status])

  const run = conv?.run
  const active = run && ['queued', 'running', 'waiting_jobs'].includes(run.status)
  const results: Record<string, any> = {}
  conv?.messages.forEach((m: any) => { if (m.type === 'tool_results') Object.assign(results, m.results) })

  const send = async (body?: string) => {
    const msg = (body ?? text).trim()
    if (!msg && !files.length) return
    let cid = id
    if (!cid) {
      const c = await act(() => api.post('/conversations', {}))
      if (!c) return
      cid = String(c.id)
    }
    const ok = await act(() => api.post(`/conversations/${cid}/messages`, { text: msg, attachment_ids: files.map((f) => f.id) }))
    if (ok) { setText(''); setFiles([]); if (cid !== id) nav(`/sohbet/${cid}`); else reload() }
  }
  const attach = async (f: File) => {
    let cid = id
    if (!cid) { const c = await act(() => api.post('/conversations', {})); if (!c) return; cid = String(c.id); nav(`/sohbet/${cid}`) }
    const a = await act(() => api.upload(`/conversations/${cid}/attachments`, f))
    if (a) setFiles((x) => [...x, a])
  }

  return (
    <div className="flex h-full">
      {/* sol: sohbetler */}
      <div className="flex w-[230px] shrink-0 flex-col border-r border-line bg-panel/60">
        <div className="p-3"><Btn kind="primary" className="w-full" onClick={() => nav('/')}>+ Yeni sohbet</Btn></div>
        <div className="flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
          {list.data?.length === 0 && <div className="px-2 py-4 text-[12px] text-mute">Henüz sohbet yok.</div>}
          {list.data?.map((c) => (
            <div key={c.id} className={cx('group flex items-center rounded-md', String(c.id) === id ? 'bg-accent/20' : 'hover:bg-white/5')}>
              <Link to={`/sohbet/${c.id}`} className="min-w-0 flex-1 px-2.5 py-2">
                <div className={cx('truncate text-[13px]', String(c.id) === id ? 'text-white' : 'text-ink')}>{c.title}</div>
                <div className="flex items-center gap-1.5 text-[11px] text-mute">{c.status && <span className={cx('h-1.5 w-1.5 rounded-full', ['running', 'waiting_jobs', 'queued'].includes(c.status) ? 'animate-pulse bg-info' : c.status === 'done' ? 'bg-ok' : c.status === 'failed' ? 'bg-bad' : 'bg-warn')} />}{RUN_LABEL[c.status]?.[1] || 'Boş'}</div>
              </Link>
              <button aria-label="Sohbeti sil" onClick={() => confirm('Sohbet silinsin mi? (Üretilen proje ve dosyalar kalır)') && act(async () => { await api.del(`/conversations/${c.id}`); if (String(c.id) === id) nav('/') })}
                className="mr-1 hidden rounded px-1.5 text-mute hover:bg-white/10 hover:text-bad group-hover:block">×</button>
            </div>))}
        </div>
      </div>

      {/* orta: sohbet */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-line px-5 py-2.5">
          <span className="truncate text-[14px] font-semibold">{conv?.title || 'Yönetmenle sohbet'}</span>
          {conv && <Badge tone={conv.mode === 'real' ? 'ok' : 'warn'} title={conv.mode === 'real' ? conv.model : 'Anthropic anahtarı yok: önceden yazılmış sahte yönetmen çalışır'}>{conv.mode === 'real' ? conv.model : 'Sahte yönetmen (mock)'}</Badge>}
          {run && <Badge tone={RUN_LABEL[run.status]?.[0]}>{RUN_LABEL[run.status]?.[1]}</Badge>}
          <span className="ml-auto flex gap-2">
            {(active || run?.status === 'waiting_user' || run?.status === 'paused_budget') && <Btn small kind="bad" onClick={() => act(() => api.post(`/conversations/${id}/stop`))}>■ Durdur</Btn>}
          </span>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="mx-auto max-w-[860px] space-y-3">
            {!id && (
              <div className="pt-16 text-center">
                <h1 className="text-2xl font-semibold">Ne çekelim?</h1>
                <p className="mx-auto mt-2 max-w-[520px] text-[13px] text-mute">Tek bir mesaj yazın: fikir, senaryo ya da talimat. Yönetmen her şeyi planlar ve bölümü baştan sona üretir.</p>
                <div className="mx-auto mt-6 flex max-w-[640px] flex-col gap-2">
                  {EXAMPLES.map((x) => <button key={x} onClick={() => setText(x)} className="rounded-lg border border-line bg-panel px-3 py-2 text-left text-[13px] text-ink hover:border-accent/60">{x}</button>)}
                </div>
              </div>)}
            {id && !conv && <Empty>Yükleniyor…</Empty>}
            {conv?.messages.map((m: any) => {
              if (m.type === 'user') return (
                <div key={m.id} className="flex justify-end"><div className={cx('max-w-[80%] rounded-2xl rounded-br-sm bg-accent/25 px-3.5 py-2 text-[13px] whitespace-pre-wrap', m.pending && 'opacity-60')}>
                  {m.text}
                  {m.attachments?.length > 0 && <div className="mt-1.5 flex flex-wrap gap-1.5">{m.attachments.map((a: any) => <a key={a.id} href={a.url} target="_blank" rel="noreferrer" className="rounded border border-white/20 px-1.5 py-0.5 text-[11px]">📎 {a.name}</a>)}</div>}
                  {m.pending && <div className="mt-1 text-[11px] text-mute">Akışa bir sonraki adımda eklenecek</div>}
                </div></div>)
              if (m.type === 'assistant') return (
                <div key={m.id} className="space-y-1.5">
                  {m.blocks.map((b: any, i: number) => b.type === 'text'
                    ? <div key={i} className="max-w-[90%] text-[13px] leading-relaxed whitespace-pre-wrap">{b.text}</div>
                    : b.type === 'thinking'
                      ? <details key={i} className="text-[12px] text-mute"><summary className="cursor-pointer">Düşünce özeti</summary><div className="mt-1 whitespace-pre-wrap border-l-2 border-line pl-2">{b.text}</div></details>
                      : <ToolRow key={b.id} b={b} result={results[b.id]} />)}
                </div>)
              if (m.type === 'note' && m.result) return <ResultCard key={m.id} r={m.result} />
              if (m.type === 'note') return (
                <div key={m.id} className={cx('rounded-lg border px-3 py-1.5 text-[12px]', m.level === 'warn' ? 'border-warn/40 bg-warn/10 text-warn' : 'border-line text-mute')}>{m.text}</div>)
              return null
            })}
            {stream && <div className="max-w-[90%] text-[13px] leading-relaxed whitespace-pre-wrap">{stream}<span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-accent align-middle" /></div>}
            {active && !stream && <div className="flex items-center gap-2 text-[12px] text-mute"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-info" />{activity || (run.status === 'waiting_jobs' ? 'Üretim sürüyor…' : 'Çalışıyor…')}</div>}

            {run?.status === 'waiting_user' && run.ask && (
              <div className="rounded-xl border border-accent/50 bg-accent/10 p-4 text-[13px]">
                <div className="mb-2 font-semibold text-[#cfc8ff]">{run.ask.kind === 'plan' ? 'Plan onayı' : 'Yönetmen soruyor'}</div>
                <div className="whitespace-pre-wrap">{run.ask.question}</div>
                {run.ask.kind === 'plan' && (
                  <div className="mt-3 space-y-2">
                    <div className="max-h-52 overflow-auto rounded border border-line"><table className="w-full text-left text-[12px]"><tbody>
                      {run.ask.scenes.map((s: any) => <tr key={s.no} className="border-b border-line last:border-0"><td className="w-8 px-2 py-1 text-mute">{s.no}</td><td className="w-24 text-mute">{s.type}</td><td className="py-1 pr-2">{s.text || '—'}</td></tr>)}</tbody></table></div>
                    <div className="text-[12px] text-mute">Tahmini maliyet ({run.ask.estimate.languages.join(', ')}): <b className="text-ink">{fmtUsd(run.ask.estimate.total_usd)}</b>
                      {' · '}{run.ask.estimate.by_provider.map((x: any) => `${x.label} ${fmtUsd(x.usd)}`).join(' · ')}</div>
                  </div>)}
                <div className="mt-3 flex flex-wrap gap-2">
                  {(run.ask.options?.length ? run.ask.options : []).map((o: string, i: number) => <Btn key={o} kind={i === 0 ? 'primary' : 'ghost'} onClick={() => send(o)}>{o}</Btn>)}
                  <span className="self-center text-[12px] text-mute">ya da yanıtınızı aşağıya yazın</span>
                </div>
              </div>)}
            {run?.status === 'waiting_connection' && (
              <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
                Üretim durdu: {run.waiting_service ? <b>{run.waiting_service}</b> : 'bir servis'} bağlantısının anahtarı geçersiz ya da kredisi bitmiş.
                {' '}<Link to="/ayarlar/baglantilar" className="underline">Bağlantılar</Link> sayfasında anahtarı yenileyip “Bağlantıyı test et”e basın; üretim kaldığı yerden devam eder.
              </div>)}
            {run?.status === 'paused_budget' && (
              <div className="rounded-xl border border-warn/50 bg-warn/10 p-4 text-[13px]">
                <div className="mb-1 font-semibold text-warn">Bütçe sınırı</div>
                <div>Harcanan {fmtUsd(run.spent_usd)}, sınır {fmtUsd(run.budget_usd)}. Devam etmek için bütçeyi artırın.</div>
                <div className="mt-3 flex gap-2">{[5, 10, 25].map((n) => <Btn key={n} kind={n === 10 ? 'primary' : 'ghost'} onClick={() => act(() => api.post(`/conversations/${id}/resume`, { add_budget: n }), 'Bütçe artırıldı, devam ediliyor')}>+{fmtUsd(n)} ekle ve devam et</Btn>)}</div>
              </div>)}
            {run?.status === 'failed' && (
              <div className="rounded-xl border border-bad/50 bg-bad/10 p-4 text-[13px]">
                <div className="mb-1 font-semibold text-bad">Çalışma durdu</div><div className="whitespace-pre-wrap text-[#ffc9ce]">{run.error}</div>
                <div className="mt-3"><Btn kind="primary" onClick={() => act(() => api.post(`/conversations/${id}/resume`, {}), 'Kaldığı yerden devam ediyor')}>Kaldığı yerden devam et</Btn></div>
              </div>)}
            <div ref={bottom} />
          </div>
        </div>

        <div className="border-t border-line px-5 py-3">
          <div className="mx-auto max-w-[860px]">
            {files.length > 0 && <div className="mb-2 flex flex-wrap gap-1.5">{files.map((f) => <span key={f.id} className="flex items-center gap-1 rounded border border-line bg-panel2 px-2 py-0.5 text-[12px]">📎 {f.name}<button aria-label="Eki kaldır" onClick={() => setFiles((x) => x.filter((y) => y.id !== f.id))} className="text-mute hover:text-bad">×</button></span>)}</div>}
            <div className="flex items-end gap-2">
              <input ref={fileRef} type="file" className="hidden" accept=".txt,.csv,.json,.tsv,.md,.png,.jpg,.jpeg,.webp,.mp3,.wav,.m4a"
                onChange={async (e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) await attach(f) }} />
              <Btn onClick={() => fileRef.current?.click()} title="Senaryo (metin/CSV/JSON), referans görsel, müzik ya da efekt ekle">📎</Btn>
              <textarea value={text} onChange={(e) => setText(e.target.value)} rows={Math.min(8, Math.max(2, text.split('\n').length))}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
                placeholder={active ? 'Araya girin: “3. sahnede kadın gülsün”, “İngilizce sürümünü de çıkar”, “durdur”…' : run?.status === 'waiting_user' ? 'Yanıtınızı yazın…' : 'Bir fikir, senaryo ya da talimat yazın…'}
                className="!rounded-xl !py-2.5" aria-label="Mesaj" />
              <Btn kind="primary" disabled={!text.trim() && !files.length} onClick={() => send()}>Gönder</Btn>
            </div>
            <div className="mt-1.5 text-[11px] text-mute">Enter gönderir, Shift+Enter yeni satır.{conv?.settings && <> Çalışma bütçesi {fmtUsd(conv.settings.budget_usd)} · plan onayı {conv.settings.plan_approval ? 'açık' : 'kapalı'} (<Link to="/ayarlar/tercihler" className="underline">Tercihler</Link>).</>}</div>
          </div>
        </div>
      </div>

      {/* sağ: canlı üretim paneli */}
      <div className="w-[330px] shrink-0 overflow-y-auto border-l border-line bg-panel/60">
        {conv ? <Panel conv={conv} /> : <Panel conv={{ panel: null }} />}
      </div>
    </div>
  )
}
