"""Süreç içi asyncio iş kuyruğu. Durum SQLite'ta kalıcıdır: uygulama yeniden başlayınca
yarım işler kaldığı yerden sürer (gönderilmiş işler yeniden gönderilmez, yalnızca sorgulanır).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta

from sqlalchemy import select

from . import connections, media, notify, pipeline, settings_store as store, storage, tenancy
from .db import session_scope
from .models import AgentRun, Asset, Character, Episode, Job, Scene, now
from .providers import registry
from .providers.base import AuthFailure, PermanentError, ProviderError, TransientError, redact

log = logging.getLogger("studyo.worker")
EXT = {"audio": ".mp3", "image": ".png", "video": ".mp4"}
POLL_RETRIES = 6  # ardışık geçici sorgu hatası sınırı (iş yeniden gönderilmez)


def backoff(attempt: int, base: float, retry_after: float | None = None) -> float:
    """Üstel bekleme + küçük rastgelelik; sağlayıcı Retry-After verdiyse en az o kadar."""
    delay = min(base * (2 ** max(attempt - 1, 0)), 300.0) * (1 + random.random() * 0.2)
    return max(delay, retry_after or 0.0)


class Worker:
    def __init__(self) -> None:
        self.tasks: dict[int, tuple[str, asyncio.Task]] = {}
        self.owners: dict[int, int | None] = {}  # iş -> kullanıcı (adil sıralama için)
        self.pokes: dict[int, asyncio.Event] = {}  # webhook gelince yoklamayı hemen tetikler
        self.runs: dict[int, asyncio.Task] = {}  # yönetmen ajanı çalışmaları
        self.llm_factory = None                  # testler sahte model takabilsin
        self._loop_task: asyncio.Task | None = None
        self._stop = False
        self.tick = 0.25

    async def start(self) -> None:
        self._stop = False
        self._aio = asyncio.get_running_loop()
        self.recover()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        for _, t in list(self.tasks.values()):
            t.cancel()
        for t in list(self.runs.values()):
            t.cancel()
        if self._loop_task:
            self._loop_task.cancel()
        await asyncio.gather(*(t for _, t in self.tasks.values()), *self.runs.values(), self._loop_task,
                             return_exceptions=True)
        self.tasks.clear()
        self.runs.clear()

    def recover(self) -> None:
        """Yarım kalan işler: gönderilememişler kuyruğa döner; gönderilmişler sorgulanmaya devam eder."""
        with session_scope() as s:
            for j in s.scalars(select(Job).where(Job.status.in_(("submitting", "running")))).all():
                if j.status == "submitting" or not j.external_id or j.mode == "local":
                    j.status = "queued"
                    j.run_after = now()
            # yarım ajan çalışmaları: geçmiş veritabanında; döngü kaldığı yerden sürer
            for r in s.scalars(select(AgentRun).where(AgentRun.status.in_(("running", "waiting_jobs")))).all():
                r.status = "queued"

    async def _loop(self) -> None:
        while not self._stop:
            try:
                self._dispatch()
                self._dispatch_runs()
            except Exception:
                log.exception("kuyruk döngüsü hatası")
            await asyncio.sleep(self.tick)

    def _dispatch(self) -> None:
        """Kuyruktan iş seçer. Sınırlar kullanıcı+sağlayıcı başınadır; sağlayıcı başına genel bir tavan da vardır.
        Seçim kullanıcılar arasında sırayla yapılır: bir kullanıcının yoğun işi diğerlerini kilitlemez."""
        for jid in [k for k, (_, t) in self.tasks.items() if t.done()]:
            self.tasks.pop(jid, None)
            self.owners.pop(jid, None)
            self.pokes.pop(jid, None)
        caps = store.sys_get("global_concurrency") or {}
        total: dict[str, int] = {}
        per_user: dict[tuple, int] = {}
        for jid, (prov, _) in self.tasks.items():
            total[prov] = total.get(prov, 0) + 1
            key = (self.owners.get(jid), prov)
            per_user[key] = per_user.get(key, 0) + 1
        with session_scope() as s:
            q = (select(Job.id, Job.provider, Job.user_id)
                 .where(Job.kind != "claude",
                        ((Job.status == "queued") & (Job.run_after <= now())) | (Job.status == "running"))
                 .order_by(Job.id))
            rows = [r for r in s.execute(q).all() if r[0] not in self.tasks]
        if not rows:
            return
        queues: dict[int | None, list] = {}
        for jid, prov, uid in rows:
            queues.setdefault(uid, []).append((jid, prov))
        limits = {uid: store.prefs(uid)["concurrency"] if uid is not None else {} for uid in queues}
        # en az çalışan işi olan kullanıcıdan başlayarak sırayla birer iş al
        order = sorted(queues, key=lambda u: (sum(v for (uu, _), v in per_user.items() if uu == u), u or 0))
        progressed = True
        while progressed:
            progressed = False
            for uid in order:
                for i, (jid, prov) in enumerate(queues[uid]):
                    if total.get(prov, 0) >= max(int(caps.get(prov, 4) or 1), 1):
                        continue
                    if per_user.get((uid, prov), 0) >= max(int(limits[uid].get(prov, 1) or 1), 1):
                        continue
                    queues[uid].pop(i)
                    total[prov] = total.get(prov, 0) + 1
                    per_user[(uid, prov)] = per_user.get((uid, prov), 0) + 1
                    self.owners[jid] = uid
                    self.tasks[jid] = (prov, asyncio.create_task(self._run(jid, uid), name=f"job-{jid}"))
                    progressed = True
                    break

    def poke(self, job_id: int) -> None:
        """Sağlayıcı webhook'u geldi: bekleyen yoklamayı hemen çalıştır."""
        ev = self.pokes.get(job_id)
        loop = getattr(self, "_aio", None)
        if ev is not None and loop is not None:
            loop.call_soon_threadsafe(ev.set)

    def _dispatch_runs(self) -> None:
        from .agent.runner import Runner
        for rid in [k for k, t in self.runs.items() if t.done()]:
            self.runs.pop(rid, None)
        with session_scope() as s:
            ids = s.scalars(select(AgentRun.id).where(AgentRun.status == "queued").order_by(AgentRun.id)).all()
        for rid in ids:
            if rid not in self.runs:
                llm = self.llm_factory() if self.llm_factory else None
                self.runs[rid] = asyncio.create_task(Runner(rid, llm).run(), name=f"run-{rid}")

    def _cancel_task(self, task: asyncio.Task) -> None:
        """API uçları iş parçacığı havuzunda çalışır; iptal olay döngüsüne güvenli biçimde iletilir."""
        loop = getattr(self, "_aio", None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(task.cancel)
        else:
            task.cancel()

    def cancel_run(self, run_id: int) -> None:
        t = self.runs.get(run_id)
        if t:
            self._cancel_task(t)

    def cancel(self, job_id: int) -> None:
        t = self.tasks.get(job_id)
        if t:
            self._cancel_task(t[1])

    # ------------------------------------------------------------------ tek iş
    def _update(self, job_id: int, **fields) -> None:
        with session_scope() as s:
            j = s.get(Job, job_id)
            for k, v in fields.items():
                setattr(j, k, v)
            pipeline.emit(s, "job", id=j.id, status=j.status, progress=j.progress,
                          episode_id=j.episode_id, scene_id=j.scene_id)

    def _fail(self, job_id: int, message: str) -> None:
        message = redact(message, store.all_secrets())[:1000]
        log.warning("iş %s hata: %s", job_id, message)
        with session_scope() as s:
            j = s.get(Job, job_id)
            j.status, j.error, j.finished_at = "failed", message, now()
            pipeline.on_job_failed(s, j)
            pipeline.emit(s, "job", id=j.id, status="failed", episode_id=j.episode_id, scene_id=j.scene_id)

    def _retry_later(self, job_id: int, e: TransientError) -> None:
        with session_scope() as s:
            j = s.get(Job, job_id)
            j.attempts += 1
            limit = int(store.get("max_attempts") or 4)
            if j.attempts >= limit:
                attempts = j.attempts
            else:
                wait = backoff(j.attempts, float(store.get("retry_base_seconds") or 5.0), e.retry_after)
                j.status, j.external_id = "queued", ""
                j.error = redact(f"Geçici hata, {wait:.0f} sn sonra yeniden denenecek ({j.attempts}/{limit}): {e}",
                                 store.all_secrets())[:1000]
                j.run_after = now() + timedelta(seconds=wait)
                pipeline.emit(s, "job", id=j.id, status="queued", episode_id=j.episode_id, scene_id=j.scene_id)
                return
        self._fail(job_id, f"{attempts} denemeden sonra vazgeçildi: {e}")

    def _block(self, job_id: int, e: AuthFailure) -> None:
        """Anahtar geçersiz ya da kredisi bitmiş: iş durur, kullanıcıya hangi bağlantının yenileneceği söylenir.
        Bağlantı düzeltilince (connections.unblock) iş kaldığı yerden sürer."""
        message = redact(str(e), store.all_secrets())[:600]
        with session_scope() as s:
            j = s.get(Job, job_id)
            service = connections.PROVIDER_SERVICE.get(j.provider, "")
            label = connections.INFO.get(service, {}).get("label", service)
            why = "kredisi/kotası bitmiş" if e.kind == "credit" else "anahtarı geçersiz ya da yetkisi yok"
            shared = j.cred_source == "shared"
            j.status, j.external_id = "blocked", ""
            j.error = (f"{label} bağlantısının {why}. " + ("Ortak (kurum) anahtarı kullanılıyordu; yöneticiye bildirin ya da kendi anahtarınızı bağlayın. "
                       if shared else "Ayarlar > Bağlantılar'dan yenileyin; iş kaldığı yerden devam eder. ") + f"({message})")
            first = not s.scalars(select(Job.id).where(Job.user_id == j.user_id, Job.status == "blocked", Job.id != j.id,
                                                       Job.provider == j.provider)).first()
            if first:
                notify.notify(s, j.user_id, f"{label} bağlantısının {why}. Üretim durdu; bağlantıyı yenileyince kaldığı yerden devam eder.",
                              "error", "/ayarlar/baglantilar", email_subject=f"{label} bağlantısı yenilenmeli")
            pipeline.emit(s, "job", id=j.id, status="blocked", episode_id=j.episode_id, scene_id=j.scene_id)
        connections.mark_failed(tenancy.uid(), service, "Üretim sırasında reddedildi: " + why)

    async def _run(self, job_id: int, user_id: int | None = None) -> None:
        # iş, sahibinin bağlamında yürür: anahtarlar, tercihler ve olaylar o kullanıcıya aittir
        tenancy.USER_ID.set(user_id)
        try:
            with session_scope() as s:
                j = s.get(Job, job_id)
                if j is None or j.status not in ("queued", "running"):
                    return
                kind, provider_key, mode, inp, status, ext_id = j.kind, j.provider, j.mode, dict(j.input or {}), j.status, j.external_id
            if mode == "local":
                await self._run_local(job_id, kind)
                return
            p = registry.build(provider_key, mode, job_id=job_id, kind=kind)
            p.inp = inp
            if status == "queued":
                self._update(job_id, status="submitting", started_at=now(), progress=0.0)
                sub = await p.submit(inp)
                ext_id = sub.external_id
                fields = dict(external_id=ext_id, status="waiting_manual" if sub.manual else "running", error="")
                if sub.cost_units is not None:
                    fields["cost_units"] = sub.cost_units
                self._update(job_id, **fields)
                if sub.manual:
                    return  # kullanıcı sonucu yükleyince iş 'running' olur ve buradan sürer
            await self._poll(job_id, p, ext_id)
            await self._collect(job_id, p, ext_id)
        except asyncio.CancelledError:
            raise
        except TransientError as e:
            self._retry_later(job_id, e)
        except AuthFailure as e:
            self._block(job_id, e)
        except (ProviderError, media.MediaError, pipeline.StepBlocked) as e:
            self._fail(job_id, str(e))
        except Exception as e:  # beklenmeyen: iş asılı kalmasın
            log.exception("iş %s beklenmeyen hata", job_id)
            self._fail(job_id, f"Beklenmeyen hata: {type(e).__name__}: {e}")

    async def _poll(self, job_id: int, p, ext_id: str) -> None:
        t0, errors, last = time.monotonic(), 0, -1.0
        base = float(store.get("retry_base_seconds") or 5.0)
        with session_scope() as s:
            hooked = bool(s.get(Job, job_id).webhook_token)
        ev = self.pokes.setdefault(job_id, asyncio.Event())
        # webhook bekleniyorsa yoklama yalnızca yedek olarak, daha seyrek yapılır
        interval = max(p.poll_interval * 6, 30.0) if hooked else p.poll_interval
        while True:
            try:
                r = await p.poll(ext_id)
                errors = 0
            except TransientError as e:
                errors += 1
                if errors > POLL_RETRIES:
                    raise PermanentError(f"Durum sorgusu {errors} kez başarısız: {e}") from e
                await asyncio.sleep(backoff(errors, base, e.retry_after))
                continue
            if r.status == "succeeded":
                return
            if r.status == "failed":
                raise PermanentError(r.error or "Sağlayıcı işi başarısız bildirdi")
            if r.status == "lost":
                raise TransientError("İş kimliği sağlayıcıda bulunamadı; yeniden gönderilecek")
            if abs(r.progress - last) >= 0.05:
                last = r.progress
                self._update(job_id, progress=round(r.progress, 2))
            if time.monotonic() - t0 > p.timeout:
                raise PermanentError(f"Zaman aşımı: iş {p.timeout / 60:.0f} dakikada bitmedi")
            try:
                await asyncio.wait_for(ev.wait(), timeout=interval)
                ev.clear()
            except asyncio.TimeoutError:
                pass

    async def _collect(self, job_id: int, p, ext_id: str) -> None:
        """Sonucu indir, doğrula, yeni sürüm olarak kaydet."""
        with session_scope() as s:
            j = s.get(Job, job_id)
            role = pipeline.KIND_ROLE[j.kind]
            sc = s.get(Scene, j.scene_id) if j.scene_id else None
            ch = s.get(Character, j.character_id) if j.character_id and not sc else None
            lang = j.language or ""
            version = storage.next_version(s, role=role, scene_id=sc.id if sc else None,
                                           character_id=ch.id if ch else None, language=lang)
            dest = storage.target_path(s, role=role, version=version, ext=EXT[p.kind], scene=sc, character=ch,
                                       language=lang)
        base = float(store.get("retry_base_seconds") or 5.0)
        for attempt in range(1, 5):
            try:
                path = await p.fetch(ext_id, dest)
                break
            except TransientError as e:
                if attempt == 4:
                    raise PermanentError(f"Sonuç indirilemedi: {e}") from e
                await asyncio.sleep(backoff(attempt, base, e.retry_after))
        try:
            meta = await asyncio.to_thread(media.validate, path, p.kind)
        except media.MediaError as e:
            path.unlink(missing_ok=True)
            raise PermanentError(f"İndirilen dosya bozuk: {e}") from e
        meta.update(p.result_meta or {})
        await asyncio.to_thread(storage.publish, path)  # S3 etkinse kalıcı kopya
        with session_scope() as s:
            j = s.get(Job, job_id)
            inp = j.input or {}
            a = Asset(user_id=j.user_id, project_id=j.project_id, episode_id=j.episode_id, scene_id=j.scene_id,
                      character_id=j.character_id if not j.scene_id else None, kind=p.kind, role=role, language=j.language or "",
                      path=storage.rel_path(path), version=version, step=j.step, provider=j.provider, mode=j.mode,
                      model="" if j.mode != "real" else (inp.get("model") or p.ctx.model),
                      prompt=inp.get("prompt") or inp.get("text") or inp.get("instruction") or "",
                      params={k: v for k, v in inp.items() if k not in ("prompt", "text", "callback_url")},
                      meta=meta, job_id=j.id)
            s.add(a)
            s.flush()
            j.status, j.progress, j.finished_at, j.result_asset_id, j.error = "succeeded", 1.0, now(), a.id, ""
            pipeline.emit(s, "job", id=j.id, status="succeeded", episode_id=j.episode_id, scene_id=j.scene_id)
            pipeline.on_job_succeeded(s, j, a)

    async def _run_local(self, job_id: int, kind: str) -> None:
        from . import montage, qc
        self._update(job_id, status="running", started_at=now(), progress=0.05)

        def progress(v: float) -> None:
            self._update(job_id, progress=round(v, 2))

        fn = montage.run if kind == "montage" else qc.run
        asset_id = await asyncio.to_thread(fn, job_id, progress)
        with session_scope() as s:
            j = s.get(Job, job_id)
            j.status, j.progress, j.finished_at, j.result_asset_id, j.error = "succeeded", 1.0, now(), asset_id, ""
            pipeline.emit(s, "job", id=j.id, status="succeeded", episode_id=j.episode_id)
            pipeline.on_job_succeeded(s, j, None)


worker = Worker()
