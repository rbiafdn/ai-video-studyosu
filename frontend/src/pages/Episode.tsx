import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, fmtSec, fmtUsd, TYPE_LABEL, useLive } from '../api'
import { Badge, Btn, Cost, cx, Empty, Field, FileBtn, JobLine, Media, Modal, Panel, Progress, useAction, useToast } from '../ui'
import { LangSelect } from './Projects'
import SceneDetail from './SceneDetail'

const STEP_HINT: Record<number, string> = {
  1: 'ElevenLabs: replik ve anlatıcı metinlerinden, bu dilde ses',
  2: 'Kling: karakter ana görselleri ve sahne görselleri (diller arası ortak)',
  3: 'HeyGen: onaylı görsel + bu dilin onaylı sesi → konuşan video',
  4: 'Kling video: sahne görseli + video prompt (diller arası ortak)',
  5: 'ffmpeg: sıralama, geçiş, müzik, -14 LUFS, bu dilin altyazısı',
  6: 'Kalite raporu: teknik denetim, dil uyumu, dudak senkronu',
  7: 'Omni, dudak senkronu, yeniden üretim',
}

function StepCard({ st, ep, scope, onResult }: { st: any; ep: any; scope: string; onResult: (r: any) => void }) {
  const act = useAction()
  const run = (mode: string) => act(async () => onResult(await api.post(`/episodes/${ep.id}/steps/${st.n}/run`, { mode, lang: scope })))
  const pct = st.total ? st.approved / st.total : 0
  const blocked = st.n === 5 && st.blockers?.length > 0
  return (
    <div className={cx('flex min-w-0 flex-1 flex-col rounded-xl border bg-panel p-3', st.complete ? 'border-ok/40' : st.failed ? 'border-bad/50' : st.running ? 'border-info/50' : 'border-line')}>
      <div className="flex items-center gap-2">
        <span className={cx('flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[12px] font-bold', st.complete ? 'bg-ok text-black' : 'bg-white/10 text-ink')}>{st.complete ? '✓' : st.n}</span>
        <span className="truncate text-[13px] font-semibold" title={st.title}>{st.title}</span>
      </div>
      <p className="mt-1 min-h-[32px] text-[11px] leading-4 text-mute">{STEP_HINT[st.n]}</p>
      {st.n !== 7 && <div className="my-2"><Progress value={pct} tone={st.complete ? 'ok' : 'accent'} live={st.running > 0} /></div>}
      <div className="flex min-h-[20px] flex-wrap gap-1 text-[11px]">
        {st.n <= 4 && <Badge tone={st.complete ? 'ok' : 'mute'}>{st.approved}/{st.total} onaylı</Badge>}
        {st.n === 7 && <Badge>{st.approved} düzeltme</Badge>}
        {st.draft > 0 && <Badge tone="warn">{st.draft} onay bekliyor</Badge>}
        {st.stale > 0 && <Badge tone="warn" title="Girdisi değişmiş; 'Eksikleri üret' bunları da yeniler">{st.stale} güncel değil</Badge>}
        {st.running > 0 && <Badge tone="info">{st.running} çalışıyor</Badge>}
        {st.failed > 0 && <Badge tone="bad">{st.failed} hata</Badge>}
        {st.note && <Badge tone="warn" title={st.note}>{st.n >= 5 ? 'Güncel değil' : st.note.slice(0, 26)}</Badge>}
      </div>
      <div className="mt-auto flex flex-wrap gap-1.5 pt-2.5">
        {st.n <= 4 && <>
          <Btn small kind="primary" disabled={!st.total} onClick={() => run('missing')} title="Onaylı sürümü ve taslağı olmayanları, ayrıca güncel olmayanları üretir">Eksikleri üret</Btn>
          <Btn small disabled={!st.total} onClick={() => confirm(`"${st.title}" adımındaki tüm öğeler için yeni sürüm üretilsin mi?`) && run('all')}>Tümünü üret</Btn>
        </>}
        {st.n === 5 && <Btn small kind="primary" disabled={blocked || st.running > 0} title={blocked ? st.blockers.join('\n') : ''} onClick={() => run('all')}>Kurguyu çalıştır</Btn>}
        {st.n === 6 && <Btn small kind="primary" disabled={!ep.final || st.running > 0} onClick={() => run('all')}>Kontrolü çalıştır</Btn>}
        {st.n === 7 && <span className="text-[11px] text-mute">Sahne ayrıntısından ya da kontrol bulgusundan başlatılır.</span>}
      </div>
    </div>
  )
}

/** Sahne tablosundaki ses/görsel/video hücresi (seçili dile göre). */
function Cell({ sc, role, kind, needed, selectedId, lang, shared }: { sc: any; role: string; kind: string; needed: boolean; selectedId: number | null; lang: string; shared?: boolean }) {
  const act = useAction()
  if (!needed) return <span className="text-[12px] text-mute">—</span>
  const versions = sc.assets.filter((a: any) => a.role === role)
  const selected = versions.find((a: any) => a.id === selectedId)
  const draft = versions.find((a: any) => a.status === 'draft')
  const shown = selected || draft
  const job = sc.jobs[role]
  const active = job && job.status !== 'failed'
  const diff = role === 'voice' && sc.audio_duration && sc.source_audio_duration ? sc.audio_duration - sc.source_audio_duration : 0
  return (
    <div className="flex w-full flex-col gap-1">
      {shown && (kind === 'audio' ? <Media asset={shown} /> : <Media asset={shown} controls={false} className="aspect-video w-full" />)}
      <div className="flex flex-wrap items-center gap-1">
        {selected ? <Badge tone="ok">Onaylı v{selected.version}</Badge> : draft ? <Badge tone="warn">Taslak v{draft.version}</Badge> : !job && <Badge>Yok</Badge>}
        {selected && draft && <Badge tone="warn" title="Daha yeni bir taslak onay bekliyor">+ taslak v{draft.version}</Badge>}
        {shared && <Badge title="Dilden bağımsız: tüm dillerde aynı varlık kullanılır">ortak</Badge>}
        {role === 'voice' && sc.voice_stale && <Badge tone="warn" title="Replik değişti; ses eski metinden">Güncel değil</Badge>}
        {role === 'scene_video' && sc.stale && <Badge tone="warn" title={sc.stale}>Güncel değil</Badge>}
        {Math.abs(diff) >= 0.8 && <Badge tone="info" title="Bu dilin sesi kaynak dilden farklı uzunlukta; sahne süresi bu dile göre ayarlanır">{diff > 0 ? '+' : ''}{diff.toFixed(1)} sn</Badge>}
        {job?.status === 'failed' && <Badge tone="bad" title={job.error}>Hata</Badge>}
        {job?.status === 'waiting_manual' && <Badge tone="accent">Elle adım</Badge>}
      </div>
      {active && job.status !== 'waiting_manual' && <Progress value={job.progress} live />}
      <div className="flex gap-1">
        {draft && <Btn small kind="ok" onClick={() => act(() => api.post(`/assets/${draft.id}/approve`))}>Onayla</Btn>}
        {!active && <Btn small onClick={() => act(() => api.post(`/scenes/${sc.id}/generate`, { kind, lang }))} title="Yalnızca bu sahne için yeni sürüm üretir">{versions.length ? 'Yeniden üret' : 'Üret'}</Btn>}
      </div>
    </div>
  )
}

function ImportModal({ ep, onClose }: { ep: any; onClose: () => void }) {
  const act = useAction()
  const [text, setText] = useState('')
  const [filename, setFilename] = useState('')
  const [rows, setRows] = useState<any[] | null>(null)
  const [replace, setReplace] = useState(ep.scenes.length === 0)
  const preview = async (t = text, fn = filename) => { const r = await act(() => api.post(`/episodes/${ep.id}/import`, { text: t, filename: fn, preview: true })); setRows(r ? r.rows : null) }
  return (
    <Modal title="Senaryo içe aktar" onClose={onClose} wide>
      <div className="grid grid-cols-2 gap-5">
        <div className="space-y-3">
          <p className="text-[13px] text-mute">Metni yapıştırın ya da CSV / JSON dosyası seçin. Sütun sırası: <b className="text-ink">sahne no, tür, karakter, replik, dil, görsel prompt, video prompt, süre</b>. Dil boşsa metinden algılanır (kısa metinde bölümün dili kullanılır). Örnekler <code>examples/</code> klasöründe.</p>
          <textarea rows={16} value={text} onChange={(e) => { setText(e.target.value); setFilename('') }} className="font-mono !text-[12px]"
            placeholder={'1 | anlatıcı | | Bir zamanlar… | | geniş plan | yavaş kaydırma | 5\n2 | konuşan | Elif | Merhaba! | tr | yakın plan | gülümser | 4\n\nya da serbest metin:\nELİF: Merhaba!\nANLATICI: Ertesi sabah…\n[Sahilde yürüyüş]'} />
          <div className="flex items-center gap-2">
            <FileBtn accept=".csv,.json,.txt,.tsv" onFile={async (f) => { const t = await f.text(); setText(t); setFilename(f.name); await preview(t, f.name) }}>Dosya seç (CSV / JSON)</FileBtn>
            <Btn small onClick={() => preview()} disabled={!text.trim()}>Önizle</Btn>
            <label className="ml-auto flex items-center gap-2 !text-[13px] !text-ink"><input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> Mevcut sahneleri sil</label>
          </div>
        </div>
        <div>
          {!rows ? <Empty>Önizleme burada görünür.</Empty> : (
            <div className="max-h-[440px] overflow-auto rounded-lg border border-line">
              <table className="w-full text-left text-[12px]"><thead className="sticky top-0 bg-panel2 text-mute"><tr><th className="px-2 py-1.5">#</th><th>Tür</th><th>Karakter</th><th>Replik</th><th>Dil</th><th>Süre</th></tr></thead>
                <tbody>{rows.map((r, i) => <tr key={i} className="border-t border-line align-top"><td className="px-2 py-1.5">{r.order_no}</td><td>{TYPE_LABEL[r.type]}</td><td>{r.characters.join(', ')}</td><td className="max-w-[240px] pr-2">{r.dialogue || <span className="text-mute">{r.image_prompt}</span>}</td>
                  <td>{r.language ? <Badge tone="accent">{r.language}</Badge> : r.detected ? <Badge tone={r.detected.fallback ? 'mute' : 'info'} title={r.detected.fallback ? `Algılama güvenilir değil (${r.detected.detected || '?'} ${Math.round(r.detected.confidence * 100)}%): bölüm dili kullanılır` : `Algılandı, güven ${Math.round(r.detected.confidence * 100)}%`}>{r.detected.language}{r.detected.fallback ? '' : ' ✓'}</Badge> : ''}</td><td>{r.duration}</td></tr>)}</tbody></table>
            </div>
          )}
          <div className="mt-3 flex justify-end gap-2">
            <Btn onClick={onClose}>Vazgeç</Btn>
            <Btn kind="primary" disabled={!rows?.length} onClick={async () => { const r = await act(() => api.post(`/episodes/${ep.id}/import`, { text, filename, replace }), `${rows!.length} sahne eklendi`); if (r) onClose() }}>{rows ? `${rows.length} sahneyi içe aktar` : 'İçe aktar'}</Btn>
          </div>
        </div>
      </div>
    </Modal>
  )
}

/** Hedef dil için çeviri alanları: otomatik önerilir, elle düzeltilir, onaylanmadan ses üretilmez. */
function Translations({ ep, lang }: { ep: any; lang: string }) {
  const act = useAction()
  const src = ep.source_language
  const rows = ep.scenes.filter((s: any) => s.type !== 'silent' && !s.fixed_language && s.texts[src]?.text)
  const [edit, setEdit] = useState<Record<number, string>>({})
  useEffect(() => setEdit({}), [lang, ep.id])
  if (!rows.length) return null
  const pending = rows.filter((s: any) => s.texts[lang]?.text && s.texts[lang].status !== 'approved')
  const missing = rows.filter((s: any) => !s.texts[lang]?.text)
  const rtl = ep.language_rows.find((l: any) => l.code === lang)?.rtl
  const save = (s: any) => act(() => api.patch(`/scenes/${s.id}?lang=${lang}`, { texts: { [lang]: edit[s.id] } }).then(() => setEdit((e) => { const n = { ...e }; delete n[s.id]; return n })))
  return (
    <Panel title={`Çeviriler: ${src} → ${lang}`} right={<>
      {missing.length > 0 && <Badge tone="warn">{missing.length} eksik</Badge>}{pending.length > 0 && <Badge tone="warn">{pending.length} onay bekliyor</Badge>}
      <Btn small disabled={!missing.length} onClick={() => act(() => api.post(`/episodes/${ep.id}/translations/suggest`, { language: lang }), 'Çeviri önerileri yazıldı; kontrol edip onaylayın')} title="Claude ile boş replikleri çevirir (Anthropic anahtarı gerekir)">Çeviri öner</Btn>
      <Btn small kind="ok" disabled={!pending.length} onClick={() => act(async () => { for (const s of pending) await api.post(`/scenes/${s.id}/texts/${lang}/approve`, { approved: true }) }, 'Çeviriler onaylandı')}>Tümünü onayla</Btn></>}>
      <p className="mb-2 text-[12px] text-mute">Çeviri onaylanmadan o dilin sesi üretilmez. Anahtar yoksa alanları elle doldurun.</p>
      <table className="w-full table-fixed text-left text-[13px]">
        <tbody>{rows.map((s: any) => {
          const t = s.texts[lang]
          const val = edit[s.id] ?? t?.text ?? ''
          const ratio = val && s.texts[src].text ? val.length / s.texts[src].text.length : 1
          return (
            <tr key={s.id} className="border-t border-line align-top">
              <td className="w-9 py-2 font-semibold">{s.order_no}</td>
              <td className="py-2 pr-3 text-mute">{s.texts[src].text}</td>
              <td className="py-2 pr-3"><textarea rows={2} dir={rtl ? 'rtl' : 'ltr'} value={val} onChange={(e) => setEdit({ ...edit, [s.id]: e.target.value })} placeholder={`${lang} çevirisi`} /></td>
              <td className="w-[210px] py-2">
                <div className="flex flex-wrap items-center gap-1">
                  {t?.text ? <Badge tone={t.status === 'approved' ? 'ok' : 'warn'}>{t.status === 'approved' ? 'Onaylı' : 'Taslak'}</Badge> : <Badge>Boş</Badge>}
                  {t?.origin === 'suggestion' && <Badge tone="info">öneri</Badge>}{t?.origin === 'agent' && <Badge tone="info">ajan</Badge>}
                  {(ratio > 1.35 || ratio < 0.65) && val && <Badge tone="info" title="Çeviri kaynaktan belirgin uzun/kısa; sahne süresi bu dilin sesine göre ayarlanır">{ratio > 1 ? 'uzun' : 'kısa'} ×{ratio.toFixed(1)}</Badge>}
                  {t?.detected && t.confidence >= 0.85 && !lang.startsWith(t.detected) && <Badge tone="warn" title="Metin başka bir dilde görünüyor">{t.detected}?</Badge>}
                </div>
                <div className="mt-1.5 flex gap-1">
                  {edit[s.id] !== undefined && edit[s.id] !== (t?.text ?? '') && <Btn small kind="primary" onClick={() => save(s)}>Kaydet</Btn>}
                  {t?.text && t.status !== 'approved' && edit[s.id] === undefined && <Btn small kind="ok" onClick={() => act(() => api.post(`/scenes/${s.id}/texts/${lang}/approve`, { approved: true }))}>Onayla</Btn>}
                </div>
              </td>
            </tr>)
        })}</tbody>
      </table>
    </Panel>
  )
}

function MontagePanel({ ep, videoRef }: { ep: any; videoRef: React.RefObject<HTMLVideoElement | null> }) {
  const act = useAction()
  const s = ep.settings
  const set = (patch: any) => act(() => api.patch(`/episodes/${ep.id}?lang=${ep.language}`, { settings: patch }))
  const [sfxAt, setSfxAt] = useState('0')
  return (
    <Panel title={`5 · Kurgu ve ses miksajı (${ep.language})`} right={ep.final && <>
      <Badge tone="ok">v{ep.final.version}</Badge>
      <a className="text-[12px] text-info hover:underline" href={ep.final.url} download>⬇ 1080p mp4</a>
      {ep.final_vertical && <a className="text-[12px] text-info hover:underline" href={ep.final_vertical.url} download>⬇ Dikey 9:16</a>}
      {ep.subtitles && <a className="text-[12px] text-info hover:underline" href={ep.subtitles.url} download>⬇ SRT ({ep.language})</a>}</>}>
      <div className="grid grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)] gap-5">
        <div>
          {ep.final ? <video ref={videoRef} key={ep.final.id} src={`${ep.final.url}?v=${ep.final.id}#t=0.5`} controls className="aspect-video w-full rounded-lg bg-black" />
            : <Empty>Bu dil için henüz kurgu çıktısı yok. Tüm sahnelerin videosu ve sesi onaylanınca “Kurguyu çalıştır”.</Empty>}
          {ep.final && <p className="mt-2 text-[12px] text-mute">{ep.final.meta.width}×{ep.final.meta.height} · {ep.final.meta.fps} fps · {fmtSec(ep.final.meta.duration)}
            {ep.final.meta.subtitle_font && ` · altyazı yazı tipi ${ep.final.meta.subtitle_font}${ep.final.meta.rtl ? ' (sağdan sola)' : ''}`}
            {ep.final.meta.rebuilt_scenes?.length ? ` · bu sürümde yeniden işlenen sahneler: ${ep.final.meta.rebuilt_scenes.join(', ')}` : ' · tüm sahneler önbellekten'}</p>}
          {ep.local_errors.map((j: any) => <div key={j.id} className="mt-2"><JobLine job={j} /></div>)}
        </div>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Geçiş"><select value={s.transition} onChange={(e) => set({ transition: e.target.value })}><option value="fade">Kararma</option><option value="xfade">Çapraz geçiş</option><option value="cut">Düz kesme</option></select></Field>
            <Field label={`Geçiş süresi: ${s.transition_dur} sn`}><input type="range" min={0.1} max={1} step={0.1} value={s.transition_dur} onChange={(e) => set({ transition_dur: +e.target.value })} /></Field>
            <Field label={`Müzik seviyesi: ${Math.round(s.music_volume * 100)}%`}><input type="range" min={0} max={1} step={0.05} value={s.music_volume} onChange={(e) => set({ music_volume: +e.target.value })} /></Field>
            <div className="flex flex-col justify-end gap-1.5 text-[13px]">
              <label className="flex items-center gap-2 !text-[13px] !text-ink"><input type="checkbox" checked={s.ducking} onChange={(e) => set({ ducking: e.target.checked })} /> Konuşmada müziği kıs</label>
              <label className="flex items-center gap-2 !text-[13px] !text-ink"><input type="checkbox" checked={s.subtitles} onChange={(e) => set({ subtitles: e.target.checked })} /> Altyazı (SRT) üret</label>
              <label className="flex items-center gap-2 !text-[13px] !text-ink"><input type="checkbox" checked={s.burn_subtitles} disabled={!s.subtitles} onChange={(e) => set({ burn_subtitles: e.target.checked })} /> Altyazıyı videoya göm</label>
              <label className="flex items-center gap-2 !text-[13px] !text-ink"><input type="checkbox" checked={s.vertical} onChange={(e) => set({ vertical: e.target.checked })} /> Dikey 9:16 da üret</label>
            </div>
          </div>
          <div className="rounded-lg border border-line p-3">
            <div className="mb-2 flex items-center gap-2 text-[12px] text-mute">Müzik ve efekt katmanı (tüm dillerde ortak)
              <span className="ml-auto flex items-center gap-1.5">
                <FileBtn accept="audio/*" onFile={(f) => act(() => api.upload(`/episodes/${ep.id}/uploads`, f, { role: 'music' }), 'Müzik eklendi')}>+ Müzik</FileBtn>
                <input value={sfxAt} onChange={(e) => setSfxAt(e.target.value)} className="!w-14 !py-[3px] text-[12px]" title="Efektin başlayacağı saniye" aria-label="Efekt saniyesi" />
                <FileBtn accept="audio/*" onFile={(f) => act(() => api.upload(`/episodes/${ep.id}/uploads`, f, { role: 'sfx', at: String(parseFloat(sfxAt.replace(',', '.')) || 0) }), 'Efekt eklendi')}>+ Efekt (sn)</FileBtn>
              </span>
            </div>
            {ep.uploads.length === 0 && <p className="text-[12px] text-mute">Yüklenmiş dosya yok. Müzik bölüm boyunca döner; efektler girilen saniyede çalar.</p>}
            {ep.uploads.map((a: any) => (
              <div key={a.id} className="mt-1.5 flex items-center gap-2 text-[12px]"><Badge tone={a.role === 'music' ? 'accent' : 'info'}>{a.role === 'music' ? 'Müzik' : `Efekt @ ${a.params.at} sn`}</Badge>
                <span className="truncate">{a.meta.name}</span><div className="w-40 shrink-0"><Media asset={a} /></div>
                <Btn small kind="bad" onClick={() => act(() => api.del(`/assets/${a.id}`))}>Kaldır</Btn></div>
            ))}
          </div>
          <p className="text-[12px] text-mute">Ayarlar tüm dillerde ortaktır. Değişince her dilin kurgusunu yeniden çalıştırın; yalnızca değişen sahneler yeniden işlenir.</p>
        </div>
      </div>
    </Panel>
  )
}

const SEV: Record<string, [string, string]> = { error: ['bad', 'Hata'], warn: ['warn', 'Uyarı'], info: ['info', 'Bilgi'] }

function QCPanel({ ep, seek, open }: { ep: any; seek: (t: number) => void; open: (sid: number, fix?: any) => void }) {
  const rep = ep.report
  const [showInfo, setShowInfo] = useState(false)
  if (!rep) return <Panel title={`6 · Kontrol (${ep.language})`}><Empty>Bu dil için kalite raporu yok. Kurgudan sonra “Kontrolü çalıştır”.</Empty></Panel>
  const s = rep.summary
  const stale = ep.final && rep.final_asset_id !== ep.final.id
  const list = rep.findings.filter((f: any) => showInfo || f.severity !== 'info')
  const sync = Object.entries(s.lipsync || {}) as [string, any][]
  return (
    <Panel title={`6 · Kontrol: kalite raporu (${ep.language})`} right={<>
      {stale && <Badge tone="warn">Rapor eski kurguya ait</Badge>}
      <Badge tone={s.errors ? 'bad' : s.warnings ? 'warn' : 'ok'}>{s.errors ? `${s.errors} hata` : s.warnings ? `${s.warnings} uyarı` : 'Temiz'}</Badge>
      <label className="flex items-center gap-1.5"><input type="checkbox" checked={showInfo} onChange={(e) => setShowInfo(e.target.checked)} /> Bilgi notları ({s.infos})</label></>}>
      <div className="mb-3 flex flex-wrap gap-x-6 gap-y-1 text-[12px] text-mute">
        <span>Kurgu v{s.final_version}</span><span>Süre <b className="text-ink">{fmtSec(s.duration)}</b></span><span>{s.width}×{s.height} · {s.fps} fps</span><span>{s.scenes} sahne</span>
        {s.loudness?.integrated != null && <span>Ses yüksekliği <b className="text-ink">{s.loudness.integrated} LUFS</b> (hedef -14)</span>}
        {sync.length > 0 && <span>Dudak senkronu: {sync.map(([no, m]) => `S${no} ${m.measurable ? `${m.offset_ms > 0 ? '+' : ''}${m.offset_ms} ms` : '?'}`).join(' · ')}</span>}
      </div>
      {list.length === 0 ? <Empty>{rep.findings.length ? 'Hata ya da uyarı yok; yalnızca bilgi notları var.' : 'Hiç bulgu yok.'}</Empty> : (
        <table className="w-full text-left text-[13px]">
          <thead className="text-[12px] text-mute"><tr><th className="w-20 pb-1.5 font-medium">Önem</th><th className="w-28 font-medium">Zaman</th><th className="w-20 font-medium">Sahne</th><th className="font-medium">Bulgu</th><th /></tr></thead>
          <tbody>{list.map((f: any, i: number) => (
            <tr key={i} className="border-t border-line">
              <td className="py-1.5"><Badge tone={SEV[f.severity][0]}>{SEV[f.severity][1]}</Badge></td>
              <td>{f.timecode ? <button className="font-mono text-[12px] text-info hover:underline" title="Oynatıcıda bu ana git" onClick={() => seek(f.time)}>{f.timecode}</button> : <span className="text-mute">—</span>}</td>
              <td>{f.scene_no ? <button className="text-info hover:underline" onClick={() => open(f.scene_id)}>Sahne {f.scene_no}</button> : <span className="text-mute">Genel</span>}</td>
              <td className="pr-3">{f.message}</td>
              <td className="text-right whitespace-nowrap">{f.scene_id && f.severity !== 'info' && <Btn small kind={f.suggest === 'lipsync' ? 'primary' : 'ghost'} onClick={() => open(f.scene_id, f)}>{f.suggest_label || 'Düzelt'}</Btn>}</td>
            </tr>))}</tbody>
        </table>)}
    </Panel>
  )
}

export default function Episode() {
  const { id, sid } = useParams()
  const [params, setParams] = useSearchParams()
  const nav = useNavigate()
  const toast = useToast()
  const act = useAction()
  const lang = params.get('dil') || ''
  const { data: ep, error, reload } = useLive<any>(`/episodes/${id}${lang ? `?lang=${lang}` : ''}`, (e) => (e.type === 'job' || e.type === 'refresh') && (!e.episode_id || String(e.episode_id) === id))
  const all = useLive<any>('/languages', () => false)
  const [importing, setImporting] = useState(false)
  const [fixHint, setFixHint] = useState<any>(null)
  const [title, setTitle] = useState('')
  const [allLangs, setAllLangs] = useState(false)
  const [adding, setAdding] = useState('')
  const videoRef = useRef<HTMLVideoElement>(null)
  useEffect(() => { if (ep) setTitle(ep.title) }, [ep?.id, ep?.title])
  if (error && !ep) return <Empty>{error}</Empty>
  if (!ep) return <Empty>Yükleniyor…</Empty>

  const cur: string = ep.language
  const q = `?dil=${cur}`
  const multi = ep.languages.length > 1
  const scope = allLangs && multi ? '*' : cur
  const chars: Record<number, any> = Object.fromEntries(ep.characters.map((c: any) => [c.id, c]))
  const used = ep.characters.filter((c: any) => !c.is_narrator && ep.scenes.some((s: any) => s.character_ids.includes(c.id)))
  const row = ep.language_rows.find((l: any) => l.code === cur)
  const onResult = (r: any) => {
    const n = r.created.length
    const why = r.skipped.map((k: any) => `• ${k.who}: ${k.reason}`).join('\n')
    if (n) toast(`${n} iş kuyruğa alındı${why ? `\nAtlananlar:\n${why}` : ''}`, 'ok')
    else toast(why ? `Hiçbir iş başlatılamadı:\n${why}` : 'Üretilecek eksik öğe yok.', why ? 'bad' : 'info')
    reload()
  }
  const open = (sceneId: number, fix?: any) => { setFixHint(fix || null); nav(`/bolum/${ep.id}/sahne/${sceneId}${q}`) }
  const seek = (t: number) => { const v = videoRef.current; if (v) { v.currentTime = t; v.scrollIntoView({ behavior: 'smooth', block: 'center' }); v.play().catch(() => {}) } }
  const auto = ep.auto_state === 'running'
  const manualJobs = ep.active_jobs.filter((j: any) => j.status === 'waiting_manual')
  const candidates = (all.data?.languages || []).filter((l: any) => !ep.languages.includes(l.code))
  const addLang = () => act(async () => {
    const r = await api.post(`/episodes/${ep.id}/languages`, { language: adding })
    if (r.warnings?.length) toast(`Dil eklendi. Uyarılar:\n${r.warnings.map((w: string) => '• ' + w).join('\n')}`, 'info')
    setParams({ dil: adding }); setAdding('')
  }, 'Dil eklendi: yalnızca sesler, konuşan videolar, altyazı ve kurgu üretilecek')

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-[13px] text-mute">
        <Link to="/projeler" className="hover:text-ink">Projeler</Link><span>/</span><Link to={`/proje/${ep.project.id}`} className="hover:text-ink">{ep.project.name}</Link><span>/</span>
        <span className="text-ink">Bölüm {ep.number}</span>
        <input value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => title !== ep.title && act(() => api.patch(`/episodes/${ep.id}`, { title }))} className="!w-64 !border-transparent !bg-transparent !py-0.5 text-[15px] font-semibold text-ink hover:!border-line" aria-label="Bölüm başlığı" />
        <span className="ml-auto flex items-center gap-3">
          <Cost cost={ep.cost} />
          <Btn small onClick={() => setImporting(true)}>Senaryo içe aktar</Btn>
          {auto ? <Btn small kind="bad" onClick={() => act(() => api.post(`/episodes/${ep.id}/auto`, { on: false }))}>Otomatik modu durdur</Btn>
            : <Btn small kind="primary" disabled={!ep.scenes.length} title="Onay beklemeden 1'den 6'ya kadar sırayla çalıştırır" onClick={() => confirm(`Otomatik mod (${scope === '*' ? 'tüm diller' : cur}): taslaklar ve çeviriler onay beklemeden kabul edilir, 1-6 adımları sırayla çalışır. Başlatılsın mı?`) && act(() => api.post(`/episodes/${ep.id}/auto`, { on: true, langs: scope === '*' ? null : [cur] }), 'Otomatik mod başladı')}>▶ Otomatik mod (1→6)</Btn>}
        </span>
      </div>

      {/* dil seçici: sahne metinleri, varlıklar, hat durumu ve çıktı seçili dile göre gösterilir */}
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-panel px-3 py-2">
        <span className="text-[12px] text-mute">Dil</span>
        {ep.language_rows.map((l: any) => (
          <button key={l.code} onClick={() => setParams({ dil: l.code })} aria-pressed={l.code === cur}
            className={cx('flex items-center gap-2 rounded-lg border px-3 py-1.5 text-[13px]', l.code === cur ? 'border-accent bg-accent/20 text-white' : 'border-line text-mute hover:text-ink')}>
            <b>{l.name}</b><span className="text-[11px] opacity-80">{l.code}{l.is_source ? ' · kaynak' : ''}</span>
            <span className="flex gap-0.5">{[1, 2, 3, 4, 5, 6].map((n) => <span key={n} className={cx('h-1.5 w-2 rounded-sm', n <= l.complete ? 'bg-ok' : 'bg-white/15')} />)}</span>
            {l.warnings.length > 0 && <span className="text-warn" title={l.warnings.join('\n')}>⚠</span>}
          </button>))}
        <span className="flex items-center gap-1.5 pl-2">
          <select value={adding} onChange={(e) => setAdding(e.target.value)} className="!w-44 !py-1 text-[12px]" aria-label="Eklenecek dil"><option value="">+ Hedef dil ekle…</option>{candidates.map((l: any) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}</select>
          {adding && <Btn small kind="primary" onClick={addLang}>Ekle</Btn>}
        </span>
        <span className="ml-auto flex items-center gap-3 text-[12px]">
          {multi && <label className="flex items-center gap-1.5 !text-[12px] !text-ink" title="Adım düğmeleri seçili dil yerine tüm hedef dillerde çalışır"><input type="checkbox" checked={allLangs} onChange={(e) => setAllLangs(e.target.checked)} /> Düğmeler tüm dillerde çalışsın</label>}
          {!row?.is_source && <Btn small kind="bad" onClick={() => confirm(`${cur} dili bölümden kaldırılsın mı? (Üretilen dosyalar silinmez)`) && act(async () => { await api.del(`/episodes/${ep.id}/languages/${cur}`); setParams({}) })}>Dili kaldır</Btn>}
        </span>
      </div>
      {row?.warnings.length > 0 && <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn"><b>{row.name} için uyarı:</b> {row.warnings.join(' · ')}</div>}

      {ep.auto_message && ep.auto_state && (
        <div className={cx('flex items-center gap-3 rounded-lg border px-3 py-2 text-[13px]', ep.auto_state === 'error' ? 'border-bad/50 bg-bad/10 text-[#ffc9ce]' : ep.auto_state === 'done' ? 'border-ok/40 bg-ok/10 text-ok' : 'border-info/40 bg-info/10 text-info')}>
          <b>{ep.auto_state === 'running' ? 'Otomatik mod' : ep.auto_state === 'done' ? 'Bitti' : 'Durdu'}</b><span className="flex-1">{ep.auto_message}</span>
          {auto && <div className="w-48"><Progress value={ep.steps.filter((s: any) => s.n <= 6 && s.complete).length / 6} live /></div>}
        </div>
      )}

      <div className="flex gap-2.5">{ep.steps.map((st: any) => <StepCard key={st.n} st={st} ep={ep} scope={scope} onResult={onResult} />)}</div>

      {manualJobs.length > 0 && <Panel title="Elle tamamlanacak adımlar"><div className="space-y-3">{manualJobs.map((j: any) => <JobLine key={j.id} job={j} />)}</div></Panel>}

      {!row?.is_source && <Translations ep={ep} lang={cur} />}

      {used.length > 0 && (
        <Panel title="2 · Karakter ana görselleri (tüm dillerde ortak)" right={<Link className="text-[12px] text-info hover:underline" to={`/proje/${ep.project.id}`}>Karakterleri düzenle</Link>}>
          <div className="flex flex-wrap gap-4">
            {used.map((c: any) => {
              const draft = c.images.find((a: any) => a.status === 'draft')
              const shown = c.main_image || draft
              const job = c.jobs.find((j: any) => ['queued', 'submitting', 'running', 'waiting_manual'].includes(j.status))
              const failed = c.jobs[0]?.status === 'failed' ? c.jobs[0] : null
              return (
                <div key={c.id} className="flex w-[300px] gap-3 rounded-lg border border-line p-2.5">
                  <div className="w-24 shrink-0">{shown ? <Media asset={shown} className="aspect-square w-full" /> : <div className="flex aspect-square items-center justify-center rounded-md border border-dashed border-line text-[11px] text-mute">Görsel yok</div>}</div>
                  <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                    <div className="truncate text-[13px] font-semibold">{c.name}</div>
                    <div className="flex flex-wrap gap-1">{c.main_image ? <Badge tone="ok">Onaylı v{c.main_image.version}</Badge> : draft ? <Badge tone="warn">Taslak v{draft.version}</Badge> : <Badge>Yok</Badge>}
                      {c.voices?.[cur]?.voice_id ? <Badge tone="info" title="Bu dil için ayrı ses tanımlı">{cur} sesi</Badge> : cur !== ep.source_language && <Badge title="Bu dil için ayrı ses yok: varsayılan ses çok dilli modelle kullanılır">varsayılan ses</Badge>}
                      {failed && <Badge tone="bad" title={failed.error}>Hata</Badge>}</div>
                    {job && <Progress value={job.progress} live />}
                    <div className="mt-auto flex gap-1">
                      {draft && <Btn small kind="ok" onClick={() => act(() => api.post(`/assets/${draft.id}/approve`))}>Onayla</Btn>}
                      {draft && <Btn small kind="bad" onClick={() => act(() => api.post(`/assets/${draft.id}/reject`))}>Reddet</Btn>}
                      {!job && <Btn small onClick={() => act(() => api.post(`/characters/${c.id}/generate-image`))}>{c.images.length ? 'Yeniden üret' : 'Üret'}</Btn>}
                    </div>
                  </div>
                </div>)
            })}
          </div>
        </Panel>
      )}

      <Panel title={`Sahneler (${ep.scenes.length}) · ${row?.name}`} right={<Btn small onClick={async () => { const s = await act(() => api.post(`/episodes/${ep.id}/scenes`, { type: 'silent' })); if (s) open(s.id) }}>+ Sahne ekle</Btn>}>
        {ep.scenes.length === 0 ? <Empty>Sahne yok. “Senaryo içe aktar” ile metin, CSV ya da JSON'dan sahneleri oluşturun; ya da sohbetten yönetmene yazdırın.</Empty> : (
          <table className="w-full table-fixed text-left">
            <thead className="text-[12px] text-mute"><tr><th className="w-9 pb-2 font-medium">#</th><th className="w-[104px] font-medium">Tür</th><th className="font-medium">Metin ({cur}) / prompt</th><th className="w-[200px] font-medium">1 · Ses</th><th className="w-[170px] font-medium">2 · Görsel</th><th className="w-[170px] font-medium">3-4 · Video</th><th className="w-[72px]" /></tr></thead>
            <tbody>
              {ep.scenes.map((sc: any) => (
                <tr key={sc.id} className="border-t border-line align-top">
                  <td className="py-3 text-[13px] font-semibold">{sc.order_no}</td>
                  <td className="py-3"><div className="flex flex-col items-start gap-1"><Badge tone={sc.type === 'talking' ? 'accent' : sc.type === 'narrator' ? 'info' : 'mute'}>{TYPE_LABEL[sc.type]}</Badge>
                    {sc.audio_moment && <Badge tone="warn" title="Videoda yerel ses (native audio) açık">Sesli an</Badge>}{sc.chain && <Badge tone="info">Baştan üretiliyor</Badge>}
                    {sc.fixed_language && <Badge tone="info" title="Bu sahne her dil sürümünde bu dilde kalır">{sc.effective_language} sabit</Badge>}
                    {sc.needs_review && <Badge tone="warn" title={sc.review_note}>Elle bakılmalı</Badge>}
                    <span className="text-[11px] text-mute">{sc.duration} sn</span></div></td>
                  <td className="py-3 pr-4">
                    <div className="mb-0.5 text-[12px] text-mute">{sc.type === 'narrator' ? 'Anlatıcı' : sc.character_ids.map((c: number) => chars[c]?.name).filter(Boolean).join(', ') || <span className="text-warn">Karakter atanmamış</span>}</div>
                    {sc.type !== 'silent' && (sc.dialogue
                      ? <div className="text-[13px]" dir={row?.rtl && !sc.fixed_language ? 'rtl' : undefined}>“{sc.dialogue}” {sc.text_status !== 'approved' && <Badge tone="warn">çeviri taslak</Badge>}</div>
                      : <div className="text-[13px] text-warn">{sc.effective_language} metni yok</div>)}
                    <div className="mt-1 line-clamp-2 text-[12px] text-mute" title={sc.image_prompt}>{sc.image_prompt || <span className="text-warn">Görsel prompt boş</span>}</div>
                  </td>
                  <td className="py-3 pr-3"><Cell sc={sc} role="voice" kind="audio" needed={sc.type !== 'silent'} selectedId={sc.audio_id} lang={cur} /></td>
                  <td className="py-3 pr-3"><Cell sc={sc} role="scene_image" kind="image" needed selectedId={sc.image_id} lang={cur} shared={multi && !sc.language_dependent.image} /></td>
                  <td className="py-3 pr-3"><Cell sc={sc} role="scene_video" kind="video" needed selectedId={sc.video_id} lang={cur} shared={multi && !sc.language_dependent.video} /></td>
                  <td className="py-3 text-right"><Btn small onClick={() => open(sc.id)}>Ayrıntı</Btn></td>
                </tr>))}
            </tbody>
          </table>)}
      </Panel>

      <MontagePanel ep={ep} videoRef={videoRef} />
      <QCPanel ep={ep} seek={seek} open={open} />

      {multi && (
        <div className="flex flex-wrap gap-x-6 gap-y-1 rounded-lg border border-line px-3 py-2 text-[12px] text-mute">
          <span className="text-ink">Dil bazında maliyet:</span>
          {ep.cost_by_language.map((l: any) => <span key={l.language}>{l.label}: <b className="text-ink">{fmtUsd(l.real_usd)}</b>{l.mock_usd > 0 && ` · mock karşılığı ${fmtUsd(l.mock_usd)}`}</span>)}
        </div>)}

      {importing && <ImportModal ep={ep} onClose={() => { setImporting(false); reload() }} />}
      {sid && <SceneDetail sceneId={+sid} episode={ep} lang={cur} fixHint={fixHint} onClose={() => { setFixHint(null); nav(`/bolum/${ep.id}${q}`) }} />}
    </div>
  )
}
