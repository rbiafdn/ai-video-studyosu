import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, useLive } from '../api'
import { Badge, Btn, Cost, Empty, Field, FileBtn, JobLine, Media, Modal, Panel, StatusBadge, useAction } from '../ui'

export function LangSelect({ value, onChange, langs, className }: { value: string; onChange: (v: string) => void; langs: any[]; className?: string }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className={className} aria-label="Dil">
      {!langs.some((l) => l.code === value) && value && <option value={value}>{value}</option>}
      {langs.map((l) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}
    </select>
  )
}

export function Projects() {
  const { data, reload, error } = useLive<any[]>('/projects', (e) => e.type === 'refresh')
  const langs = useLive<any>('/languages', () => false)
  const act = useAction()
  const nav = useNavigate()
  const [name, setName] = useState('')
  const [lang, setLang] = useState('tr')
  return (
    <div className="space-y-5">
      <div className="flex items-end justify-between">
        <div><h1 className="text-xl font-semibold">Projeler</h1><p className="text-[13px] text-mute">Elle kontrol ekranları. Her proje bir dizi/film; karakterler proje düzeyinde, sahneler bölümlerde.</p></div>
        <div className="flex gap-2">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Yeni proje adı" className="!w-52" />
          <LangSelect value={lang} onChange={setLang} langs={langs.data?.languages || []} className="!w-40" />
          <Btn kind="primary" onClick={async () => { const p = await act(() => api.post('/projects', { name: name || 'Yeni proje', default_language: lang })); if (p) nav(`/proje/${p.id}`) }}>Proje oluştur</Btn>
          <Btn onClick={async () => { await act(() => api.post('/seed'), 'Örnek proje eklendi'); reload() }}>Örnek projeyi ekle</Btn>
        </div>
      </div>
      {error && <Empty>{error}</Empty>}
      {data && data.length === 0 && <Empty>Henüz proje yok. Sohbetten bir bölüm ürettirin, yeni proje oluşturun ya da örnek projeyi ekleyin.</Empty>}
      <div className="grid grid-cols-3 gap-4">
        {data?.map((p) => (
          <Link key={p.id} to={`/proje/${p.id}`} className="rounded-xl border border-line bg-panel p-4 transition-colors hover:border-accent/60">
            <div className="mb-1 flex items-center gap-2 text-[15px] font-semibold">{p.name}<Badge tone="accent">{p.default_language}</Badge></div>
            <p className="mb-3 line-clamp-2 min-h-[36px] text-[13px] text-mute">{p.description || 'Açıklama yok'}</p>
            <div className="flex items-center gap-2"><Badge>{p.character_count} karakter</Badge><Badge>{p.episode_count} bölüm</Badge><span className="ml-auto"><Cost cost={p.cost} /></span></div>
          </Link>
        ))}
      </div>
    </div>
  )
}

function CharacterCard({ ch, voices, langs, reload }: { ch: any; voices: any[]; langs: any[]; reload: () => void }) {
  const act = useAction()
  const [f, setF] = useState(ch)
  const [open, setOpen] = useState(false)
  useEffect(() => setF(ch), [ch])
  const dirty = ['name', 'description', 'voice_id', 'is_narrator'].some((k) => f[k] !== ch[k]) ||
    ['voice_settings', 'heygen', 'voices'].some((k) => JSON.stringify(f[k]) !== JSON.stringify(ch[k]))
  const save = () => act(async () => { await api.patch(`/characters/${ch.id}`, { name: f.name, description: f.description, voice_id: f.voice_id, is_narrator: f.is_narrator, voice_settings: f.voice_settings, heygen: f.heygen, voices: f.voices }); reload() }, 'Karakter kaydedildi')
  const vs = f.voice_settings || {}
  const setVs = (k: string, v: number) => setF({ ...f, voice_settings: { ...vs, [k]: v } })
  const setVoice = (l: string, patch: any) => {
    const next = { ...(f.voices || {}) }
    const cur = { ...(next[l] || {}), ...patch }
    if (!cur.voice_id) delete next[l]; else next[l] = cur
    setF({ ...f, voices: next })
  }
  const draft = ch.images?.find((a: any) => a.status === 'draft')
  const job = ch.jobs?.find((j: any) => ['queued', 'submitting', 'running', 'waiting_manual', 'failed'].includes(j.status))
  const extra = langs.filter((l) => l.code !== langs[0]?.code)
  return (
    <div className="rounded-xl border border-line bg-panel p-4">
      <div className="flex gap-4">
        <div className="w-[132px] shrink-0">
          {ch.is_narrator ? <div className="flex aspect-square items-center justify-center rounded-md border border-dashed border-line text-center text-[12px] text-mute">Anlatıcı<br />(yalnızca ses)</div>
            : ch.main_image ? <Media asset={ch.main_image} className="aspect-square w-full" />
              : <div className="flex aspect-square items-center justify-center rounded-md border border-dashed border-line text-center text-[12px] text-mute">Onaylı ana<br />görsel yok</div>}
          {!ch.is_narrator && <div className="mt-2 flex flex-col gap-1.5">
            <Btn small kind="primary" onClick={() => act(() => api.post(`/characters/${ch.id}/generate-image`), 'Görsel işi kuyruğa alındı')}>{ch.images?.length ? 'Yeniden üret' : 'Ana görsel üret'}</Btn>
            <Btn small onClick={() => setOpen(true)}>Sürümler ({ch.images?.length || 0})</Btn>
          </div>}
        </div>
        <div className="grid flex-1 grid-cols-2 gap-3">
          <Field label="Ad"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
          <Field label="Varsayılan ElevenLabs ses kimliği" hint="Dile özel ses yoksa çok dilli modelle bu kullanılır">
            <input list={`voices-${ch.id}`} value={f.voice_id} onChange={(e) => setF({ ...f, voice_id: e.target.value })} placeholder="Örn. 21m00Tcm4TlvDq8ikWAM" />
            <datalist id={`voices-${ch.id}`}>{voices.map((v) => <option key={v.voice_id} value={v.voice_id}>{v.name}</option>)}</datalist>
          </Field>
          <Field label="Açıklama (görsel prompt için, İngilizce önerilir)" className="col-span-2">
            <textarea rows={2} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} disabled={f.is_narrator} />
          </Field>
          <Field label={`Kararlılık (stability): ${(vs.stability ?? 0.5).toFixed(2)}`}><input type="range" min={0} max={1} step={0.05} value={vs.stability ?? 0.5} onChange={(e) => setVs('stability', +e.target.value)} /></Field>
          <Field label={`Benzerlik (similarity): ${(vs.similarity_boost ?? 0.75).toFixed(2)}`}><input type="range" min={0} max={1} step={0.05} value={vs.similarity_boost ?? 0.75} onChange={(e) => setVs('similarity_boost', +e.target.value)} /></Field>
          {extra.length > 0 && (
            <div className="col-span-2 rounded-lg border border-line p-2.5">
              <div className="mb-1.5 text-[12px] text-mute">Dil başına ayrı ses (isteğe bağlı)</div>
              <div className="grid grid-cols-2 gap-2">
                {extra.map((l) => {
                  const v = (f.voices || {})[l.code] || {}
                  return (
                    <div key={l.code} className="flex items-center gap-2"><Badge tone="accent">{l.code}</Badge>
                      <input list={`voices-${ch.id}`} value={v.voice_id || ''} onChange={(e) => setVoice(l.code, { voice_id: e.target.value })} placeholder={`${l.name} için ses kimliği`} />
                      <input type="number" min={0} max={1} step={0.05} title="Kararlılık (bu dilde)" aria-label={`${l.code} kararlılık`} className="!w-16" disabled={!v.voice_id}
                        value={v.voice_settings?.stability ?? ''} onChange={(e) => setVoice(l.code, { voice_settings: { ...(v.voice_settings || {}), stability: e.target.value === '' ? undefined : +e.target.value } })} />
                    </div>)
                })}
              </div>
            </div>)}
          <Field label="HeyGen avatar bilgisi (isteğe bağlı not / avatar_id)"><input value={f.heygen?.avatar_id || ''} onChange={(e) => setF({ ...f, heygen: { ...f.heygen, avatar_id: e.target.value } })} placeholder="Boşsa onaylı görsel kullanılır" /></Field>
          <div className="flex items-end justify-between gap-2">
            <label className="flex items-center gap-2 !text-[13px] !text-ink"><input type="checkbox" checked={f.is_narrator} onChange={(e) => setF({ ...f, is_narrator: e.target.checked })} /> Anlatıcı sesi</label>
            <div className="flex gap-2">
              <Btn small kind="bad" onClick={() => confirm(`${ch.name} silinsin mi?`) && act(async () => { await api.del(`/characters/${ch.id}`); reload() })}>Sil</Btn>
              <Btn small kind="primary" disabled={!dirty} onClick={save}>Kaydet</Btn>
            </div>
          </div>
        </div>
      </div>
      {!ch.is_narrator && (
        <div className="mt-3 flex items-center gap-2 border-t border-line pt-3">
          <span className="text-[12px] text-mute">Referans görseller:</span>
          {ch.references?.map((a: any) => (
            <span key={a.id} className="group relative"><Media asset={a} className="h-10 w-10" />
              <button aria-label="Referansı kaldır" onClick={() => act(async () => { await api.del(`/assets/${a.id}`); reload() })} className="absolute -right-1 -top-1 hidden h-4 w-4 items-center justify-center rounded-full bg-bad text-[10px] text-white group-hover:flex">×</button></span>
          ))}
          <FileBtn accept="image/*" onFile={(file) => act(async () => { await api.upload(`/characters/${ch.id}/references`, file); reload() }, 'Referans eklendi')}>+ Görsel yükle</FileBtn>
          {draft && <span className="ml-auto flex items-center gap-2"><Badge tone="warn">Onay bekleyen görsel var</Badge><Btn small kind="ok" onClick={() => act(async () => { await api.post(`/assets/${draft.id}/approve`); reload() })}>Onayla</Btn></span>}
        </div>
      )}
      {job && <div className="mt-3 border-t border-line pt-3"><JobLine job={job} /></div>}
      {open && (
        <Modal title={`${ch.name}: ana görsel sürümleri`} onClose={() => setOpen(false)} wide>
          {!ch.images?.length && <Empty>Henüz üretilmiş görsel yok.</Empty>}
          <div className="grid grid-cols-4 gap-4">
            {ch.images?.map((a: any) => (
              <div key={a.id} className={`rounded-lg border p-2 ${a.id === ch.main_image_id ? 'border-ok' : 'border-line'}`}>
                <Media asset={a} className="aspect-square w-full" />
                <div className="mt-2 flex items-center gap-1.5 text-[12px]"><b>v{a.version}</b><StatusBadge status={a.status} />{a.id === ch.main_image_id && <Badge tone="ok">Seçili</Badge>}{a.approved_by === 'agent' && <Badge tone="info">ajan onayı</Badge>}</div>
                <div className="mt-2 flex gap-1.5">
                  {a.id !== ch.main_image_id && <Btn small kind="ok" onClick={() => act(async () => { await api.post(`/assets/${a.id}/approve`); reload() })}>{a.status === 'approved' ? 'Bunu seç' : 'Onayla'}</Btn>}
                  {a.status !== 'rejected' && <Btn small kind="bad" onClick={() => act(async () => { await api.post(`/assets/${a.id}/reject`); reload() })}>Reddet</Btn>}
                </div>
              </div>
            ))}
          </div>
        </Modal>
      )}
    </div>
  )
}

/** Dil bazlı telaffuz sözlüğü: her satır "yazılış = okunuş". */
function Pronunciation({ p, langs, reload }: { p: any; langs: any[]; reload: () => void }) {
  const act = useAction()
  const toText = (l: string) => ((p.pronunciations?.[l]?.rules || []) as string[][]).map((r) => `${r[0]} = ${r[1]}`).join('\n')
  const [lang, setLang] = useState(langs[0]?.code || p.default_language)
  const [text, setText] = useState(toText(lang))
  const [dict, setDict] = useState(p.pronunciations?.[lang]?.dictionary_id || '')
  useEffect(() => { setText(toText(lang)); setDict(p.pronunciations?.[lang]?.dictionary_id || '') }, [lang, p.pronunciations])
  const save = () => act(async () => {
    const rules = text.split('\n').map((l) => l.split('=').map((x) => x.trim())).filter((r) => r.length === 2 && r[0] && r[1])
    await api.patch(`/projects/${p.id}`, { pronunciations: { ...(p.pronunciations || {}), [lang]: { rules, dictionary_id: dict.trim() } } })
    reload()
  }, 'Telaffuz sözlüğü kaydedildi')
  return (
    <Panel title="Telaffuz sözlüğü (dil başına)" right={<><LangSelect value={lang} onChange={setLang} langs={langs} className="!w-44 !py-1 text-[12px]" /><Btn small kind="primary" onClick={save}>Kaydet</Btn></>}>
      <div className="grid grid-cols-[minmax(0,1fr)_320px] gap-4">
        <Field label="Sayı, tarih, kısaltma okunuşları: her satıra “yazılış = okunuş”" hint="Ses üretilmeden önce metinde değiştirilir; altyazıda özgün yazılış kalır.">
          <textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} className="font-mono !text-[12px]" placeholder={'Dr. = Doktor\nTBMM = Türkiye Büyük Millet Meclisi\n2026 = iki bin yirmi altı'} />
        </Field>
        <Field label="ElevenLabs sözlük kimliği (isteğe bağlı)" hint="ElevenLabs’te oluşturduğunuz pronunciation dictionary kimliği; isteğe eklenir.">
          <input value={dict} onChange={(e) => setDict(e.target.value)} placeholder="pronunciation_dictionary_id" />
        </Field>
      </div>
    </Panel>
  )
}

export function Project() {
  const { id } = useParams()
  const { data: p, reload, error } = useLive<any>(`/projects/${id}`, (e) => e.type === 'job' || e.type === 'refresh')
  const voices = useLive<any>('/voices', () => false)
  const all = useLive<any>('/languages', () => false)
  const act = useAction()
  const nav = useNavigate()
  const [f, setF] = useState<any>(null)
  useEffect(() => { if (p && (!f || f.id !== p.id)) setF({ id: p.id, name: p.name, description: p.description, style: p.style, default_language: p.default_language }) }, [p])
  if (error) return <Empty>{error}</Empty>
  if (!p || !f) return <Empty>Yükleniyor…</Empty>
  const dirty = ['name', 'description', 'style', 'default_language'].some((k) => f[k] !== p[k])
  const langs: any[] = p.languages
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2 text-[13px] text-mute"><Link to="/projeler" className="hover:text-ink">Projeler</Link><span>/</span><span className="text-ink">{p.name}</span><span className="ml-auto"><Cost cost={p.cost} /></span></div>
      <Panel title="Proje" right={<>
        <Btn small kind="bad" onClick={() => confirm('Proje ve tüm dosyaları silinsin mi?') && act(async () => { await api.del(`/projects/${p.id}`); nav('/projeler') })}>Projeyi sil</Btn>
        <Btn small kind="primary" disabled={!dirty} onClick={() => act(async () => { await api.patch(`/projects/${p.id}`, f); reload() }, 'Kaydedildi')}>Kaydet</Btn></>}>
        <div className="grid grid-cols-4 gap-3">
          <Field label="Ad"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
          <Field label="Açıklama"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
          <Field label="Ortak görsel üslup (tüm görsel promptlara eklenir)"><input value={f.style} onChange={(e) => setF({ ...f, style: e.target.value })} placeholder="cinematic, warm light…" /></Field>
          <Field label="Varsayılan dil" hint="Bölüm ve sahne düzeyinde değiştirilebilir"><LangSelect value={f.default_language} onChange={(v) => setF({ ...f, default_language: v })} langs={all.data?.languages || []} /></Field>
        </div>
        <div className="mt-3 flex items-center gap-2 text-[12px] text-mute">Projede kullanılan diller: {langs.map((l) => <Badge key={l.code} tone="accent">{l.name} ({l.code})</Badge>)}<span>· yeni hedef dil bölüm ekranından eklenir</span></div>
      </Panel>

      <div className="flex items-center justify-between"><h2 className="text-[15px] font-semibold">Bölümler</h2>
        <Btn kind="primary" onClick={async () => { const e = await act(() => api.post(`/projects/${p.id}/episodes`, {})); if (e) nav(`/bolum/${e.id}`) }}>+ Bölüm ekle</Btn></div>
      {p.episodes.length === 0 && <Empty>Bölüm yok. Bir bölüm ekleyip senaryoyu içe aktarın.</Empty>}
      <div className="grid grid-cols-3 gap-4">
        {p.episodes.map((e: any) => (
          <Link key={e.id} to={`/bolum/${e.id}`} className="rounded-xl border border-line bg-panel p-4 hover:border-accent/60">
            <div className="flex items-center justify-between"><span className="text-[15px] font-semibold">Bölüm {e.number}: {e.title}</span>
              {e.auto_state === 'running' && <Badge tone="info">Otomatik çalışıyor</Badge>}{e.auto_state === 'done' && <Badge tone="ok">Tamamlandı</Badge>}{e.auto_state === 'error' && <Badge tone="bad">Durdu</Badge>}</div>
            <div className="my-3 flex gap-1">{e.steps.filter((s: any) => s.n <= 6).map((s: any) => <span key={s.n} title={`Adım ${s.n}`} className={`h-1.5 flex-1 rounded-full ${s.complete ? 'bg-ok' : 'bg-white/10'}`} />)}</div>
            <div className="flex items-center gap-2"><Badge>{e.scene_count} sahne</Badge>{e.languages.map((l: string) => <Badge key={l} tone="accent">{l}</Badge>)}<span className="ml-auto"><Cost cost={e.cost} /></span></div>
          </Link>
        ))}
      </div>

      <Pronunciation p={p} langs={langs} reload={reload} />

      <div className="flex items-center justify-between"><h2 className="text-[15px] font-semibold">Karakterler</h2>
        <Btn onClick={() => act(async () => { await api.post(`/projects/${p.id}/characters`, { name: 'Yeni karakter' }); reload() })}>+ Karakter ekle</Btn></div>
      {voices.data?.mode === 'mock' && <p className="text-[12px] text-mute">Mock modda ses kimliği zorunlu değil. Gerçek modda her karakter için ElevenLabs voice_id girin (anahtar girilince liste otomatik dolar).</p>}
      <div className="grid grid-cols-2 gap-4">
        {p.characters.map((c: any) => <CharacterCard key={c.id} ch={c} voices={voices.data?.voices || []} langs={langs} reload={reload} />)}
      </div>
    </div>
  )
}
