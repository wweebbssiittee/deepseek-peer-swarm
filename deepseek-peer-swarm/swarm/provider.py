from __future__ import annotations

import httpx


class ProviderError(Exception):
    def __init__(self, message, retryable=False, usage=None):
        super().__init__(message)
        self.retryable = retryable
        # A response can be unusable while its billed usage is still known.
        self.usage = usage


class DeepSeek:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20), trust_env=False)

    async def complete(self, key, config, messages, tools):
        body = {"model": config["model"], "messages": messages, "tools": tools, "max_tokens": config.get("max_output_tokens", 8192), "stream": False,
                "thinking": {"type": "enabled" if config["thinking"] else "disabled"}}
        if config["thinking"]:
            body["reasoning_effort"] = config["reasoning_effort"]
        try:
            response = await self.client.post("https://api.deepseek.com/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
        except httpx.HTTPError as error:
            raise ProviderError(f"DeepSeek network failure: {type(error).__name__}", True) from None
        if response.status_code != 200:
            raise ProviderError(f"DeepSeek HTTP {response.status_code}; check model, key, balance, or rate limits", response.status_code == 429 or response.status_code >= 500)
        usage = None
        try:
            data = response.json()
            choice = data["choices"][0]
            usage = data.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            if choice.get("finish_reason") == "length":
                raise ProviderError(
                    "DeepSeek output limit exceeded; the response was truncated and its tools were not executed. "
                    "Increase Output tokens per request in this run's Limits, then resume.",
                    retryable=False, usage=usage,
                )
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("Invalid message shape")
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError("Invalid content shape")
            if message.get("reasoning_content") is not None and not isinstance(message["reasoning_content"], str):
                raise ValueError("Invalid reasoning shape")
            calls = message.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list):
                    raise ValueError("Invalid tool call shape")
                for call in calls:
                    if (not isinstance(call, dict) or not isinstance(call.get("id"), str)
                            or not isinstance(call.get("function"), dict)
                            or not isinstance(call["function"].get("name"), str)
                            or not isinstance(call["function"].get("arguments"), str)):
                        raise ValueError("Invalid tool call shape")
            # Preserve reasoning_content for ALL assistant turns as required by DeepSeek.
            message = {k: v for k, v in message.items() if k in {"role", "content", "reasoning_content", "tool_calls"}}
            message["role"] = "assistant"
            return message, usage
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError("DeepSeek returned an invalid response", True, usage=usage) from None

    async def close(self):
        await self.client.aclose()
