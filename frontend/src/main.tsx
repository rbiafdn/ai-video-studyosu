import { StrictMode, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes } from 'react-router-dom'
import { api, live, useEvents, useLive } from './api'
import { AuthProvider, Forgot, Login, Register, Reset, useAuth, Verify } from './auth'
import './index.css'
import Admin from './pages/Admin'
import Chat from './pages/Chat'
import Episode from './pages/Episode'
import Jobs from './pages/Jobs'
import { Project, Projects } from './pages/Projects'
import { Costs, Settings } from './pages/Settings'
import Wizard from './pages/Wizard'
import { cx, ToastHost, useToast } from './ui'

function Nav({ to, children, badge, match }: { to: string; children: string; badge?: number; match?: string }) {
  return (
    <NavLink to={to} end={to === '/'}
      className={({ isActive }) => cx('flex items-center justify-between rounded-md px-3 py-2 text-[13px] font-medium',
        isActive || (match && location.pathname.startsWith(match)) ? 'bg-accent/20 text-white' : 'text-mute hover:bg-white/5 hover:text-ink')}>
      {children}
      {!!badge && <span className="rounded-full bg-accent px-1.5 text-[11px] text-white">{badge}</span>}
    </NavLink>
  )
}

function Page({ children }: { children: React.ReactNode }) {
  return <main className="min-w-0 flex-1 overflow-y-auto"><div className="mx-auto max-w-[1500px] px-6 py-5">{children}</div></main>
}

const DOT: Record<string, string> = { connected: 'bg-ok', shared: 'bg-info', demo: 'bg-warn', error: 'bg-bad' }
const DOT_TEXT: Record<string, string> = { connected: 'bağlı', shared: 'ortak anahtar', demo: 'deneme modu', error: 'hatalı: anahtarı yenileyin' }

/** Üst çubuk: bağlantı durumu göstergesi, bildirimler, kullanıcı menüsü. */
function TopBar({ connected }: { connected: boolean }) {
  const { me, reload, logout } = useAuth()
  const toast = useToast()
  const [open, setOpen] = useState<'' | 'bell' | 'user'>('')
  const [notes, setNotes] = useState<any[]>([])
  const box = useRef<HTMLDivElement>(null)
  useEvents((e) => {
    if (e.type === 'notification') { toast(e.text, e.level === 'error' ? 'bad' : e.level === 'ok' ? 'ok' : 'info'); reload() }
    if (e.type === 'refresh' || (e.type === 'job' && e.status === 'blocked')) reload()
  })
  useEffect(() => {
    const h = (ev: MouseEvent) => { if (box.current && !box.current.contains(ev.target as Node)) setOpen('') }
    window.addEventListener('mousedown', h)
    return () => window.removeEventListener('mousedown', h)
  }, [])
  if (!me) return null
  const services: any[] = me.connections.services
  const openBell = async () => {
    if (open === 'bell') return setOpen('')
    setOpen('bell')
    try { setNotes(await api.get('/notifications')); if (me.unread) { await api.post('/notifications/read'); reload() } } catch { /* yoksay */ }
  }
  return (
    <header className="flex h-11 shrink-0 items-center justify-between border-b border-line bg-panel px-4">
      <Link to="/ayarlar/baglantilar" className="flex items-center gap-3 text-[12px] text-mute hover:text-ink" title="Bağlantıları yönet">
        {services.map((s) => {
          const k = s.status === 'error' ? 'error' : s.mode
          return <span key={s.service} className="flex items-center gap-1.5" title={`${s.label}: ${DOT_TEXT[k]}`}><span className={cx('h-2 w-2 rounded-full', DOT[k])} />{s.label.replace(' (Claude)', '')}</span>
        })}
        {me.connections.demo_steps.length > 0 && <span className="rounded border border-warn/40 px-1.5 py-0.5 text-[11px] text-warn">deneme modu: {me.connections.demo_steps.join(', ')}. adım</span>}
        {me.director === 'mock' && <span className="rounded border border-warn/40 px-1.5 py-0.5 text-[11px] text-warn">sahte yönetmen</span>}
      </Link>
      <div ref={box} className="relative flex items-center gap-3 text-[12px]">
        <span className="flex items-center gap-1.5 text-mute"><span className={cx('h-2 w-2 rounded-full', connected ? 'bg-ok' : 'bg-bad')} />{connected ? 'Canlı' : 'Bağlantı yok'}</span>
        <button onClick={openBell} aria-label="Bildirimler" className="relative rounded px-2 py-1 text-[15px] text-mute hover:bg-white/10 hover:text-ink">
          🔔{me.unread > 0 && <span className="absolute -right-0.5 -top-0.5 rounded-full bg-bad px-1 text-[10px] font-semibold text-white">{me.unread}</span>}
        </button>
        <button onClick={() => setOpen(open === 'user' ? '' : 'user')} className="rounded px-2 py-1 text-mute hover:bg-white/10 hover:text-ink">{me.user.email} ▾</button>
        {open === 'bell' && (
          <div className="absolute right-0 top-9 z-30 max-h-[420px] w-[380px] overflow-y-auto rounded-lg border border-line bg-panel2 p-2 shadow-2xl">
            {notes.length === 0 ? <div className="p-3 text-center text-mute">Bildirim yok.</div> : notes.map((n) => (
              <Link key={n.id} to={n.link || '#'} onClick={() => setOpen('')} className={cx('block rounded px-2.5 py-2 text-[12px] hover:bg-white/5', !n.read && 'bg-accent/10')}>
                <div className={cx(n.level === 'error' ? 'text-[#ffc9ce]' : n.level === 'ok' ? 'text-[#c5f5df]' : 'text-ink')}>{n.text}</div>
                <div className="mt-0.5 text-[11px] text-mute">{new Date(n.created_at).toLocaleString('tr-TR')}</div>
              </Link>))}
          </div>)}
        {open === 'user' && (
          <div className="absolute right-0 top-9 z-30 w-[200px] rounded-lg border border-line bg-panel2 p-1.5 shadow-2xl" onClick={() => setOpen('')}>
            <Link to="/ayarlar/hesap" className="block rounded px-2.5 py-1.5 hover:bg-white/5">Hesap</Link>
            <Link to="/ayarlar/baglantilar" className="block rounded px-2.5 py-1.5 hover:bg-white/5">Bağlantılar</Link>
            <Link to="/ayarlar/tercihler" className="block rounded px-2.5 py-1.5 hover:bg-white/5">Tercihler</Link>
            {me.user.role === 'admin' && <Link to="/yonetim" className="block rounded px-2.5 py-1.5 hover:bg-white/5">Yönetici paneli</Link>}
            <button onClick={logout} className="block w-full rounded px-2.5 py-1.5 text-left text-bad hover:bg-white/5">Çıkış yap</button>
          </div>)}
      </div>
    </header>
  )
}

function Shell() {
  const { me } = useAuth()
  const jobs = useLive<any>('/jobs?status=active&limit=1', (e) => e.type === 'job')
  const [connected, setConnected] = useState(live.connected)
  useEvents((e) => { if (e.type === 'open') setConnected(true); if (e.type === 'closed') setConnected(false) })
  const c = jobs.data?.counts || {}
  const active = (c.queued || 0) + (c.submitting || 0) + (c.running || 0) + (c.waiting_manual || 0) + (c.blocked || 0)
  const chat = <main className="min-w-0 flex-1 overflow-hidden"><Chat /></main>
  return (
    <div className="flex h-full">
      <aside className="flex w-[190px] shrink-0 flex-col border-r border-line bg-panel px-3 py-4">
        <div className="mb-5 flex items-center gap-2 px-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-[13px] text-white">▶</div>
          <div className="leading-tight"><div className="text-[13px] font-semibold">AI Video</div><div className="text-[11px] text-mute">Prodüksiyon Stüdyosu</div></div>
        </div>
        <nav className="flex flex-col gap-1">
          <Nav to="/" match="/sohbet">Sohbet</Nav>
          <Nav to="/projeler" match="/proje">Projeler</Nav>
          <Nav to="/isler" badge={active}>İşler</Nav>
          <Nav to="/maliyet">Maliyet</Nav>
          <Nav to="/ayarlar/baglantilar" match="/ayarlar">Ayarlar</Nav>
          {me?.user.role === 'admin' && <Nav to="/yonetim">Yönetim</Nav>}
        </nav>
        {(c.blocked || 0) > 0 && (
          <Link to="/ayarlar/baglantilar" className="mt-auto rounded-md border border-bad/50 bg-bad/10 px-2 py-1.5 text-[12px] text-[#ffc9ce]">
            {c.blocked} iş bağlantı bekliyor: anahtarı yenileyin →</Link>)}
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar connected={connected} />
        <div className="flex min-h-0 flex-1">
          <Routes>
            {/* açılış ekranı sohbet: tam genişlik */}
            <Route path="/" element={chat} />
            <Route path="/sohbet/:id" element={chat} />
            <Route path="/projeler" element={<Page><Projects /></Page>} />
            <Route path="/proje/:id" element={<Page><Project /></Page>} />
            <Route path="/bolum/:id" element={<Page><Episode /></Page>} />
            <Route path="/bolum/:id/sahne/:sid" element={<Page><Episode /></Page>} />
            <Route path="/isler" element={<Page><Jobs /></Page>} />
            <Route path="/maliyet" element={<Page><Costs /></Page>} />
            <Route path="/ayarlar" element={<Navigate to="/ayarlar/baglantilar" replace />} />
            <Route path="/ayarlar/:tab" element={<Page><Settings /></Page>} />
            {me?.user.role === 'admin' && <Route path="/yonetim" element={<Page><Admin /></Page>} />}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </div>
    </div>
  )
}

/** Oturum kapısı: giriş yoksa giriş ekranları, kurulum bitmediyse sihirbaz, sonra uygulama. */
function Gate() {
  const { me, loading } = useAuth()
  if (loading) return <div className="flex h-full items-center justify-center text-mute">Yükleniyor…</div>
  if (!me) {
    return (
      <Routes>
        <Route path="/giris" element={<Login />} />
        <Route path="/kayit" element={<Register />} />
        <Route path="/sifremi-unuttum" element={<Forgot />} />
        <Route path="/sifre-sifirla" element={<Reset />} />
        <Route path="/dogrula" element={<Verify />} />
        <Route path="*" element={<Navigate to="/giris" replace />} />
      </Routes>
    )
  }
  return (
    <Routes>
      <Route path="/dogrula" element={<Verify />} />
      <Route path="/sifre-sifirla" element={<Reset />} />
      <Route path="*" element={me.user.setup_done ? <Shell /> : <Wizard />} />
    </Routes>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <ToastHost><AuthProvider><Gate /></AuthProvider></ToastHost>
    </BrowserRouter>
  </StrictMode>,
)
