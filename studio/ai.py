"""Talking to the AI service. You bring your own key, and choose the service and model in the dashboard
(Settings, AI) or in .env. What is saved from the dashboard is kept in ai.json in the data folder and wins.

    PEAKCUT_AI_PROVIDER   gemini | openai | xai | groq | openrouter | anthropic | ollama | custom
    PEAKCUT_AI_MODELS     one or more model ids, comma separated, tried in this order
    PEAKCUT_AI_KEY        your key (or PEAKCUT_AI_KEY_FILE, a file holding it)
    PEAKCUT_AI_BASE_URL   only for "custom" (any service that speaks the OpenAI chat format), or to override

Every service is asked for the same thing: JSON that follows a small schema. Gemini takes the schema directly.
The others are told the schema in the prompt and asked for a JSON object, which works with any chat model.

One picture can also be shown (`see`), which is how the webcam is looked for on a gaming stream. A model that
cannot look at pictures answers with something else, and that is simply taken as "it did not look"."""
import base64
import collections
import contextvars
import hashlib
import json
import math
import os
import re
import threading
import time

import httpx

from . import config

# name -> (label shown on the pages, address, how it is spoken to, where to get a key, an example model id)
PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemini-2.5-flash"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-2.5-flash"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", ""),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama"}
NO_KEY = {"ollama", "custom"}       # a model running on your own machine needs no key

# ---------------------------------------------------------------- the settings in use
_lock = threading.Lock()
_saved = {"at": None, "data": {}}
_rate_cv = threading.Condition()
_rate_events = collections.deque()  # [monotonic time, reserved tokens], shared by all video and live workers
_key_override = contextvars.ContextVar("peakcut_ai_key", default=None)
_key_state_lock = threading.Lock()
_key_state = {}  # key-pool hash -> index that most recently succeeded


def token_budget():
    """Configured local safety budget for Groq; None means no local cap is configured."""
    if provider() != "groq":
        return None
    try:
        budget = int(config.env("PEAKCUT_GROQ_TPM_BUDGET", "6800"))
    except (TypeError, ValueError):
        budget = 6800
    return budget if budget > 0 else None


def estimate_tokens(prompt: str, schema=None, image=False) -> int:
    chars = len(_wrapped(prompt, schema or {}).encode("utf-8"))
    return max(1, math.ceil(chars / 3.5) + (1800 if image else 1400))


def _reserve_tokens(prompt: str, schema=None, image=False):
    """Keep simultaneous Groq jobs inside the configured per-minute token budget.

    Groq's free limit for the GPT-OSS and Qwen models is 8K tokens/minute. Leave headroom for
    completion/reasoning tokens and requests made by the user's other apps. The limit can be raised
    for a plan with a higher allowance using PEAKCUT_GROQ_TPM_BUDGET.
    """
    budget = token_budget()
    if budget is None:
        return None
    estimate = estimate_tokens(prompt, schema, image)
    if estimate > budget:
        raise AIError(f"This request is about {estimate:,} tokens, above the configured Groq per-minute budget of {budget:,}. "
                      "Reduce the video section or raise PEAKCUT_GROQ_TPM_BUDGET if your Groq plan allows it.")
    with _rate_cv:
        while True:
            now = time.monotonic()
            while _rate_events and now - _rate_events[0][0] >= 60:
                _rate_events.popleft()
            used = sum(event[1] for event in _rate_events)
            if used + estimate <= budget:
                ticket = [now, estimate]
                _rate_events.append(ticket)
                return ticket
            wait = max(0.1, _rate_events[0][0] + 60 - now)
            _rate_cv.wait(timeout=wait)


def _settle_tokens(ticket, actual):
    if ticket is None or not actual:
        return
    with _rate_cv:
        now = time.monotonic()
        if any(event is ticket for event in _rate_events):
            ticket[1] = max(1, int(actual))
            _rate_cv.notify_all()


def _release_tokens(ticket):
    """Remove an estimate when the provider rejected a call before doing useful work."""
    if ticket is None:
        return
    with _rate_cv:
        for event in tuple(_rate_events):
            if event is ticket:
                _rate_events.remove(event)
                _rate_cv.notify_all()
                break


def _file():
    return config.DATA_DIR / "ai.json"


def _from_dashboard() -> dict:
    """What was saved from the dashboard, read again only when the file changes."""
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


def current() -> dict:
    """Provider, models, address and key: what the dashboard saved, or else what .env says."""
    d = _from_dashboard()
    p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    vision_model = (str(d.get("vision_model") or "").strip() if "vision_model" in d else
                    ("qwen/qwen3.8-27b" if ALIASES.get(p, p) == "groq" else ""))
    if d.get("provider"):
        raw_keys = d.get("keys") if isinstance(d.get("keys"), list) else [d.get("key", "")]
        api_keys = list(dict.fromkeys(str(v).strip()[:400] for v in raw_keys if str(v).strip()))[:100]
    else:
        env_key = config.ai_key()
        api_keys = [env_key] if env_key else []
    return {"provider": ALIASES.get(p, p),
            "models": list(d["models"]) if d.get("provider") else list(config.AI_MODELS),
            "vision_model": vision_model,
            "base_url": (str(d.get("base_url") or "") if d.get("provider") else config.AI_BASE_URL).rstrip("/"),
            "keys": api_keys, "key": api_keys[0] if api_keys else ""}


def provider() -> str:
    return current()["provider"]


def models() -> list:
    return current()["models"]


def model_candidates() -> list:
    """Keep the chosen model first, with a compatible Groq fallback for provider outages."""
    selected = models()
    if provider() == "groq":
        selected += [m for m in ("openai/gpt-oss-20b", "qwen/qwen3.8-27b") if m not in selected]
    return selected


def key() -> str:
    override = _key_override.get()
    return override if override is not None else current()["key"]


def keys() -> list[str]:
    return current()["keys"]


def redact(text) -> str:
    result = str(text)
    for saved_key in keys():
        result = result.replace(saved_key, "<key>")
    return result


def _rotatable(e) -> bool:
    status = getattr(e, "status_code", None)
    return status in (401, 403, 408, 429, 500, 502, 503, 504) or any(
        f"HTTP {code}" in str(e) for code in (401, 403, 408, 429, 500, 502, 503, 504))


def _with_key_rotation(fn):
    pool = current()["keys"] or [""]
    if len(pool) == 1:
        return fn()
    pool_id = hashlib.sha256("\0".join(pool).encode("utf-8")).hexdigest()
    with _key_state_lock:
        start = _key_state.get(pool_id, 0) % len(pool)
    last = None
    for offset in range(len(pool)):
        index = (start + offset) % len(pool)
        token = _key_override.set(pool[index])
        try:
            result = fn()
            with _key_state_lock:
                _key_state[pool_id] = index
            return result
        except Exception as e:  # noqa: BLE001
            last = e
            if not _rotatable(e) or offset == len(pool) - 1:
                raise
        finally:
            _key_override.reset(token)
    raise last


def label() -> str:
    return PROVIDERS.get(provider(), ("",))[0]


def base_url() -> str:
    c = current()
    return c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]


def ready() -> bool:
    c = current()
    return bool(c["provider"] in PROVIDERS and c["models"] and base_url() and (c["key"] or c["provider"] in NO_KEY))


def public() -> dict:
    """What the settings page may see. The key itself never leaves the server: only whether there is one, and
    its last four characters so the owner can tell which key it is."""
    c = current()
    hints = [v[-4:] for v in c["keys"] if len(v) >= 12]
    return {"provider": c["provider"], "models": c["models"], "vision_model": c["vision_model"], "base_url": c["base_url"], "has_key": bool(c["keys"]),
            "key_hint": c["key"][-4:] if len(c["key"]) >= 12 else "", "ready": ready(), "label": label(),
            "key_count": len(c["keys"]), "key_hints": hints,
            "saved_here": bool(_from_dashboard().get("provider")),
            "providers": [{"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
                          for k, v in PROVIDERS.items()]}


def save(provider_id: str, model_ids, address: str = "", new_key=None, vision_model="", api_keys=None,
         remove_keys=False) -> dict:
    """Save the choice made in the dashboard. `new_key` None keeps the key that is already there (the page never
    has it to send back); an empty string removes it."""
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:5]
    if not ms:
        raise ValueError("Enter the model to use.")
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")
    vision_model = str(vision_model or "").strip()[:120]
    if vision_model and not re.fullmatch(r"[\w.\-:/@]+", vision_model):
        raise ValueError("The vision model id has letters, digits and . - : / only.")
    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError("A custom service needs its address, for example http://127.0.0.1:1234/v1")
    old = current()
    key_pool = list(old["keys"]) if old["provider"] == p else []
    if remove_keys:
        key_pool = []
    elif api_keys:
        additions = api_keys if isinstance(api_keys, list) else str(api_keys).splitlines()
        additions = [str(v).strip()[:400] for v in additions if str(v).strip()][:100]
        key_pool = list(dict.fromkeys(key_pool + additions))[:100]
    elif new_key is not None:
        entered = str(new_key or "").strip()[:400]
        key_pool = [entered] if entered else []
    if not key_pool and p not in NO_KEY and not remove_keys:
        raise ValueError("Paste at least one key for this service.")
    with _lock:
        f = _file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"provider": p, "models": ms, "vision_model": vision_model,
                                   "base_url": address, "keys": key_pool,
                                   "key": key_pool[0] if key_pool else ""}), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)        # the key is only for whoever runs the server
        except OSError:
            pass
        tmp.replace(f)
        _saved["at"] = None
    return public()


def list_models(provider_id=None, address=None, with_key=None) -> list:
    """The model ids a service offers, so the settings page can show a list to pick from."""
    c = current()
    p = ALIASES.get(str(provider_id or c["provider"]).lower(), str(provider_id or c["provider"]).lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(with_key, list):
        k = next((str(v).strip() for v in with_key if str(v).strip()), "")
    else:
        k = next((v.strip() for v in str(with_key or "").splitlines() if v.strip()), "")
    k = k or (c["key"] if c["provider"] == p else "")
    base = str(address or "").strip().rstrip("/") or (c["base_url"] if c["provider"] == p else "") or PROVIDERS[p][1]
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {k}"} if k else {}, timeout=20)
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code} {r.text[:200].replace(k or chr(0), '<key>')}")
    data = r.json()
    if how == "gemini":
        found = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


def test() -> dict:
    """One tiny request with the settings in use, to prove the key, the address and the model all work."""
    ms = model_candidates()
    if not ready():
        raise ValueError("Save a service, a model and a key first.")
    last = None
    for model in ms:
        try:
            out, sent, wrote = call(model, 'Answer with a JSON array that holds the single string "ok".', {"type": "ARRAY", "items": {"type": "STRING"}})
            clear_problem()
            return {"ok": True, "model": model, "tokens": sent + wrote, "answer": str(out)[:80]}
        except Exception as e:  # noqa: BLE001
            last = e
            # Groq may exhaust the selected model's daily limit while a backup model still has quota.
            # Try compatible backup models on rate limits and temporary overloads before declaring
            # the whole provider unavailable. Key rotation happens inside call() for each model.
            retryable = getattr(e, "status_code", None) in (429, 503) or any(
                f"HTTP {code}" in str(e) for code in (429, 503))
            if not retryable:
                break
    note_problem(last)
    raise RuntimeError(f"The AI did not answer: {explain(last) or last}")


class AIError(RuntimeError):
    def __init__(self, message, *, status_code=None, retry_after=None, reset_tokens=None,
                 reset_requests=None, remaining_requests=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.reset_tokens = reset_tokens
        self.reset_requests = reset_requests
        self.remaining_requests = remaining_requests


problem = {"text": "", "at": 0.0, "config": ""}       # only show a failure for the settings that caused it


def config_signature() -> str:
    c = current()
    raw = "\0".join((c["provider"], ",".join(c["models"]), c.get("vision_model", ""), c["base_url"], "\0".join(c["keys"])))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def explain(e) -> str:
    """What went wrong with an AI call, in words the owner can act on."""
    text, low = str(e), str(e).lower()
    name = (models() or ["?"])[0]
    if "above the configured groq per-minute budget" in low:
        return ("This transcript section was too large for the Groq request budget. ClipShorts now analyzes shorter sections; "
                "retry AI selection from the saved transcript.")
    if "404" in text or "not found" in low or "does not exist" in low or "unknown model" in low or "not_found" in low:
        return f"{label()} doesn't know the model \"{name}\". Open Settings, AI, press Find models and pick one from the list."
    if "401" in text or "403" in text or "api key" in low or "api_key" in low or "unauthorized" in low or "permission" in low:
        return f"{label()} refused the key. Paste a working key under Settings, AI."
    if getattr(e, "status_code", None) == 429 or "HTTP 429" in text:
        request_reset = getattr(e, "reset_requests", None)
        remaining_requests = getattr(e, "remaining_requests", None)
        if remaining_requests == 0:
            if request_reset and request_reset <= 90:
                return f"{label()} reached its request limit. ClipShorts will retry in about {max(1, round(request_reset))} seconds."
            return f"{label()} reached its daily request limit. Check the account's Limits page for its reset time; the app cannot raise that allowance."
        wait = getattr(e, "retry_after", None) or getattr(e, "reset_tokens", None)
        if wait and wait <= 90:
            return f"{label()} briefly limited requests. ClipShorts will wait about {max(1, round(wait))} seconds and retry."
        if any(x in low for x in ("daily", "per day", "quota", "billing", "credit", "exhausted")):
            return f"{label()} daily usage or credit is exhausted. Check the account's Limits and billing pages; the app cannot raise that allowance."
        return (f"{label()} returned a 429 rate limit. ClipShorts now spaces its requests; check the account's Limits page "
                "if the limit keeps returning.")
    if "quota" in low or "rate limit" in low or "exhausted" in low:
        return f"{label()} says the key has hit its limit or has no credit left. Check the account with {label()}, or try again later."
    if "503" in text or "high demand" in low or "temporarily unavailable" in low:
        detail = ""
        match = re.search(r"HTTP 503\s+(\{.*\})", text, re.S)
        if match:
            try:
                payload = json.loads(match.group(1))
                error = payload.get("error") or {}
                detail = str(error.get("message") or error.get("code") or "")[:180]
            except (TypeError, ValueError):
                pass
        suffix = f" Provider says: {detail}" if detail else ""
        return (f"{label()} is temporarily overloaded.{suffix} The small key test passed, but analysis requests are being rejected. "
                "ClipShorts tries backup models; retry when the provider recovers or switch providers in Settings, AI.")
    if "connect" in low or "timed out" in low or "timeout" in low or "getaddrinfo" in low or "refused" in low or "nodename" in low or "servname" in low or "nameresolutionerror" in low or "name resolution" in low:
        return f"PeakCut couldn't reach {label()}. Check the internet connection" + (" and that Ollama is running." if provider() == "ollama" else ".")
    return ""


def retry_delay(e):
    """Seconds before a safe retry, when the provider gives a short rate-limit reset."""
    if getattr(e, "remaining_requests", None) == 0:
        value = getattr(e, "reset_requests", None)
        return max(0.0, float(value)) if value is not None else None
    value = getattr(e, "retry_after", None) or getattr(e, "reset_tokens", None)
    if value is not None:
        return max(0.0, float(value))
    match = re.search(r"(?:retry|try again|reset)(?:\s+after|\s+in)?\s+(\d+(?:\.\d+)?)\s*(ms|seconds?|secs?|s|minutes?|mins?|m)\b",
                      str(e), re.I)
    if not match:
        return None
    number = float(match.group(1))
    return number / 1000 if match.group(2).lower() == "ms" else number * 60 if match.group(2).lower().startswith(("m", "min")) else number


def note_problem(e, expected=None):
    import time
    signature = config_signature()
    if expected is not None and expected != signature:
        return
    why = explain(e)
    if why:
        problem.update(text=why, at=time.time(), config=signature)


def clear_problem(expected=None):
    if expected is not None and problem.get("config") not in ("", expected):
        return
    problem.update(text="", at=0.0, config=config_signature())


def _fail(r, model):
    text = r.text[:300].replace(key() or "\0", "<key>")
    headers = getattr(r, "headers", {}) or {}
    retry = headers.get("retry-after")
    reset = headers.get("x-ratelimit-reset-tokens")
    request_reset = headers.get("x-ratelimit-reset-requests")
    remaining_requests = headers.get("x-ratelimit-remaining-requests")
    def seconds(value):
        if value is None:
            return None
        value = str(value).strip().lower()
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            return float(value)
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
        if not parts:
            return None
        return sum(float(number) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit] for number, unit in parts)
    retry_s, reset_s, request_reset_s = seconds(retry), seconds(reset), seconds(request_reset)
    try:
        remaining_requests = int(remaining_requests) if remaining_requests is not None else None
    except (TypeError, ValueError):
        remaining_requests = None
    details = []
    if retry_s is not None:
        details.append(f"retry-after={retry_s:g}s")
    if reset_s is not None:
        details.append(f"token-reset={reset_s:g}s")
    if request_reset_s is not None:
        details.append(f"request-reset={request_reset_s:g}s")
    suffix = "; " + ", ".join(details) if details else ""
    raise AIError(f"HTTP {r.status_code} {text}{suffix}", status_code=r.status_code,
                  retry_after=retry_s, reset_tokens=reset_s, reset_requests=request_reset_s,
                  remaining_requests=remaining_requests)


# ---------------------------------------------------------------- the schema, said in words for chat models
def _json_schema(s):
    """Gemini's schema spelling (ARRAY, OBJECT...) as ordinary JSON Schema."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def _json_chunks(text: str):
    """Every balanced {...} or [...] in the text, in the order they appear."""
    out, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(text[start:i + 1])
                start = None
            elif depth < 0:
                depth = 0
    return out


def _parse(text):
    """The JSON in an answer, even if the model wrapped it in a code fence or a sentence.

    A model that is not careful sometimes puts a sentence in front of the JSON,
    or a second object after it, or forgets a comma. The JSON is looked for
    inside the answer rather than demanded of it."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    out = None
    try:
        out = json.loads(text)
    except ValueError:
        for chunk in _json_chunks(text):
            try:
                out = json.loads(chunk)
                break
            except ValueError:
                continue
        if out is None:
            m = re.search(r"[\[{].*[\]}]", text, re.S)
            if not m:
                raise AIError("The AI did not answer with JSON.")
            try:
                out = json.loads(m.group(0))
            except ValueError:
                raise AIError("The AI did not answer with JSON.")
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    return out


# ---------------------------------------------------------------- one call
def call(model: str, prompt: str, schema: dict, timeout: float | None = None):
    """Ask one model. Returns (the parsed JSON answer, tokens sent, tokens written back).

    `timeout` is how long one answer may take. A live stream passes a short one, because a slow
    answer must not stop the recording."""
    how = PROVIDERS[provider()][2]
    ticket = _reserve_tokens(prompt, schema)
    try:
        result = _with_key_rotation(lambda: {"gemini": _gemini, "openai": _openai, "anthropic": _anthropic}[how](model, prompt, schema, timeout))
    except Exception as e:
        # Keep capacity reserved through provider outages so retry/fallback calls do not burst past TPM.
        if getattr(e, "status_code", None) is not None and 400 <= e.status_code < 500 and e.status_code != 429:
            _release_tokens(ticket)
        raise
    _settle_tokens(ticket, result[1] + result[2])
    return result


def _see_raw(prompt: str, image: bytes, timeout: float | None = None) -> tuple[str, int, int]:
    """Ask one model about one picture: (the answer as text, tokens in, tokens out).

    Nothing is broken by a service that cannot look at pictures, or a model that is only text: the answer
    comes back empty, or says something the caller does not understand, and the caller carries on without
    it. The picture is a JPEG, passed as base64 inside the ordinary message."""
    if not ready() or not image or not models():
        return "", 0, 0
    model = current().get("vision_model") or models()[0]
    b64 = base64.b64encode(image).decode()
    how = PROVIDERS[provider()][2]
    if how == "gemini":
        body = {"contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mimeType": "image/jpeg", "data": b64}}]}]}
        r = httpx.post(f"{base_url()}/{model}:generateContent", json=body,
                       headers={"x-goog-api-key": key()}, timeout=timeout or 120)
        if r.status_code != 200:
            _fail(r, model)
        data = r.json()
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        u = data.get("usageMetadata", {})
        sent = int(u.get("promptTokenCount") or 0)
        return text, sent, max(0, int(u.get("totalTokenCount") or 0) - sent)
    if how == "anthropic":
        body = {"model": model, "max_tokens": 300, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": prompt}]}]}
        r = httpx.post(f"{base_url()}/messages", json=body, timeout=timeout or 120,
                       headers={"x-api-key": key(), "anthropic-version": "2023-06-01"})
        if r.status_code != 200:
            _fail(r, model)
        data = r.json()
        text = "".join(p.get("text", "") for p in data.get("content") or [] if p.get("type") == "text")
        u = data.get("usage") or {}
        return text, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    headers = {"Authorization": f"Bearer {key()}"} if key() else {}
    body = {"model": model, "stream": False, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
    r = httpx.post(f"{base_url()}/chat/completions", json=body, headers=headers, timeout=timeout or 120)
    if r.status_code != 200:
        _fail(r, model)
    data = _openai_body(r)
    text = (data["choices"][0]["message"].get("content") or "")
    u = data.get("usage") or {}
    return text, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def see(prompt: str, image: bytes, timeout: float | None = None) -> tuple[str, int, int]:
    if not ready() or not image or not models():
        return "", 0, 0
    ticket = _reserve_tokens(prompt, {"type": "image"}, image=True)
    try:
        result = _with_key_rotation(lambda: _see_raw(prompt, image, timeout))
    except Exception as e:
        if getattr(e, "status_code", None) is not None and 400 <= e.status_code < 500 and e.status_code != 429:
            _release_tokens(ticket)
        raise
    _settle_tokens(ticket, result[1] + result[2])
    return result


def _gemini(model, prompt, schema, timeout=None):
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    r = httpx.post(f"{base_url()}/{model}:generateContent", json=body, headers={"x-goog-api-key": key()}, timeout=timeout or 120)
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    u = data.get("usageMetadata", {})
    sent = int(u.get("promptTokenCount") or 0)
    return json.loads(text), sent, max(0, int(u.get("totalTokenCount") or 0) - sent)   # the answer, and any thinking billed with it


def _openai_body(r):
    """The answer, whether the service sent plain JSON or an event stream.

    Some OpenAI-compatible servers answer with a stream of events unless the
    request says otherwise, so the answer is read from whichever shape arrived.
    A model that answers with a tool call instead of a message leaves the JSON in
    the call's arguments, so that is used too."""
    try:
        data = r.json()
    except ValueError:
        data = _from_events(r.text)
    for ch in data.get("choices") or []:
        msg = ch.get("message") or {}
        if (msg.get("content") or "").strip():
            continue
        for call in msg.get("tool_calls") or []:
            args = (call.get("function") or {}).get("arguments")
            if args:
                msg["content"] = args
                break
    return data


def _from_events(text: str) -> dict:
    """Read an answer out of a server-sent event stream."""
    parts, usage, calls = [], {}, {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            d = json.loads(chunk)
        except ValueError:
            continue
        usage = d.get("usage") or usage
        for ch in d.get("choices") or []:
            for piece in ((ch.get("message") or {}), (ch.get("delta") or {})):
                if piece.get("content"):
                    parts.append(piece["content"])
                for call in piece.get("tool_calls") or []:
                    i = call.get("index", 0)
                    calls[i] = calls.get(i, "") + ((call.get("function") or {}).get("arguments") or "")
    message = {"content": "".join(parts)}
    if not message["content"] and calls:
        message["content"] = "".join(calls.values())
    return {"choices": [{"message": message}], "usage": usage}


def _openai(model, prompt, schema, timeout=None):
    headers = {"Authorization": f"Bearer {key()}"} if key() else {}
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}], "stream": False}
    # Most services accept these two extras; a few models refuse one of them, so they are dropped on a refusal.
    extras = [{"temperature": 0.4, "response_format": {"type": "json_object"}}, {"response_format": {"type": "json_object"}}, {}]
    r = None
    for extra in extras:
        r = httpx.post(f"{base_url()}/chat/completions", json={**body, **extra}, headers=headers, timeout=timeout or 180)
        if r.status_code not in (400, 422):
            break
    if r.status_code != 200:
        _fail(r, model)
    data = _openai_body(r)
    u = data.get("usage") or {}
    return _parse(data["choices"][0]["message"].get("content")), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _anthropic(model, prompt, schema, timeout=None):
    body = {"model": model, "max_tokens": 8192, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    r = httpx.post(f"{base_url()}/messages", json=body, timeout=timeout or 180,
                   headers={"x-api-key": key(), "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return _parse(text), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
