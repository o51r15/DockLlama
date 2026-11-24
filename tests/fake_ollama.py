"""A fake Ollama for tests, shaped like the real answers from Ollama 0.40.17 on Thor (Nov 23 2026)."""

from __future__ import annotations

import json

import httpx

GB = 10**9


def tag(name, size_gb, family, params, pulled="2026-11-20T23:17:53-05:00", digest=None):
    return {"name": name, "model": name, "modified_at": pulled, "size": int(size_gb * GB),
            "digest": digest or ("d" + str(abs(hash(name)))).ljust(64, "0")[:64],
            "details": {"family": family, "parameter_size": params, "quantization_level": "Q4_K_M"}}


SHOW = {
    "gpt-oss:20b": {"capabilities": ["completion", "tools", "thinking"],
                    "model_info": {"general.architecture": "gpt-oss", "gpt-oss.context_length": 131072},
                    "thinking": {"values": ["low", "medium", "high"], "default": "medium"}},
    "gemma4:latest": {"capabilities": ["completion", "vision", "tools", "thinking"],
                      "model_info": {"general.architecture": "gemma4", "gemma4.context_length": 131072},
                      "thinking": {"values": [False, True], "default": True}},
    "llama3.1:8b": {"capabilities": ["completion", "tools"],
                    "model_info": {"general.architecture": "llama", "llama.context_length": 131072}},
    "granite4.2:8b": {"capabilities": ["tools", "thinking", "completion"],
                      "model_info": {"general.architecture": "granite", "granite.context_length": 131072}},
    "bge-m3:latest": {"capabilities": ["embedding"],
                      "model_info": {"general.architecture": "bert", "bert.context_length": 8192}},
    "tiny:8k": {"capabilities": ["completion"],
                "model_info": {"general.architecture": "tiny", "tiny.context_length": 8192}},
}


class FakeServer:
    def __init__(self, version="0.40.17", tags=None, loaded=None):
        self.version = version
        self.tags = tags if tags is not None else [
            tag("gpt-oss:20b", 13.7, "gpt-oss", "20.9B"),
            tag("gemma4:latest", 6.6, "gemma4", "7.5B"),
            tag("llama3.1:8b", 4.9, "llama", "8.0B"),
            tag("bge-m3:latest", 1.2, "bert", "566.70M"),
        ]
        self.loaded = loaded if loaded is not None else ["gpt-oss:20b"]
        self.generate_calls: list[dict] = []
        self.generate_response = {"response": '{"ok": true}', "done": True, "done_reason": "stop",
                                  "prompt_eval_count": 900, "eval_count": 12}
        self.pull_fails = False


class FakeOllama:
    """httpx transport serving several fake servers by host name."""

    def __init__(self):
        self.servers: dict[str, FakeServer] = {}
        self.not_ollama: set[str] = set()  # hosts that answer, but aren't Ollama

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.not_ollama:
            return httpx.Response(200, text="<html>router login</html>")
        server = self.servers.get(host)
        if server is None:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": server.version})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": server.tags})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in server.loaded]})
        if path == "/api/show":
            name = json.loads(request.content)["model"]
            if name not in {t["name"] for t in server.tags}:
                return httpx.Response(404, json={"error": f"model '{name}' not found"})
            return httpx.Response(200, json=SHOW.get(name, {"capabilities": ["completion"], "model_info": {}}))
        if path == "/api/pull":
            name = json.loads(request.content)["model"]
            if server.pull_fails or name.startswith("nope"):
                return httpx.Response(200, text=json.dumps({"error": "pull model manifest: file does not exist"}) + "\n")
            lines = [{"status": "pulling manifest"},
                     {"status": "pulling abc", "total": 1000, "completed": 500},
                     {"status": "pulling abc", "total": 1000, "completed": 1000},
                     {"status": "success"}]
            server.tags.append(tag(name, 5.0, "llama", "8.0B", pulled="2026-11-24T03:00:00-05:00"))
            return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines) + "\n")
        if path == "/api/generate":
            server.generate_calls.append(json.loads(request.content))
            return httpx.Response(200, json=server.generate_response)
        return httpx.Response(404, json={"error": "not found"})
