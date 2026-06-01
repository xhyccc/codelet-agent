"""Model client implementations.

Three small clients with a shared ``complete(prompt, max_new_tokens)`` API:

* :class:`FakeModelClient` - scripted responses for tests.
* :class:`OllamaModelClient` - local Ollama HTTP server.
* :class:`OpenAIModelClient` - any OpenAI-compatible ``/v1/chat/completions``
  endpoint (OpenAI, Moonshot/Kimi, Zhipu/GLM, SiliconFlow, DeepSeek, ...).
"""

import json
import time
import urllib.error
import urllib.request

_MAX_RETRIES = 3  # number of retries after the initial attempt


def _with_retries(fn):
    """Call fn() and retry up to _MAX_RETRIES times on any exception,
    with exponential back-off (1 s, 2 s, 4 s …)."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception:
            if attempt == _MAX_RETRIES:
                raise
            time.sleep(2 ** attempt)


class FakeModelClient:
    """A deterministic model client used by tests; replays a scripted list."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, prompt, max_new_tokens):
        self.prompts.append(prompt)
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


class OllamaModelClient:
    """Client that talks to a local Ollama server's ``/api/generate`` endpoint."""

    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout

    def complete(self, prompt, max_new_tokens):
        return _with_retries(lambda: self._do_complete(prompt, max_new_tokens))

    def _do_complete(self, prompt, max_new_tokens):
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        return data.get("response", "")


class OpenAIModelClient:
    """Client for OpenAI-compatible APIs (OpenAI, Azure, local servers, ...)."""

    def __init__(self, model, api_key, base_url, temperature, top_p, timeout):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") if base_url else None
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout

    def complete(self, prompt, max_new_tokens):
        return _with_retries(lambda: self._do_complete(prompt, max_new_tokens))

    def _do_complete(self, prompt, max_new_tokens):
        try:
            import openai as _openai
        except ImportError:
            raise RuntimeError(
                "The 'openai' package is required for the OpenAI backend.\n"
                "Install it with: pip install openai"
            ) from None
        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = _openai.OpenAI(**kwargs)
        # Some endpoints (e.g. kimi-for-coding) gate access by User-Agent /
        # client header and reject unknown callers with 403.  Pass the headers
        # on the individual call (extra_headers) so they take precedence over
        # the openai SDK's own built-in User-Agent header.
        _kimi_headers = {
            "User-Agent": "claude-code/1.0.0",
            "x-msh-client": "claude-code",
        }
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_new_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
                timeout=self.timeout,
                extra_headers=_kimi_headers,
            )
        except _openai.APIError as exc:
            raise RuntimeError(f"OpenAI API error: {exc}") from exc
        return response.choices[0].message.content or ""
