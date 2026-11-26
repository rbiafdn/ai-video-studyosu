import { useEffect, useState } from 'react'
import { api, fmtSec, fmtTime, MODE_LABEL, TYPE_LABEL, useLive } from '../api'
import { Badge, Btn, cx, Empty, Field, JobLine, Media, Modal, StatusBadge, useAction } from '../ui'

const FIELDS = ['type', 'character_ids', 'image_prompt', 'video_prompt', 'duration', 'audio_moment', 'notes', 'order_no', 'image_text']

function Versions({ sc, role, kind, title, selectedId, lang, shared }: { sc: any; role: string; kind: string; title: string; selectedId: number | null; lang: string; shared?: boolean }) {
  const act = useAction()
  const versions = sc.assets.filter((a: any) => a.role === role)
  const job = sc.jobs[role]
  const busy = job && job.status !== 'failed'
  return (
    <div className="min-w-0">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-[13px] font-semibold">{title}</h3><Badge>{versions.length} sürüm</Badge>
        {shared ? <Badge title="Dilden bağımsız: tüm dillerde aynı varlık kullanılır">ortak</Badge> : <Badge tone="accent">{sc.effective_language}</Badge>}
        <Btn small className="ml-auto" disabled={busy} onClick={() => act(() => api.post(`/scenes/${sc.id}/generate`, { kind, lang }), 'Yeni sürüm kuyruğa alındı')}>{versions.length ? 'Yeniden üret' : 'Üret'}</Btn>
      </div>
      {job && <div className="mb-2 rounded-lg border border-line bg-panel2 p-2"><JobLine job={job} compact /></div>}
      {versions.length === 0 && !job && <Empty>Henüz sürüm yok.</Empty>}
      {/* sürümler yan yana */}
      <div className="flex gap-3 overflow-x-auto pb-2">
        {versions.map((a: any) => {
          const sel = a.id === selectedId
          return (
            <div key={a.id} className={cx('shrink-0 rounded-lg border p-2', kind === 'audio' ? 'w-[250px]' : 'w-[260px]', sel ? 'border-ok bg-ok/5' : a.status === 'rejected' ? 'border-line opacity-60' : 'border-line')}>
              {kind === 'audio' ? <Media asset={a} /> : <Media asset={a} className="aspect-video w-full" />}
              <div className="mt-2 flex flex-wrap items-center gap-1 text-[12px]">
                <b>v{a.version}</b><StatusBadge status={a.status} />{sel && <Badge tone="ok">Seçili</Badge>}
                <Badge tone={a.mode === 'real' ? 'ok' : a.mode === 'manual' ? 'accent' : 'mute'}>{MODE_LABEL[a.mode] || a.mode}</Badge>
                {a.step === 7 && <Badge tone="info">Düzeltme</Badge>}
                {a.approved_by === 'agent' && <Badge tone="info" title="Yönetmen ajanı onayladı">ajan onayı</Badge>}
                {a.approved_by === 'auto' && <Badge title="Otomatik modda onaylandı">otomatik</Badge>}
              </div>
              <div className="mt-1 text-[11px] text-mute">{a.provider}{a.model ? ` · ${a.model}` : ''} · {a.meta.duration ? fmtSec(a.meta.duration) : `${a.meta.width}×${a.meta.height}`}{a.meta.width && a.meta.duration ? ` · ${a.meta.width}×${a.meta.height}` : ''} · {fmtTime(a.created_at)}</div>
              {a.prompt && <div className="mt-1 line-clamp-2 text-[11px] text-mute" title={a.prompt}>{a.prompt}</div>}
              <div className="mt-2 flex gap-1.5">
                {!sel && <Btn small kind="ok" onClick={() => act(() => api.post(`/assets/${a.id}/approve`))}>{a.status === 'approved' ? 'Bu sürümü seç' : 'Onayla'}</Btn>}
                {a.status !== 'rejected' && <Btn small kind="bad" onClick={() => act(() => api.post(`/assets/${a.id}/reject`))}>Reddet</Btn>}
                <a href={a.url} download className="ml-auto self-center text-[12px] text-info hover:underline">İndir</a>
              </div>
            </div>)
        })}
      </div>
    </div>
  )
}

export default function SceneDetail({ sceneId, episode, lang, fixHint, onClose }: { sceneId: number; episode: any; lang: string; fixHint?: any; onClose: () => void }) {
  const act = useAction()
  const { data: sc } = useLive<any>(`/scenes/${sceneId}?lang=${lang}`, (e) => (e.type === 'job' || e.type === 'refresh') && (!e.scene_id || e.scene_id === sceneId))
  const [f, setF] = useState<any>(null)
  const [texts, setTexts] = useState<Record<string, string>>({})
  const [instruction, setInstruction] = useState('')
  useEffect(() => { if (sc && (!f || f.id !== sc.id)) { setF(sc); setTexts({}) } }, [sc])
  useEffect(() => { if (fixHint) setInstruction(`${fixHint.timecode} civarı: ${fixHint.message}. Bunu düzelt.`) }, [fixHint])
  if (!sc || !f) return <Modal title="Sahne" onClose={onClose} wide><Empty>Yükleniyor…</Empty></Modal>

  const dirtyTexts = Object.entries(texts).filter(([l, v]) => v !== (sc.texts[l]?.text ?? ''))
  const dirty = FIELDS.some((k) => JSON.stringify(f[k]) !== JSON.stringify(sc[k])) || dirtyTexts.length > 0 || (f.language || '') !== (sc.language || '')
  const save = () => act(async () => {
    const body: any = Object.fromEntries(FIELDS.map((k) => [k, f[k]]))
    body.language = f.language || episode.source_language
    if (dirtyTexts.length) body.texts = Object.fromEntries(dirtyTexts)
    const r = await api.patch(`/scenes/${sc.id}?lang=${lang}`, body)
    setF(r); setTexts({})
  }, 'Sahne kaydedildi')
  const chars = episode.characters.filter((c: any) => !c.is_narrator)
  const toggle = (id: number) => setF({ ...f, character_ids: f.character_ids.includes(id) ? f.character_ids.filter((x: number) => x !== id) : [...f.character_ids, id] })
  const fix = (method: string) => act(() => api.post(`/scenes/${sc.id}/fix`, { method, instruction, lang }), 'Düzeltme başlatıldı')
  const busy = Object.values(sc.jobs).some((j: any) => j.status !== 'failed')
  const talking = sc.type === 'talking'
  const langs: string[] = sc.fixed_language ? [sc.effective_language] : sc.languages
  const rows: any[] = episode.language_rows
  const rtl = (l: string) => rows.find((x) => x.code === l)?.rtl
  const hasImageText = Object.values(f.image_text || {}).some((v: any) => (v || '').trim())

  return (
    <Modal wide onClose={onClose} title={<span className="flex items-center gap-2">Sahne {sc.order_no}<Badge tone={talking ? 'accent' : sc.type === 'narrator' ? 'info' : 'mute'}>{TYPE_LABEL[sc.type]}</Badge>
      <Badge tone="accent">{sc.effective_language}</Badge>{sc.stale && <Badge tone="warn">{sc.stale}</Badge>}{sc.needs_review && <Badge tone="warn" title={sc.review_note}>Elle bakılmalı</Badge>}</span>}>
      <div className="grid grid-cols-[380px_minmax(0,1fr)] gap-6">
        {/* sol: sahne bilgileri, dil başına replik ve prompt düzenleme */}
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-3">
            <Field label="Sıra no"><input type="number" value={f.order_no} onChange={(e) => setF({ ...f, order_no: +e.target.value })} /></Field>
            <Field label="Tür"><select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })}><option value="talking">Konuşan</option><option value="silent">Konuşmasız</option><option value="narrator">Anlatıcı</option></select></Field>
            <Field label="Süre (sn)"><input type="number" min={1} step={0.5} value={f.duration} onChange={(e) => setF({ ...f, duration: +e.target.value })} /></Field>
          </div>
          <Field label="Karakter(ler)">
            <div className="flex flex-wrap gap-1.5">{chars.map((c: any) => (
              <button key={c.id} onClick={() => toggle(c.id)} className={cx('rounded-md border px-2 py-1 text-[12px]', f.character_ids.includes(c.id) ? 'border-accent bg-accent/20 text-white' : 'border-line text-mute hover:text-ink')}>{c.name}</button>))}
              {chars.length === 0 && <span className="text-[12px] text-mute">Projede karakter yok.</span>}</div>
          </Field>

          {f.type !== 'silent' && (
            <div className="rounded-lg border border-line p-2.5">
              <div className="mb-2 flex items-center gap-2 text-[12px] text-mute">{f.type === 'narrator' ? 'Anlatıcı metni' : 'Replik'} (dil başına)
                <span className="ml-auto flex items-center gap-1.5">Sahnenin dili
                  <select value={f.language || episode.source_language} onChange={(e) => setF({ ...f, language: e.target.value === episode.source_language ? '' : e.target.value })} className="!w-28 !py-0.5 text-[12px]" title="Bölümün dilinden farklı seçilirse sahne her dil sürümünde bu dilde kalır" aria-label="Sahnenin dili">
                    {rows.map((l) => <option key={l.code} value={l.code}>{l.code}{l.is_source ? ' (bölüm)' : ''}</option>)}</select></span>
              </div>
              {langs.map((l) => {
                const t = sc.texts[l]
                const val = texts[l] ?? t?.text ?? ''
                const isSrc = l === sc.source_language
                return (
                  <div key={l} className="mb-2 last:mb-0">
                    <div className="mb-1 flex items-center gap-1.5 text-[12px]"><Badge tone={l === sc.effective_language ? 'accent' : 'mute'}>{l}</Badge>{isSrc && <span className="text-mute">kaynak</span>}
                      {t?.text ? <Badge tone={t.status === 'approved' ? 'ok' : 'warn'}>{t.status === 'approved' ? 'Onaylı' : 'Taslak'}</Badge> : <Badge>Boş</Badge>}
                      {t?.detected && <span className="text-[11px] text-mute" title="Otomatik algılama (yalnızca öneri)">algılanan: {t.detected} %{Math.round(t.confidence * 100)}</span>}
                      {t?.detected && t.confidence >= 0.85 && !l.startsWith(t.detected) && <Badge tone="warn">başka dil?</Badge>}
                      <span className="ml-auto text-[11px] text-mute">{val.length} karakter</span>
                      {!isSrc && t?.text && t.status !== 'approved' && texts[l] === undefined && <Btn small kind="ok" onClick={() => act(() => api.post(`/scenes/${sc.id}/texts/${l}/approve`, { approved: true }))}>Onayla</Btn>}
                    </div>
                    <textarea rows={2} dir={rtl(l) ? 'rtl' : 'ltr'} value={val} onChange={(e) => setTexts({ ...texts, [l]: e.target.value })} placeholder={isSrc ? 'Kaynak metin' : `${l} çevirisi (onaylanmadan ses üretilmez)`} />
                  </div>)
              })}
            </div>)}

          <Field label="Görsel prompt (dilden bağımsız)" hint={talking ? 'Dudak senkronu şartları (yüz kameraya dönük, ağız net ve kapalı…) otomatik eklenir.' : undefined}><textarea rows={3} value={f.image_prompt} onChange={(e) => setF({ ...f, image_prompt: e.target.value })} /></Field>
          <Field label="Video prompt (dilden bağımsız)" hint={talking ? 'HeyGen’e hareket yönergesi olarak gider.' : 'Kling video’ya gider; boşsa görsel prompt kullanılır.'}><textarea rows={2} value={f.video_prompt} onChange={(e) => setF({ ...f, video_prompt: e.target.value })} /></Field>
          <details className="rounded-lg border border-line" open={hasImageText}>
            <summary className="cursor-pointer px-2.5 py-1.5 text-[12px] text-mute">Görseldeki metin (tabela, ekran yazısı) {hasImageText && <Badge tone="warn">sahne dile bağlı</Badge>}</summary>
            <div className="space-y-1.5 border-t border-line p-2.5">
              <p className="text-[11px] text-mute">Doldurulursa bu sahnenin görseli ve videosu her dil için ayrı üretilir.</p>
              {sc.languages.map((l: string) => <div key={l} className="flex items-center gap-2"><Badge tone="accent">{l}</Badge>
                <input dir={rtl(l) ? 'rtl' : 'ltr'} value={(f.image_text || {})[l] || ''} onChange={(e) => setF({ ...f, image_text: { ...(f.image_text || {}), [l]: e.target.value } })} placeholder="Görselde okunacak yazı" /></div>)}
            </div>
          </details>
          <label className="flex items-center gap-2 !text-[13px] !text-ink"><input type="checkbox" checked={f.audio_moment} onChange={(e) => setF({ ...f, audio_moment: e.target.checked })} /> Sesli an (Kling yerel ses / native audio açık)</label>
          <Field label="Notlar"><textarea rows={2} value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} /></Field>
          <div className="flex justify-between gap-2 pt-1">
            <Btn kind="bad" onClick={() => confirm('Sahne silinsin mi?') && act(async () => { await api.del(`/scenes/${sc.id}`); onClose() })}>Sahneyi sil</Btn>
            {sc.needs_review && <Btn onClick={() => act(() => api.patch(`/scenes/${sc.id}?lang=${lang}`, { needs_review: false }))}>“Elle bakılmalı” işaretini kaldır</Btn>}
            <Btn kind="primary" disabled={!dirty} onClick={save}>{dirty ? 'Değişiklikleri kaydet' : 'Kaydedildi'}</Btn>
          </div>
          {dirty && <p className="text-[12px] text-warn">Replik değişirse o dilin sesi ve konuşan videosu “güncel değil” olur ve yeniden üretilmesi gerekir.</p>}
        </div>

        {/* sağ: sürümler ve düzeltme */}
        <div className="min-w-0 space-y-5">
          {sc.type !== 'silent' && <Versions sc={sc} role="voice" kind="audio" title="Ses (ElevenLabs)" selectedId={sc.audio_id} lang={lang} />}
          <Versions sc={sc} role="scene_image" kind="image" title="Sahne görseli (Kling)" selectedId={sc.image_id} lang={lang} shared={!sc.language_dependent.image} />
          <Versions sc={sc} role="scene_video" kind="video" title={talking ? 'Konuşan video (HeyGen)' : 'Video (Kling)'} selectedId={sc.video_id} lang={lang} shared={!sc.language_dependent.video} />

          <div className="rounded-xl border border-line bg-panel2 p-4">
            <div className="mb-2 flex items-center gap-2"><h3 className="text-[13px] font-semibold">7 · Düzeltme ({sc.effective_language})</h3>
              <span className="text-[12px] text-mute">Sonuç onaylanınca yalnızca bu sahne, etkilenen dil ve kurgu yeniden işlenir.</span></div>
            {fixHint && <div className="mb-2 rounded border border-warn/30 bg-warn/10 px-2 py-1 text-[12px] text-warn">Kontrol bulgusu: {fixHint.timecode} · {fixHint.message}{fixHint.suggest_label && <b> → {fixHint.suggest_label}</b>}</div>}
            <textarea rows={2} value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="Omni için düzeltme talimatı: örn. “arka plandaki tabelayı kaldır, gökyüzünü günbatımı yap”" />
            <div className="mt-2 grid grid-cols-4 gap-2">
              <Btn disabled={busy || !sc.video_id || !instruction.trim()} onClick={() => fix('omni')} title="Seçili video + talimat → düzeltilmiş video">Kling Omni ile düzelt</Btn>
              <Btn kind={fixHint?.suggest === 'lipsync' ? 'primary' : 'ghost'} disabled={busy || !talking || !sc.video_id || !sc.audio_id} onClick={() => fix('lipsync')} title="Seçili video + bu dilin onaylı sesi → ağız hareketi düzeltmesi">Kling dudak senkronu</Btn>
              <Btn disabled={busy || sc.type === 'silent'} onClick={() => fix('regen_audio')}>Sesi yeniden üret</Btn>
              <Btn disabled={busy} onClick={() => confirm('Ses, görsel ve video baştan üretilip kendiliğinden onaylanacak. Devam edilsin mi?') && fix('regen_scene')}>Sahneyi baştan üret</Btn>
            </div>
            <p className="mt-2 text-[12px] text-mute">Kling, Omni ve dudak senkronunda kaynak videoyu yalnızca internet adresiyle kabul ediyor. Videonun sağlayıcıdaki adresi hâlâ geçerliyse iş API ile, değilse “elle adım” olarak açılır.</p>
          </div>

          <details className="rounded-lg border border-line">
            <summary className="cursor-pointer px-3 py-2 text-[13px] text-mute">İş geçmişi ({sc.history.length})</summary>
            <div className="space-y-2 border-t border-line p-3">{sc.history.map((j: any) => <JobLine key={j.id} job={j} />)}</div>
          </details>
        </div>
      </div>
    </Modal>
  )
}
