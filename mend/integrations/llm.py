"""
Chat-completions client for the LLM forge agent.

Defaults to NEAR AI Cloud (https://cloud.near.ai), which exposes an
OpenAI-compatible API. Any compatible endpoint works via MEND_LLM_BASE_URL.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


class LLMError(Exception):
    pass


def _ssl_context() -> ssl.SSLContext:
    cafile = "/etc/ssl/cert.pem"
    return ssl.create_default_context(cafile=cafile) if Path(cafile).exists() else ssl.create_default_context()


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 90.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        if not self.configured:
            raise LLMError("No LLM API key configured")
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"LLM provider returned HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        choices: List[Dict[str, Any]] = payload.get("choices") or []
        if not choices:
            raise LLMError("LLM provider returned no choices")
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not content:
            raise LLMError("LLM provider returned an empty message")
        return str(content)


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Finds the first balanced JSON object in a model reply."""
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            return value
        index = text.find("{", index + 1)
    return None
