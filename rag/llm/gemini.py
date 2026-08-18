import os, json
import time

import requests

from config import Config

REFUSAL = "I couldn't find this information in the available policies."

class GeminiClient:
    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "google/gemini-2.0-flash-001")
        self.base_url = "https://openrouter.ai/api/v1"

    def complete(self, messages: list[dict]) -> str:
        try:
            from openai import OpenAI
            client = OpenAI(base_url=self.base_url, api_key=self.api_key)
            resp = client.chat.completions.create(model=self.model, messages=messages, max_tokens=1024)
            return resp.choices[0].message.content.strip()
        except Exception as e:
            return f"{REFUSAL} (LLM error: {e})"


class OllamaClient:
    def __init__(self, model: str = "qwen2.5:7b", base_url: str | None = None):
        self.model = os.environ.get("OLLAMA_MODEL") or model
        raw_base = base_url or os.environ.get("OLLAMA_URL", Config.OLLAMA_URL)

        normalized = raw_base.strip()
        for suffix in ("/api/chat", "/api/generate", "/v1/chat/completions", "/chat/completions"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        self.base_url = normalized.rstrip("/") or "http://127.0.0.1:11434"
        self.keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", Config.OLLAMA_KEEP_ALIVE) or "1h"

    def _prompt_from_messages(self, messages: list[dict]) -> str:
        parts = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            if not content:
                continue
            parts.append(f"{role}: {content}")
        return "\n".join(parts).strip()

    def _json_payload(self, prompt: str | None = None, *, stream: bool = False, messages: list[dict] | None = None):
        payload = {
            "model": self.model,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.2},
        }
        if prompt is not None:
            payload["prompt"] = prompt
        if messages is not None:
            payload["messages"] = messages
        payload["stream"] = stream
        return payload

    def stream_complete(self, messages: list[dict]):
        try:
            prompt = self._prompt_from_messages(messages)
            if not prompt:
                return

            payload = self._json_payload(prompt=prompt, stream=True)
            response = requests.post(
                f"{self.base_url.rstrip('/')}/api/generate",
                json=payload,
                stream=True,
                timeout=60,
            )
            response.raise_for_status()

            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    try:
                        data = json.loads(line.decode('utf-8'))
                    except Exception:
                        continue
                text = data.get("response")
                if text:
                    yield str(text)
        except Exception:
            return

    def complete(self, messages: list[dict]) -> str:
        try:
            chunks = []
            for token in self.stream_complete(messages):
                chunks.append(str(token))
            return "".join(chunks).strip()
        except Exception as e:
            return f"{REFUSAL} (Ollama error: {e})"


class ExtractiveClient:
    def complete(self, messages: list[dict]) -> str:
        import re
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        excerpts_match = re.search(r"POLICY EXCERPTS:\n\n(.+?)\n\nQUESTION:", user_msg, re.DOTALL)
        question = re.search(r"QUESTION: (.+)", user_msg)
        if not excerpts_match or not question:
            return REFUSAL

        q_tokens = set(re.findall(r"[a-z0-9]+", question.group(1).lower()))
        best, best_score = "", -1

        for block in excerpts_match.group(1).split("---"):
            lines = block.strip().split("\n")
            body = "\n".join(lines[2:]) if len(lines) > 2 and lines[0].strip().startswith("[Excerpt") else block
            sentences = re.split(r"(?<=[.!?])\s+", body.strip())
            for s in sentences:
                s = s.strip()
                if not s:
                    continue
                score = len(set(re.findall(r"[a-z0-9]+", s.lower())) & q_tokens)
                if score > best_score:
                    best, best_score = s, score

        return best if best_score > 0 else REFUSAL


_client = None

def get_llm():
    global _client
    if _client:
        return _client

    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")

    if provider == "ollama":
        import urllib.request

        base_env = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip('/')

        # 1. Ping the base server (fast 3.0s GET check)
        server_up = False
        try:
            with urllib.request.urlopen(base_env, timeout=3.0) as resp:
                if resp.status == 200:
                    server_up = True
        except Exception:
            pass

        if not server_up:
            _client = ExtractiveClient()
            print("[LLM] Ollama server offline on 127.0.0.1:11434 — using extractive fallback")
            return _client

        # 2. Probe candidate endpoints with 10.0s timeout to allow cold model load
        test_paths = [
            f"{base_env}/v1/chat/completions",
            f"{base_env}/api/chat",
        ]

        def _probe(url: str) -> bool:
            try:
                payload = json.dumps({
                    "model": os.environ.get("OLLAMA_MODEL", Config.OLLAMA_MODEL),
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "stream": False,
                    "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", Config.OLLAMA_KEEP_ALIVE),
                    "options": {"temperature": 0.2},
                }).encode()
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    return resp.status == 200
            except Exception:
                return False

        resolved = None
        for url in test_paths:
            if _probe(url):
                resolved = url
                break

        if not resolved:
            resolved = f"{base_env}/v1/chat/completions"

        _client = OllamaClient(base_url=resolved)
        print(f"[LLM] Using Ollama endpoint={resolved} model={_client.model}")

    elif key:
        _client = GeminiClient()
        print("[LLM] Using Gemini via OpenRouter")
    else:
        _client = ExtractiveClient()
        print("[LLM] No API key — using extractive fallback")

    return _client