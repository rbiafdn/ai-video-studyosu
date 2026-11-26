import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, fmtTime, fmtUsd, useLive } from '../api'
import { Badge, Btn, cx, Empty, ManualStep, ModeBadge, Panel, Progress, StatusBadge, useAction } from '../ui'

const FILTERS: [string, string][] = [['', 'Tümü'], ['active', 'Süren'], ['waiting_manual', 'Elle adım'], ['blocked', 'Bağlantı bekleyen'], ['failed', 'Hatalı'], ['succeeded', 'Tamamlanan']]

export default function Jobs() {
  const [filter, setFilter] = useState('')
  const { data, error } = useLive<any>(`/jobs?limit=300${filter ? `&status=${filter}` : ''}`, (e) => e.type === 'job')
  const act = useAction()
  const c = data?.counts || {}
  const active = (c.queued || 0) + (c.submitting || 0) + (c.running || 0)
  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <div><h1 className="text-xl font-semibold">İşler</h1><p className="text-[13px] text-mute">Durum canlı güncellenir (SSE). Geçici hatalar üstel bekleme ile kendiliğinden yeniden denenir.</p></div>
        <div className="flex gap-2 text-[12px]"><Badge tone="info">{active} süren</Badge><Badge tone="accent">{c.waiting_manual || 0} elle adım</Badge><Badge tone="bad">{c.failed || 0} hatalı</Badge><Badge tone="ok">{c.succeeded || 0} tamamlanan</Badge></div>
      </div>
      <div className="flex gap-1.5">{FILTERS.map(([k, label]) => (
        <button key={k} onClick={() => setFilter(k)} className={cx('rounded-md border px-3 py-1 text-[13px]', filter === k ? 'border-accent bg-accent/20 text-white' : 'border-line text-mute hover:text-ink')}>{label}</button>))}</div>
      <Panel>
        {error && <Empty>{error}</Empty>}
        {data && data.jobs.length === 0 && <Empty>Bu süzgeçte iş yok.</Empty>}
        {data && data.jobs.length > 0 && (
          <table className="w-full text-left text-[13px]">
            <thead className="text-[12px] text-mute"><tr><th className="w-12 pb-2 font-medium">#</th><th className="font-medium">İş</th><th className="font-medium">Sağlayıcı</th><th className="w-44 font-medium">Durum</th><th className="w-24 font-medium">Maliyet</th><th className="w-24 font-medium">Başlangıç</th><th className="w-36" /></tr></thead>
            <tbody>
              {data.jobs.map((j: any) => {
                const live = ['queued', 'submitting', 'running'].includes(j.status)
                return (
                  <tr key={j.id} className="border-t border-line align-top">
                    <td className="py-2 text-mute">{j.id}</td>
                    <td className="py-2">
                      <div className="font-medium">{j.title}{j.is_fix && <span className="ml-1.5"><Badge tone="info">Düzeltme</Badge></span>}</div>
                      <div className="text-[12px] text-mute">Adım {j.step}{j.episode_id && <> · <Link className="text-info hover:underline" to={(j.scene_id ? `/bolum/${j.episode_id}/sahne/${j.scene_id}` : `/bolum/${j.episode_id}`) + (j.language ? `?dil=${j.language}` : "")}>{j.scene_no ? `Sahne ${j.scene_no}` : "Bölüm"}</Link>{j.language && <> · <Badge tone="accent">{j.language}</Badge></>}{j.run_id && <> · <Badge tone="info">ajan</Badge></>}</>}</div>
                      {j.error && <div className={cx('mt-1 max-w-[640px] rounded border px-2 py-1 text-[12px] whitespace-pre-wrap break-words', j.status === 'failed' ? 'border-bad/40 bg-bad/10 text-[#ffc9ce]' : 'border-warn/30 bg-warn/10 text-warn')}>{j.error}</div>}
                      {j.manual && <div className="mt-2 max-w-[640px]"><ManualStep job={j} /></div>}
                    </td>
                    <td className="py-2"><div className="flex items-center gap-1.5">{j.provider_label}<ModeBadge mode={j.mode} /></div></td>
                    <td className="py-2 pr-4"><div className="flex items-center gap-1.5"><StatusBadge status={j.status} />{j.attempts > 0 && <Badge tone="warn">{j.attempts}. deneme</Badge>}</div>
                      {live && <div className="mt-1.5"><Progress value={j.progress} live /></div>}
                      {j.retry_at && <div className="mt-1 text-[11px] text-mute">Yeniden deneme: {fmtTime(j.retry_at)}</div>}</td>
                    <td className="py-2 text-[12px]">{j.cost_usd ? <span title={`${j.cost_units} ${j.cost_unit}`} className={j.mode === 'mock' ? 'text-mute' : ''}>{fmtUsd(j.cost_usd)}{j.mode === 'mock' && ' (mock)'}</span> : <span className="text-mute">—</span>}</td>
                    <td className="py-2 text-[12px] text-mute">{fmtTime(j.started_at || j.created_at)}</td>
                    <td className="py-2 text-right">
                      {(live || j.status === 'waiting_manual') && <Btn small kind="bad" onClick={() => act(() => api.post(`/jobs/${j.id}/cancel`))}>İptal</Btn>}
                      {['failed', 'canceled', 'blocked'].includes(j.status) && <Btn small onClick={() => act(() => api.post(`/jobs/${j.id}/retry`), 'Yeniden kuyruğa alındı')}>Yeniden dene</Btn>}
                    </td>
                  </tr>)
              })}
            </tbody>
          </table>)}
      </Panel>
    </div>
  )
}
