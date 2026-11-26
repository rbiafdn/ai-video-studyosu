# AI Video Prodüksiyon Stüdyosu

ElevenLabs, Kling ve HeyGen'i tek panelden yöneten **çok kullanıcılı** web uygulaması. Her kullanıcı kendi hesabıyla
giriş yapar, kendi servis hesaplarını (API anahtarlarını) bir kurulum sihirbazıyla bağlar ve yalnızca kendi
projelerini görür. Merkezinde Claude'a bağlı bir sohbet ekranı vardır: tek bir mesaj yazarsınız (fikir, senaryo ya da
talimat), **yönetmen ajanı** karakterleri ve sahneleri çıkarır, sesleri seçer, üretim hattını 1'den 6'ya çalıştırır,
çıktıları inceleyip onaylar, kalite raporundaki bulguları düzeltir ve bitince sohbete final videoyu, altyazıyı,
kalite raporunu ve maliyeti koyar. Bölümler birden fazla dilde üretilebilir.

Anahtar olmadan da denenebilir: bir kullanıcının anahtarını girmediği her servis o kullanıcı için **deneme modunda**
(mock) çalışır ve ffmpeg ile sahte ses, görsel ve video üretir. Yönetici **demo modunu** açarsa Anthropic anahtarı
olmayan kullanıcılar sohbeti önceden yazılmış **sahte yönetmenle** deneyebilir.

## Üretim hattı

| # | Adım | Sağlayıcı | Dile bağlı mı | Ne yapar |
|---|------|-----------|---------------|----------|
| 1 | Sesler | ElevenLabs | Evet | Replik ve anlatıcı metinlerinden, seçili dilde ses. Karakterin o dildeki sesi otomatik seçilir. |
| 2 | Görseller | Kling görsel | Hayır (ortak) | Karakter ana görselleri ve sahne görselleri. Onaylı ana görsel referans olarak gider. |
| 3 | Konuşan sahneler | HeyGen | Evet | Onaylı görsel + o dilin onaylı sesi → konuşan video. |
| 4 | Konuşmasız sahneler | Kling video | Hayır (ortak) | Sahne görseli + video prompt → video. "Sesli an" işaretliyse yerel ses açık. |
| 5 | Kurgu ve miksaj | ffmpeg | Evet | Sıralama, biçim eşitleme, geçişler, müzik/efekt, ducking, -14 LUFS, o dilin altyazısı, 1080p + dikey 9:16. |
| 6 | Kontrol | ffprobe/ffmpeg | Evet | Sahneye bağlı, zaman kodlu kalite raporu; dil uyumu ve dudak senkronu denetimi dahil. |
| 7 | Düzeltme | Kling Omni / dudak senkronu | Sahneye göre | Sahne bazında düzeltme; yalnızca o sahne, dil ve kurgu yeniden işlenir. |

Kurallar (kodda zorlanır):

- Bir adım, bağımlı olduğu varlıklar onaylanmadan başlamaz. Onayı kullanıcı da ajan da verebilir; varlıkta kimin
  onayladığı kayıtlıdır.
- Konuşan sahne yalnızca HeyGen ile ve her zaman **o dilin sesinden** üretilir. Bir dilin videosu başka dilin sesiyle
  eşleştirilemez; kurgu adımı bunu ayrıca doğrular. Konuşan sahne Kling video adımına hiç gönderilmez.
- Konuşan sahne görsellerine dudak senkronu şartları (yüz kameraya dönük, ağız net ve kapalı, ağzı kapatan nesne yok,
  yüz karede büyük) otomatik eklenir.
- Replik ya da ses değişirse o dilin sesi ve konuşan videosu "güncel değil" olur; "Eksikleri üret" bunları da yeniler
  ve eski sesle kurgu yapılmaz.
- Her yeniden üretim yeni **sürüm** oluşturur, eskisi silinmez. Sahne ve dil başına "seçili sürüm" tutulur.
- Her adımda **Eksikleri üret**, **Tümünü üret** ve tek sahne için **Üret / Yeniden üret** vardır; seçili dil ya da
  tüm diller için çalışır.

## Kurulum

Gerekenler: [uv](https://docs.astral.sh/uv/) (Python 3.11+ sürümünü kendisi kurar), Node.js 20+, ffmpeg ve ffprobe.

```bash
# Windows
winget install astral-sh.uv
winget install Gyan.FFmpeg
# macOS
brew install uv ffmpeg node
```

### Sistem anahtarlarını üretme (zorunlu)

Uygulama iki sistem anahtarı olmadan açılmaz ve eksikse hangisinin eksik olduğunu söyleyen bir hatayla durur:

| Değişken | Ne için |
|----------|---------|
| `APP_SECRET` | Oturum belirteçleri ve tek kullanımlık bağlantıların imzası |
| `ENCRYPTION_KEY` | Kullanıcıların servis anahtarlarını veritabanında şifreleyen ana anahtar (Fernet) |

```bash
make init                                   # ya da: cd backend && uv run python -m app.init_env
```

Komut değerleri üretip proje kökündeki `.env` dosyasına yazar; var olan değerlere dokunmaz. Windows'ta `baslat.cmd`
bunu kendisi yapar. **`.env` dosyasını yedekleyin:** `ENCRYPTION_KEY` kaybolursa ya da değişirse kayıtlı servis
anahtarları çözülemez ve kullanıcıların anahtarlarını yeniden girmesi gerekir.

### Yerel kurulum

| Yol | Komut | Adres |
|-----|-------|-------|
| Windows, çift tıklama | `baslat.cmd` | http://127.0.0.1:8765 |
| Geliştirme | `make dev` (Windows'ta `make` yoksa `./dev.ps1`) | http://127.0.0.1:5273 |
| Tek süreç | `make start` | http://127.0.0.1:8765 |
| Docker | `make docker` (= `make init` + `docker compose up --build`) | http://127.0.0.1:8765 |

İlk açılışta veritabanı kurulur (varsayılan SQLite, `data/studyo.db`) ve `SEED_DEMO=true` ise bir **demo kullanıcı**
ile örnek bölüm eklenir (2 karakter + anlatıcı, 5 sahne, kaynak dil Türkçe, ikinci dil İngilizce). Demo kullanıcının
e-postası `demo@studyo.local`, rastgele üretilen şifresi `data/demo_sifre.txt` dosyasındadır.

### İlk yöneticiyi oluşturma

Uygulamayı açın: henüz yönetici yokken giriş ekranı kayıt ekranına yönlenir. **İlk kayıt olan kullanıcı yönetici
olur.** Sonraki kayıtlar varsayılan olarak yalnızca davetle yapılır: Yönetim → *Davet oluştur* tek kullanımlık bir
bağlantı üretir (SMTP ayarlıysa e-postayla da gönderilir). Yönetici kayıt modunu "herkese açık" yapabilir.

Tek kullanıcılı eski sürümden yükseltiyorsanız veritabanı kendiliğinden taşınır ve eski projeler, sohbetler ve
dosyalar ilk yöneticiye devredilir. Eski sürümde kaydedilmiş servis anahtarları taşınmaz; sihirbazda yeniden girilir.

### Sunucuya kurulum (HTTPS)

Uygulama internete açılacaksa HTTPS arkasında çalıştırın. Depoda Caddy (otomatik Let's Encrypt sertifikası) ve
PostgreSQL içeren hazır bir düzen vardır:

1. Alan adınızın DNS kaydını sunucuya yönlendirin; 80 ve 443 portlarını açın.
2. `make init` ile `.env` dosyasını üretin ve şunları ekleyin:

   ```ini
   PUBLIC_BASE_URL=https://studyo.example.com
   SITE_DOMAIN=studyo.example.com
   POSTGRES_PASSWORD=<güçlü bir şifre>
   DATABASE_URL=postgresql://studyo:<aynı şifre>@postgres:5432/studyo
   SEED_DEMO=false
   ```
3. `make docker-https` (= `docker compose --profile https --profile postgres up --build -d`).
4. Tarayıcıda adresi açıp ilk yönetici hesabını **hemen** oluşturun (ilk kayıt yönetici olur).

`PUBLIC_BASE_URL` https ile başladığında: oturum çerezi `Secure` olur, HSTS başlığı eklenir, CORS ve `Host` denetimi
yalnızca bu alan adına izin verir, davet/sıfırlama bağlantıları bu adresle üretilir, API belgeleri (`/docs`) kapanır
ve sağlayıcı **webhook**'ları etkinleşir (aşağıda). Kendi vekil sunucunuzu (nginx vb.) kullanacaksanız uygulamayı
`uvicorn app.main:app --host 127.0.0.1 --port 8765 --proxy-headers` ile başlatın, `/api/events` için arabelleği
kapatın ve yükleme sınırını en az 600 MB yapın (örnek: `deploy/Caddyfile`).

İsteğe bağlı sistem ayarları (hepsi `.env`, açıklamalar `.env.example` içinde):

| Ayar | Etki |
|------|------|
| `DATABASE_URL` | Boşsa SQLite. `postgresql://...` verilirse PostgreSQL (çok kullanıcılı sunucuda önerilir). |
| `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_ENDPOINT`, `S3_REGION` | S3 uyumlu depo (AWS S3, Cloudflare R2, MinIO). Dosyalar kovada kalıcı durur; yerel disk çalışma kopyasıdır. Kova **herkese açık olmamalıdır**. |
| `SMTP_HOST`, `SMTP_FROM`, ... | Şifre sıfırlama, e-posta doğrulama, davet ve bildirim e-postaları. Yoksa yönetici tek kullanımlık bağlantı üretir. |
| `DEMO_MODE` | Anthropic anahtarı olmayan kullanıcıya sahte yönetmen (yönetici panelinden de açılır). |
| `SEED_DEMO` | Demo kullanıcı ve örnek bölüm. Sunucuda `false` yapın. |

Testler: `make test` (ya da `cd backend && uv run pytest -q`). Yaklaşık 15 dakika sürer; gerçek ffmpeg çalıştırır.
PostgreSQL üzerinde çalıştırmak için `TEST_DATABASE_URL=postgresql://...` verin.

## Kullanıcılar, bağlantılar ve yalıtım

- **Giriş:** e-posta + şifre (argon2). Oturum, sunucuda tutulan ve httpOnly / SameSite=Lax (https'te Secure) bir
  çerezle taşınan rastgele belirteçtir; veri değiştiren her istekte ayrıca CSRF belirteci istenir. 5 hatalı denemede
  hesap 15 dakika kilitlenir; giriş, kayıt ve sıfırlama uçlarında IP başına deneme sınırı vardır.
- **Kurulum sihirbazı** ilk girişten sonra zorunludur; sonradan *Ayarlar → Bağlantılar*'dan değiştirilir. Adımlar:
  Anthropic (zorunlu), ElevenLabs, Kling, HeyGen, Tercihler, Özet. Her adımda servisin ne işe yaradığı, anahtarın
  nereden alınacağı, giriş alanları ve **Bağlantıyı test et** düğmesi vardır. Test, servise gerçek ama ücretsiz bir
  hesap sorgusu gönderir ve sonucu (bağlı / hatalı), kalan krediyi, ElevenLabs'te ses kütüphanesini, Kling'de
  erişilebilen özellikleri, HeyGen'de avatar motorlarını gösterir. Anthropic dışındaki adımlar atlanabilir; atlanan
  servis o kullanıcı için **deneme modunda** çalışır ve arayüzde böyle etiketlenir.
- **Anahtarlar** veritabanında `ENCRYPTION_KEY` ile şifreli saklanır; kaydedildikten sonra arayüzde yalnızca son 4
  karakteri görünür, değiştirilebilir ve silinebilir. Tarayıcıya, günlüklere, hata iletilerine ve Claude'a giden
  içeriğe düşmez. **Yönetici dahil kimse başkasının anahtarını göremez.** Servislerin e-posta/şifresi hiçbir zaman
  istenmez; yalnızca resmi API anahtarları kullanılır.
- **Ortak (kurum) anahtarı:** yönetici her servis için bir ortak anahtar tanımlayabilir. Kendi anahtarı olmayan
  kullanıcı bunu kullanır; harcaması kullanıcı bazında izlenir ve aylık sınırı (varsayılan 50 $, kullanıcı başına
  değiştirilebilir) aşınca yeni işi başlamaz.
- **Yalıtım:** projeler, karakterler, varlıklar, işler, sohbetler, bağlantılar ve maliyetler kullanıcıya aittir. Her
  sorgu ve dosya isteğinde sahiplik denetlenir; başkasının kaydı **404** döner. Medya dosyaları herkese açık adreste
  durmaz: `/media/...` yalnızca oturumu açık sahibine verilir. Her arka plan işi ve ajan çalışması sahibinin
  anahtarıyla, bütçesiyle ve eşzamanlılık sınırıyla yürür; ajanın araçlarında kullanıcı kimliği araç girdisinden
  değil sunucudaki sohbet kaydından alınır.
- **Anahtar üretim sırasında geçersiz çıkarsa ya da kredisi biterse** iş "hata" olmaz, **bağlantı bekliyor** durumuna
  geçer; kullanıcıya sohbette ve bildirimle hangi bağlantıyı yenileyeceği söylenir. Anahtar yenilenip test edilince
  işler ve ajan kaldığı yerden devam eder (sağlayıcıya gönderilmiş iş yeniden gönderilmez).
- **Adil sıra:** eşzamanlılık sınırı kullanıcı + sağlayıcı başınadır (Tercihler), sağlayıcı başına genel bir tavan
  vardır ve kuyruk kullanıcılar arasında sırayla dağıtılır; bir kullanıcının yoğun işi diğerlerini bekletmez.
- **Hesap silme** (Ayarlar → Hesap): kullanıcının anahtarları, projeleri, sohbetleri, işleri ve bütün dosyaları
  (yerelde ve S3'te) silinir.
- **Yönetici paneli:** kullanıcılar ve davetler, devre dışı bırakma, şifre sıfırlama bağlantısı, kayıt modu, sistem
  anahtarlarının durumu (yalnızca tanımlı olup olmadığı), ortak anahtarlar ve kullanıcı bazlı sınırlar, toplam
  kullanım, demo/bakım modu ve denetim günlüğü.

### Servis anahtarları nereden alınır

| Servis | Nereden | Sihirbazda istenen |
|--------|---------|--------------------|
| Anthropic (Claude) | console.anthropic.com → API Keys | API anahtarı |
| ElevenLabs | elevenlabs.io → Developers → API Keys | API anahtarı (ses ve kullanıcı okuma izniyle) |
| Kling | kling.ai/dev/api-key (yalnızca bir kez gösterilir) | API anahtarı + **isteğe bağlı Webhook Secret** (`whsec_...`) |
| HeyGen | HeyGen → Settings → API | API anahtarı (kapsamlar: `account:read`, `assets:write`, `videos:write`, `videos:read`) |

Güncel dokümanlara göre Kling'in bütün uçları (eski dudak senkronu uçları dahil) tek API anahtarıyla çalışır; eski
"Access Key + Secret Key" çifti gerekmez. Dokümandan çıkan tek ek alan **Webhook Secret**'tır: Kling konsolunda
oluşturulursa geri çağrıların imzası doğrulanır. Anthropic'te kalan bakiye normal API anahtarıyla sorgulanamaz;
sihirbaz bunu söyler ve erişilebilen modelleri gösterir.

### Deneme modundan gerçek moda geçiş

1. *Ayarlar → Bağlantılar*'da ilgili servisin anahtarını kaydedin; "Bağlı" görünmeli. Anahtarı olan servis gerçek
   çalışır, olmayan deneme modunda kalır (karışık çalışabilir). Yönetim'deki "Bakım modu" kapalı olmalı.
2. Anthropic anahtarı girildikten sonra **yeni açılan** sohbetler Claude ile çalışır. Bir sohbetin modu, modeli,
   sistem istemi ve araç listesi sohbet açılırken sabitlenir.
3. Karakterlere ElevenLabs ses kimliği girin ya da bunu sohbette ajana bırakın (ses kütüphanesini listeleyip atar).
4. İlk gerçek denemede bütçe sınırını düşük tutun ve "planı onayla" anahtarını açın; tahmini maliyeti görürsünüz.

### Webhook'lar

`PUBLIC_BASE_URL` https ise Kling ve HeyGen işlerine geri çağrı adresi eklenir (`/api/webhooks/<tahmin edilemez
belirteç>`). Geri çağrının içeriğine güvenilmez: yalnızca bekleyen yoklamayı hemen tetikler, sonuç yine sağlayıcıdan
kullanıcının anahtarıyla sorgulanır. Kullanıcı Kling Webhook Secret tanımladıysa imzasız ya da hatalı imzalı çağrı yok
sayılır. Adres ayarlı değilse ya da geri çağrı gelmezse düzenli yoklama (polling) sürer. ElevenLabs ses üretimi
eşzamanlı olduğundan webhook kullanmaz.

## Sohbetten kullanım

Açılış ekranı sohbettir. Solda sohbetler, ortada yönetmen, sağda canlı üretim paneli (7 aşama, sahne küçük
resimleri, ilerleme, anlık maliyet) durur. Ajanın her araç çağrısı sohbette katlanabilir bir satırdır; üretilen ses,
görsel ve videolar sohbet içinde oynatılır.

Örnek mesajlar:

- `Terk edilmiş bir deniz fenerinin sırrını anlatan 5 sahnelik kısa bir bölüm yap`
- Senaryo dosyası (metin/CSV/JSON) ekleyip: `Ekteki senaryoyu çek, İngilizce sürümünü de çıkar`
- Üretim sürerken: `3. sahnede kadın gülsün`, `anlatıcının sesini değiştir`, `durdur`

Ajan arka planda çalışır: sekmeyi kapatsanız da sürer, uygulama yeniden başlarsa kaldığı yerden devam eder. Sohbete
referans görsel, müzik ve efekt dosyası da eklenebilir. Elle kontrol ekranlarında (Projeler, bölüm, sahne ayrıntısı)
yaptığınız değişiklikleri ajan bir sonraki mesajınızda görür.

**Harcama güvenliği** (Ayarlar → Tercihler, kullanıcıya özel):

- *Çalışma başına bütçe sınırı* (varsayılan 25 $): sağlayıcı işlerinin tahmini maliyeti + Claude API maliyeti.
  Sınırı aşacak iş kuyruğa alınmaz; sınıra yaklaşınca ajan durur ve sohbette bütçeyi artırmanızı ister.
- *Üretimden önce planı onayla* (varsayılan kapalı): açıkken ajan sahne listesini ve tahmini maliyeti gösterip tek
  bir onay bekler; kapalıyken sormadan ilerler.
- Sahne başına en fazla 3 yeniden deneme (sonra sahne "elle bakılmalı" olur), en fazla 2 düzeltme turu.

Ajan işe başlarken `baglanti_durumu` aracıyla hangi servislerin bağlı olduğuna bakar; deneme modunda kalacak
adımları söyler ve kurulum sayfasına yönlendirir. Yalnızca tanımlı araçları çağırabilir; kabuk komutu, serbest dosya
erişimi ya da rastgele ağ isteği aracı yoktur ve yalnızca sohbet sahibinin verisine, sohbetin kendi projesine dokunabilir. İçe aktarılan senaryo metni veri olarak ele
alınır, talimat olarak değil.

## Dil desteği

- Her projenin varsayılan dili vardır; bölüm ve sahne düzeyinde değiştirilebilir. Kodlar BCP-47 (`tr`, `en`, `es-MX`).
- Kullanıcı açıkça dil seçtiyse o geçerlidir. Seçmediyse metnin dili algılanır (lingua); metin kısaysa ya da güven
  düşükse proje/bölüm dili kullanılır. Algılama yalnızca öneri olarak gösterilir.
- Bir bölüm birden fazla hedef dilde üretilebilir. Replikler dil başına ayrı tutulur (kaynak + çeviriler). Elle modda
  çeviri onaylanmadan ses üretilmez; otomatik akışta ajan çeviriyi kendisi yapar ve onaylar.
- Yeni dil eklenince yalnızca dile bağlı varlıklar üretilir: sesler, konuşan sahne videoları, altyazı ve kurgu.
  Karakter görselleri ve konuşmasız sahneler diller arasında ortaktır.
- Karakter başına dil bazında ayrı ses kimliği tanımlanabilir; tanımlı değilse varsayılan ses çok dilli modelle kullanılır.
- Proje ekranında dil bazlı **telaffuz sözlüğü** vardır (`yazılış = okunuş`); ses üretilmeden önce uygulanır.
- Çevrilen replik belirgin uzun ya da kısa çıkarsa sahne süresi o dilin ses süresine göre ayarlanır ve fark gösterilir.
- Altyazı her dil için ayrı üretilir; gömmede yazı sistemine uygun yazı tipi seçilir (Arapça, İbranice, CJK dahil).
- Görsel ve video promptları dilden bağımsızdır. Görselde okunacak yazı varsa "görseldeki metin" alanı dil başına
  doldurulur ve o sahne dile bağlı sayılır.
- Bir sahne bölümün dilinden farklı bir dile sabitlenirse her dil sürümünde o dilde kalır.

### Hangi dil hangi sağlayıcıda desteklenmiyor

`config/languages.yaml` içindeki 21 dil için (26 Kasım 2026 dokümanları):

| Sağlayıcı | Durum |
|-----------|-------|
| ElevenLabs `eleven_v4`, `eleven_v3` | 21 dilin tamamı destekleniyor. |
| ElevenLabs `eleven_multilingual_v2`, `eleven_flash_v2_5` | **İbranice (he) ve Farsça (fa) desteklenmiyor.** Bu modeller seçiliyken o dillerde `eleven_v4` kullanılır ve arayüz bunu üretimden önce bildirir. |
| HeyGen (avatar) | API'de dil parametresi yok; ses dosyasından çalışır. Desteklenen dil listesi **belgelenmemiş**: hiçbir dil engellenmez, kalite doğrulanmadı. |
| Kling dudak senkronu | Dil parametresi yok, dil listesi **belgelenmemiş**: aynı durum. |
| Kling görsel / video | Dilden bağımsız. |

Bir sağlayıcı seçilen dili desteklemiyorsa üretim başlamadan uyarı verilir; sessizce başka dile geçilmez.
ElevenLabs'e taban dil kodu (ISO 639-1) gönderilir; `es-MX` gibi bölgeli kodların kabul edilip edilmediği
doğrulanamadı, bölge yalnızca ses seçimiyle karşılanır.

### Yeni dil ekleme

`config/languages.yaml` dosyasına bir satır ekleyin:

```yaml
- {code: az, name: Azerbaycanca, script: latin, rtl: false, elevenlabs: [eleven_v4, eleven_v3]}
```

`script` altyazı yazı tipini belirler (aynı dosyadaki `fonts` bölümü); `elevenlabs` o dili destekleyen modellerdir.
Uygulamayı yeniden başlatın; dil seçicilerde ve otomatik algılamada görünür.



## Modeller ve fiyatlar

Model kimlikleri kodda değil, [config/models.yaml](config/models.yaml) dosyasındadır.

| Hedef | API'deki karşılığı (26 Kasım 2026) | Varsayılan |
|-------|-------------------------------------|------------|
| Claude (en yetenekli güncel model) | `claude-fable-5-2`; ayrıca `claude-opus-5-5`, `claude-sonnet-5-5` | `claude-fable-5-2` |
| Eleven v4 | `eleven_v4` mevcut | `eleven_v4` |
| Kling görsel 3.0/4.0 | `kling-4.0` | `kling-4.0` |
| Kling video 3.0/4.0 | `kling-3.0`, `kling-4.0` ve diğerleri | `kling-3.0` |
| Kling Omni | `kling-3.0-omni`, `kling-4.0`, `kling-o1` | `kling-3.0-omni` |
| HeyGen Avatar 6/Pro | `heygen-avatar-6`; "Avatar Pro" adında motor yok | `heygen-avatar-6` |

Kling videoda varsayılanın 3.0 olmasının nedeni: 4.0'da ses her zaman açık ve kapatılamıyor. 3.0'da "sesli an"
işaretine göre `audio: native / off` gönderilebiliyor.

Claude tarafı (yönetmen): resmi Anthropic Python SDK'sı, Messages API ve araç kullanımı döngüsü. Akan yanıt (streaming),
istem önbelleği (prompt caching), güvenlik reddinde sunucu tarafı yedek model ve eski araç sonuçlarının sunucu
tarafında temizlenmesi açıktır; hepsi `config/models.yaml > claude` altından kapatılabilir.

## Genişletme

**Yeni sağlayıcı:** `backend/app/providers/` altında `base.Provider`'dan türeyen bir sınıf yazın (`submit`, `poll`,
`fetch`, isteğe bağlı `estimate_cost`), `providers/registry.py > CLASSES`'a ekleyin, `config/models.yaml`'a bölümünü
koyun; yeni servisse `connections.py` içine alanlarını (`FIELDS`), tanıtımını (`INFO`), ücretsiz bir bağlantı
testini (`TESTS`) ve `settings_store.py > SECRET_FIELDS` satırını ekleyin (sihirbazda ve Bağlantılar'da kendiliğinden
görünür) ve `pipeline.py`'deki ilgili iş kurucuda
`provider="..."` yapın. Mock ve "elle adım" modları kendiliğinden gelir.

**Yeni ajan aracı:**

1. İş kuralını `services.py` içine bir işlev olarak yazın (arayüz ucu da aynı işlevi çağırsın; iki ayrı kod yolu olmasın).
2. `agent/tools.py > definitions()` içine araç tanımını (ad, açıklama, JSON şeması) ekleyin.
3. Aynı dosyada `t_<ad>(c, i)` işleyicisini yazıp `SYNC` sözlüğüne ekleyin. `c.project()`, `c.episode()`, `c.asset()`
   gibi yardımcılar yetki denetimini yapar; kullanıcı kimliğini araç girdisine koymayın, bağlamdan gelir.
4. Gerekiyorsa `prompts/yonetmen.md` içinde aracın ne zaman kullanılacağını anlatın.

Araç listesi ve sistem istemi sohbet açılırken sohbete kopyalanır; değişiklik yalnızca yeni sohbetlerde geçerli olur.

**Sistem istemi:** [prompts/yonetmen.md](prompts/yonetmen.md). Üretim sırası, dudak senkronu ve dil kuralları, maliyet
bilinci ve ne zaman kullanıcıya sorulacağı orada yazar.

## Güvenlik ve dayanıklılık

- Bütün uçlar oturum ister (giriş, kayıt, şifre sıfırlama ve sağlayıcı webhook'u dışında). Girdiler şemayla
  doğrulanır; yüklemelerde tür ve boyut sınırı vardır (metin 2 MB, görsel 25 MB, ses 60 MB, video 600 MB) ve yüklenen
  dosya ffprobe ile doğrulanır.
- Güvenlik başlıkları (CSP, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, https'te HSTS), yalnızca kendi alan
  adına CORS, `Host` başlığı denetimi ve IP başına genel hız sınırı.
- Dosya yolları depo kökü dışına çıkamaz; medya ucu yalnızca kullanıcının kendi klasörünü (`u<kimlik>/`) sunar.
- Gizli değerler günlüğe yazılmaz; hata iletilerinde maskelenir ve günlük süzgeci anahtar biçimlerini ayrıca siler.
- Ajan yalnızca tanımlı araçları çağırabilir; kabuk komutu, serbest dosya erişimi ya da rastgele ağ isteği aracı
  yoktur. İçe aktarılan senaryo metni veri olarak ele alınır, talimat olarak değil.
- İş kuyruğu ve ajan döngüsü süreç içi asyncio'dur, durum veritabanındadır. Yeniden başlatmada gönderilmiş işler
  yeniden gönderilmez (yalnızca sorgulanır), yarım ajan çalışması sohbet geçmişinden devam eder.
- Hız sınırı, zaman aşımı ve 5xx hatalarında üstel bekleme ile yeniden denenir; kalıcı hatada iş "hata" olur.
- İndirilen her dosya doğrulanır (boyut > 0, ffprobe okuyor); bozuksa iş hata sayılır.
- Önemli işlemler (giriş, hatalı giriş, kilitlenme, davet, bağlantı kaydetme/silme, yönetici işlemleri, hesap silme)
  denetim günlüğüne yazılır; günlükte anahtar değeri bulunmaz.

## Doğrulama durumu

**Gerçek API ile doğrulanan: hiçbiri.** Geliştirme sırasında hiçbir servisin (Anthropic dahil) anahtarı yoktu; hiçbir
ücretli istek gönderilmedi. Adaptörler, bağlantı testleri ve Claude bağlantısı 26 Kasım 2026'da okunan resmi
dokümanlara göre yazıldı. İlk gerçek çağrılarda alan adı ya da biçim uyuşmazlığı çıkabilir; hata iletisi sohbette,
bağlantı kartında ve İşler ekranında görünür.

| Parça | Nasıl test edildi |
|-------|-------------------|
| Kayıt, giriş, oturum, CSRF, kilitlenme, davet, şifre sıfırlama, yönetici paneli | Uçtan uca (gerçek HTTP katmanı, SQLite ve PostgreSQL) |
| Kurulum sihirbazı ve "Bağlantıyı test et" (doğru/yanlış anahtar) | Sağlayıcı yanıtları taklit edilerek; **gerçek servise hiç bağlanılmadı** |
| Yalıtım: iki kullanıcı aynı anda üretim, her iş kendi anahtarıyla, çapraz erişim 404 | Uçtan uca (proje, bölüm, sahne, varlık, dosya, iş, sohbet, bağlantı) |
| Anahtar geçersiz/kredisiz → durma → yenileyince devam; ortak anahtar ve aylık sınır; adil sıra | Sahte HTTP katmanıyla |
| ElevenLabs, Kling (görsel, video, Omni, dudak), HeyGen adaptörleri | İstek biçimi, dil parametresi, hata ve yeniden deneme: sahte HTTP katmanıyla |
| Claude bağlantısı | Yalnızca istek parametreleri ve maliyet hesabı birim testli; **hiç gerçek model çağrısı yapılmadı** |
| Ajan döngüsü, araçlar, yetki, bütçe, plan onayı, durdurma, yeniden başlatmada devam | Sahte yönetmenle (aynı döngü, aynı araçlar) |
| Sohbetten uçtan uca akış; çok dilli hat (tr + en) | Sahte yönetmen + deneme modundaki sağlayıcılarla: tek mesaj → mp4, altyazı, kalite raporu, maliyet |
| Kurgu, miksaj, altyazı, kalite raporu, dudak senkronu ölçümü | Gerçek ffmpeg ile (girdiler deneme üretimi) |
| S3 uyumlu depo | Sahte S3 istemcisiyle (yükleme, geri getirme, imzalı adres, silme); **gerçek kovayla denenmedi** |
| Webhook | Uç ve imza doğrulaması testli; **gerçek sağlayıcı geri çağrısıyla denenmedi** |
| PostgreSQL | Geçişler ve test takımı gerçek PostgreSQL 17 üzerinde çalıştırıldı |
| E-posta (SMTP), Caddy/HTTPS düzeni | **Denenmedi** (yapılandırma dokümana göre yazıldı) |

Sahte yönetmen metin anlamaz: fikri hazır bir örnek senaryoya (ya da ekteki senaryo dosyasına) çevirir ve yalnızca
birkaç basit müdahaleyi tanır ("İngilizce sürümü", "N. sahne ..."). Claude'un gerçek davranışı (prompt kalitesi,
görsel inceleme, çeviri) denenmedi.

**"Elle adım" olarak kalan:** kalıcı olarak elle olan özellik yok. Omni ve dudak senkronu resmi API'de var; ancak
Kling bu ikisinde kaynak videoyu yalnızca internet adresiyle kabul ediyor ve dosya yükleme ucu sunmuyor. Videonun
sağlayıcıdaki adresi yeniyse (varsayılan 72 saat) ya da S3 deposu etkinse (kısa ömürlü imzalı adres verilir) düzeltme
API ile yapılır; değilse iş kendiliğinden elle adım olur: ekranda talimat, indirme bağlantıları ve yükleme alanı
çıkar, ajan da sohbette bunu söyleyip diğer işlere devam eder.

## Varsayımlar

- **Çok kullanıcılı sürümde:** oturum için JWT yerine sunucuda saklanan rastgele belirteç seçildi (çıkışta ve şifre
  değişiminde anında geçersiz kılınabilsin diye). Servis OAuth'u kullanılmadı: dört servisin de resmi yolu API
  anahtarıdır. Şifre kuralı yalnızca uzunluktur (en az 10 karakter).
- Yöneticinin "başkasının verisini görememesi" içerik için de geçerlidir: yönetici paneli kullanıcıların proje ve
  dosyalarını açamaz; yalnızca e-posta, durum, bağlı servis adları ve harcama tutarlarını görür.
- Kling'de hangi özelliğe erişildiğini soran bir uç yoktur. Sihirbaz, ücretsiz hesap sorgusundaki kaynak paketi
  adlarından **tahmin** eder ("paket var / belirsiz / paket yok") ve bunu açıkça belirtir; ücretli olabilecek deneme
  isteği göndermez. Kesin sonuç ilk gerçek çağrıda belli olur.
- Çıktı biçimi tercihi (16:9 / 9:16) yeni bölümlerin varsayılanını belirler: 9:16 seçiliyse yatay kurgunun yanında
  dikey sürüm de üretilir. Kaynak görüntüler her iki durumda da yatay üretilir.
- Demo kullanıcı yalnızca yerel denemeler içindir (`SEED_DEMO`); yönetici değildir.
- Hız sınırı ve canlı olay yolu bellek içidir: uygulama **tek süreç** olarak çalıştırılmalıdır (birden fazla
  uvicorn işçisi kullanmayın).
- İki ayrı istek metninden ikincisi (sohbet merkezli) esas alındı. Çeviri ayrı bir sağlayıcı değil, Claude ile
  yapılır: otomatik akışta ajan çevirir; elle ekranda "Çeviri öner" düğmesi Claude'a sorar, anahtar yoksa alan elle
  doldurulur.
- **Sohbet geçmişi yalnızca eklenir.** Varsayılan modelde geçmişi sonradan düzenlemek (eski araç sonuçlarını silmek ya
  da özetlemek) modelin düşünme bloklarını geçersiz kılıyor. Bu yüzden "eski araç sonuçlarını özetle" isteği sunucu
  tarafı bağlam temizliğiyle karşılandı; istemci hiçbir eski mesajı değiştirmez, sistem istemi ve araç listesi sohbet
  başında dondurulur. Araç sonuçları da baştan kısa tutulur.
- Bütçe, mock işlerin tahmini maliyetini de sayar; böylece bütçe davranışı anahtarsız da denenebilir. Mock işlerde
  gerçekte ücret kesilmez.
- **Dil uyumu denetimi** üst veriyle yapılır (varlığın dili, sağlayıcıya gönderilen dil kodu, metnin algılanan dili).
  Yerelde konuşma tanıma yoktur; ses içeriği dinlenerek doğrulanmaz.
- **Dudak senkronu ölçümü** hafif bir yöntemdir: ses zarfı ile karenin alt-orta bölgesindeki (ağız varsayımı) hareket
  ve parlaklık değişimi farklı gecikmelerde ilişkilendirilir; ayrıca ses dosyası ile videodaki sesin başlangıç farkına
  bakılır. Yüz tanıma yoktur. Ölçüm güveni düşükse bulgu yalnızca bilgi notu olur. Eşik 120 ms (`models.yaml > qc`).
- Anlatıcı, proje düzeyinde "yalnızca ses" bir karakterdir. Anlatıcı sahnesinin görüntüsü Kling video ile üretilir ve
  diller arasında ortaktır; süre en uzun dilin sesine göre verilir, kısa kalırsa son kare dondurulur.
- Konuşan sahnede HeyGen'e sahnenin onaylı sahne görseli gider. Birden fazla karakterli konuşan sahnede konuşan,
  listedeki ilk karakterdir.
- Onaylamak aynı zamanda o sürümü seçili yapar; seçili sürüm reddedilirse bir önceki onaylı sürüme dönülür.
- Telaffuz sözlüğü metin değiştirme olarak uygulanır (her modelde çalışır); isteğe bağlı olarak ElevenLabs sözlük
  kimliği de isteğe eklenir.
- Maliyetler liste fiyatlarıyla tahmindir. HeyGen saniye fiyatları resmi sayfadan doğrulanamadı; `models.yaml`'da
  "DOĞRULANMADI" notuyla durur. Claude maliyeti gerçek token kullanımından hesaplanır.
- Bağlantı noktaları başka yerel projelerle çakışmasın diye 8765 (backend) ve 5273 (Vite) seçildi.

## Bilinen kısıtlar

- Kling'e yerel görseller base64 olarak `url` alanında gönderilir; `data:` öneki gerekip gerekmediği doğrulanamadı
  (`models.yaml > base64_data_prefix`).
- Kling dudak senkronu: ses en çok 5 MB ve 2-60 sn. Omni: modele göre en çok 10-30 sn video. HeyGen: ses en çok 10 dk.
- Kurgu tek bir ffmpeg süzgeç grafiğiyle yapılır; onlarca sahneli bölümlerde bellek ve süre artar.
- Altyazı zamanlaması replik süresine orantılı bölünür; kelime düzeyinde hizalama yoktur. Sağdan sola yazıda yön
  libass tarafından metinden belirlenir; gömülü sonuç Arapça/İbranice gerçek metinle gözle doğrulanmadı.
- Dil algılama ilk kullanımda dil modellerini belleğe yükler (yüzlerce MB olabilir).
- Aynı anda tek çalışan ajan akışı sohbet başına bir tanedir; farklı sohbetler aynı anda çalışabilir ama kullanıcının
  sağlayıcı eşzamanlılık sınırını paylaşır.
- Tek süreçli tasarım: iş kuyruğu, hız sınırı ve canlı olaylar bellek içidir; yatay ölçekleme (birden çok sunucu)
  desteklenmez. Kurgu ve kalite kontrolü sunucunun işlemcisinde çalışır; çok kullanıcılı yoğun kullanımda darboğaz olur.
- İki adımlı doğrulama (2FA) ve servis OAuth'u yoktur. E-posta doğrulaması SMTP ayarlıysa gönderilir ama girişi engellemez.
- S3 etkinken dosyalar ayrıca yerel diskte çalışma kopyası olarak kalır; yerel kopyalar kendiliğinden temizlenmez.
- Kullanım kotası yalnızca ortak anahtar için vardır; kendi anahtarını kullanan kullanıcıyı yalnızca kendi bütçe sınırı durdurur.

## Dosya düzeni

```
backend/app/
  auth.py, api_auth.py  hesap, oturum, CSRF, sihirbaz, yönetici paneli, webhook uçları
  connections.py        kullanıcı bağlantıları (şifreli), bağlantı testleri, ortak anahtar ve aylık sınır
  tenancy.py            geçerli kullanıcı bağlamı (istek, iş ve ajan çalışması boyunca)
  crypto.py             anahtar şifreleme, maskeleme, belirteçler
  agent/                yönetmen ajanı: llm.py (Claude), tools.py, runner.py, mock_director.py
  providers/            adaptörler: elevenlabs, kling, heygen, mock, manual
  services.py           arayüzün ve ajanın ortak kullandığı işlemler
  pipeline.py           7 adım, dil ve bağımlılık kuralları, onay, otomatik mod
  worker.py             iş kuyruğu: adil sıra, yeniden deneme, bağlantı bekleme, yeniden başlatmada devam
  storage.py            yerel dosya düzeni + isteğe bağlı S3 uyumlu depo
  montage.py, qc.py     kurgu ve kalite raporu (ffmpeg)
prompts/yonetmen.md     ajanın sistem istemi
config/models.yaml      modeller, uç noktalar, fiyatlar, ajan sınırları
config/languages.yaml   diller, sağlayıcı desteği, altyazı yazı tipleri
deploy/Caddyfile        HTTPS vekil sunucu örneği
storage/u<kullanıcı>/<proje>/<bölüm>/<dil>/<sahne>/    dile bağlı varlıklar (ses, konuşan video)
storage/u<kullanıcı>/<proje>/<bölüm>/ortak/<sahne>/    dilden bağımsız varlıklar (görsel, konuşmasız video)
storage/u<kullanıcı>/<proje>/<bölüm>/<dil>/kurgu/      final_vN.mp4, altyazi_<dil>_vN.srt, kalite_raporu_<dil>_vN.json
```
