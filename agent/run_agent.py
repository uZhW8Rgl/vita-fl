#!/usr/bin/env python3
"""Agent entrypoint for deterministic runs, terminal chat, or a small web UI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from blockchain_source import (
    DEFAULT_ENV_FILE,
    fetch_onchain_bundle,
    load_env_file,
    normalize_host_rpc_url,
    normalize_ipfs_api_url,
    read_current_bundle_from_contract,
    verify_download_with_registry,
)
from ipfs_rag import (
    DEFAULT_DOWNLOAD_DIR,
    DEFAULT_IPFS_API_URL,
    DEFAULT_IPFS_ROOT,
    discover_latest,
    fetch_bundle,
    list_ipfs_tree,
    rag_search,
)
from otel_trace import service_edge


REPO_ROOT = Path(__file__).resolve().parents[1]
API_HOME_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Agent API</title>
</head>
<body>
  <h1>Master Thesis Agent API</h1>
  <p>The chat UI is served by the separate <code>ui</code> container.</p>
  <p>Use <code>/health</code>, <code>/api/sessions</code>, and <code>/api/chat</code> from the UI proxy.</p>
</body>
</html>
"""


def remote_prove_single_image(
    service_url: str,
    *,
    model_path: str,
    signature_path: str,
    index: int | None,
    workdir: str,
    query_dir: str,
    skip_calibration: bool,
) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model_path": model_path,
            "signature_path": signature_path,
            "index": index,
            "workdir": workdir,
            "query_dir": query_dir,
            "skip_calibration": skip_calibration,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        service_url.rstrip("/") + "/prove-single-image",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with service_edge("agent.prove_single_image", source="agent", target="zk-inference"):
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"zk_inference service returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach zk_inference service at {service_url}: {exc}") from exc


def remote_prepare_mnist_sample(
    service_url: str,
    *,
    index: int | None,
    images: str = "data/mnist/data/t10k-images.idx3-ubyte",
    labels: str = "data/mnist/data/t10k-labels.idx1-ubyte",
    out_dir: str = "zk_inference/single_query",
    input_json: str = "zk_inference/out/input.json",
    metadata_out: str = "zk_inference/single_query/selection.json",
    seed: int | None = None,
) -> dict[str, Any]:
    payload = json.dumps(
        {
            "index": index,
            "images": images,
            "labels": labels,
            "out_dir": out_dir,
            "input_json": input_json,
            "metadata_out": metadata_out,
            "seed": seed,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        service_url.rstrip("/") + "/prepare-mnist-sample",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with service_edge("agent.prepare_mnist_sample", source="agent", target="zk-inference"):
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"zk_inference service returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach zk_inference service at {service_url}: {exc}") from exc


def deterministic_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    ipfs_api_url = normalize_ipfs_api_url(args.ipfs_api_url)
    if args.source == "contract":
        env_file = load_env_file(args.env_file)
        rpc_url = normalize_host_rpc_url(args.rpc_url or env_file.get("RPC_URL", "http://127.0.0.1:8545"))
        gm_storage_address = args.gm_storage_address or env_file.get("GM_STORAGE_ADDRESS", "")
        registry_address = args.registry_address or env_file.get("REGISTRY_ADDRESS", "")
        with service_edge("agent.read_contract_bundle", source="agent", target="smart-contracts"):
            bundle = read_current_bundle_from_contract(rpc_url, gm_storage_address, registry_address=registry_address)
        with service_edge("agent.fetch_ipfs_bundle", source="agent", target="ipfs-kubo"):
            download = fetch_onchain_bundle(bundle, args.download_dir, ipfs_api_url=ipfs_api_url)
        with service_edge("agent.verify_registry", source="agent", target="smart-contracts"):
            verification = verify_download_with_registry(bundle, download, rpc_url, registry_address)
        if not verification["ok"]:
            return {
                "bundle": bundle,
                "download": download,
                "verification": verification,
                "proof_run": {"ok": False, "stage": "signature_verification"},
            }
    else:
        with service_edge("agent.discover_ipfs_bundle", source="agent", target="ipfs-kubo"):
            bundle = discover_latest(
                api_url=ipfs_api_url,
                root=args.ipfs_root,
                require_latest_model=args.require_latest_model,
                pair_window_seconds=args.pair_window_seconds,
            )
            download = fetch_bundle(bundle, args.download_dir, api_url=ipfs_api_url)
        verification = {
            "ok": None,
            "skipped": "source=ipfs-scan has no on-chain last aggregator context",
        }

    if args.zk_inference_url:
        proof = remote_prove_single_image(
            args.zk_inference_url,
            model_path=download["model_path"],
            signature_path=download["signature_path"],
            index=args.index,
            workdir=args.workdir,
            query_dir=args.query_dir,
            skip_calibration=args.skip_calibration,
        )
    else:
        sys.path.insert(0, str(REPO_ROOT / "agent"))
        from zk_mcp_server import prove_single_image

        proof = prove_single_image(
            model_path=download["model_path"],
            signature_path=download["signature_path"],
            index=args.index,
            workdir=args.workdir,
            query_dir=args.query_dir,
            skip_calibration=args.skip_calibration,
        )
    return {"bundle": bundle, "download": download, "verification": verification, "proof_run": proof}


def _local_langchain_tools():
    from langchain_core.tools import tool

    @tool
    def fetch_current_onchain_model_bundle() -> str:
        """Read current CIDs from GMStorage, fetch both artifacts, and verify the signature."""

        env_file = load_env_file(DEFAULT_ENV_FILE)
        rpc_url = normalize_host_rpc_url(os.environ.get("RPC_URL") or env_file.get("RPC_URL", "http://127.0.0.1:8545"))
        gm_storage_address = os.environ.get("GM_STORAGE_ADDRESS") or env_file.get("GM_STORAGE_ADDRESS", "")
        registry_address = os.environ.get("REGISTRY_ADDRESS") or env_file.get("REGISTRY_ADDRESS", "")
        ipfs_api_url = normalize_ipfs_api_url(os.environ.get("IPFS_API_URL", DEFAULT_IPFS_API_URL))
        bundle = read_current_bundle_from_contract(rpc_url, gm_storage_address, registry_address=registry_address)
        download = fetch_onchain_bundle(bundle, DEFAULT_DOWNLOAD_DIR, ipfs_api_url=ipfs_api_url)
        verification = verify_download_with_registry(bundle, download, rpc_url, registry_address)
        return json.dumps({"bundle": bundle, "download": download, "verification": verification}, indent=2)

    @tool
    def rag_search_ipfs_models(query: str) -> str:
        """Debug fallback: search local IPFS model/signature metadata for relevant artifacts."""

        ipfs_api_url = normalize_ipfs_api_url(os.environ.get("IPFS_API_URL", DEFAULT_IPFS_API_URL))
        entries = list_ipfs_tree(api_url=ipfs_api_url, root=DEFAULT_IPFS_ROOT)
        return json.dumps(rag_search(query, entries), indent=2)

    @tool
    def fetch_latest_model_bundle() -> str:
        """Fetch the latest aggregated model and matching signature from local IPFS."""

        ipfs_api_url = normalize_ipfs_api_url(os.environ.get("IPFS_API_URL", DEFAULT_IPFS_API_URL))
        bundle = discover_latest(api_url=ipfs_api_url, root=DEFAULT_IPFS_ROOT)
        download = fetch_bundle(bundle, DEFAULT_DOWNLOAD_DIR, api_url=ipfs_api_url)
        return json.dumps({"bundle": bundle, "download": download}, indent=2)

    return [fetch_current_onchain_model_bundle, rag_search_ipfs_models, fetch_latest_model_bundle]


def _default_llm_prompt(args: argparse.Namespace) -> str:
    return (
        "You are the local thesis assistant and orchestration agent. "
        "Stay conversational, but when a user asks about model provenance, bundle discovery, signatures, "
        "proof generation, or MNIST inference, decide whether to answer directly, inspect local IPFS/RAG "
        "metadata, or call the MCP zk_inference tools. "
        "Treat GMStorage and DeviceRegistry as the source of truth for the current verified model bundle. "
        "Use local IPFS metadata search as a fallback or debugging aid. "
        "When you run a proof-related flow, include prediction label, true label if available, model path, "
        "signature path, and proof path. "
        f"Default to MNIST index {args.index} only when a proof workflow needs a specific sample and none was given."
    )


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part).strip()
    return str(content)


class AgentRuntime:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._chat_model: Any | None = None
        self._lock = asyncio.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    async def ensure_chat_model(self) -> Any:
        if self._chat_model is not None:
            return self._chat_model

        async with self._lock:
            if self._chat_model is not None:
                return self._chat_model
            try:
                from langchain_ollama import ChatOllama
            except ImportError as exc:
                raise RuntimeError(
                    "LangChain mode needs agent/requirements.txt. Install it with: pip install -r agent/requirements.txt"
                ) from exc

            model_name = os.environ.get("OLLAMA_MODEL", "qwen3:0.6b")
            base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            self._chat_model = ChatOllama(model=model_name, base_url=base_url, temperature=0)
            return self._chat_model

    def _system_prompt(self) -> str:
        return _default_llm_prompt(self.args)

    def _assistant_response(self, content: str, tool_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content}
        if tool_events:
            payload["tool_events"] = tool_events
        return payload

    def _route_message(self, user_message: str) -> str:
        lowered = user_message.lower()
        if "mnist" in lowered and any(
            term in lowered for term in ("image", "bild", "preview", "show", "zeige", "generier", "generate")
        ):
            return "mnist_image"
        if any(term in lowered for term in ("proof", "prove", "ezkl", "mnist", "inference", "predict")):
            return "proof"
        if any(
            term in lowered
            for term in (
                "bundle",
                "signature",
                "aggregator",
                "registry",
                "on-chain",
                "onchain",
                "gmstorage",
                "current model",
                "latest model",
                "provenance",
            )
        ):
            return "bundle"
        if any(term in lowered for term in ("rag", "search", "ipfs", "cid", "artifact", "debug")):
            return "rag"
        return "chat"

    def _extract_index(self, user_message: str) -> int | None:
        match = re.search(r"\b(?:index|sample|mnist)\s*[:=]?\s*(\d+)\b", user_message, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    async def _plain_chat(self, messages: list[tuple[str, str]]) -> str:
        model = await self.ensure_chat_model()
        response = await model.ainvoke(messages)
        return _message_text(response)

    async def _summarize_tool_output(self, user_message: str, payload: dict[str, Any], preface: str) -> str:
        tool_json = json.dumps(payload, indent=2)
        return await self._plain_chat(
            [
                (
                    "system",
                    "You are a concise local thesis assistant. Summarize the provided tool output accurately. "
                    "If a workflow failed, explain the failure plainly and do not pretend it succeeded.",
                ),
                (
                    "user",
                    f"{preface}\n\nUser question:\n{user_message}\n\nTool output:\n{tool_json}",
                ),
            ]
        )

    def _fetch_current_onchain_bundle(self) -> dict[str, Any]:
        env_file = load_env_file(DEFAULT_ENV_FILE)
        rpc_url = normalize_host_rpc_url(os.environ.get("RPC_URL") or env_file.get("RPC_URL", "http://127.0.0.1:8545"))
        gm_storage_address = os.environ.get("GM_STORAGE_ADDRESS") or env_file.get("GM_STORAGE_ADDRESS", "")
        registry_address = os.environ.get("REGISTRY_ADDRESS") or env_file.get("REGISTRY_ADDRESS", "")
        ipfs_api_url = normalize_ipfs_api_url(os.environ.get("IPFS_API_URL", DEFAULT_IPFS_API_URL))
        bundle = read_current_bundle_from_contract(rpc_url, gm_storage_address, registry_address=registry_address)
        download = fetch_onchain_bundle(bundle, DEFAULT_DOWNLOAD_DIR, ipfs_api_url=ipfs_api_url)
        verification = verify_download_with_registry(bundle, download, rpc_url, registry_address)
        return {"bundle": bundle, "download": download, "verification": verification}

    async def _handle_bundle_request(self, user_message: str) -> dict[str, Any]:
        try:
            result = self._fetch_current_onchain_bundle()
            tool_events = [
                {"type": "route", "label": "Bundle Route", "detail": "On-chain bundle lookup selected"},
                {"type": "tool", "label": "GMStorage", "detail": "Read current model/signature CIDs from contract"},
                {"type": "tool", "label": "IPFS Fetch", "detail": "Downloaded model bundle from IPFS"},
                {"type": "tool", "label": "Registry Verify", "detail": "Verified signature against DeviceRegistry"},
            ]
        except Exception as exc:
            ipfs_api_url = normalize_ipfs_api_url(os.environ.get("IPFS_API_URL", DEFAULT_IPFS_API_URL))
            bundle = discover_latest(api_url=ipfs_api_url, root=DEFAULT_IPFS_ROOT)
            download = fetch_bundle(bundle, DEFAULT_DOWNLOAD_DIR, api_url=ipfs_api_url)
            result = {
                "status": "ipfs_fallback",
                "fallback_reason": str(exc),
                "bundle": bundle,
                "download": download,
            }
            tool_events = [
                {"type": "route", "label": "Bundle Route", "detail": "On-chain lookup failed, switched to IPFS fallback"},
                {"type": "tool", "label": "IPFS Discover", "detail": "Resolved latest bundle directly from IPFS"},
            ]
        content = await self._summarize_tool_output(
            user_message,
            result,
            "Explain the current verified on-chain model bundle, its signature verification state, and the relevant paths.",
        )
        return self._assistant_response(content, tool_events)

    async def _handle_rag_request(self, user_message: str) -> dict[str, Any]:
        ipfs_api_url = normalize_ipfs_api_url(os.environ.get("IPFS_API_URL", DEFAULT_IPFS_API_URL))
        entries = list_ipfs_tree(api_url=ipfs_api_url, root=DEFAULT_IPFS_ROOT)
        matches = rag_search(user_message, entries)
        if not matches:
            return self._assistant_response(
                "Ich habe auf IPFS gerade keine passenden Modell-/Signatur-Artefakte gefunden.",
                [
                    {"type": "route", "label": "RAG Route", "detail": "Local IPFS metadata search selected"},
                    {"type": "tool", "label": "IPFS Scan", "detail": "Scanned local IPFS metadata tree"},
                ],
            )

        lines = [f"Ich habe {len(matches)} passende IPFS-Artefakte gefunden:"]
        for match in matches:
            metadata = match.get("metadata", {})
            kind = metadata.get("kind", "artifact")
            kind_label = "Modell" if kind == "model" else "Signatur"
            lines.append(
                f"- {kind_label}: {metadata.get('path', 'unbekannt')} | "
                f"CID {metadata.get('hash', 'unbekannt')} | "
                f"Groesse {metadata.get('size', 'unbekannt')}"
            )
        return self._assistant_response(
            "\n".join(lines),
            [
                {"type": "route", "label": "RAG Route", "detail": "Local IPFS metadata search selected"},
                {"type": "tool", "label": "IPFS Scan", "detail": f"Found {len(matches)} matching artifacts"},
            ],
        )

    async def _handle_mnist_image_request(self, user_message: str) -> dict[str, Any]:
        inferred_index = self._extract_index(user_message)
        result = remote_prepare_mnist_sample(
            self.args.zk_inference_url,
            index=inferred_index,
            out_dir="zk_inference/single_query",
            input_json="zk_inference/out/input.json",
            metadata_out="zk_inference/single_query/selection.json",
        )
        selection = result.get("selection", {})
        files = selection.get("files", {})
        lines = ["Ich habe ein MNIST-Bild vorbereitet:"]
        lines.append(f"- Index: {selection.get('source_index', 'unbekannt')}")
        lines.append(f"- True Label: {selection.get('true_label', 'unbekannt')}")
        lines.append(f"- Preview: {files.get('preview_pgm', 'unbekannt')}")
        lines.append(f"- IDX: {files.get('single_image_idx', 'unbekannt')}")
        lines.append(f"- EZKL Input: {files.get('ezkl_input_json', 'unbekannt')}")
        return self._assistant_response(
            "\n".join(lines),
            [
                {"type": "route", "label": "MNIST Image Route", "detail": "Lightweight sample generation selected"},
                {"type": "tool", "label": "prepare_mnist_sample", "detail": "Generated one MNIST sample and preview files"},
                {"type": "service", "label": "zk-inference", "detail": "Served the sample-generation request"},
            ],
        )

    async def _handle_proof_request(self, user_message: str) -> dict[str, Any]:
        proof_args = argparse.Namespace(**vars(self.args))
        inferred_index = self._extract_index(user_message)
        if inferred_index is not None:
            proof_args.index = inferred_index
        result = deterministic_pipeline(proof_args)
        content = await self._summarize_tool_output(
            user_message,
            result,
            "Summarize the proof or inference run. Include verification status, prediction, and file paths when present.",
        )
        return self._assistant_response(
            content,
            [
                {"type": "route", "label": "Proof Route", "detail": "Heavy MNIST proof/inference workflow selected"},
                {"type": "tool", "label": "prove_single_image", "detail": "Exported model, built query, and ran EZKL proof flow"},
                {"type": "service", "label": "zk-inference", "detail": "Executed proof/inference workflow"},
            ],
        )

    def create_session(self, title: str | None = None) -> dict[str, Any]:
        session_id = uuid.uuid4().hex[:12]
        session = {
            "id": session_id,
            "title": title or "New session",
            "messages": [],
        }
        self._sessions[session_id] = session
        return session

    def clear_sessions(self) -> None:
        self._sessions.clear()

    def get_session(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "id": session["id"],
                "title": session["title"],
                "message_count": len(session["messages"]),
            }
            for session in self._sessions.values()
        ]

    async def chat(self, session_id: str, user_message: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session["title"] == "New session":
            session["title"] = user_message[:60] or "New session"

        history = [(message["role"], message["content"]) for message in session["messages"]]
        route = self._route_message(user_message)
        if route == "mnist_image":
            assistant_message = await self._handle_mnist_image_request(user_message)
        elif route == "proof":
            assistant_message = await self._handle_proof_request(user_message)
        elif route == "bundle":
            assistant_message = await self._handle_bundle_request(user_message)
        elif route == "rag":
            assistant_message = await self._handle_rag_request(user_message)
        else:
            assistant_message = self._assistant_response(
                await self._plain_chat([("system", self._system_prompt()), *history, ("user", user_message)]),
                [{"type": "route", "label": "Chat Route", "detail": "Answered directly with the local LLM"}],
            )
        session["messages"].extend(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", **assistant_message},
            ]
        )
        return session

    async def run_prompt(self, prompt: str) -> dict[str, Any]:
        session = self.create_session("CLI prompt")
        updated_session = await self.chat(session["id"], prompt)
        return {"messages": [updated_session["messages"][-1]["content"]]}


async def interactive_langchain_chat(runtime: AgentRuntime) -> dict[str, Any]:
    session = runtime.create_session("Terminal chat")
    print("Interactive agent chat started. Type 'exit' or 'quit' to stop.", file=sys.stderr)

    while True:
        try:
            user_input = input("agent> ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print(file=sys.stderr)
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break

        updated_session = await runtime.chat(session["id"], user_input)
        print(updated_session["messages"][-1]["content"])

    return {"messages": session["messages"]}


async def langchain_agent(args: argparse.Namespace) -> dict[str, Any]:
    runtime = AgentRuntime(args)
    if args.interactive:
        return await interactive_langchain_chat(runtime)

    prompt = args.prompt or runtime._system_prompt()
    return await runtime.run_prompt(prompt)


async def serve_agent(args: argparse.Namespace) -> None:
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("Server mode requires FastAPI and uvicorn in agent/requirements.txt") from exc

    runtime = AgentRuntime(args)
    app = FastAPI(title="Master Thesis Agent")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return API_HOME_HTML

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": runtime.list_sessions()}

    @app.post("/api/sessions")
    async def create_session() -> dict[str, Any]:
        session = runtime.create_session()
        return {
            "session": {
                "id": session["id"],
                "title": session["title"],
                "messages": session["messages"],
            }
        }

    @app.delete("/api/sessions")
    async def clear_sessions() -> dict[str, bool]:
        runtime.clear_sessions()
        return {"ok": True}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            session = runtime.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown session") from exc
        return {"session": session}

    @app.post("/api/chat")
    async def chat(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        message = str(payload.get("message", "")).strip()
        if not session_id or not message:
            raise HTTPException(status_code=400, detail="session_id and message are required")
        try:
            session = await runtime.chat(session_id, message)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown session") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"session": session}

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local IPFS + LangChain/MCP ZK inference agent.")
    parser.add_argument(
        "--source",
        choices=["contract", "ipfs-scan"],
        default=os.environ.get("AGENT_SOURCE", "contract"),
        help="Read CIDs from GMStorage by default, or scan IPFS as a fallback/debug mode.",
    )
    parser.add_argument("--rpc-url", default=os.environ.get("RPC_URL"))
    parser.add_argument("--gm-storage-address", default=os.environ.get("GM_STORAGE_ADDRESS"))
    parser.add_argument("--registry-address", default=os.environ.get("REGISTRY_ADDRESS"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--ipfs-api-url", default=os.environ.get("IPFS_API_URL", DEFAULT_IPFS_API_URL))
    parser.add_argument("--ipfs-root", default=DEFAULT_IPFS_ROOT)
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--workdir", default=os.environ.get("ZK_WORKDIR", "zk_inference/out"))
    parser.add_argument("--query-dir", default=os.environ.get("ZK_QUERY_DIR", "zk_inference/single_query"))
    parser.add_argument("--zk-inference-url", default=os.environ.get("ZK_INFERENCE_URL"))
    parser.add_argument("--index", type=int, default=None)
    parser.add_argument("--skip-calibration", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--require-latest-model",
        action="store_true",
        help="Fail if the newest IPFS model has no matching .sig instead of using the newest complete bundle.",
    )
    parser.add_argument(
        "--pair-window-seconds",
        type=int,
        default=int(os.environ.get("AGENT_PAIR_WINDOW_SECONDS", "120")),
        help="Maximum timestamp distance for pairing a model with a separately written signature.",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Use LangChain LLM agent mode instead of deterministic orchestration.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Custom user prompt for LLM mode. If omitted, the default thesis proof prompt is used.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Start an interactive terminal chat in LLM mode.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start a persistent web chat service backed by the same MCP/RAG/Ollama agent.",
    )
    parser.add_argument("--host", default=os.environ.get("AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGENT_PORT", "8089")))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.serve:
            asyncio.run(serve_agent(args))
            return 0
        if args.llm:
            result = asyncio.run(langchain_agent(args))
        else:
            result = deterministic_pipeline(args)
        print(json.dumps(result, indent=2))
        return 0 if result.get("proof_run", result).get("ok", True) else 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
