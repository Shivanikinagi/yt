"""Free LLM access with graceful fallback.

Providers (in configured order): groq, ollama, github_models, gemini, openrouter.
None of these use the Claude Max subscription; it has no API access and we do not try to
borrow its session. The "template" provider is handled in scripting.py and always works.
"""
from __future__ import annotations

import os
import time

import requests


class LLMError(Exception):
    pass


def _post_json(url: str, headers: dict, body: dict, timeout: float) -> dict:
    r = requests.post(url, headers=headers, json=body, timeout=timeout)
    if r.status_code == 429:
        wait = r.headers.get("retry-after")
        try:
            wait = min(float(wait), 70.0) if wait else 20.0
        except ValueError:
            wait = 20.0
        time.sleep(wait)
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
    if r.status_code >= 400:
        raise LLMError(f"{url.split('/')[2]} HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _openai_compat(url: str, key: str, model: str, system: str, user: str, cfg, extra_headers=None,
                   extra_body=None) -> str:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": int(cfg.llm.max_output_tokens),
        "temperature": float(cfg.llm.temperature),
    }
    body.update(extra_body or {})
    data = _post_json(url, headers, body, cfg.llm.timeout_seconds)
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as e:
        raise LLMError(f"unexpected response shape: {str(data)[:200]}") from e
    if not content.strip():
        raise LLMError(f"{model} returned empty content (finish_reason="
                       f"{data['choices'][0].get('finish_reason')})")
    return content


def _groq(system, user, cfg):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMError("GROQ_API_KEY not set")
    url = "https://api.groq.com/openai/v1/chat/completions"

    def extra(model: str) -> dict:
        # gpt-oss models on Groq are reasoning models: keep thinking short so tokens go to the answer
        return {"reasoning_effort": "low"} if "gpt-oss" in model else {}
    try:
        return _openai_compat(url, key, cfg.llm.groq_model, system, user, cfg, extra_body=extra(cfg.llm.groq_model))
    except (LLMError, requests.RequestException) as e:      # timeouts too: the smaller model is usually free
        fb = cfg.llm.get("groq_fallback_model")
        if fb and fb != cfg.llm.groq_model:
            return _openai_compat(url, key, fb, system, user, cfg, extra_body=extra(fb))
        raise e


def _ollama(system, user, cfg):
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        requests.get(f"{host}/api/tags", timeout=1.5).raise_for_status()
    except Exception as e:
        raise LLMError(f"ollama not reachable at {host}") from e
    body = {"model": cfg.llm.ollama_model, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": float(cfg.llm.temperature), "num_predict": int(cfg.llm.max_output_tokens)}}
    data = _post_json(f"{host}/api/chat", {}, body, max(cfg.llm.timeout_seconds, 300))
    return data.get("message", {}).get("content", "")


def _github_models(system, user, cfg):
    key = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not key:
        raise LLMError("GITHUB_TOKEN not set")
    url = "https://models.github.ai/inference/chat/completions"
    return _openai_compat(url, key, cfg.llm.github_model, system, user, cfg,
                          extra_headers={"Accept": "application/vnd.github+json",
                                         "X-GitHub-Api-Version": "2022-11-28"})


def _gemini(system, user, cfg):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY not set")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.llm.gemini_model}"
           f":generateContent?key={key}")
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": float(cfg.llm.temperature),
                                 "maxOutputTokens": int(cfg.llm.max_output_tokens),
                                 "responseMimeType": "application/json"}}
    data = _post_json(url, {"Content-Type": "application/json"}, body, cfg.llm.timeout_seconds)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"unexpected gemini response: {str(data)[:200]}") from e


def _openrouter(system, user, cfg):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise LLMError("OPENROUTER_API_KEY not set")
    return _openai_compat("https://openrouter.ai/api/v1/chat/completions", key,
                          cfg.llm.openrouter_model, system, user, cfg,
                          extra_headers={"HTTP-Referer": "https://github.com", "X-Title": "Tech Shorts Factory"})


PROVIDERS = {
    "groq": _groq,
    "ollama": _ollama,
    "github_models": _github_models,
    "gemini": _gemini,
    "openrouter": _openrouter,
}


def available_providers(cfg) -> list[str]:
    """Providers that look usable right now (key present / server reachable)."""
    out = []
    for name in cfg.llm.providers:
        if name == "template":
            out.append(name)
        elif name == "groq" and os.environ.get("GROQ_API_KEY"):
            out.append(name)
        elif name == "github_models" and (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
            out.append(name)
        elif name == "gemini" and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            out.append(name)
        elif name == "openrouter" and os.environ.get("OPENROUTER_API_KEY"):
            out.append(name)
        elif name == "ollama":
            host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
            try:
                requests.get(f"{host}/api/tags", timeout=1.0)
                out.append(name)
            except Exception:
                pass
    return out


def chat(cfg, system: str, user: str, logger) -> tuple[str, str]:
    """Try each configured provider in order. Returns (text, provider_name)."""
    errors = []
    for name in cfg.llm.providers:
        if name == "template":
            break
        fn = PROVIDERS.get(name)
        if not fn:
            continue
        t0 = time.time()
        try:
            text = fn(system, user, cfg)
            if not text or not text.strip():
                raise LLMError("empty response")
            logger.info("llm %s ok (%.1fs, %d chars)", name, time.time() - t0, len(text))
            return text, name
        except (LLMError, requests.RequestException) as e:
            msg = str(e)[:200]
            errors.append(f"{name}: {msg}")
            logger.warning("llm %s failed: %s", name, msg)
    raise LLMError("all LLM providers failed: " + " | ".join(errors))
