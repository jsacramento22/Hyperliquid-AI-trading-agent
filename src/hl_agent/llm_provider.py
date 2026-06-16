"""LLM provider abstraction.

The trading agent's `run_cycle` was originally hardcoded to Anthropic's API
with its native caching shape. This module gives both Anthropic and any
OpenAI-compatible endpoint (OpenRouter, DeepInfra, Together, Fireworks,
Groq, …) a uniform `complete()` interface so the cycle can swap providers
at runtime without touching the agent logic.

Why agent.py uses Anthropic-shape blocks internally
---------------------------------------------------
The cycle loop appends assistant turns and tool_result turns to a single
`messages` list that gets re-sent each iteration. Anthropic's content-block
shape is the richer of the two formats (it can carry text + tool_use in a
single assistant message), so we keep agent.py expressing things in that
shape and translate to OpenAI's flatter format inside the provider.

Caching ownership
-----------------
Prompt caching is Anthropic-specific (cache_control blocks, ttl, the
extended-cache-ttl beta). It lives ENTIRELY inside `AnthropicProvider` —
the agent passes plain strings and lists, and the provider wraps them.
OpenAI-compatible providers either don't expose caching or apply it
transparently; either way the agent doesn't need to know.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from anthropic import Anthropic
from openai import OpenAI


# --- Content blocks -------------------------------------------------------
# These mimic the Anthropic SDK's block objects (.type, .text, .id, .name,
# .input) so that agent.py's attribute-based reads work uniformly whether
# the content came from the native SDK (AnthropicProvider) or our shims
# (OpenAICompatibleProvider).


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


# --- Usage shim -----------------------------------------------------------
# Mirrors the Anthropic UsageBlock's attribute names. Non-Anthropic
# providers populate input_tokens/output_tokens and leave cache fields at
# zero (or report provider-specific caching as cache_read_input_tokens
# when available).


@dataclass
class CacheCreation:
    ephemeral_5m_input_tokens: int = 0
    ephemeral_1h_input_tokens: int = 0


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: CacheCreation | None = None


# --- Result ---------------------------------------------------------------


@dataclass
class CompletionResult:
    """What every provider.complete() returns. `content` holds blocks
    (native Anthropic objects OR our TextBlock/ToolUseBlock shims) and the
    rest mirrors the Anthropic response shape so the existing
    `agent.run_cycle` loop can consume both transparently.

    `served_by` is set when an aggregator (OpenRouter) tells us which
    backing provider actually served the request — useful for logging
    when fallbacks fire. None for direct providers (Anthropic) or when
    the aggregator didn't surface it."""
    content: list[Any]
    stop_reason: str
    usage: Any  # native Anthropic Usage OR our Usage shim
    served_by: str | None = None


# --- Protocol -------------------------------------------------------------


class LLMProvider(Protocol):
    def complete(
        self,
        *,
        system: str,
        tools: list[dict],
        messages: list[dict],
        model: str,
        max_tokens: int,
    ) -> CompletionResult: ...


# --- Anthropic provider ---------------------------------------------------


class AnthropicProvider:
    """Native Anthropic API caller. Owns all the cache_control / TTL /
    beta-header logic so the agent can pass plain inputs."""

    def __init__(self, api_key: str):
        self.client = Anthropic(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        tools: list[dict],
        messages: list[dict],
        model: str,
        max_tokens: int,
    ) -> CompletionResult:
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ]
        tools_cached = [dict(t) for t in tools]
        if tools_cached:
            # Anchor the tools cache on the last entry so the whole
            # tools array caches as one unit at 1h TTL.
            tools_cached[-1] = {
                **tools_cached[-1],
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        messages_cached = _wrap_first_user_for_anthropic_cache(messages)

        # The beta endpoint is required to pass the extended-cache-ttl beta;
        # the headers route silently downgrades the 1h TTL to 5m on this
        # SDK version (empirically verified).
        resp = self.client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            tools=tools_cached,
            messages=messages_cached,
            betas=["extended-cache-ttl-2025-04-11"],
        )
        return CompletionResult(
            content=resp.content,
            stop_reason=resp.stop_reason,
            usage=resp.usage,
        )


def _wrap_first_user_for_anthropic_cache(messages: list[dict]) -> list[dict]:
    """If the first user message has plain-string content, wrap it as a
    cacheable text block (5m TTL default). Within-cycle multi-turn calls
    re-read this cached user message so call 2+ doesn't re-bill the user
    text. Once content is already a list of blocks, leave it alone."""
    if not messages:
        return messages
    out = [dict(m) for m in messages]
    first = out[0]
    if first.get("role") == "user" and isinstance(first.get("content"), str):
        out[0] = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": first["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    return out


# --- OpenAI-compatible provider -------------------------------------------


class OpenAICompatibleProvider:
    """Generic OpenAI-protocol caller. Works against OpenRouter, DeepInfra,
    Together, Fireworks, Groq, etc. by changing base_url + api_key.
    Translates the agent's Anthropic-shape messages/tools into OpenAI's
    chat-completions shape on the way out, and the response back into our
    block shims on the way in.

    `extra_body` lets the caller pass aggregator-specific routing config
    (e.g. OpenRouter's `provider: {order, quantizations, ...}`). It's
    forwarded verbatim into chat.completions.create()'s extra_body
    parameter and merged into the request body."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        default_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ):
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers or {},
        )
        self._extra_body = extra_body or {}

    def complete(
        self,
        *,
        system: str,
        tools: list[dict],
        messages: list[dict],
        model: str,
        max_tokens: int,
    ) -> CompletionResult:
        oai_tools = _anthropic_tools_to_openai(tools)
        oai_messages = _anthropic_messages_to_openai(system, messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body

        resp = self.client.chat.completions.create(**kwargs)

        choice = resp.choices[0]
        message = choice.message

        content_blocks: list[Any] = []
        if message.content:
            content_blocks.append(TextBlock(text=message.content))
        if getattr(message, "tool_calls", None):
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                content_blocks.append(
                    ToolUseBlock(id=tc.id, name=tc.function.name, input=args)
                )

        # OpenAI's finish_reason vocabulary differs from Anthropic's. Map
        # "tool_calls" → "tool_use" so the loop's existing check works.
        finish = choice.finish_reason or ""
        stop_reason = "tool_use" if finish == "tool_calls" else (finish or "stop")

        usage = Usage(
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        )
        # OpenRouter surfaces the actual backing provider on the response.
        # None for plain OpenAI-protocol providers that don't aggregate.
        served_by = getattr(resp, "provider", None)
        return CompletionResult(
            content=content_blocks,
            stop_reason=stop_reason,
            usage=usage,
            served_by=served_by,
        )


def _anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Translate Anthropic tool defs into OpenAI function-calling format.
    Strips Anthropic-specific cache_control keys."""
    out = []
    for t in tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _anthropic_messages_to_openai(
    system: str, messages: list[dict]
) -> list[dict]:
    """Translate Anthropic-shape messages to OpenAI chat-completions shape.

    Mapping:
      - System prompt → leading {role: system, content: str}
      - {role: user, content: str} → unchanged
      - {role: user, content: [text + tool_result blocks]} → one
        {role: tool, tool_call_id, content} per tool_result, plus a
        {role: user, content: text} if any text blocks remain
      - {role: assistant, content: [text + tool_use blocks]} →
        {role: assistant, content: text-or-null, tool_calls: [...]}
    """
    out: list[dict] = [{"role": "system", "content": system}]
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        blocks = content or []
        text_parts: list[str] = []
        tool_results: list[tuple[str, str]] = []
        tool_calls: list[dict] = []

        for b in blocks:
            btype = _block_get(b, "type")
            if btype == "text":
                text_parts.append(_block_get(b, "text") or "")
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": _block_get(b, "id"),
                        "type": "function",
                        "function": {
                            "name": _block_get(b, "name"),
                            "arguments": json.dumps(
                                _block_get(b, "input") or {}
                            ),
                        },
                    }
                )
            elif btype == "tool_result":
                tr_content = _block_get(b, "content")
                if not isinstance(tr_content, str):
                    tr_content = (
                        str(tr_content) if tr_content is not None else ""
                    )
                tool_results.append(
                    (_block_get(b, "tool_use_id"), tr_content)
                )

        if role == "assistant":
            asst: dict = {"role": "assistant"}
            asst["content"] = "\n".join(text_parts) if text_parts else None
            if tool_calls:
                asst["tool_calls"] = tool_calls
            out.append(asst)
        elif role == "user":
            for tc_id, tr_content in tool_results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tr_content,
                    }
                )
            if text_parts:
                out.append(
                    {"role": "user", "content": "\n".join(text_parts)}
                )
    return out


def _block_get(block: Any, key: str) -> Any:
    """Access a key on either a dict or an Anthropic SDK object."""
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


# --- Factory --------------------------------------------------------------


def build_provider(
    *,
    provider: str,
    anthropic_api_key: str = "",
    openrouter_api_key: str = "",
) -> LLMProvider:
    """Build the right provider for the runtime provider string. Raises
    early if the chosen provider's API key isn't set so cycles never fire
    an empty-auth request."""
    if provider == "anthropic":
        if not anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY required for the anthropic provider — "
                "set it in .env"
            )
        return AnthropicProvider(api_key=anthropic_api_key)
    if provider == "openrouter":
        if not openrouter_api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY required for the openrouter provider — "
                "get one at https://openrouter.ai/keys and add it to .env"
            )
        return OpenAICompatibleProvider(
            api_key=openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            # OpenRouter recommends these headers for attribution/analytics;
            # they don't affect routing or billing.
            default_headers={
                "HTTP-Referer": "https://github.com/jsacramento22/Hyperliquid-AI-trading-agent",
                "X-Title": "hl-agent",
            },
            # Routing: pin to Novita first (FP8, native DeepSeek
            # quantization, better reported tool-use reliability) but
            # allow fallback to ANY FP8 provider so a Novita outage
            # doesn't kill the cycle. The quantizations filter
            # explicitly excludes DeepInfra's FP4 "Turbo" variant —
            # we want consistent precision, not lowest-cost-at-the-
            # expense-of-quality. Cost delta vs unpinned: ~$0.04/day.
            #
            # `reasoning.enabled: false` forces non-thinking mode on
            # every DeepSeek hybrid model. V3.1's chat alias defaults
            # this OFF and ignores the flag; V3.2 defaults it ON and
            # NEEDS it forced off — leaving reasoning on with
            # structured tool output is documented-broken (vllm #41132,
            # vercel/ai #10778, DeepSeek's own docs). Setting it
            # always-off is the safe minimal-surprise default.
            extra_body={
                "provider": {
                    "order": ["novita"],
                    "allow_fallbacks": True,
                    "quantizations": ["fp8"],
                },
                "reasoning": {"enabled": False},
            },
        )
    raise ValueError(
        f"unknown provider {provider!r}; choose anthropic or openrouter"
    )
