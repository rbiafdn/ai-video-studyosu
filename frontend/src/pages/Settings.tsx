import { useEffect, useState } from 'react'
import { Link, NavLink, useNavigate, useParams } from 'react-router-dom'
import { api, fmtUsd, useLive } from '../api'
import { useAuth } from '../auth'
import { Badge, Btn, cx, Empty, Field, ModeBadge, Panel, useAction } from '../ui'
import { ConnectionCard } from './Connections'

const MODEL_SHORT: Record<string, string> = { eleven_v4: 'v4', eleven_v3: 'v3', eleven_multilingual_v2: 'ML v2', eleven_flash_v2_5: 'Flash' }
const TABS: [string, string][] = [['hesap', 'Hesap'], ['baglantilar', 'Bağlantılar'], ['tercihler', 'Tercihler']]

export function Settings() {
  const { tab = 'baglantilar' } = useParams()
  return (
    <div className="max-w-[1100px] space-y-5">
      <div><h1 className="text-xl font-semibold">Ayarlar</h1>
        <p className="text-[13px] text-mute">Anahtarlarınız sunucuda şifreli saklanır; tarayıcıya, günlüklere, hata mesajlarına ve Claude’a giden içeriğe gönderilmez. Yönetici dahil kimse anahtarınızı göremez.</p></div>
      <div className="flex gap-1 border-b border-line">
        {TABS.map(([k, label]) => (
          <NavLink key={k} to={`/ayarlar/${k}`} className={cx('-mb-px border-b-2 px-4 py-2 text-[13px] font-medium', tab === k ? 'border-accent text-white' : 'border-transparent text-mute hover:text-ink')}>{label}</NavLink>))}
      </div>
      {tab === 'hesap' ? <Account /> : tab === 'tercihler' ? <Prefs /> : <ConnectionsTab />}
    </div>
  )
}

function ConnectionsTab() {
  const { reload: reloadMe } = useAuth()
  const { data, reload } = useLive<any>('/connections', (e) => e.type === 'refresh')
  if (!data) return <Empty>Yükleniyor…</Empty>
  const demo = data.summary.demo_steps
  return (
    <div className="space-y-4">
      {demo.length > 0 && <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">Şu adımlar deneme modunda (sahte çıktıyla) çalışıyor: {demo.join(', ')}. Gerçek üretim için ilgili servisi bağlayın.</div>}
      <div className="grid grid-cols-2 gap-4">
        {data.connections.map((c: any) => <ConnectionCard key={c.service} c={c} onChange={() => { reload(); reloadMe() }} />)}
      </div>
      <p className="text-[12px] text-mute">Servislerin e-posta/şifre ile hesap girişi hiçbir zaman istenmez; yalnızca resmi API anahtarları kullanılır. “Bağlantıyı test et” ücretsiz bir hesap sorgusu yapar, üretim başlatmaz.</p>
    </div>
  )
}

function Account() {
  const { me, reload, logout } = useAuth()
  const act = useAction()
  const nav = useNavigate()
  const [f, setF] = useState({ email: me?.user.email || '', current_password: '', new_password: '' })
  const [del, setDel] = useState('')
  if (!me) return null
  return (
    <div className="space-y-5">
      <Panel title="Hesap bilgileri">
        <div className="grid grid-cols-3 gap-4">
          <Field label="E-posta" hint={me.user.email_verified ? 'Doğrulandı' : 'Doğrulanmadı'}><input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
          <Field label="Yeni şifre" hint="Değiştirmeyecekseniz boş bırakın. En az 10 karakter."><input type="password" autoComplete="new-password" value={f.new_password} onChange={(e) => setF({ ...f, new_password: e.target.value })} /></Field>
          <Field label="Mevcut şifre" hint="Değişikliği onaylamak için"><input type="password" autoComplete="current-password" value={f.current_password} onChange={(e) => setF({ ...f, current_password: e.target.value })} /></Field>
        </div>
        <div className="mt-3 flex items-center gap-3">
          <Btn kind="primary" disabled={!f.current_password} onClick={() => act(async () => {
            await api.patch('/account', { current_password: f.current_password, email: f.email, new_password: f.new_password || null })
            setF({ ...f, current_password: '', new_password: '' }); await reload()
          }, 'Hesap güncellendi')}>Kaydet</Btn>
          <span className="text-[12px] text-mute">Rol: {me.user.role === 'admin' ? 'Yönetici' : 'Kullanıcı'}{me.user.is_demo && ' · demo hesabı'}. Şifre değişince diğer cihazlardaki oturumlar kapanır.</span>
        </div>
      </Panel>
      <Panel title="Oturum"><Btn onClick={async () => { await logout(); nav('/giris') }}>Çıkış yap</Btn></Panel>
      <Panel title="Hesabı sil">
        <p className="text-[13px] text-mute">Hesabınızla birlikte <b className="text-ink">bütün projeleriniz, üretilen dosyalar, sohbetler, işler ve kayıtlı servis anahtarlarınız</b> kalıcı olarak silinir. Bu işlem geri alınamaz.</p>
        <div className="mt-3 flex max-w-[460px] items-end gap-3">
          <Field label="Onay için şifreniz" className="flex-1"><input type="password" autoComplete="current-password" value={del} onChange={(e) => setDel(e.target.value)} /></Field>
          <Btn kind="bad" disabled={!del} onClick={() => confirm('Hesabınız ve bütün verileriniz kalıcı olarak silinsin mi?') && act(async () => { await api.post('/account/delete', { password: del }); await logout(); nav('/giris') })}>Hesabımı ve verilerimi sil</Btn>
        </div>
      </Panel>
    </div>
  )
}

function Prefs() {
  const { reload: reloadMe } = useAuth()
  const { data, setData } = useLive<any>('/config', () => false)
  const act = useAction()
  const [f, setF] = useState<any>(null)
  useEffect(() => {
    if (data) setF({ ...data.prefs, concurrency: { ...data.prefs.concurrency }, models: Object.fromEntries(data.providers.map((p: any) => [p.key, p.model])), claude_model: data.claude.model })
  }, [data])
  if (!data || !f) return <Empty>Yükleniyor…</Empty>
  const save = () => act(async () => { setData(await api.put('/prefs', f)); await reloadMe() }, 'Tercihler kaydedildi')
  const SaveBtn = <Btn small kind="primary" onClick={save}>Kaydet</Btn>
  return (
    <div className="space-y-5">
      <Panel title="Genel" right={SaveBtn}>
        <div className="grid grid-cols-4 gap-4">
          <Field label="Varsayılan proje dili">
            <select value={f.default_language} onChange={(e) => setF({ ...f, default_language: e.target.value })}>
              {data.languages.map((l: any) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}</select></Field>
          <Field label="Çalışma başına bütçe sınırı ($)" hint="Sağlayıcı işleri + Claude maliyeti. 0 = sınırsız.">
            <input type="number" min={0} step={1} value={f.budget_usd} onChange={(e) => setF({ ...f, budget_usd: +e.target.value })} /></Field>
          <Field label="Varsayılan çıktı biçimi" hint="Yeni bölümler için. 9:16: dikey sürüm de üretilir.">
            <select value={f.output_format} onChange={(e) => setF({ ...f, output_format: e.target.value })}><option value="16:9">16:9 (yatay)</option><option value="9:16">9:16 (dikey)</option></select></Field>
          <label className="flex items-start gap-3 pt-5 !text-[13px] !text-ink">
            <input type="checkbox" className="mt-0.5" checked={f.plan_approval} onChange={(e) => setF({ ...f, plan_approval: e.target.checked })} />
            <span><b>Üretimden önce planı onayla</b><br /><span className="text-mute">Ajan sahne listesi ve maliyet için tek onay bekler.</span></span>
          </label>
        </div>
      </Panel>

      <Panel title="Yönetmen ajanı (Claude)" right={<><ModeBadge mode={data.claude.mode} />{SaveBtn}</>}>
        <Field label="Model" hint={`Düşünme derinliği: ${data.claude.effort} (config/models.yaml)`} className="max-w-[520px]">
          <select value={f.claude_model} onChange={(e) => setF({ ...f, claude_model: e.target.value })}>
            {data.claude.models.map((m: any) => <option key={m.id} value={m.id}>{m.label} · ${m.input}/${m.output} / 1M token{m.id === data.claude.default ? ' (varsayılan)' : ''}</option>)}</select>
        </Field>
        {data.claude.mode === 'mock' && <p className="mt-3 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">Anthropic bağlantınız yok: demo modunda önceden yazılmış sahte yönetmen çalışır. Gerçek Claude için <Link to="/ayarlar/baglantilar" className="underline">Bağlantılar</Link>’dan anahtarınızı girin; yeni açılan sohbetler Claude ile çalışır.</p>}
        {data.claude.mode === 'none' && <p className="mt-3 rounded border border-bad/50 bg-bad/10 px-3 py-2 text-[13px] text-bad">Anthropic bağlantısı yok: sohbet başlatılamaz. <Link to="/ayarlar/baglantilar" className="underline">Bağlantılar</Link>’dan anahtarınızı girin.</p>}
      </Panel>

      <Panel title="Sağlayıcılar: model ve eşzamanlılık" right={SaveBtn}>
        <table className="w-full text-left text-[13px]">
          <thead className="text-[12px] text-mute"><tr><th className="pb-2 font-medium">Sağlayıcı</th><th className="w-36 font-medium">Şu anki mod</th><th className="w-[300px] font-medium">Model</th><th className="w-44 font-medium">Aynı anda en çok işim</th></tr></thead>
          <tbody>
            {data.providers.map((p: any) => (
              <tr key={p.key} className="border-t border-line">
                <td className="py-2">{p.label}{p.needs_remote_video && <div className="text-[11px] text-mute">Kaynak videonun uzak adresi yoksa “elle adım” olur</div>}</td>
                <td><ModeBadge mode={p.mode} /></td>
                <td className="pr-4"><select value={f.models[p.key]} onChange={(e) => setF({ ...f, models: { ...f.models, [p.key]: e.target.value } })}>
                  {p.models.map((m: any) => <option key={m.id} value={m.id}>{m.label}{m.id === p.default ? ' (varsayılan)' : ''}</option>)}</select></td>
                <td><input type="number" min={1} max={10} value={f.concurrency[p.key] ?? 1} onChange={(e) => setF({ ...f, concurrency: { ...f.concurrency, [p.key]: +e.target.value } })} className="!w-20" /></td>
              </tr>))}
            <tr className="border-t border-line"><td className="py-2">Kurgu ve kontrol (ffmpeg, sunucuda)</td><td><ModeBadge mode="local" /></td><td className="text-[12px] text-mute">{data.montage.width}×{data.montage.height} · {data.montage.fps} fps · {data.montage.loudness_lufs} LUFS</td>
              <td><input type="number" min={1} max={4} value={f.concurrency.local ?? 1} onChange={(e) => setF({ ...f, concurrency: { ...f.concurrency, local: +e.target.value } })} className="!w-20" /></td></tr>
          </tbody>
        </table>
        <p className="mt-3 text-[12px] text-mute">Eşzamanlılık sınırı size özeldir; sıra kullanıcılar arasında adil paylaştırılır ve sunucunun genel bir tavanı vardır. Model listesi ve fiyatlar <code>config/models.yaml</code> dosyasından gelir.</p>
        {!data.ffmpeg && <p className="mt-3 rounded border border-bad/50 bg-bad/10 px-3 py-2 text-[13px] text-bad">Sunucuda ffmpeg / ffprobe bulunamadı; kurgu, kontrol ve deneme modu buna bağlı.</p>}
      </Panel>

      <Panel title="Diller ve sağlayıcı desteği">
        <p className="mb-2 text-[12px] text-mute">Bir sağlayıcı seçilen dili desteklemiyorsa üretim başlamadan uyarılır; sessizce başka dile geçilmez. HeyGen ve Kling dudak senkronu ses dosyasından çalışır: API’de dil parametresi yoktur ve dil listesi belgelenmemiştir.</p>
        <table className="w-full text-left text-[13px]">
          <thead className="text-[12px] text-mute"><tr><th className="pb-1.5 font-medium">Dil</th><th className="w-16 font-medium">Kod</th><th className="w-24 font-medium">Yön</th><th className="font-medium">ElevenLabs modelleri</th><th className="w-48 font-medium">Altyazı yazı tipi</th></tr></thead>
          <tbody>{data.languages.map((l: any) => (
            <tr key={l.code} className="border-t border-line"><td className="py-1">{l.name}</td><td><Badge tone="accent">{l.code}</Badge></td><td className="text-mute">{l.rtl ? 'sağdan sola' : 'soldan sağa'}</td>
              <td>{Object.keys(MODEL_SHORT).map((m) => <span key={m} className="mr-1"><Badge tone={l.elevenlabs.includes(m) ? 'ok' : 'bad'} title={l.elevenlabs.includes(m) ? 'destekliyor' : 'desteklemiyor'}>{MODEL_SHORT[m]}{l.elevenlabs.includes(m) ? '' : ' ✕'}</Badge></span>)}</td>
              <td className="text-[12px] text-mute">{l.font}</td></tr>))}</tbody>
        </table>
      </Panel>
    </div>
  )
}

export function Costs() {
  const { data } = useLive<any>('/costs', (e) => e.type === 'job' && ['succeeded', 'failed'].includes(e.status))
  if (!data) return <Empty>Yükleniyor…</Empty>
  const t = data.total
  return (
    <div className="max-w-[1100px] space-y-5">
      <div><h1 className="text-xl font-semibold">Maliyet özeti</h1><p className="text-[13px] text-mute">Sağlayıcı tutarları <code>config/models.yaml</code>’daki liste fiyatlarıyla hesaplanan tahminlerdir; Claude maliyeti gerçek token kullanımından hesaplanır. Kesin tutar sağlayıcı faturasındadır.</p></div>
      <div className="grid grid-cols-4 gap-4">
        <Panel><div className="text-[12px] text-mute">Gerçek harcama (tahmini)</div><div className="mt-1 text-2xl font-semibold">{fmtUsd(t.real_usd)}</div></Panel>
        <Panel><div className="text-[12px] text-mute">Bunun Claude API kısmı</div><div className="mt-1 text-2xl font-semibold">{fmtUsd(t.claude_usd)}</div></Panel>
        <Panel><div className="text-[12px] text-mute">Süren işler (bekleyen)</div><div className="mt-1 text-2xl font-semibold">{fmtUsd(t.pending_usd)}</div></Panel>
        <Panel><div className="text-[12px] text-mute">Mock işlerin gerçek karşılığı</div><div className="mt-1 text-2xl font-semibold text-mute">{fmtUsd(t.mock_usd)}</div><div className="text-[11px] text-mute">Ücret kesilmedi.</div></Panel>
      </div>
      <Panel title="Sağlayıcıya göre">
        {data.by_provider.length === 0 ? <Empty>Henüz tamamlanmış iş yok.</Empty> : (
          <table className="w-full text-left text-[13px]"><thead className="text-[12px] text-mute"><tr><th className="pb-2 font-medium">Sağlayıcı</th><th className="font-medium">Mod</th><th className="font-medium">İş</th><th className="font-medium">Kullanım</th><th className="text-right font-medium">Tutar</th></tr></thead>
            <tbody>{data.by_provider.map((r: any, i: number) => <tr key={i} className="border-t border-line"><td className="py-1.5">{r.label}</td><td><ModeBadge mode={r.mode} /></td><td>{r.jobs}</td><td>{r.units} {r.unit}</td><td className={`text-right ${r.mode === 'mock' ? 'text-mute' : 'font-semibold'}`}>{fmtUsd(r.usd)}</td></tr>)}</tbody></table>)}
      </Panel>
      <Panel title="Proje, bölüm ve dile göre">
        {data.projects.length === 0 && <Empty>Proje yok.</Empty>}
        <div className="space-y-4">{data.projects.map((p: any) => (
          <div key={p.id}>
            <div className="flex items-center gap-3 border-b border-line pb-1.5"><Link to={`/proje/${p.id}`} className="font-semibold hover:text-info">{p.name}</Link><Badge>{p.cost.jobs} iş</Badge>
              <span className="ml-auto text-[13px]">Gerçek <b>{fmtUsd(p.cost.real_usd)}</b> <span className="text-mute">· mock karşılığı {fmtUsd(p.cost.mock_usd)}</span></span></div>
            {p.episodes.map((e: any) => (
              <div key={e.id} className="py-1.5 pl-4 text-[13px]">
                <div className="flex items-center gap-3"><Link to={`/bolum/${e.id}`} className="text-mute hover:text-info">Bölüm {e.number}: {e.title}</Link><span className="text-[12px] text-mute">{e.cost.jobs} iş</span>
                  <span className="ml-auto">Gerçek <b>{fmtUsd(e.cost.real_usd)}</b> <span className="text-mute">· mock karşılığı {fmtUsd(e.cost.mock_usd)}</span></span></div>
                <div className="mt-0.5 flex flex-wrap gap-x-5 pl-4 text-[12px] text-mute">{e.by_language.map((l: any) => <span key={l.language}>{l.label}: {fmtUsd(l.real_usd)}{l.mock_usd > 0 && ` (mock ${fmtUsd(l.mock_usd)})`}</span>)}</div>
              </div>))}
          </div>))}</div>
      </Panel>
    </div>
  )
}
