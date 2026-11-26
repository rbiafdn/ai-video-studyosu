"""Claude bağlantısı (resmi Anthropic Python SDK'sı, Messages API + araç kullanımı).

Doküman notları (Kasım 2026):
- Varsayılan model claude-fable-5-2: düşünme her zaman açık; zorunlu tool_choice, prefill ve
  sampling parametreleri 400 döner. Derinlik output_config.effort ile ayarlanır.
- Bu modelde geçmişi istemcide düzenlemek (eski araç sonuçlarını silmek/özetlemek) sonraki düşünme
  bloklarını geçersiz kılar. Bu yüzden geçmiş YALNIZCA EKLENİR; sistem istemi ve araç listesi sohbet
  başında dondurulur; eski araç sonuçları sunucu tarafı bağlam düzenlemesiyle temizlenir.
- İstem önbelleği: cache_control işaretleri geçmişi bozmaz; sabit önek (araçlar + sistem + geçmiş)
  her turda önbellekten okunur.
- Güvenlik sınıflandırıcısı reddederse (stop_reason "refusal") sunucu tarafı yedek model devreye girer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .. import settings_store as store
from ..config import get_models


@dataclass
class LLMResult:
    content: list[dict]                 # içerik blokları (aynen saklanır ve aynen geri gönderilir)
    stop_reason: str
    usage: dict = field(default_factory=dict)
    model: str = ""
    refusal: str = ""


class LLMError(Exception):
    """Kalıcı Claude API hatası (geçici hataları SDK kendisi yeniden dener)."""

    def __init__(self, message: str, retryable: bool = False, auth_kind: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.auth_kind = auth_kind  # 'invalid' | 'credit': Anthropic bağlantısı yenilenmeli


def claude_cfg() -> dict:
    return get_models()["claude"]


def build_request(model: str, system: str, tools: list[dict], messages: list[dict]) -> dict:
    """Messages API isteğinin parametreleri (saf işlev: testlerde biçim doğrulanır)."""
    cfg = claude_cfg()
    kw: dict = {
        "model": model,
        "max_tokens": int(cfg.get("max_tokens", 64000)),
        # sistem istemi dondurulmuş metindir; önbellek kesme noktası
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
        # son bloğu otomatik önbelleğe al: her turda önceki geçmiş önbellekten okunur
        "cache_control": {"type": "ephemeral"},
        "output_config": {"effort": cfg.get("effort", "high")},
        "thinking": {"type": "adaptive", "display": cfg.get("thinking_display", "summarized")},
    }
    if tools:
        kw["tools"] = tools
    betas: list[str] = []
    if cfg.get("fallbacks") == "default":
        betas.append("server-side-fallback-2026-07-01")
        kw["fallbacks"] = "default"
    if cfg.get("context_editing") and tools:
        # eski araç sonuçlarını sunucu temizler; istemci geçmişi değişmez (yalnızca eklemeli kalır)
        betas.append("context-management-2025-06-27")
        kw["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
    kw["betas"] = betas
    return kw


def cost_usd(model: str, usage: dict) -> float:
    m = next((x for x in claude_cfg()["models"] if x["id"] == model), None) or claude_cfg()["models"][0]
    return round((usage.get("input_tokens", 0) * m["input"] + usage.get("output_tokens", 0) * m["output"]
                  + usage.get("cache_read_input_tokens", 0) * m["cache_read"]
                  + usage.get("cache_creation_input_tokens", 0) * m["cache_write"]) / 1_000_000, 6)


class ClaudeLLM:
    mode = "real"

    def __init__(self, api_key: str):
        import anthropic
        self._anthropic = anthropic
        # geçici hatalarda (429, 5xx, bağlantı) SDK üstel bekleme ile yeniden dener
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=4)

    async def complete(self, model: str, system: str, tools: list[dict], messages: list[dict],
                       on_text: Callable[[str], None] = lambda t: None) -> LLMResult:
        a = self._anthropic
        kw = build_request(model, system, tools, messages)
        last_err: Exception | None = None
        for _ in range(3):
            try:
                async with self.client.beta.messages.stream(**kw) as stream:
                    async for ev in stream:
                        if getattr(ev, "type", "") == "text":
                            on_text(getattr(ev, "text", "") or "")
                    msg = await stream.get_final_message()
                break
            except ValueError as e:  # araç girdisi JSON'u hiç çözülemedi: turu yeniden iste
                last_err = e
                continue
            except a.AuthenticationError as e:
                raise LLMError("Anthropic API anahtarı geçersiz.", auth_kind="invalid") from e
            except a.PermissionDeniedError as e:
                raise LLMError(f"Anthropic API bu isteğe izin vermiyor: {e.message}", auth_kind="invalid") from e
            except a.NotFoundError as e:
                raise LLMError(f"Model bulunamadı ({model}). config/models.yaml > claude bölümünü kontrol edin.") from e
            except a.BadRequestError as e:
                if "credit balance" in str(e.message).lower() or "spend limit" in str(e.message).lower():
                    raise LLMError("Anthropic hesabının kredisi bitmiş ya da harcama sınırına ulaşılmış.", auth_kind="credit") from e
                raise LLMError(f"Anthropic API isteği reddetti: {e.message}") from e
            except a.RateLimitError as e:
                if "spend_limit" in str(getattr(e, "body", "")):
                    raise LLMError("Anthropic aylık harcama sınırına ulaşıldı.", auth_kind="credit") from e
                raise LLMError("Anthropic hız sınırı aşıldı; biraz sonra 'Devam et' deyin.", retryable=True) from e
            except a.APIStatusError as e:
                if e.status_code == 402:
                    raise LLMError("Anthropic hesabının kredisi bitmiş.", auth_kind="credit") from e
                raise LLMError(f"Anthropic API hatası ({e.status_code}): {e.message}", retryable=e.status_code >= 500) from e
            except a.APIConnectionError as e:
                raise LLMError("Anthropic API'ye ulaşılamıyor (ağ hatası).", retryable=True) from e
        else:
            raise LLMError(f"Model yanıtı çözümlenemedi: {last_err}", retryable=True)
        u = msg.usage
        usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
        refusal = ""
        if msg.stop_reason == "refusal":
            sd = getattr(msg, "stop_details", None)
            refusal = (getattr(sd, "explanation", "") or getattr(sd, "category", "") or "güvenlik nedeniyle reddedildi") if sd else "reddedildi"
        return LLMResult(content=[b.model_dump(mode="json", exclude_none=True) for b in msg.content],
                         stop_reason=msg.stop_reason or "end_turn", usage=usage, model=msg.model or model, refusal=refusal)

    async def text(self, model: str, system: str, prompt: str) -> str:
        """Araçsız tek atımlık istek (arayüzdeki 'çeviri öner' düğmesi için)."""
        r = await self.complete(model, system, [], [{"role": "user", "content": prompt}])
        return "".join(b.get("text", "") for b in r.content if b.get("type") == "text")


def get_llm():
    """Anahtar varsa gerçek Claude; yoksa (ya da mock modu zorlanmışsa) önceden yazılmış sahte yönetmen."""
    key = store.get_secret("anthropic_api_key")
    if key and not store.sys_get("mock_mode"):
        return ClaudeLLM(key)
    from .mock_director import MockDirector
    return MockDirector()


def demo_allowed() -> bool:
    """Sahte yönetmen: yönetici demo modunu açtıysa ya da demo kullanıcısı için."""
    from .. import tenancy
    from ..db import session_scope
    from ..models import User
    if store.sys_get("demo_mode") or store.sys_get("mock_mode"):
        return True
    uid = tenancy.uid()
    if uid is None:
        return False
    with session_scope() as s:
        u = s.get(User, uid)
        return bool(u and u.is_demo)


def llm_mode() -> str:
    """real: kullanıcının (ya da kurumun) Anthropic anahtarı var · mock: demo modu · none: bağlantı gerekli"""
    if store.get_secret("anthropic_api_key") and not store.sys_get("mock_mode"):
        return "real"
    return "mock" if demo_allowed() else "none"
