"""The LLM client of the AI settings: provider presets, a request adapted to
what a model takes, function tools with a fallback for servers without them,
the model list, and unloading a local Ollama model (llm.py). No network:
httpx.Client is replaced by a scripted server."""
import httpx
import pytest

import llm


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = body if isinstance(body, str) else str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)  # type: ignore[arg-type]


@pytest.fixture
def server(monkeypatch):
    """Scripted replies (status, body) in order; records (method, url, json)."""
    state = {"replies": [], "seen": []}

    class Client:
        def __init__(self, timeout=None, follow_redirects=True):
            assert follow_redirects is False      # the key is never replayed to another host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            state["seen"].append(("POST", url, json, headers))
            return _Resp(*state["replies"].pop(0))

        def get(self, url, headers=None):
            state["seen"].append(("GET", url, None, headers))
            return _Resp(*state["replies"].pop(0))
    monkeypatch.setattr(llm.httpx, "Client", Client)
    llm._NO_TOOLS.clear()
    return state


def _client(base="https://api.openai.com/v1", model="gpt-5-mini", **arch):
    return llm.LLMClient({"architect": {"base_url": base, "model": model, "api_key": "sk-test",
                                        "retries": 0, **arch}})


def _ok(content="hi", **msg):
    return 200, {"choices": [{"message": {"role": "assistant", "content": content, **msg},
                              "finish_reason": "stop"}]}


def test_presets_name_their_endpoint_and_local_servers_are_known():
    assert llm.provider_of("https://api.openai.com/v1/") == "openai"
    assert llm.provider_of("http://127.0.0.1:11434/v1") == "ollama"
    assert llm.provider_of("https://example.test/v1") == "custom"
    assert all(p["key_url"].startswith("https://") for p in llm.PROVIDERS.values() if p["needs_key"])
    assert llm.is_local("http://localhost:1234/v1") and llm.is_local("http://127.0.0.1:11434/v1")
    assert not llm.is_local("https://api.groq.com/openai/v1")


def test_chat_sends_tools_and_returns_the_tool_calls(server):
    calls = [{"id": "c1", "type": "function", "function": {"name": "get_shot", "arguments": "{}"}}]
    server["replies"] = [_ok("", tool_calls=calls)]
    res = _client().chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    assert res["ok"] and res["tools"] and res["message"]["tool_calls"] == calls
    _, url, body, headers = server["seen"][0]
    assert url == "https://api.openai.com/v1/chat/completions" and body["tools"]
    assert headers["Authorization"] == "Bearer sk-test"


def test_a_server_without_tools_is_asked_again_without_them_and_remembered(server):
    server["replies"] = [(400, "this model does not support tools"), _ok("plain")]
    c = _client(base="http://127.0.0.1:11434/v1", model="llama3")
    res = c.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    assert res["ok"] and not res["tools"] and res["message"]["content"] == "plain"
    assert "tools" not in server["seen"][1][2]
    assert not c.tools_ok()
    assert not _client(tools="off").tools_ok()


def test_a_request_is_adapted_to_what_the_model_takes(server):
    server["replies"] = [(400, "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."),
                         (400, "Unsupported value: 'temperature' does not support 0.7, only the default (1)"),
                         _ok("done")]
    res = _client().chat([{"role": "user", "content": "x"}], max_tokens=100)
    assert res["ok"] and res["message"]["content"] == "done"
    last = server["seen"][-1][2]
    assert last["max_completion_tokens"] == 100 and "max_tokens" not in last and "temperature" not in last


def test_errors_come_back_as_messages_not_exceptions(server):
    server["replies"] = [(401, "invalid api key")]
    res = _client().chat([{"role": "user", "content": "x"}])
    assert res == {"ok": False, "error": "HTTP 401: invalid api key"}
    server["replies"] = [(500, "overloaded")]
    assert "HTTP 500" in _client().test()["error"]


def test_the_model_list_and_the_connection_test(server):
    server["replies"] = [(200, {"data": [{"id": "models/gemini-2.5-pro"}, {"id": "gpt-5"}, {"id": "gpt-5"}]}),
                         _ok("OK")]
    c = _client()
    assert c.list_models() == {"ok": True, "models": ["gemini-2.5-pro", "gpt-5"]}
    assert c.test() == {"ok": True, "reply": "OK"}


def test_only_a_local_ollama_is_asked_to_unload_its_model(server):
    server["replies"] = [(200, {})]
    assert _client(base="http://127.0.0.1:11434/v1", model="qwen3:8b").release_memory()
    method, url, body, _ = server["seen"][0]
    assert url == "http://127.0.0.1:11434/api/generate" and body == {"model": "qwen3:8b", "keep_alive": 0}
    assert not _client().release_memory()
    assert not _client(base="http://127.0.0.1:1234/v1").release_memory()    # LM Studio
    assert len(server["seen"]) == 1


def test_a_server_that_answers_at_all_counts_as_reachable():
    llm.forget_reach()
    answers = iter([401, 502])
    calls = []

    def probe(url):
        calls.append(url)
        return next(answers)
    assert llm.reachable("http://127.0.0.1:11434/v1/", probe=probe)          # a 401 still answers
    assert llm.reachable("http://127.0.0.1:11434/v1", probe=probe) and len(calls) == 1   # remembered
    llm.forget_reach()
    assert not llm.reachable("http://127.0.0.1:11434/v1", probe=probe)       # a proxy's 502

    def refused(url):
        raise ConnectionRefusedError()
    llm.forget_reach()
    assert not llm.reachable("http://127.0.0.1:11434/v1", probe=refused)
    assert not llm.reachable("", probe=probe)
    llm.forget_reach()


def test_an_answer_with_no_text_is_an_empty_reply_not_a_failure(server):
    c = _client()
    server["replies"] = [_ok(content=None), (503, "down")]
    assert c.complete("sys", "user") == "" and c.last_error == ""
    assert c.complete("sys", "user") is None and "503" in c.last_error
