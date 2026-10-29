"""Characterize our real Mem0 boundary using only a local embedding HTTP fixture."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def test_mem0_approved_text_crud_preserves_metadata_without_llm_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    monkeypatch.setenv("MEM0_DIR", str(tmp_path / "mem0-home"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(self.path)
            if self.path != "/v1/embeddings":
                self.send_error(503)
                return
            inputs = body["input"]
            if isinstance(inputs, str):
                inputs = [inputs]
            result = {"object": "list", "model": body["model"], "data": [
                {"object": "embedding", "index": i, "embedding": [1.0, float(i + 1), 0.5]}
                for i, _ in enumerate(inputs)
            ], "usage": {"prompt_tokens": 1, "total_tokens": 1}}
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    memory = None
    try:
        from mem0 import Memory

        url = f"http://127.0.0.1:{server.server_port}/v1"
        config = {
            "llm": {"provider": "openai", "config": {
                "model": "contract-no-llm", "api_key": "fixture", "openai_base_url": url}},
            "embedder": {"provider": "openai", "config": {
                "model": "contract-embedding", "api_key": "fixture",
                "openai_base_url": url, "embedding_dims": 3}},
            "vector_store": {"provider": "qdrant", "config": {
                "path": str(tmp_path / "vectors"), "collection_name": "contract",
                "embedding_model_dims": 3}},
            "history_db_path": str(tmp_path / "history.db"),
        }
        memory = Memory.from_config(config)
        added = memory.add("甲喜欢茶", user_id="group-123", infer=False,
                           metadata={"app_id": "memory-1", "subject_id": "甲", "tier": "important",
                                     "revision": 1})
        mid = added["results"][0]["id"]
        memory.add("乙喜欢咖啡", user_id="group-456", infer=False,
                   metadata={"app_id": "memory-2", "tier": "secondary"})
        found = memory.search("偏好", user_id="group-123", filters={"tier": "important"})
        assert [x["id"] for x in found["results"]] == [mid]
        assert found["results"][0]["metadata"]["app_id"] == "memory-1"
        memory.update(mid, "甲现在喜欢咖啡", metadata={"revision": 2})
        assert memory.get(mid)["memory"] == "甲现在喜欢咖啡"
        assert memory.get(mid)["metadata"]["revision"] == 2
        memory.delete(mid)
        assert memory.search("偏好", user_id="group-123")["results"] == []
        assert requests and set(requests) == {"/v1/embeddings"}
    finally:
        if memory:
            memory.vector_store.client.close()
            memory.db.connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
