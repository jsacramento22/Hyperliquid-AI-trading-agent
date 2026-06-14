"""Contract tests for the LLM provider abstraction.

The trading-cycle loop calls `provider.complete(...)` and consumes the
returned CompletionResult. Both AnthropicProvider and OpenAICompatible-
Provider must produce a result whose `content` blocks expose the
attribute interface the loop reads: `.type`, `.text`, `.id`, `.name`,
`.input`. These tests fake the underlying SDKs and assert the shape.

What we test:
  - Tool definition translation (Anthropic → OpenAI shape)
  - Message translation, including the tricky case of tool_results in a
    user message getting split into multiple OpenAI role:tool messages
  - Response translation, including tool_calls → ToolUseBlock with parsed
    JSON arguments
  - The first-user cache_control wrapper inside AnthropicProvider
  - Factory error paths (missing API keys, unknown provider)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from hl_agent.llm_provider import (
    AnthropicProvider,
    CompletionResult,
    OpenAICompatibleProvider,
    TextBlock,
    ToolUseBlock,
    _anthropic_messages_to_openai,
    _anthropic_tools_to_openai,
    _wrap_first_user_for_anthropic_cache,
    build_provider,
)


# --- Tool translation -----------------------------------------------------


def test_anthropic_tool_translation_basic() -> None:
    anthropic_tools = [
        {
            "name": "hold",
            "description": "Take no action this cycle.",
            "input_schema": {
                "type": "object",
                "properties": {"reasoning": {"type": "string"}},
                "required": ["reasoning"],
            },
        }
    ]
    out = _anthropic_tools_to_openai(anthropic_tools)
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "hold",
                "description": "Take no action this cycle.",
                "parameters": {
                    "type": "object",
                    "properties": {"reasoning": {"type": "string"}},
                    "required": ["reasoning"],
                },
            },
        }
    ]


def test_anthropic_tool_translation_strips_cache_control() -> None:
    anthropic_tools = [
        {
            "name": "hold",
            "description": "X",
            "input_schema": {"type": "object", "properties": {}},
            "cache_control": {"type": "ephemeral", "ttl": "1h"},  # ignored
        }
    ]
    out = _anthropic_tools_to_openai(anthropic_tools)
    assert "cache_control" not in out[0]
    assert "cache_control" not in out[0]["function"]


# --- Message translation --------------------------------------------------


def test_message_translation_plain_user_string() -> None:
    out = _anthropic_messages_to_openai(
        "you are a trader",
        [{"role": "user", "content": "hello"}],
    )
    assert out == [
        {"role": "system", "content": "you are a trader"},
        {"role": "user", "content": "hello"},
    ]


def test_message_translation_assistant_with_tool_use() -> None:
    """The cycle appends `{"role": "assistant", "content": resp.content}`
    where resp.content is a list of blocks. The translator splits text
    parts into `content` and tool_use parts into `tool_calls`."""
    anthropic_msgs = [
        {"role": "user", "content": "what's BTC?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me check"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "hold",
                    "input": {"reasoning": "no signal"},
                },
            ],
        },
    ]
    out = _anthropic_messages_to_openai("sys", anthropic_msgs)
    assert out[2] == {
        "role": "assistant",
        "content": "let me check",
        "tool_calls": [
            {
                "id": "tu_1",
                "type": "function",
                "function": {
                    "name": "hold",
                    "arguments": json.dumps({"reasoning": "no signal"}),
                },
            }
        ],
    }


def test_message_translation_tool_results_become_role_tool() -> None:
    """One `{role: user, content: [tool_result]}` in Anthropic shape
    becomes one `{role: tool, tool_call_id, content}` in OpenAI shape.
    Multiple tool_results split into multiple role:tool messages."""
    anthropic_msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_a", "content": "ok"},
                {"type": "tool_result", "tool_use_id": "tu_b", "content": "ok"},
            ],
        }
    ]
    out = _anthropic_messages_to_openai("sys", anthropic_msgs)
    # system + 2 tool messages
    assert len(out) == 3
    assert out[1] == {"role": "tool", "tool_call_id": "tu_a", "content": "ok"}
    assert out[2] == {"role": "tool", "tool_call_id": "tu_b", "content": "ok"}


def test_message_translation_assistant_text_only_no_tool_calls() -> None:
    """When the assistant returned only text, no tool_calls key should
    appear on the OpenAI message (OpenAI errors on `tool_calls: []`)."""
    out = _anthropic_messages_to_openai(
        "sys",
        [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            }
        ],
    )
    assert "tool_calls" not in out[1]
    assert out[1] == {"role": "assistant", "content": "hi"}


# --- First-user cache wrapping (Anthropic-specific) -----------------------


def test_first_user_cache_wraps_plain_string() -> None:
    out = _wrap_first_user_for_anthropic_cache(
        [{"role": "user", "content": "hello"}]
    )
    assert out[0]["content"][0]["type"] == "text"
    assert out[0]["content"][0]["text"] == "hello"
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_first_user_cache_leaves_block_list_alone() -> None:
    """If the first user content is already a list (e.g. tool results),
    don't re-wrap — preserve what the caller intended."""
    blocks = [{"type": "text", "text": "x"}]
    out = _wrap_first_user_for_anthropic_cache(
        [{"role": "user", "content": blocks}]
    )
    assert out[0]["content"] is blocks  # unchanged reference is fine


def test_first_user_cache_handles_empty_messages() -> None:
    assert _wrap_first_user_for_anthropic_cache([]) == []


# --- OpenAICompatibleProvider response translation ------------------------


@dataclass
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    type: str
    function: _FakeFunction


@dataclass
class _FakeMessage:
    content: str | None
    tool_calls: list | None = None


@dataclass
class _FakeChoice:
    message: _FakeMessage
    finish_reason: str


@dataclass
class _FakeResponse:
    choices: list
    usage: _FakeUsage
    provider: str | None = None  # OpenRouter surfaces backing provider here


class _FakeOpenAIClient:
    """In-place stand-in for openai.OpenAI. The provider only touches
    `.chat.completions.create(...)` — we hand-craft a response that
    mimics that shape."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_call: dict[str, Any] | None = None
        self.chat = self  # so .chat.completions.create works
        self.completions = self

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.last_call = kwargs
        return self._response


def _make_provider_with_response(
    resp: _FakeResponse, *, extra_body: dict | None = None
) -> OpenAICompatibleProvider:
    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p.client = _FakeOpenAIClient(resp)
    p._extra_body = extra_body or {}
    return p


def test_openai_provider_translates_tool_call_response() -> None:
    fake = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(
                    content="thinking...",
                    tool_calls=[
                        _FakeToolCall(
                            id="call_xyz",
                            type="function",
                            function=_FakeFunction(
                                name="hold",
                                arguments=json.dumps({"reasoning": "ok"}),
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=_FakeUsage(prompt_tokens=120, completion_tokens=30),
    )
    provider = _make_provider_with_response(fake)
    result = provider.complete(
        system="sys",
        tools=[{"name": "hold", "description": "", "input_schema": {"type": "object"}}],
        messages=[{"role": "user", "content": "go"}],
        model="x",
        max_tokens=100,
    )
    assert isinstance(result, CompletionResult)
    assert result.stop_reason == "tool_use"  # not "tool_calls"
    assert len(result.content) == 2  # text + tool_use
    text_blocks = [b for b in result.content if b.type == "text"]
    tool_blocks = [b for b in result.content if b.type == "tool_use"]
    assert text_blocks[0].text == "thinking..."
    assert tool_blocks[0].id == "call_xyz"
    assert tool_blocks[0].name == "hold"
    assert tool_blocks[0].input == {"reasoning": "ok"}
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 30


def test_openai_provider_handles_text_only_response() -> None:
    fake = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(content="just a thought", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=_FakeUsage(prompt_tokens=50, completion_tokens=10),
    )
    provider = _make_provider_with_response(fake)
    result = provider.complete(
        system="s", tools=[], messages=[{"role": "user", "content": "?"}],
        model="x", max_tokens=50,
    )
    assert result.stop_reason == "stop"
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert result.content[0].text == "just a thought"


def test_openai_provider_handles_malformed_tool_arguments() -> None:
    """If the model returns invalid JSON in arguments, the provider must
    not crash — it falls back to {} so the cycle can recover."""
    fake = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(
                    content=None,
                    tool_calls=[
                        _FakeToolCall(
                            id="c1",
                            type="function",
                            function=_FakeFunction(
                                name="hold",
                                arguments="not valid json {",
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
    )
    provider = _make_provider_with_response(fake)
    result = provider.complete(
        system="s", tools=[], messages=[{"role": "user", "content": "?"}],
        model="x", max_tokens=50,
    )
    tool_blocks = [b for b in result.content if b.type == "tool_use"]
    assert tool_blocks[0].input == {}


def test_openai_provider_passes_tools_when_present() -> None:
    """When tools are non-empty, both `tools` and `tool_choice: "auto"`
    must be sent. When empty, neither key should be sent (OpenAI errors
    on tool_choice without tools)."""
    fake = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
    )
    provider = _make_provider_with_response(fake)
    provider.complete(
        system="s",
        tools=[
            {"name": "hold", "description": "", "input_schema": {"type": "object"}}
        ],
        messages=[{"role": "user", "content": "go"}],
        model="x", max_tokens=10,
    )
    assert provider.client.last_call["tool_choice"] == "auto"
    assert len(provider.client.last_call["tools"]) == 1

    # Now without tools
    provider2 = _make_provider_with_response(fake)
    provider2.complete(
        system="s", tools=[], messages=[{"role": "user", "content": "go"}],
        model="x", max_tokens=10,
    )
    assert "tools" not in provider2.client.last_call
    assert "tool_choice" not in provider2.client.last_call


# --- Factory --------------------------------------------------------------


def test_factory_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        build_provider(provider="cohere", anthropic_api_key="x")


def test_factory_anthropic_requires_key() -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_provider(provider="anthropic", anthropic_api_key="")


def test_factory_openrouter_requires_key() -> None:
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_provider(provider="openrouter", openrouter_api_key="")


def test_factory_anthropic_with_key_builds() -> None:
    p = build_provider(provider="anthropic", anthropic_api_key="sk-ant-fake")
    assert isinstance(p, AnthropicProvider)


def test_factory_openrouter_with_key_builds() -> None:
    p = build_provider(provider="openrouter", openrouter_api_key="sk-or-fake")
    assert isinstance(p, OpenAICompatibleProvider)


# --- OpenRouter routing: pin to Novita FP8 ---------------------------------


def test_factory_openrouter_pins_novita_fp8() -> None:
    """The OpenRouter provider must ship with a routing config that pins
    Novita first, allows fallback to any FP8 backing provider, and
    explicitly excludes FP4 quantizations (DeepInfra's Turbo variant).
    Quality consistency for the trading bot."""
    p = build_provider(provider="openrouter", openrouter_api_key="sk-or-fake")
    assert p._extra_body == {
        "provider": {
            "order": ["novita"],
            "allow_fallbacks": True,
            "quantizations": ["fp8"],
        }
    }


def test_openai_provider_forwards_extra_body() -> None:
    """When the provider is constructed with extra_body, every call must
    forward it via the openai SDK's extra_body kwarg so OpenRouter
    receives the routing config in the request body."""
    fake = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
    )
    routing = {"provider": {"order": ["novita"], "quantizations": ["fp8"]}}
    p = _make_provider_with_response(fake, extra_body=routing)
    p.complete(
        system="s", tools=[],
        messages=[{"role": "user", "content": "go"}],
        model="x", max_tokens=10,
    )
    assert p.client.last_call["extra_body"] == routing


def test_openai_provider_omits_extra_body_when_empty() -> None:
    """No extra_body configured → don't send the kwarg at all (some
    OpenAI-compatible providers reject unknown top-level fields)."""
    fake = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
    )
    p = _make_provider_with_response(fake)  # no extra_body
    p.complete(
        system="s", tools=[],
        messages=[{"role": "user", "content": "go"}],
        model="x", max_tokens=10,
    )
    assert "extra_body" not in p.client.last_call


def test_openai_provider_captures_served_by_when_present() -> None:
    """OpenRouter sets `provider` on the response to indicate which
    backing provider actually served the request — capture it so a
    Novita→Fireworks fallback is visible in logs."""
    fake = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
        provider="Novita",
    )
    p = _make_provider_with_response(fake)
    result = p.complete(
        system="s", tools=[],
        messages=[{"role": "user", "content": "go"}],
        model="x", max_tokens=10,
    )
    assert result.served_by == "Novita"


def test_openai_provider_served_by_none_when_missing() -> None:
    """Plain OpenAI-protocol providers (not aggregators) don't surface a
    provider field. CompletionResult.served_by should be None, not crash."""
    fake = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
        # provider deliberately defaulted to None
    )
    p = _make_provider_with_response(fake)
    result = p.complete(
        system="s", tools=[],
        messages=[{"role": "user", "content": "go"}],
        model="x", max_tokens=10,
    )
    assert result.served_by is None
