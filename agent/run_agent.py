#!/usr/bin/env python3
"""LangChain entrypoint for fetching a model bundle and proving one image."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
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


REPO_ROOT = Path(__file__).resolve().parents[1]


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
        with urllib.request.urlopen(request, timeout=600) as response:
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
        bundle = read_current_bundle_from_contract(rpc_url, gm_storage_address, registry_address=registry_address)
        download = fetch_onchain_bundle(bundle, args.download_dir, ipfs_api_url=ipfs_api_url)
        verification = verify_download_with_registry(bundle, download, rpc_url, registry_address)
        if not verification["ok"]:
            return {
                "bundle": bundle,
                "download": download,
                "verification": verification,
                "proof_run": {"ok": False, "stage": "signature_verification"},
            }
    else:
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


async def langchain_agent(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_ollama import ChatOllama
        from langgraph.prebuilt import create_react_agent
    except ImportError as exc:
        raise RuntimeError(
            "LangChain mode needs agent/requirements.txt. Install it with: pip install -r agent/requirements.txt"
        ) from exc

    client = MultiServerMCPClient(
        {
            "zk_inference": {
                "command": str(Path(sys.executable)),
                "args": [str(REPO_ROOT / "agent" / "zk_mcp_server.py")],
                "transport": "stdio",
            }
        }
    )
    tools = _local_langchain_tools() + await client.get_tools()
    model_name = os.environ.get("OLLAMA_MODEL", "gemma4:e2b")
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    llm = ChatOllama(model=model_name, base_url=base_url, temperature=0)
    agent = create_react_agent(llm, tools)
    prompt = (
        "You are the local thesis ZK inference agent. "
        "Use the GMStorage smart contract as the source of truth for the current model CID and signature CID. "
        "Read the last aggregator from GMStorage, get its public key from DeviceRegistry, "
        "and verify the downloaded model signature. "
        "Only if verification succeeds, use the MCP zk_inference tools to prepare an MNIST sample, "
        "create a single-image prediction, and generate a proof. "
        "Only use local IPFS metadata search as a debug fallback if the contract source fails. "
        f"Use MNIST index {args.index} if it is not null, otherwise let the tool select a random correctly "
        "classified image. "
        "Return the prediction label, true label if available, model path, signature path, and proof path."
    )
    response = await agent.ainvoke({"messages": [("user", prompt)]})
    return {"messages": [getattr(message, "content", str(message)) for message in response["messages"]]}


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
    parser.add_argument("--ipfs-api-url", default=DEFAULT_IPFS_API_URL)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
