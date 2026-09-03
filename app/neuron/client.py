import asyncio
import httpx
from app.errors import AuthenticationError, NeuronError


class NeuronClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60):
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout

    async def complete(self, messages: list[dict], tools: list[dict], temperature: float = 0.2, max_tokens: int | None = None) -> dict:
        if not self.api_key: raise AuthenticationError("NEURON_API_KEY is not configured")
        payload = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": temperature}
        if max_tokens is not None: payload["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
                if response.status_code in (401, 403): raise AuthenticationError("Neuron authentication failed")
                if response.status_code >= 500 and attempt < 2:
                    await asyncio.sleep(0.25 * (2 ** attempt)); continue
                response.raise_for_status()
                data = response.json()
                if not data.get("choices"): raise NeuronError("Neuron returned no choices")
                return data["choices"][0]["message"]
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 2: raise NeuronError(f"Neuron request failed: {exc}") from exc
                await asyncio.sleep(0.25 * (2 ** attempt))
            except httpx.HTTPStatusError as exc: raise NeuronError(f"Neuron HTTP error: {exc.response.status_code}") from exc
        raise NeuronError("Neuron request failed")
