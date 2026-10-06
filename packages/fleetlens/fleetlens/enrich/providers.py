"""Model providers for the optional enrichment tier — LLM (summaries) + embeddings.

Two integrations, each usable for either role, configured independently:
  * Ollama (local)         — the default; no code leaves the machine.
  * OpenAI-compatible       — base_url + api_key; covers OpenAI, Azure, OpenRouter, vLLM,
                              LM Studio, Groq, Together, and Ollama's own /v1 endpoint.

Thin stdlib HTTP (urllib) — no SDK dependency. Providers are injected, so tests use fakes.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    pass


#: Seconds to wait for a model to answer. The first request to a local model loads the
#: weights: several gigabytes for a 14b, which on a CPU-only box can take minutes before a
#: single token is produced. 120 seconds was generous for a warm model and too short for a
#: cold one, so an overnight job died on its first call.
TIMEOUT = int(os.environ.get("FLEETLENS_LLM_TIMEOUT", "600"))


#: Attempts per request. Enrichment runs for hours and each model is loaded on its first
#: use, so a timeout is usually a cold start rather than a broken endpoint: the retry
#: arrives after the weights are resident and succeeds. Only timeouts are retried. An HTTP
#: error means the server answered, and answering "model not found" faster will not help.
ATTEMPTS = int(os.environ.get("FLEETLENS_LLM_ATTEMPTS", "3"))


def _post(url: str, payload: dict, headers: Optional[dict] = None,
          timeout: Optional[int] = None) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    limit = timeout or TIMEOUT
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=limit) as resp:
                return json.loads(resp.read().decode())
        except TimeoutError as exc:
            # Not a URLError, so this escaped the handler below and surfaced as a bare
            # traceback from inside http.client, saying nothing about what to do next.
            if attempt < ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            raise ProviderError(
                f"{url} did not answer within {limit}s, after {ATTEMPTS} attempts.\n"
                f"  A model loading for the first time can exceed this. Either warm it\n"
                f"  first (`ollama run <model> ''`), raise FLEETLENS_LLM_TIMEOUT, or use\n"
                f"  a smaller model.") from exc
        except urllib.error.HTTPError as exc:
            # The server answered, and its body says why. "HTTP Error 500: Internal Server
            # Error" on its own is useless; Ollama puts the real cause there, and for a
            # runner the kernel killed that is the difference between a mystery and
            # "out of memory".
            try:
                detail = exc.read().decode("utf-8", "replace").strip()[:400]
            except Exception:  # noqa: BLE001
                detail = ""
            # 5xx can be transient: a model runner that died is restarted on the next
            # request. 429 is the other retryable case and the only 4xx that is: it means
            # the request was fine and arrived too soon, which is precisely what --jobs
            # produces against a remote provider. Everything else will not improve by
            # asking again. Retry-After is the provider telling us how long to wait, so
            # prefer it over our own guess.
            if (exc.code >= 500 or exc.code == 429) and attempt < ATTEMPTS:
                delay = 2 * attempt
                after = (exc.headers or {}).get("Retry-After")
                if after:
                    try:
                        delay = max(delay, min(float(after), 60))
                    except ValueError:      # a date rather than seconds; our guess stands
                        pass
                time.sleep(delay)
                continue
            hint = ""
            if exc.code == 429:
                hint = ("\n  Rate limited after every attempt. Lower --jobs, or raise\n"
                        "  FLEETLENS_LLM_ATTEMPTS to wait the provider out.")
            if exc.code >= 500 and ("memory" in detail.lower() or "killed" in detail.lower()):
                hint = ("\n  The model runner was killed, which on a shared box means it ran\n"
                        "  out of memory. Use a smaller model, or free memory on the host.")
            raise ProviderError(
                f"{url} returned HTTP {exc.code}"
                + (f"\n  {detail}" if detail else "") + hint) from exc
        except (urllib.error.URLError, OSError) as exc:  # pragma: no cover - network
            raise ProviderError(f"request to {url} failed: {exc}") from exc
    raise ProviderError(f"request to {url} failed")      # unreachable; keeps the type honest


def _get(url: str, timeout: int = 15) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except TimeoutError as exc:
        raise ProviderError(f"{url} did not answer within {timeout}s") from exc
    except (urllib.error.URLError, OSError) as exc:  # pragma: no cover - network
        raise ProviderError(f"request to {url} failed: {exc}") from exc


@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, prompt: str, *, system: Optional[str] = None, max_tokens: int = 64) -> str: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    model: str
    def embed(self, texts: list[str]) -> list[list[float]]: ...


# --- Ollama (local) --------------------------------------------------------
class OllamaLLM:
    def __init__(self, model: str, base_url: str = "http://localhost:11434"):
        self.model, self.base_url = model, base_url.rstrip("/")

    def complete(self, prompt: str, *, system: Optional[str] = None, max_tokens: int = 64) -> str:
        body = {"model": self.model, "prompt": prompt, "stream": False,
                "options": {"num_predict": max_tokens}}
        if system:
            body["system"] = system
        return _post(f"{self.base_url}/api/generate", body).get("response", "").strip()


class OllamaEmbeddings:
    def __init__(self, model: str, base_url: str = "http://localhost:11434"):
        self.model, self.base_url = model, base_url.rstrip("/")

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = _post(f"{self.base_url}/api/embed", {"model": self.model, "input": texts})
        return r.get("embeddings", [])


def ollama_installed_models(base_url: str = "http://localhost:11434") -> list[str]:
    tags = _get(f"{base_url.rstrip('/')}/api/tags").get("models", [])
    return [m.get("name", "") for m in tags]


def ollama_has_model(name: str, installed: list[str]) -> bool:
    """`foo` matches any installed `foo:<tag>`; `foo:tag` must match exactly — Ollama 404s
    on a tag you don't have even when a sibling tag is present."""
    if ":" in name:
        return name in installed
    return any(m == name or m.startswith(name + ":") for m in installed)


# --- OpenAI-compatible -----------------------------------------------------
class OpenAICompatLLM:
    def __init__(self, model: str, base_url: str, api_key: str = ""):
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key

    def complete(self, prompt: str, *, system: Optional[str] = None, max_tokens: int = 64) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
        r = _post(f"{self.base_url}/chat/completions",
                  {"model": self.model, "messages": msgs, "max_tokens": max_tokens},
                  headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else None)
        return r["choices"][0]["message"]["content"].strip()


class OpenAICompatEmbeddings:
    def __init__(self, model: str, base_url: str, api_key: str = ""):
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = _post(f"{self.base_url}/embeddings", {"model": self.model, "input": texts},
                  headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else None)
        return [d["embedding"] for d in r["data"]]


# --- factories -------------------------------------------------------------
def build_llm(provider: str, model: str, base_url: str = "", api_key: str = "") -> LLMProvider:
    if provider == "ollama":
        return OllamaLLM(model, base_url or "http://localhost:11434")
    if provider in ("openai", "openai-compatible"):
        return OpenAICompatLLM(model, base_url or "https://api.openai.com/v1", api_key)
    raise ProviderError(f"unknown LLM provider {provider!r} (use 'ollama' or 'openai')")


def build_embeddings(provider: str, model: str, base_url: str = "", api_key: str = "") -> EmbeddingProvider:
    if provider == "ollama":
        return OllamaEmbeddings(model, base_url or "http://localhost:11434")
    if provider in ("openai", "openai-compatible"):
        return OpenAICompatEmbeddings(model, base_url or "https://api.openai.com/v1", api_key)
    raise ProviderError(f"unknown embedding provider {provider!r} (use 'ollama' or 'openai')")
