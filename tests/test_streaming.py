import requests

from rag.llm.gemini import OllamaClient


def test_ollama_stream_complete_yields_tokens(monkeypatch):
    class FakeResponse:
        def __init__(self):
            self._lines = [
                b'{"model":"qwen2.5:7b","response":"Hel"}',
                b'{"response":"lo"}',
                b'{"done":true}',
            ]

        def iter_lines(self):
            return iter(self._lines)

    def fake_post(url, json=None, stream=False, timeout=60):
        assert url.endswith("/api/generate")
        assert stream is True
        assert json["keep_alive"] == "1h"
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    client = OllamaClient()
    tokens = list(client.stream_complete([{"role": "user", "content": "hi"}]))

    assert tokens == ["Hel", "lo"]
