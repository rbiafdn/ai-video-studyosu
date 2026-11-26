import { createContext, FormEvent, ReactNode, useCallback, useContext, useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, authLost, closeLive, setCsrf } from './api'
import { Btn, Field } from './ui'

export type Me = {
  user: { id: number; email: string; role: string; setup_done: boolean; is_demo: boolean; email_verified: boolean }
  csrf: string; prefs: any; connections: any; director: 'real' | 'mock' | 'none'; demo_mode: boolean; unread: number
}
type Ctx = { me: Me | null; loading: boolean; reload: () => Promise<void>; accept: (m: Me) => void; logout: () => Promise<void> }
const AuthCtx = createContext<Ctx>({ me: null, loading: true, reload: async () => {}, accept: () => {}, logout: async () => {} })
export const useAuth = () => useContext(AuthCtx)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null)
  const [loading, setLoading] = useState(true)
  const accept = useCallback((m: Me) => { setCsrf(m.csrf); setMe(m) }, [])
  const reload = useCallback(async () => {
    try { accept(await api.get<Me>('/auth/me')) } catch { setCsrf(''); setMe(null) }
    setLoading(false)
  }, [accept])
  const logout = useCallback(async () => {
    try { await api.post('/auth/logout') } catch { /* oturum zaten düşmüş olabilir */ }
    closeLive(); setCsrf(''); setMe(null)
  }, [])
  useEffect(() => { reload() }, [reload])
  useEffect(() => { authLost.handler = () => { closeLive(); setCsrf(''); setMe(null) } }, [])
  return <AuthCtx.Provider value={{ me, loading, reload, accept, logout }}>{children}</AuthCtx.Provider>
}

function Card({ title, sub, children }: { title: string; sub?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-[400px]">
        <div className="mb-6 flex items-center justify-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-accent text-white">▶</div>
          <div className="leading-tight"><div className="text-[15px] font-semibold">AI Video</div><div className="text-[12px] text-mute">Prodüksiyon Stüdyosu</div></div>
        </div>
        <div className="rounded-xl border border-line bg-panel p-6">
          <h1 className="text-[17px] font-semibold">{title}</h1>
          {sub && <p className="mt-1 text-[13px] text-mute">{sub}</p>}
          <div className="mt-5">{children}</div>
        </div>
      </div>
    </div>
  )
}

const Err = ({ text }: { text: string }) => text ? <div role="alert" className="rounded border border-bad/50 bg-bad/10 px-3 py-2 text-[13px] text-[#ffc9ce]">{text}</div> : null
const Note = ({ children }: { children: ReactNode }) => <div className="rounded border border-ok/40 bg-ok/10 px-3 py-2 text-[13px] text-[#c5f5df]">{children}</div>

export function Login() {
  const { accept } = useAuth()
  const nav = useNavigate()
  const [state, setState] = useState<any>(null)
  const [f, setF] = useState({ email: '', password: '' })
  const [err, setErr] = useState('')
  useEffect(() => { api.get('/auth/state').then(setState).catch(() => {}) }, [])
  useEffect(() => { if (state?.first_run) nav('/kayit', { replace: true }) }, [state, nav])
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { accept(await api.post('/auth/login', f)); nav('/', { replace: true }) } catch (x: any) { setErr(x.message) }
  }
  return (
    <Card title="Giriş yap">
      <form onSubmit={submit} className="space-y-3">
        <Field label="E-posta"><input type="email" autoComplete="username" required autoFocus value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
        <Field label="Şifre"><input type="password" autoComplete="current-password" required value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
        <Err text={err} />
        <Btn kind="primary" type="submit" className="w-full justify-center">Giriş yap</Btn>
      </form>
      <div className="mt-4 flex justify-between text-[12px] text-mute">
        <Link to="/sifremi-unuttum" className="hover:text-ink">Şifremi unuttum</Link>
        {state?.registration === 'open' ? <Link to="/kayit" className="hover:text-ink">Hesap oluştur</Link> : <span>Kayıt yalnızca davetle</span>}
      </div>
    </Card>
  )
}

export function Register() {
  const { accept } = useAuth()
  const nav = useNavigate()
  const [q] = useSearchParams()
  const invite = q.get('token') || ''
  const [state, setState] = useState<any>(null)
  const [f, setF] = useState({ email: '', password: '', again: '' })
  const [err, setErr] = useState('')
  useEffect(() => { api.get('/auth/state').then(setState).catch(() => {}) }, [])
  const closed = state && !state.first_run && state.registration !== 'open' && !invite
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    if (f.password !== f.again) return setErr('Şifreler aynı değil')
    try { accept(await api.post('/auth/register', { email: f.email, password: f.password, invite: invite || null })); nav('/', { replace: true }) } catch (x: any) { setErr(x.message) }
  }
  return (
    <Card title={state?.first_run ? 'İlk kurulum: yönetici hesabı' : 'Hesap oluştur'}
      sub={state?.first_run ? 'Bu kurulumda henüz kullanıcı yok. İlk kayıt olan kişi yönetici olur.' : invite ? 'Davet bağlantısıyla kayıt oluyorsunuz.' : undefined}>
      {closed ? <><Err text="Kayıt yalnızca davetle yapılır. Yöneticinizden davet bağlantısı isteyin." /><Link to="/giris" className="mt-4 block text-[13px] text-info">Giriş ekranına dön</Link></> : (
        <form onSubmit={submit} className="space-y-3">
          <Field label="E-posta"><input type="email" autoComplete="username" required autoFocus value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
          <Field label="Şifre" hint="En az 10 karakter"><input type="password" autoComplete="new-password" required minLength={10} value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
          <Field label="Şifre (yeniden)"><input type="password" autoComplete="new-password" required value={f.again} onChange={(e) => setF({ ...f, again: e.target.value })} /></Field>
          <Err text={err} />
          <Btn kind="primary" type="submit" className="w-full justify-center">Kayıt ol</Btn>
          {!state?.first_run && <Link to="/giris" className="block text-center text-[12px] text-mute hover:text-ink">Zaten hesabım var</Link>}
        </form>)}
    </Card>
  )
}

export function Forgot() {
  const [email, setEmail] = useState('')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { setMsg((await api.post('/auth/forgot', { email })).message) } catch (x: any) { setErr(x.message) }
  }
  return (
    <Card title="Şifremi unuttum" sub="E-posta gönderimi ayarlıysa sıfırlama bağlantısı gelir; değilse yöneticiniz size tek kullanımlık bir bağlantı üretir.">
      {msg ? <Note>{msg}</Note> : (
        <form onSubmit={submit} className="space-y-3">
          <Field label="E-posta"><input type="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
          <Err text={err} />
          <Btn kind="primary" type="submit" className="w-full justify-center">Bağlantı iste</Btn>
        </form>)}
      <Link to="/giris" className="mt-4 block text-center text-[12px] text-mute hover:text-ink">Giriş ekranına dön</Link>
    </Card>
  )
}

export function Reset() {
  const [q] = useSearchParams()
  const token = q.get('token') || ''
  const [f, setF] = useState({ password: '', again: '' })
  const [done, setDone] = useState(false)
  const [err, setErr] = useState(token ? '' : 'Bağlantıda belirteç yok')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    if (f.password !== f.again) return setErr('Şifreler aynı değil')
    try { await api.post('/auth/reset', { token, password: f.password }); setDone(true) } catch (x: any) { setErr(x.message) }
  }
  return (
    <Card title="Yeni şifre belirle" sub="Bağlantı tek kullanımlıktır. Şifre değişince açık bütün oturumlar kapanır.">
      {done ? <><Note>Şifreniz değişti.</Note><Link to="/giris" className="mt-4 block text-center text-[13px] text-info">Giriş yap</Link></> : (
        <form onSubmit={submit} className="space-y-3">
          <Field label="Yeni şifre" hint="En az 10 karakter"><input type="password" autoComplete="new-password" required minLength={10} autoFocus value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
          <Field label="Yeni şifre (yeniden)"><input type="password" autoComplete="new-password" required value={f.again} onChange={(e) => setF({ ...f, again: e.target.value })} /></Field>
          <Err text={err} />
          <Btn kind="primary" type="submit" className="w-full justify-center" disabled={!token}>Şifreyi değiştir</Btn>
        </form>)}
    </Card>
  )
}

export function Verify() {
  const [q] = useSearchParams()
  const [state, setState] = useState<'wait' | 'ok' | 'bad'>('wait')
  useEffect(() => { api.post('/auth/verify', { token: q.get('token') || '' }).then(() => setState('ok')).catch(() => setState('bad')) }, [q])
  return (
    <Card title="E-posta doğrulama">
      {state === 'wait' ? <p className="text-[13px] text-mute">Doğrulanıyor…</p> : state === 'ok' ? <Note>E-posta adresiniz doğrulandı.</Note> : <Err text="Bağlantı geçersiz ya da süresi dolmuş." />}
      <Link to="/" className="mt-4 block text-center text-[13px] text-info">Uygulamaya dön</Link>
    </Card>
  )
}
