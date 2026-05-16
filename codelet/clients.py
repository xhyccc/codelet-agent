"""Model client implementations.

Three small clients with a shared ``complete(prompt, max_new_tokens)`` API:

* :class:`FakeModelClient` - scripted responses for tests.
* :class:`OllamaModelClient` - local Ollama HTTP server.
* :class:`OpenAIModelClient` - any OpenAI-compatible ``/v1/chat/completions``
  endpoint (OpenAI, Moonshot/Kimi, Zhipu/GLM, SiliconFlow, DeepSeek, ...).
"""

import json
import urllib.error
import urllib.request


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
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_new_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
                timeout=self.timeout,
            )
        except _openai.APIError as exc:
            raise RuntimeError(f"OpenAI API error: {exc}") from exc
        return response.choices[0].message.content or ""
