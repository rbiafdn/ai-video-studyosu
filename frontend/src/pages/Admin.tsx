import { useState } from 'react'
import { api, fmtUsd, useLive } from '../api'
import { useAuth } from '../auth'
import { Badge, Btn, Empty, Field, Panel, useAction, useToast } from '../ui'
import { ConnectionCard } from './Connections'

const Yes = ({ ok, yes = 'Tanımlı', no = 'Yok' }: { ok: boolean; yes?: string; no?: string }) => <Badge tone={ok ? 'ok' : 'mute'}>{ok ? yes : no}</Badge>

function CopyLink({ url, label }: { url: string; label: string }) {
  const toast = useToast()
  return (
    <div className="mt-2 rounded-lg border border-accent/40 bg-accent/10 p-3 text-[12px]">
      <div className="mb-1 text-mute">{label} (tek kullanımlık; yalnızca şimdi gösterilir)</div>
      <div className="flex items-center gap-2"><input readOnly value={url} onFocus={(e) => e.target.select()} className="!text-[12px]" />
        <Btn small onClick={async () => { try { await navigator.clipboard.writeText(url); toast('Kopyalandı', 'ok') } catch { toast('Kopyalanamadı; elle seçip kopyalayın', 'bad') } }}>Kopyala</Btn></div>
    </div>
  )
}

export default function Admin() {
  const { me, reload: reloadMe } = useAuth()
  const { data, reload } = useLive<any>('/admin/overview', (e) => e.type === 'refresh')
  const audit = useLive<any[]>('/admin/audit?limit=150', (e) => e.type === 'refresh')
  const act = useAction()
  const [invite, setInvite] = useState('')
  const [link, setLink] = useState<{ url: string; label: string } | null>(null)
  const [limits, setLimits] = useState<Record<number, string>>({})
  if (!data) return <Empty>Yükleniyor…</Empty>
  const st = data.settings, sys = data.system
  const setSys = (patch: any, ok = 'Kaydedildi') => act(async () => { await api.put('/admin/settings', patch); reload(); reloadMe() }, ok)
  const patchUser = (id: number, patch: any, ok: string) => act(async () => { await api.patch(`/admin/users/${id}`, patch); reload() }, ok)
  return (
    <div className="max-w-[1200px] space-y-5">
      <div><h1 className="text-xl font-semibold">Yönetici paneli</h1><p className="text-[13px] text-mute">Kullanıcıların anahtarlarını göremezsiniz; yalnızca hangi servisin bağlı olduğunu ve kullanım tutarlarını görürsünüz.</p></div>

      <div className="grid grid-cols-4 gap-4">
        <Panel><div className="text-[12px] text-mute">Kullanıcı</div><div className="mt-1 text-2xl font-semibold">{data.users.length}</div></Panel>
        <Panel><div className="text-[12px] text-mute">Toplam gerçek harcama (tahmini)</div><div className="mt-1 text-2xl font-semibold">{fmtUsd(data.total.real_usd)}</div></Panel>
        <Panel><div className="text-[12px] text-mute">Bunun Claude kısmı</div><div className="mt-1 text-2xl font-semibold">{fmtUsd(data.total.claude_usd)}</div></Panel>
        <Panel><div className="text-[12px] text-mute">Tamamlanan iş</div><div className="mt-1 text-2xl font-semibold">{data.total.jobs}</div></Panel>
      </div>

      <Panel title="Kullanıcılar ve davetler">
        <table className="w-full text-left text-[13px]">
          <thead className="text-[12px] text-mute"><tr><th className="pb-2 font-medium">E-posta</th><th className="font-medium">Rol</th><th className="font-medium">Durum</th><th className="font-medium">Bağlı servisler</th><th className="font-medium">Gerçek harcama</th><th className="font-medium">Ortak anahtar (bu ay / sınır $)</th><th className="text-right font-medium">İşlemler</th></tr></thead>
          <tbody>{data.users.map((u: any) => (
            <tr key={u.id} className="border-t border-line align-middle">
              <td className="py-2">{u.email}{u.is_demo && <Badge tone="warn">demo</Badge>}<div className="text-[11px] text-mute">{u.usage.projects} proje · son giriş {u.last_login_at ? new Date(u.last_login_at).toLocaleDateString('tr-TR') : 'yok'}</div></td>
              <td>{u.role === 'admin' ? <Badge tone="accent">Yönetici</Badge> : 'Kullanıcı'}</td>
              <td><Badge tone={u.status === 'active' ? 'ok' : 'bad'}>{u.status === 'active' ? 'Etkin' : 'Devre dışı'}</Badge></td>
              <td className="text-[12px] text-mute">{u.connected.length ? u.connected.join(', ') : 'yok (deneme modu)'}</td>
              <td>{fmtUsd(u.usage.real_usd)}</td>
              <td><div className="flex items-center gap-1.5"><span className="w-14">{fmtUsd(u.usage.shared_month_usd)}</span>/
                <input type="number" min={0} className="!w-20 !py-1" value={limits[u.id] ?? u.usage.shared_limit_usd} onChange={(e) => setLimits({ ...limits, [u.id]: e.target.value })} />
                {limits[u.id] !== undefined && <Btn small onClick={() => patchUser(u.id, { shared_monthly_usd: +limits[u.id] }, 'Sınır kaydedildi')}>Kaydet</Btn>}</div></td>
              <td><div className="flex justify-end gap-1.5">
                <Btn small onClick={() => act(async () => setLink({ url: (await api.post(`/admin/users/${u.id}/reset-link`)).url, label: `${u.email} için şifre sıfırlama bağlantısı` }))}>Sıfırlama bağlantısı</Btn>
                {u.id !== me?.user.id && <Btn small onClick={() => patchUser(u.id, { role: u.role === 'admin' ? 'user' : 'admin' }, 'Rol değişti')}>{u.role === 'admin' ? 'Yöneticiliği al' : 'Yönetici yap'}</Btn>}
                {u.id !== me?.user.id && <Btn small kind={u.status === 'active' ? 'bad' : 'ok'} onClick={() => patchUser(u.id, { status: u.status === 'active' ? 'disabled' : 'active' }, 'Durum değişti')}>{u.status === 'active' ? 'Devre dışı bırak' : 'Etkinleştir'}</Btn>}
              </div></td>
            </tr>))}</tbody>
        </table>
        {link && <CopyLink {...link} />}
        <div className="mt-4 flex items-end gap-3 border-t border-line pt-4">
          <Field label="Davet edilecek e-posta" hint="Boş bırakılırsa herhangi bir e-postayla kullanılabilen tek kullanımlık bağlantı üretilir. 7 gün geçerlidir." className="w-[340px]">
            <input type="email" value={invite} onChange={(e) => setInvite(e.target.value)} placeholder="kisi@ornek.com" /></Field>
          <Btn kind="primary" onClick={() => act(async () => { const r = await api.post('/admin/invites', { email: invite }); setLink({ url: r.url, label: r.emailed ? 'Davet e-postayla gönderildi; bağlantı' : 'Davet bağlantısı' }); setInvite(''); reload() })}>Davet oluştur</Btn>
          <div className="ml-auto flex items-center gap-2 text-[13px]"><span className="text-mute">Kayıt:</span>
            <select className="!w-auto" value={st.registration_mode} onChange={(e) => setSys({ registration_mode: e.target.value }, 'Kayıt modu değişti')}>
              <option value="invite">Yalnızca davetle</option><option value="open">Herkese açık</option></select></div>
        </div>
        {data.invites.length > 0 && (
          <div className="mt-3 text-[12px] text-mute">Bekleyen davetler: {data.invites.map((i: any) => (
            <span key={i.id} className="mr-3">{i.email || 'herkese açık bağlantı'} <button className="text-bad hover:underline" onClick={() => act(async () => { await api.del(`/admin/invites/${i.id}`); reload() }, 'Davet iptal edildi')}>iptal</button></span>))}</div>)}
      </Panel>

      <Panel title="Sistem durumu (.env)">
        <div className="grid grid-cols-4 gap-x-6 gap-y-2 text-[13px]">
          <div className="flex justify-between">APP_SECRET <Yes ok={sys.app_secret} /></div>
          <div className="flex justify-between">ENCRYPTION_KEY <Yes ok={sys.encryption_key} /></div>
          <div className="flex justify-between">Veritabanı <Badge tone="info">{sys.database}</Badge></div>
          <div className="flex justify-between">Dosya deposu <Badge tone="info">{sys.storage}</Badge></div>
          <div className="flex justify-between">HTTPS (PUBLIC_BASE_URL) <Yes ok={sys.https} yes="Açık" no="Yerel / kapalı" /></div>
          <div className="flex justify-between">Güvenli çerez <Yes ok={sys.secure_cookies} yes="Açık" no="Kapalı" /></div>
          <div className="flex justify-between">Webhook <Yes ok={sys.webhooks} yes="Açık" no="Yoklama (polling)" /></div>
          <div className="flex justify-between">E-posta (SMTP) <Yes ok={sys.smtp} yes="Ayarlı" no="Yok: bağlantı elden" /></div>
        </div>
        <p className="mt-3 text-[12px] text-mute">Bu değerler sunucudaki <code>.env</code> dosyasından okunur ve buradan değiştirilemez; yalnızca durumları gösterilir.{sys.public_base_url && <> Adres: {sys.public_base_url}</>}</p>
        <div className="mt-4 grid grid-cols-2 gap-4 border-t border-line pt-4">
          <label className="flex items-start gap-3 !text-[13px] !text-ink">
            <input type="checkbox" className="mt-0.5" checked={st.demo_mode} onChange={(e) => setSys({ demo_mode: e.target.checked }, 'Demo modu değişti')} />
            <span><b>Demo modu</b><br /><span className="text-mute">Anthropic anahtarı olmayan kullanıcılar sahte yönetmenle sohbeti deneyebilir. Kapalıyken anahtar zorunludur.</span></span>
          </label>
          <label className="flex items-start gap-3 !text-[13px] !text-ink">
            <input type="checkbox" className="mt-0.5" checked={st.mock_mode} onChange={(e) => setSys({ mock_mode: e.target.checked }, 'Bakım modu değişti')} />
            <span><b>Bakım modu (her şey deneme)</b><br /><span className="text-mute">Açıkken anahtar olsa bile hiçbir servise istek gitmez; herkes için sahte üretim yapılır.</span></span>
          </label>
        </div>
      </Panel>

      <Panel title="Ortak (kurum) anahtarları">
        <p className="mb-3 text-[13px] text-mute">Kendi anahtarını girmeyen kullanıcılar bu anahtarları kullanır. Kullanım kullanıcı bazında izlenir; aylık sınırı aşan kullanıcının yeni işleri başlamaz. Kullanıcılar ortak anahtarın hiçbir karakterini göremez.</p>
        <div className="mb-4 flex max-w-[420px] items-end gap-3">
          <Field label="Varsayılan aylık sınır (kullanıcı başına, $)" className="flex-1"><input type="number" min={0} defaultValue={st.shared_monthly_usd} id="sharedLimit" /></Field>
          <Btn onClick={() => setSys({ shared_monthly_usd: +(document.getElementById('sharedLimit') as HTMLInputElement).value })}>Kaydet</Btn>
        </div>
        <div className="grid grid-cols-2 gap-4">{data.shared.map((c: any) => <ConnectionCard key={c.service} c={c} base="/admin/connections" shared onChange={() => reload()} />)}</div>
      </Panel>

      <Panel title="Denetim günlüğü">
        {!audit.data?.length ? <Empty>Kayıt yok.</Empty> : (
          <div className="max-h-[360px] overflow-y-auto">
            <table className="w-full text-left text-[12px]">
              <thead className="text-mute"><tr><th className="pb-1.5 font-medium">Zaman</th><th className="font-medium">Kullanıcı</th><th className="font-medium">İşlem</th><th className="font-medium">Ayrıntı</th><th className="font-medium">IP</th></tr></thead>
              <tbody>{audit.data.map((a: any) => (
                <tr key={a.id} className="border-t border-line"><td className="py-1 text-mute">{new Date(a.created_at).toLocaleString('tr-TR')}</td><td>{a.user}</td><td><Badge>{a.action}</Badge></td><td className="text-mute">{a.detail}</td><td className="text-mute">{a.ip}</td></tr>))}</tbody>
            </table>
          </div>)}
      </Panel>
    </div>
  )
}
