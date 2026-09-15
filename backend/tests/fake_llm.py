"""
fake_llm.py — a scripted stand-in for the local llama-server.

local_llm.local_agent_loop() talks to the server through httpx; this patches
the module's httpx reference so every request hits an in-process
MockTransport that replays a scripted list of OpenAI-style responses and
records every request body. Tests can then drive the real agent loop —
tool dispatch, transcript assembly, the supervision hook — without a model.

Usage:
    server = ScriptedLlamaServer([
        tool_call_response([("web_search", '{"query": "x"}')]),
        text_response("final answer"),
    ])
    with server.patched():
        result = await local_agent_loop(...)
    server.requests  # -> list of request bodies, in order
"""

import json
from unittest import mock

import httpx

from backend.agent import local_llm


def tool_call_response(calls: list[tuple[str, str]], content: str = "") -> dict:
    """An assistant turn that requests tool calls. `calls` is a list of
    (function name, raw arguments string) — the string is passed through
    verbatim so tests can hand the loop malformed JSON on purpose."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                    for i, (name, arguments) in enumerate(calls)
                ],
            }
        }]
    }


def text_response(text: str) -> dict:
    """A final assistant turn with no tool calls."""
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def error_response(status: int = 500, text: str = "boom") -> dict:
    """Marker for 'the server errors on this turn' — see ScriptedLlamaServer."""
    return {"__http_error__": status, "text": text}


class ScriptedLlamaServer:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if not self.responses:
            return httpx.Response(500, json={"error": "scripted responses exhausted"})
        nxt = self.responses.pop(0)
        if "__http_error__" in nxt:
            return httpx.Response(nxt["__http_error__"], text=nxt.get("text", ""))
        return httpx.Response(200, json=nxt)

    def patched(self):
        server = self

        class _FakeHttpx:
            @staticmethod
            def AsyncClient(**kwargs):
                kwargs.pop("transport", None)
                return httpx.AsyncClient(transport=httpx.MockTransport(server.handler), **kwargs)

        return mock.patch.object(local_llm, "httpx", _FakeHttpx)

    # Convenience accessors over recorded requests -------------------------

    def tool_names_offered(self, request_index: int = 0) -> list[str]:
        return [t["function"]["name"] for t in self.requests[request_index].get("tools", [])]

    def tool_messages(self, request_index: int) -> list[str]:
        """Contents of every role=tool message the model was sent on that turn."""
        return [
            m["content"] for m in self.requests[request_index]["messages"] if m.get("role") == "tool"
        ]
