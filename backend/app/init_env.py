"""`make init` / `python -m app.init_env`: zorunlu sistem anahtarlarını üretip .env dosyasına yazar.
Var olan değerlere dokunmaz (ENCRYPTION_KEY değişirse kayıtlı servis anahtarları çözülemez)."""
from __future__ import annotations

import re
import secrets
import sys

from cryptography.fernet import Fernet

from .config import ROOT


def main() -> int:
    try:  # Windows konsolu Türkçe karakterde hata vermesin
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    env = ROOT / ".env"
    example = ROOT / ".env.example"
    text = env.read_text(encoding="utf-8") if env.exists() else (example.read_text(encoding="utf-8") if example.exists() else "")
    made = []
    for name, gen in (("APP_SECRET", lambda: secrets.token_urlsafe(48)), ("ENCRYPTION_KEY", lambda: Fernet.generate_key().decode())):
        m = re.search(rf"^{name}=(.*)$", text, flags=re.M)
        if m and m.group(1).strip():
            continue
        line = f"{name}={gen()}"
        text = re.sub(rf"^{name}=.*$", lambda _: line, text, flags=re.M) if m else (text.rstrip("\n") + "\n" + line + "\n").lstrip("\n")
        made.append(name)
    if made:
        env.write_text(text, encoding="utf-8")
        print("Üretildi ve .env dosyasına yazıldı: " + ", ".join(made))
        print("ÖNEMLİ: .env dosyasını yedekleyin; ENCRYPTION_KEY kaybolursa kayıtlı servis anahtarları çözülemez.")
    else:
        print("Sistem anahtarları zaten var; değişiklik yapılmadı.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
