import { useEffect, useState } from 'react'
import { api, fmtUsd } from '../api'
import { useAuth } from '../auth'
import { Badge, Btn, cx, Field, useAction } from '../ui'
import { ConnectionCard, MODE_TEXT } from './Connections'

const STEPS = ['Anthropic', 'ElevenLabs', 'Kling', 'HeyGen', 'Tercihler', 'Özet']
const SERVICES = ['anthropic', 'elevenlabs', 'kling', 'heygen']

/** İlk girişten sonra zorunlu kurulum sihirbazı. Sonradan Ayarlar > Bağlantılar'dan değiştirilebilir. */
export default function Wizard() {
  const { me, reload, logout } = useAuth()
  const act = useAction()
  const [step, setStep] = useState(0)
  const [conns, setConns] = useState<any[] | null>(null)
  const [cfg, setCfg] = useState<any>(null)
  const [prefs, setPrefs] = useState<any>(null)
  const load = async () => {
    const [c, g] = await Promise.all([api.get('/connections'), api.get('/config')])
    setConns(c.connections); setCfg(g); setPrefs((p: any) => p || g.prefs)
  }
  useEffect(() => { load().catch(() => {}) }, [])
  if (!conns || !cfg || !prefs || !me) return <div className="p-10 text-center text-mute">Yükleniyor…</div>

  const of = (s: string) => conns.find((c) => c.service === s)
  const anth = of('anthropic')
  const anthOk = (anth.set && anth.status === 'connected') || anth.mode === 'shared'
  const canChat = anthOk || me.demo_mode
  const isAdmin = me.user.role === 'admin'
  const finish = () => act(async () => { await api.put('/prefs', prefs); await api.post('/setup/complete'); await reload() }, 'Kurulum tamamlandı')
  const demoAll = () => act(async () => {
    if (!me.demo_mode && isAdmin) await api.put('/admin/settings', { demo_mode: true })
    await api.put('/prefs', prefs); await api.post('/setup/complete'); await reload()
  }, 'Deneme modunda devam ediliyor')

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[760px] px-6 py-8">
        <div className="flex items-center justify-between">
          <div><h1 className="text-xl font-semibold">Kurulum</h1><p className="text-[13px] text-mute">{me.user.email} · Servis hesaplarınızı bağlayın. Anahtarlar sunucuda şifreli saklanır; kaydedildikten sonra yalnızca son 4 karakteri görünür.</p></div>
          <Btn small onClick={logout}>Çıkış</Btn>
        </div>
        <ol className="mt-5 flex gap-1.5">
          {STEPS.map((s, i) => {
            const c = i < 4 ? of(SERVICES[i]) : null
            return (
              <li key={s} className="flex-1">
                <button onClick={() => (i === 0 || canChat) && setStep(i)} className={cx('w-full rounded-md border px-2 py-1.5 text-left text-[12px]',
                  i === step ? 'border-accent bg-accent/15 text-white' : 'border-line text-mute hover:text-ink')}>
                  <div className="text-[11px] opacity-70">{i + 1}. adım</div>
                  <div className="flex items-center gap-1.5 font-medium">{s}{c && <span className={cx('h-1.5 w-1.5 rounded-full', c.set && c.status === 'connected' ? 'bg-ok' : c.set && c.status === 'error' ? 'bg-bad' : 'bg-warn')} />}</div>
                </button>
              </li>)
          })}
        </ol>

        <div className="mt-5">
          {step < 4 && (
            <>
              <ConnectionCard c={of(SERVICES[step])} onChange={() => load()} />
              {step === 0 && !anthOk && (
                <div className="mt-3 rounded-lg border border-line bg-panel2 p-3 text-[13px]">
                  {me.demo_mode ? <>Demo modu açık: Anthropic anahtarı olmadan da <b>sahte yönetmenle</b> devam edebilirsiniz (akışı denemek için; gerçek Claude çalışmaz).</>
                    : isAdmin ? <>Anahtarınız yoksa demo modunu açıp sahte yönetmenle deneyebilirsiniz. Demo modu bu kurulumdaki bütün anahtarsız kullanıcılar için geçerli olur.</>
                      : <>Sohbetteki yönetmen ajanı için Anthropic anahtarı zorunludur. Anahtarınız yoksa yöneticinizden ortak anahtar ya da demo modu isteyin.</>}
                  {(me.demo_mode || isAdmin) && <div className="mt-2"><Btn onClick={demoAll}>Deneme modunda devam et</Btn></div>}
                </div>)}
              {step === 0 && anthOk && (
                <div className="mt-3 flex items-center justify-between rounded-lg border border-line bg-panel2 p-3 text-[13px]">
                  <span>Diğer servisleri şimdi bağlamak istemezseniz hepsi deneme modunda (sahte çıktıyla) çalışır.</span>
                  <Btn onClick={() => setStep(4)}>Deneme modunda devam et</Btn>
                </div>)}
            </>)}

          {step === 4 && (
            <div className="rounded-xl border border-line bg-panel p-4">
              <div className="text-[14px] font-semibold">Tercihler</div>
              <div className="mt-3 grid grid-cols-2 gap-4">
                <Field label="Varsayılan proje dili">
                  <select value={prefs.default_language} onChange={(e) => setPrefs({ ...prefs, default_language: e.target.value })}>
                    {cfg.languages.map((l: any) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}</select></Field>
                <Field label="Çalışma başına bütçe sınırı ($)" hint="Sınıra gelince ajan durur ve sohbette sorar. 0 = sınırsız.">
                  <input type="number" min={0} step={1} value={prefs.budget_usd} onChange={(e) => setPrefs({ ...prefs, budget_usd: +e.target.value })} /></Field>
                <Field label="Varsayılan çıktı biçimi" hint="9:16 seçilirse yatay kurgunun yanında dikey sürüm de üretilir.">
                  <select value={prefs.output_format} onChange={(e) => setPrefs({ ...prefs, output_format: e.target.value })}>
                    <option value="16:9">16:9 (yatay)</option><option value="9:16">9:16 (dikey)</option></select></Field>
                <label className="flex items-start gap-3 pt-5 !text-[13px] !text-ink">
                  <input type="checkbox" className="mt-0.5" checked={prefs.plan_approval} onChange={(e) => setPrefs({ ...prefs, plan_approval: e.target.checked })} />
                  <span><b>Üretimden önce planı onayla</b><br /><span className="text-mute">Ajan sahne listesini ve tahmini maliyeti gösterip tek onay bekler.</span></span>
                </label>
              </div>
            </div>)}

          {step === 5 && (
            <div className="rounded-xl border border-line bg-panel p-4">
              <div className="text-[14px] font-semibold">Özet</div>
              <table className="mt-3 w-full text-left text-[13px]">
                <tbody>{conns.map((c) => {
                  const bad = c.set && c.status === 'error'
                  const [tone, label] = bad ? ['bad', 'Hatalı'] : c.service === 'anthropic' && c.mode === 'demo' ? ['warn', 'Sahte yönetmen (demo)'] : c.mode === 'demo' ? ['warn', 'Atlandı · deneme modu'] : MODE_TEXT[c.mode]
                  return (
                    <tr key={c.service} className="border-t border-line first:border-0">
                      <td className="py-2 font-medium">{c.label}</td>
                      <td className="text-mute">{c.steps.length ? `${c.steps.join(', ')}. adım` : 'Yönetmen ajanı ve çeviri'}</td>
                      <td>{c.info?.credit && c.status === 'connected' ? <span className="text-[12px] text-mute">{c.info.credit}</span> : null}</td>
                      <td className="text-right"><Badge tone={tone}>{label}</Badge></td>
                    </tr>)
                })}</tbody>
              </table>
              <div className="mt-3 border-t border-line pt-3 text-[13px] text-mute">
                Dil: <b className="text-ink">{cfg.languages.find((l: any) => l.code === prefs.default_language)?.name}</b> · Bütçe: <b className="text-ink">{prefs.budget_usd ? fmtUsd(prefs.budget_usd) : 'sınırsız'}</b> ·
                Plan onayı: <b className="text-ink">{prefs.plan_approval ? 'açık' : 'kapalı'}</b> · Biçim: <b className="text-ink">{prefs.output_format}</b>
              </div>
              <p className="mt-2 text-[12px] text-mute">Deneme modundaki adımlar gerçek servise gitmez; ffmpeg ile sahte ses/görsel/video üretilir ve ücret kesilmez. Bağlantıları sonradan <b>Ayarlar → Bağlantılar</b>’dan ekleyebilirsiniz.</p>
            </div>)}
        </div>

        <div className="mt-5 flex items-center justify-between">
          <Btn disabled={step === 0} onClick={() => setStep(step - 1)}>← Geri</Btn>
          <div className="flex gap-2">
            {step > 0 && step < 4 && !of(SERVICES[step]).set && <Btn onClick={() => setStep(step + 1)}>Atla (deneme modu)</Btn>}
            {step < 5 && <Btn kind="primary" disabled={step === 0 && !canChat} onClick={() => setStep(step + 1)}>İleri →</Btn>}
            {step === 5 && <Btn kind="primary" disabled={!canChat} onClick={finish}>Kurulumu bitir</Btn>}
          </div>
        </div>
      </div>
    </div>
  )
}
