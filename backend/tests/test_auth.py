"""Hesap ve oturum, kurulum sihirbazı (uçtan uca test 1), anahtar şifreleme/maskeleme, yönetici paneli,
hesap silme, S3 uyumlu depo ve webhook."""
import base64
import hashlib
import hmac
import logging
import time

import httpx
import httpx2
import pytest

import helpers
from app import auth, connections, crypto, settings_store as store, storage, tenancy
from app.db import session_scope
from app.main import SecretFilter, app
from app.models import Asset, AuditLog, AuthSession, Connection, Job, Project, User
from app.worker import worker

GOOD = {"anthropic": "sk-ant-api03-DOGRU-ANAHTAR-aaaa1111", "elevenlabs": "el-DOGRU-ANAHTAR-bbbb2222",
        "kling": "kl-DOGRU-ANAHTAR-cccc3333", "heygen": "hg-DOGRU-ANAHTAR-dddd4444"}

WHSEC = "whsec_" + base64.b64encode(b"gizli-imza-anahtari").decode()


def provider_handler(req):
    """Sağlayıcıların sınama uçlarını taklit eder: doğru anahtar 200, yanlış anahtar 401."""
    host, path = req.url.host, req.url.path
    if "elevenlabs" in host:
        if req.headers.get("xi-api-key") != GOOD["elevenlabs"]:
            return httpx.Response(401, json={"detail": {"status": "invalid_api_key"}})
        if path.endswith("/subscription"):
            return httpx.Response(200, json={"tier": "creator", "character_count": 2500, "character_limit": 100000})
        return httpx.Response(200, json={"voices": [{"voice_id": "v-ada", "name": "Ada", "labels": {"language": "tr"}},
                                                    {"voice_id": "v-can", "name": "Can", "labels": {}}]})
    if "kling" in host:
        if req.headers.get("authorization") != f"Bearer {GOOD['kling']}":
            return httpx.Response(401, json={"code": 1002, "message": "invalid api key"})
        return httpx.Response(200, json={"code": 0, "data": {"resource_pack_subscribe_infos": [
            {"resource_pack_name": "Video Generation Pack", "remaining_quantity": 800, "total_quantity": 1000, "status": "online"},
            {"resource_pack_name": "Image Pack", "remaining_quantity": 50, "total_quantity": 100, "status": "online"}]}})
    if "heygen" in host:
        if req.headers.get("x-api-key") != GOOD["heygen"]:
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        if path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"billing_type": "wallet", "wallet": {"remaining_balance": 42.5, "currency": "usd"}}})
        return httpx.Response(200, json={"data": [{"id": "look1", "name": "Sunucu", "supported_api_engines": ["heygen-avatar-6"]}]})
    return httpx.Response(404)


def anthropic_handler(req):
    if GOOD["anthropic"] not in (req.headers.get("x-api-key"), (req.headers.get("authorization") or "").removeprefix("Bearer ")):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    return httpx2.Response(200, json={"data": [{"type": "model", "id": "claude-fable-5-2", "display_name": "Claude Fable 5.2",
                                                "created_at": "2026-09-01T00:00:00Z"},
                                               {"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                                                "created_at": "2026-08-01T00:00:00Z"}],
                                      "has_more": False, "first_id": "claude-fable-5-2", "last_id": "claude-opus-5-5"})


def fake_providers():
    connections.transport_override = httpx.MockTransport(provider_handler)
    connections.anthropic_http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(anthropic_handler))


# ------------------------------------------------------------------ uçtan uca 1: kayıt, giriş, sihirbaz
async def test_register_login_and_setup_wizard(client):
    admin = client
    fake_providers()
    async with helpers.raw_client(app) as c:
        state = (await c.get("/api/auth/state")).json()
        assert state == {"first_run": False, "registration": "invite", "smtp": False}
        # oturumsuz hiçbir uç çalışmaz
        for url in ("/api/projects", "/api/jobs", "/api/conversations", "/api/connections", "/api/costs", "/api/auth/me",
                    "/api/admin/overview", "/api/config", "/api/events", "/media/u1/x.mp4"):
            assert (await c.get(url)).status_code == 401, url

        # kayıt yalnızca davetle (varsayılan)
        r = await c.post("/api/auth/register", json={"email": "yeni@ornek.com", "password": "uzun-bir-sifre-1"})
        assert r.status_code == 403 and "davet" in r.json()["detail"]
        inv = (await admin.post("/api/admin/invites", json={"email": "yeni@ornek.com"})).json()
        token = inv["url"].split("token=")[1]
        assert "/kayit?token=" in inv["url"] and inv["emailed"] is False
        bad = await c.post("/api/auth/register", json={"email": "baskasi@ornek.com", "password": "uzun-bir-sifre-1", "invite": token})
        assert bad.status_code == 403  # davet başka e-postaya
        short = await c.post("/api/auth/register", json={"email": "yeni@ornek.com", "password": "kisa", "invite": token})
        assert short.status_code == 400
        r = await c.post("/api/auth/register", json={"email": "Yeni@Ornek.com", "password": "uzun-bir-sifre-1", "invite": token})
        assert r.status_code == 200, r.text
        me = r.json()
        assert me["user"]["email"] == "yeni@ornek.com" and me["user"]["role"] == "user" and me["user"]["setup_done"] is False
        # oturum çerezi: httpOnly + SameSite; şifre yanıtta/çerezde yok
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
        assert "uzun-bir-sifre" not in r.text
        # davet tek kullanımlık
        again = await helpers.raw_client(app).post("/api/auth/register", json={"email": "yeni2@ornek.com", "password": "uzun-bir-sifre-1", "invite": token})
        assert again.status_code == 403

        # CSRF: belirteçsiz veri değiştiren istek reddedilir
        assert (await c.put("/api/prefs", json={"budget_usd": 5})).status_code == 403
        c.headers[auth.CSRF_HEADER] = me["csrf"]
        uid = me["user"]["id"]

        # sihirbaz 1: Anthropic zorunlu (demo modu kapalıyken)
        store.sys_put("demo_mode", False)
        assert (await c.get("/api/auth/me")).json()["director"] == "none"
        r = await c.post("/api/setup/complete")
        assert r.status_code == 409 and "Anthropic" in r.json()["detail"]
        assert (await c.post("/api/conversations", json={})).status_code == 409

        listing = (await c.get("/api/connections")).json()
        assert [x["service"] for x in listing["connections"]] == ["anthropic", "elevenlabs", "kling", "heygen"]
        assert all(x["mode"] == "demo" and x["url"].startswith("https://") and x["purpose"] for x in listing["connections"])
        kling_fields = next(x for x in listing["connections"] if x["service"] == "kling")["fields"]
        assert [(f["name"], f["required"]) for f in kling_fields] == [("api_key", True), ("webhook_secret", False)]

        # yanlış anahtar -> hatalı; doğru anahtar -> bağlı (her servis için)
        for service in ("anthropic", "elevenlabs", "kling"):
            wrong = (await c.put(f"/api/connections/{service}", json={"values": {"api_key": "yanlis-anahtar-0000zzzz"}})).json()
            assert wrong["result"]["ok"] is False and wrong["connection"]["status"] == "error", (service, wrong)
            assert wrong["connection"]["last4"] == "zzzz" and "yanlis-anahtar" not in str(wrong)
            values = {"api_key": GOOD[service]}
            if service == "kling":
                values["webhook_secret"] = WHSEC
            r = await c.put(f"/api/connections/{service}", json={"values": values})
            right = r.json()
            assert right["result"]["ok"] is True and right["connection"]["status"] == "connected", (service, right)
            assert right["connection"]["last4"] == GOOD[service][-4:] and right["connection"]["mode"] == "connected"
            assert GOOD[service] not in r.text
            assert WHSEC not in r.text and WHSEC[6:] not in r.text
        el = next(x for x in (await c.get("/api/connections")).json()["connections"] if x["service"] == "elevenlabs")
        assert el["info"]["voice_count"] == 2 and el["info"]["voices"][0]["name"] == "Ada" and "97500" in el["info"]["credit"]
        kl = next(x for x in (await c.get("/api/connections")).json()["connections"] if x["service"] == "kling")
        assert kl["info"]["features"]["video"]["state"] == "paket var" and kl["info"]["features"]["image"]["state"] == "paket var"
        assert kl["info"]["features"]["lipsync"]["state"] == "belirsiz" and kl["info"]["webhook_signed"] is True
        an = next(x for x in (await c.get("/api/connections")).json()["connections"] if x["service"] == "anthropic")
        assert an["info"]["selected_model_available"] is True and "claude-fable-5-2" in an["info"]["models"]
        # boş anahtar ve bilinmeyen servis
        assert (await c.put("/api/connections/heygen", json={"values": {"api_key": "  "}})).status_code == 400
        assert (await c.put("/api/connections/openai", json={"values": {"api_key": "x"}})).status_code == 404
        # "bağlantıyı test et" düğmesi kayıtlı anahtarı yeniden sınar
        assert (await c.post("/api/connections/kling/test")).json()["result"]["ok"] is True
        assert (await c.post("/api/connections/heygen/test")).json()["result"]["ok"] is False  # atlandı: anahtar yok

        # veritabanında anahtar şifreli durur
        with session_scope() as s:
            rows = s.query(Connection).filter(Connection.user_id == uid).all()
            assert len(rows) == 3
            for row in rows:
                assert GOOD[row.service] not in row.secret and row.last4 == GOOD[row.service][-4:]
                assert crypto.decrypt_json(row.secret)["api_key"] == GOOD[row.service]

        # sihirbaz 5: tercihler
        cfg = (await c.put("/api/prefs", json={"default_language": "en", "budget_usd": 8, "plan_approval": True, "output_format": "9:16"})).json()
        assert cfg["prefs"]["default_language"] == "en" and cfg["prefs"]["budget_usd"] == 8 and cfg["prefs"]["output_format"] == "9:16"
        modes = {p["key"]: p["mode"] for p in cfg["providers"]}
        assert modes["elevenlabs_tts"] == "real" and modes["kling_video"] == "real" and modes["heygen_avatar"] == "mock"
        # sihirbaz 6: özet. HeyGen atlandı -> o adım "deneme modu"nda
        done = (await c.post("/api/setup/complete")).json()
        summ = {x["service"]: x["mode"] for x in done["connections"]["services"]}
        assert summ == {"anthropic": "connected", "elevenlabs": "connected", "kling": "connected", "heygen": "demo"}
        assert done["connections"]["demo_steps"] == [3] and done["connections"]["all_connected"] is False
        me = (await c.get("/api/auth/me")).json()
        assert me["user"]["setup_done"] is True and me["director"] == "real"
        # yöneticinin tercihleri ve bağlantıları etkilenmedi
        assert (await admin.get("/api/config")).json()["prefs"]["default_language"] == "tr"
        assert all(x["mode"] == "demo" for x in (await admin.get("/api/connections")).json()["connections"])

        # anahtar değiştirilebilir ve silinebilir
        assert (await c.delete("/api/connections/elevenlabs")).status_code == 200
        assert (await c.delete("/api/connections/elevenlabs")).status_code == 404
        modes = {p["key"]: p["mode"] for p in (await c.get("/api/config")).json()["providers"]}
        assert modes["elevenlabs_tts"] == "mock"

        # çıkış: oturum sunucuda da biter
        assert (await c.post("/api/auth/logout")).status_code == 200
        assert (await c.get("/api/auth/me")).status_code == 401

    # yönetici paneli: kullanıcı görünür, hangi servisin bağlı olduğu görünür, anahtar ASLA görünmez
    r = await admin.get("/api/admin/overview")
    text = r.text
    for secret in GOOD.values():
        assert secret not in text and secret[-4:] not in text
    row = next(u for u in r.json()["users"] if u["email"] == "yeni@ornek.com")
    assert row["connected"] == ["anthropic", "kling"] and "last4" not in str(row)
    assert r.json()["system"]["app_secret"] is True and r.json()["system"]["database"] in ("SQLite", "PostgreSQL")
    acts = [a["action"] for a in (await admin.get("/api/admin/audit")).json()]
    for needed in ("register", "invite_created", "connection_saved", "connection_deleted", "logout"):
        assert needed in acts, needed
    assert not any(GOOD[k] in str(a) for a in (await admin.get("/api/admin/audit")).json() for k in GOOD)


async def test_demo_mode_lets_user_continue_without_anthropic(client):
    """Yönetici demo modunu açarsa Anthropic'siz kullanıcı sahte yönetmenle devam eder; bütün adımlar deneme modunda."""
    uid = helpers.make_user("demoda@ornek.com")
    with session_scope() as s:
        s.get(User, uid).setup_done = False
    async with helpers.raw_client(app) as c:
        me = await helpers.login(c, "demoda@ornek.com")
        assert me["director"] == "mock" and me["demo_mode"] is True
        done = (await c.post("/api/setup/complete")).json()
        assert done["connections"]["demo_steps"] == [1, 2, 3, 4, 7]
        conv = (await c.post("/api/conversations", json={})).json()
        assert conv["mode"] == "mock"
        assert (await c.delete(f"/api/conversations/{conv['id']}")).status_code == 200


# ------------------------------------------------------------------ oturum güvenliği
def test_password_hashing_and_token_helpers():
    h = auth.hash_password("cok-gizli-sifre-1")
    assert h.startswith("$argon2id$") and "cok-gizli" not in h
    assert auth.verify_password(h, "cok-gizli-sifre-1") and not auth.verify_password(h, "yanlis") and not auth.verify_password("", "x")
    assert crypto.last4("abcdefgh") == "efgh" and "abcd" not in crypto.mask("abcdefgh12345678")
    blob = crypto.encrypt_json({"api_key": "sk-gizli-123456"})
    assert "sk-gizli" not in blob and crypto.decrypt_json(blob) == {"api_key": "sk-gizli-123456"}
    assert crypto.decrypt_json("bozuk-veri") is None and crypto.decrypt_json("") is None
    t = crypto.new_token()
    assert len(t) >= 40 and crypto.token_hash(t) != t and crypto.token_hash(t) == crypto.token_hash(t)
    # günlük süzgeci anahtar biçimlerini maskeler
    rec = logging.LogRecord("x", logging.INFO, "", 0, "hata: Authorization: Bearer abcdefghijklmnop1234 ve sk-ant-api03-GIZLI9999", (), None)
    SecretFilter().filter(rec)
    assert "abcdefghijklmnop" not in rec.getMessage() and "GIZLI9999" not in rec.getMessage()


async def test_login_lockout_reset_link_and_disable(client):
    admin = client
    uid = helpers.make_user("kilit@ornek.com", "ilk-sifre-123456")
    async with helpers.raw_client(app) as c:
        # var olmayan hesap ile yanlış şifre aynı yanıtı verir
        a = await c.post("/api/auth/login", json={"email": "yok@ornek.com", "password": "x" * 12})
        b = await c.post("/api/auth/login", json={"email": "kilit@ornek.com", "password": "yanlis-sifre-00"})
        assert a.status_code == b.status_code == 401 and a.json() == b.json()
        for _ in range(4):
            await c.post("/api/auth/login", json={"email": "kilit@ornek.com", "password": "yanlis-sifre-00"})
        locked = await c.post("/api/auth/login", json={"email": "kilit@ornek.com", "password": "ilk-sifre-123456"})
        assert locked.status_code == 429 and "kilitlendi" in locked.json()["detail"]  # doğru şifre bile kabul edilmez
        with session_scope() as s:
            s.get(User, uid).locked_until = None
        me = await helpers.login(c, "kilit@ornek.com", "ilk-sifre-123456")
        assert me["user"]["id"] == uid
        # yönetici olmayan için yönetici uçları yok gibidir
        for url in ("/api/admin/overview", "/api/admin/audit"):
            assert (await c.get(url)).status_code == 404
        assert (await c.post(f"/api/admin/users/{helpers.UID}/reset-link")).status_code == 404
        assert (await c.put("/api/admin/settings", json={"registration_mode": "open"})).status_code == 404

        # SMTP yok: "şifremi unuttum" hesabın varlığını sızdırmaz, yönetici tek kullanımlık bağlantı üretir
        f1 = (await c.post("/api/auth/forgot", json={"email": "kilit@ornek.com"})).json()
        f2 = (await c.post("/api/auth/forgot", json={"email": "olmayan@ornek.com"})).json()
        assert f1 == f2 and f1["smtp"] is False
        url = (await admin.post(f"/api/admin/users/{uid}/reset-link")).json()["url"]
        token = url.split("token=")[1]
        assert "/sifre-sifirla?token=" in url
        assert (await c.post("/api/auth/reset", json={"token": token, "password": "kisa"})).status_code == 400
        assert (await c.post("/api/auth/reset", json={"token": token, "password": "yepyeni-sifre-789"})).status_code == 200
        assert (await c.post("/api/auth/reset", json={"token": token, "password": "bir-daha-sifre-000"})).status_code == 400  # tek kullanımlık
        assert (await c.get("/api/auth/me")).status_code == 401  # sıfırlama bütün oturumları kapatır
        assert (await c.post("/api/auth/login", json={"email": "kilit@ornek.com", "password": "ilk-sifre-123456"})).status_code == 401
        me = await helpers.login(c, "kilit@ornek.com", "yepyeni-sifre-789")

        # hesap ayarı: şifre değiştirme mevcut şifreyi ister
        assert (await c.patch("/api/account", json={"current_password": "yanlis", "new_password": "baska-sifre-4567"})).status_code == 403
        assert (await c.patch("/api/account", json={"current_password": "yepyeni-sifre-789", "email": helpers.EMAIL})).status_code == 409
        r = await c.patch("/api/account", json={"current_password": "yepyeni-sifre-789", "new_password": "baska-sifre-4567", "email": "kilit2@ornek.com"})
        assert r.status_code == 200 and r.json()["email"] == "kilit2@ornek.com"

        # yönetici hesabı devre dışı bırakır: oturum hemen düşer, giriş yapılamaz
        assert (await admin.patch(f"/api/admin/users/{uid}", json={"status": "disabled"})).json()["status"] == "disabled"
        assert (await c.get("/api/auth/me")).status_code == 401
        assert (await c.post("/api/auth/login", json={"email": "kilit2@ornek.com", "password": "baska-sifre-4567"})).status_code == 403
        assert (await admin.patch(f"/api/admin/users/{helpers.UID}", json={"status": "disabled"})).status_code == 409
        assert (await admin.patch(f"/api/admin/users/{uid}", json={"status": "active", "shared_monthly_usd": 7.5})).json()["limits"] == {"shared_monthly_usd": 7.5}

    # kayıt modu: açık kayıt
    assert (await admin.put("/api/admin/settings", json={"registration_mode": "herkes"})).status_code == 400
    assert (await admin.put("/api/admin/settings", json={"registration_mode": "open"})).json()["registration_mode"] == "open"
    async with helpers.raw_client(app) as c2:
        r = await c2.post("/api/auth/register", json={"email": "acik@ornek.com", "password": "acik-kayit-sifre-1"})
        assert r.status_code == 200 and r.json()["user"]["role"] == "user"
        assert (await c2.post("/api/auth/register", json={"email": "acik@ornek.com", "password": "acik-kayit-sifre-1"})).status_code == 409


async def test_security_headers_rate_limit_and_hosts(client):
    r = await client.get("/api/auth/me")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and "no-store" in r.headers["cache-control"]
    assert (await client.get("/api/projects", headers={"Host": "kotu.example.com"})).status_code == 400
    # yabancı kaynağa CORS izni verilmez
    pre = await client.options("/api/projects", headers={"Origin": "https://kotu.example.com", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in pre.headers
    # giriş denemesi sınırı (IP başına)
    async with helpers.raw_client(app) as c:
        codes = [(await c.post("/api/auth/login", json={"email": f"x{i}@ornek.com", "password": "yanlis-sifre-00"})).status_code for i in range(22)]
        assert codes[0] == 401 and codes[-1] == 429
    # süresi dolmuş oturum geçersizdir
    with session_scope() as s:
        sess = s.query(AuthSession).filter(AuthSession.user_id == helpers.UID).order_by(AuthSession.id.desc()).first()
        from datetime import timedelta
        sess.expires_at = sess.expires_at - timedelta(days=60)
    assert (await client.get("/api/auth/me")).status_code == 401


# ------------------------------------------------------------------ ortak (kurum) anahtarı
async def test_shared_key_is_used_with_monthly_limit(client):
    admin = client
    fake_providers()
    uid = helpers.make_user("ortak@ornek.com")
    r = await admin.put("/api/admin/connections/elevenlabs", json={"values": {"api_key": GOOD["elevenlabs"]}})
    assert r.json()["result"]["ok"] and GOOD["elevenlabs"] not in r.text
    async with helpers.raw_client(app) as c:
        await helpers.login(c, "ortak@ornek.com")
        el = next(x for x in (await c.get("/api/connections")).json()["connections"] if x["service"] == "elevenlabs")
        assert el["mode"] == "shared" and el["set"] is False and el["last4"] == ""  # ortak anahtarın izi kullanıcıya gitmez
        assert GOOD["elevenlabs"][-4:] not in (await c.get("/api/connections")).text
    with tenancy.as_user(uid):
        assert store.cred_source("elevenlabs") == "shared" and store.get_secret("elevenlabs_api_key") == GOOD["elevenlabs"]
    # aylık sınır: ortak anahtarla yapılan gerçek harcama sınırı aşınca yeni iş açılmaz
    from app import pipeline, services
    with session_scope() as s, tenancy.as_user(uid):
        p = services.create_project(s, "Ortak anahtar", "", "", "tr")
        services.create_character(s, p, name="Anlatıcı", is_narrator=True, voice_id="v-ada")
        ep = services.create_episode(s, p, title="b", language="tr", langs=["tr"])
        services.write_scenes(s, ep, [{"order_no": 1, "type": "narrator", "dialogue": "Bir varmış bir yokmuş.", "duration": 3}])
        s.flush()
        from app.models import Scene
        sc = s.query(Scene).filter(Scene.episode_id == ep.id).first()
        spent = Job(kind="voice", provider="elevenlabs_tts", mode="real", status="succeeded", user_id=uid, cred_source="shared",
                    cost_usd=7.5, project_id=p.id, input={})
        s.add(spent)
        s.get(User, uid).limits = {"shared_monthly_usd": 7.5}
        s.flush()
        with pytest.raises(pipeline.BudgetExceeded) as err:
            pipeline.voice_job(s, ep, sc, "tr")
        assert "aylık sınırı" in str(err.value)
        spent.cost_usd = 1.0
        s.flush()
        j = pipeline.voice_job(s, ep, sc, "tr")
        assert isinstance(j, Job) and j.mode == "real" and j.cred_source == "shared" and j.user_id == uid
        j.status = "canceled"
    await admin.delete("/api/admin/connections/elevenlabs")
    with tenancy.as_user(uid):
        assert store.cred_source("elevenlabs") == ""


# ------------------------------------------------------------------ hesap silme
async def test_account_deletion_removes_keys_data_and_files(client):
    uid = helpers.make_user("silinecek@ornek.com")
    helpers.set_key("kling_api_key", "kl-silinecek-anahtar-1", user_id=uid)
    async with helpers.raw_client(app) as c:
        await helpers.login(c, "silinecek@ornek.com")
        proj = (await c.post("/api/seed")).json()
        conv = (await c.post("/api/conversations", json={})).json()
        up = await c.post(f"/api/conversations/{conv['id']}/attachments", files={"file": ("not.txt", "merhaba dünya".encode(), "text/plain")})
        assert up.status_code == 200, up.text
        folder = storage.user_dir(uid)
        assert folder.exists() and any(folder.rglob("*.txt"))
        assert (await c.post("/api/account/delete", json={"password": "yanlis"})).status_code == 403
        assert (await c.post("/api/account/delete", json={"password": helpers.PASSWORD})).status_code == 200
        assert (await c.get("/api/auth/me")).status_code == 401
        assert (await c.post("/api/auth/login", json={"email": "silinecek@ornek.com", "password": helpers.PASSWORD})).status_code == 401
    assert not folder.exists()
    with session_scope() as s:
        assert s.get(User, uid) is None and s.get(Project, proj["id"]) is None
        for model in (Connection, Project, Asset, Job, AuthSession):
            assert s.query(model).filter(model.user_id == uid).count() == 0, model.__name__
        assert s.query(AuditLog).filter(AuditLog.action == "account_deleted").count() >= 1
    # tek yönetici, başka kullanıcılar varken hesabını silemez
    assert (await client.post("/api/account/delete", json={"password": helpers.PASSWORD})).status_code == 409


# ------------------------------------------------------------------ S3 uyumlu depo
class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, path, bucket, key):
        self.objects[key] = open(path, "rb").read()

    def download_file(self, bucket, key, dest):
        if key not in self.objects:
            raise KeyError(key)
        open(dest, "wb").write(self.objects[key])

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://kova.example.com/{Params['Key']}?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=abc"

    def list_objects_v2(self, **kw):
        return {"Contents": [{"Key": k} for k in self.objects if k.startswith(kw["Prefix"])]}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)


async def test_s3_backend_publish_restore_signed_url_and_delete(client, samples):
    fake = FakeS3()
    storage.s3_override = storage.S3Backend(client=fake, bucket="kova")
    rel = f"u{helpers.UID}/s3-deneme/ses.mp3"
    local = storage.root() / rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(samples["audio"].read_bytes())
    storage.publish(local)
    assert list(fake.objects) == [rel]
    # yerel kopya yoksa (başka sunucu) kovadan geri gelir
    local.unlink()
    assert storage.abs_path(rel).read_bytes() == samples["audio"].read_bytes()
    assert "X-Amz-Expires=600" in storage.signed_url(rel, 600)
    # medya ucu: sahibi indirir; yol dışına çıkış ve başkasının klasörü 404
    assert (await client.get(f"/media/{rel}")).status_code == 200
    for bad in ("/media/u999999/x.mp3", f"/media/u{helpers.UID}0/x.mp3",
                "/media/_tmp/x.mp3", f"/media/u{helpers.UID}/%2e%2e/%2e%2e/.env", f"/media/u{helpers.UID}/yok.mp3"):
        assert (await client.get(bad)).status_code == 404, bad
    with pytest.raises(ValueError):
        storage.abs_path("../.env")
    # tarayıcıda çalışabilecek türler (html, svg) görüntülenmez, indirme olarak verilir
    evil = storage.root() / f"u{helpers.UID}/s3-deneme/sayfa.html"
    evil.write_text("<script>alert(1)</script>", encoding="utf-8")
    r = await client.get(f"/media/u{helpers.UID}/s3-deneme/sayfa.html")
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    assert "attachment" in r.headers["content-disposition"] and r.headers["x-content-type-options"] == "nosniff"
    assert (await client.get(f"/media/{rel}")).headers["content-type"].startswith("audio/")
    storage.delete_tree(f"u{helpers.UID}/s3-deneme")
    assert not fake.objects and not local.exists()


# ------------------------------------------------------------------ webhook
async def test_webhook_pokes_job_and_checks_signature(client, monkeypatch):
    secret_raw = b"kling-imza-anahtari-123"
    secret = "whsec_" + base64.b64encode(secret_raw).decode()
    helpers.set_key("kling_api_key", "kl-webhook-anahtar-1")
    helpers.set_key("kling_webhook_secret", secret)
    token = crypto.new_token()
    with session_scope() as s:
        j = Job(kind="silent_video", provider="kling_video", mode="real", status="running", user_id=helpers.UID, input={},
                external_id="T1", webhook_token=token)
        s.add(j)
        s.flush()
        jid = j.id
    poked = []
    monkeypatch.setattr(worker, "poke", lambda job_id: poked.append(job_id))
    body = b'{"task_id":"T1","status":"succeeded"}'

    def headers(key: bytes, ts: int) -> dict:
        sig = base64.b64encode(hmac.new(key, f"msg_1.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
        return {"webhook-id": "msg_1", "webhook-timestamp": str(ts), "webhook-signature": f"v1,{sig}", "content-type": "application/json"}

    async with helpers.raw_client(app) as c:  # oturumsuz: sağlayıcı çağırır
        now_ts = int(time.time())
        assert (await c.post("/api/webhooks/" + "x" * 40, content=body)).json() == {"ok": True} and poked == []
        assert (await c.post(f"/api/webhooks/{token}", content=body, headers=headers(b"yanlis-anahtar", now_ts))).status_code == 200
        assert poked == []  # imza yanlış
        await c.post(f"/api/webhooks/{token}", content=body, headers=headers(secret_raw, now_ts - 4000))
        assert poked == []  # zaman damgası eski (yeniden oynatma)
        await c.post(f"/api/webhooks/{token}", content=body, headers=headers(secret_raw, now_ts))
        assert poked == [jid]
    with session_scope() as s:
        s.get(Job, jid).status = "canceled"
