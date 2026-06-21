#!/usr/bin/env python3
"""Agent entrypoint for deterministic runs, terminal chat, or a small web UI."""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

try:
    from .agent_skills import (
        describe_agent_skills,
        format_selection_summary,
        format_verified_bundle_summary,
        resolve_preferred_sample_index,
    )
    from .blockchain_source import (
        DEFAULT_ENV_FILE,
        normalize_ipfs_api_url,
    )
    from .ipfs_bundle import (
        DEFAULT_DOWNLOAD_DIR,
        DEFAULT_IPFS_API_URL,
        DEFAULT_IPFS_ROOT,
        discover_latest,
        fetch_bundle,
    )
except ImportError:
    from agent_skills import (
        describe_agent_skills,
        format_selection_summary,
        format_verified_bundle_summary,
        resolve_preferred_sample_index,
    )
    from blockchain_source import (
        DEFAULT_ENV_FILE,
        normalize_ipfs_api_url,
    )
    from ipfs_bundle import (
        DEFAULT_DOWNLOAD_DIR,
        DEFAULT_IPFS_API_URL,
        DEFAULT_IPFS_ROOT,
        discover_latest,
        fetch_bundle,
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_STATE_DIR = REPO_ROOT / "agent" / "state"
DATASET_NAME = os.environ.get("DATASET_NAME", "mnist").strip().lower()
CURRENT_SESSION_STATE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "current_agent_session_state",
    default=None,
)
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
PUBLIC_SKILL_NAMES = (
    "fetch_latest_verified_model_bundle",
    "generate_random_chestmnist_image",
    "generate_zk_inference_proof",
)


def deterministic_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    if args.zk_inference_url:
        import mcp_server

        mcp_server.ZK_INFERENCE_URL = args.zk_inference_url

    if args.source != "contract":
        ipfs_api_url = normalize_ipfs_api_url(args.ipfs_api_url)
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
        return {
            "bundle": bundle,
            "download": download,
            "verification": verification,
            "proof_run": {"ok": False, "stage": "contract_source_required"},
        }

    from mcp_server import (
        fetch_latest_verified_model_bundle,
        generate_random_chestmnist_image,
        generate_zk_inference_proof,
    )

    bundle_result = json.loads(fetch_latest_verified_model_bundle(out_dir=args.workdir))
    selection_result = json.loads(
        generate_random_chestmnist_image(
            index=args.index,
            query_dir=args.query_dir,
            input_json=str(Path(args.workdir) / "input.json"),
        )
    )
    proof_result = json.loads(
        generate_zk_inference_proof(
            workdir=args.workdir,
            model="model_logits.onnx",
            data="input.json",
            skip_calibration=args.skip_calibration,
        )
    )
    return {
        "bundle": bundle_result.get("bundle", {}),
        "download": bundle_result.get("download", {}),
        "verification": bundle_result.get("verification", {}),
        "selection": selection_result.get("selection", {}),
        "proof_run": proof_result,
    }


def _local_langchain_tools():
    from langchain_core.tools import tool

    def _compact_json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

    def _session_state() -> dict[str, Any]:
        state = CURRENT_SESSION_STATE.get()
        if state is None:
            raise RuntimeError("No active chat session is bound to the current tool execution.")
        return state

    def _session_snapshot_dir() -> Path:
        return _session_snapshot_dir_for_state(_session_state())

    def _remember_generated_sample(sample: dict[str, Any]) -> None:
        _remember_generated_sample_for_state(_session_state(), sample)

    def _remember_verified_bundle(payload: dict[str, Any]) -> None:
        _remember_verified_bundle_for_state(_session_state(), payload)

    def _normalize_workdir(workdir: Any) -> str:
        if workdir is None or isinstance(workdir, dict):
            return "zk_inference/out"
        if isinstance(workdir, str):
            normalized = workdir.strip()
            if not normalized or normalized.startswith("<output of "):
                return "zk_inference/out"
            return normalized
        return str(workdir)

    @tool
    def fetch_latest_verified_model_bundle() -> str:
        """Resolve the current on-chain bundle, decrypt it, verify it, and export the model."""
        from mcp_server import fetch_latest_verified_model_bundle as fetch_skill_impl

        payload = json.loads(fetch_skill_impl())
        _remember_verified_bundle(payload)
        return format_verified_bundle_summary(payload)

    @tool
    def generate_random_chestmnist_image(index: Any = None) -> str:
        """Prepare one ChestMNIST sample and the EZKL input artifacts."""
        from mcp_server import generate_random_chestmnist_image as generate_image_impl

        requested_index = resolve_preferred_sample_index(_session_state(), index)
        payload = json.loads(
            generate_image_impl(
                index=requested_index,
                query_dir="zk_inference/single_query",
                input_json=str(Path(_normalize_workdir("zk_inference/out")) / "input.json"),
            )
        )
        if isinstance(payload.get("selection"), dict):
            _remember_generated_sample(payload)
        return format_selection_summary(payload)

    @tool
    def generate_zk_inference_proof() -> str:
        """Run EZKL against the current prepared artifacts."""
        from mcp_server import generate_zk_inference_proof as generate_proof_impl

        payload = json.loads(
            generate_proof_impl(
                workdir=_normalize_workdir("zk_inference/out"),
                model="model_logits.onnx",
                data="input.json",
            )
        )
        return _compact_json(payload)

    return [
        fetch_latest_verified_model_bundle,
        generate_random_chestmnist_image,
        generate_zk_inference_proof,
    ]


def _default_llm_prompt(args: argparse.Namespace) -> str:
    return (
        "You are VITA-FL, a local medical advisor agent for "
        "Verifiable Inference and Trust for AI Agents in Federated Learning. "
        "Answer greetings and simple conversation directly. "
        f"{describe_agent_skills()} "
        "Use the available skill tools instead of composing low-level verification steps yourself. "
        "Treat GMStorage and DeviceRegistry as the source of truth for the current verified bundle. "
        "For proof requests, first fetch the verified bundle if needed, "
        "then prepare a sample, then run the proof skill. "
        "Reuse remembered session artifacts when they satisfy the request. "
        "If session image memory exists, reuse that sample unless the user explicitly requests a different index. "
        "Do not claim that a tool was executed unless you actually called it. "
        "When a tool is needed, call it instead of describing what you would do. "
        "Keep answers short and concrete. "
        f"Active dataset: {DATASET_NAME}."
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


def _session_memory_messages(session_state: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(session_state, dict):
        return []

    lines: list[str] = []
    bundle_state = session_state.get("latest_bundle")
    if isinstance(bundle_state, dict):
        bundle = bundle_state.get("bundle", {})
        verification = bundle_state.get("verification", {})
        lines.append(
            "Current session bundle memory: "
            f"model_cid={bundle.get('model_cid', 'unknown')}, "
            f"signature_cid={bundle.get('signature_cid', 'unknown')}, "
            f"model_path={bundle_state.get('model_path', 'unknown')}, "
            f"encrypted_bundle={bundle_state.get('download', {}).get('encrypted_bundle', 'unknown')}, "
            f"signature_verified={verification.get('ok', 'unknown')}."
        )

    selection = session_state.get("latest_selection")
    if isinstance(selection, dict):
        lines.append(
            "Current session image memory: "
            f"dataset={selection.get('dataset', 'unknown')}, "
            f"source_index={selection.get('source_index', 'unknown')}, "
            f"random_selection={selection.get('random_selection', 'unknown')}, "
            f"true_label={selection.get('true_label', 'unknown')}."
        )

    if not lines:
        return []

    lines.append(
        "Reuse remembered session artifacts when they satisfy the request. "
        "Do not ask the user to repeat an image index if the current session already has a generated sample."
    )
    return [{"role": "assistant", "content": "\n".join(lines)}]


def _history_messages(
    session_messages: list[dict[str, Any]],
    session_state: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    history.extend(_session_memory_messages(session_state))
    for message in session_messages:
        role = str(message.get("role", "")).strip()
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        history.append({"role": role, "content": content})
    return history


def _session_snapshot_dir_for_state(session_state: dict[str, Any]) -> Path:
    session_id = str(session_state.get("id", "")).strip() or "unknown"
    path = AGENT_STATE_DIR / "sessions" / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _remember_generated_sample_for_state(session_state: dict[str, Any], sample: dict[str, Any]) -> None:
    selection = sample.get("selection")
    if not isinstance(selection, dict):
        return
    session_state["latest_selection"] = selection
    snapshot_dir = _session_snapshot_dir_for_state(session_state)
    (snapshot_dir / "latest_selection.json").write_text(
        json.dumps(selection, ensure_ascii=True),
        encoding="utf-8",
    )


def _remember_verified_bundle_for_state(session_state: dict[str, Any], payload: dict[str, Any]) -> None:
    download = payload.get("download")
    verification = payload.get("verification")
    if not isinstance(download, dict) or not isinstance(verification, dict):
        return
    if not verification.get("ok", False):
        return
    model_path = download.get("model_path")
    signature_path = download.get("signature_path")
    if not isinstance(model_path, str) or not isinstance(signature_path, str):
        return
    stored_bundle = {
        "model_path": model_path,
        "signature_path": signature_path,
        "bundle": payload.get("bundle", {}),
        "verification": verification,
        "export": payload.get("export", {}),
        "download": download,
    }
    session_state["latest_bundle"] = stored_bundle
    snapshot_dir = _session_snapshot_dir_for_state(session_state)
    (snapshot_dir / "latest_bundle.json").write_text(
        json.dumps(stored_bundle, ensure_ascii=True),
        encoding="utf-8",
    )


class AgentRuntime:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._agent_chat_model: Any | None = None
        self._agent_executor: Any | None = None
        self._agent_backend = "uninitialized"
        self._tools: list[Any] = []
        self._lock = asyncio.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    async def _new_chat_model(self) -> Any:
        try:
            from langchain_ollama import ChatOllama
        except ImportError as exc:
            raise RuntimeError(
                f"LangChain mode dependencies are missing or incompatible. Original import error: {exc}"
            ) from exc

        model_name = os.environ.get("OLLAMA_MODEL", "qwen3:0.6b")
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        num_predict = int(os.environ.get("OLLAMA_NUM_PREDICT", "64"))
        return ChatOllama(
            model=model_name,
            base_url=base_url,
            reasoning=False,
            temperature=0,
            num_predict=num_predict,
        )

    async def ensure_agent_model(self) -> Any:
        if self._agent_chat_model is not None and self._agent_executor is not None:
            return self._agent_chat_model

        async with self._lock:
            if self._agent_chat_model is not None and self._agent_executor is not None:
                return self._agent_chat_model
            try:
                import langchain.agents as langchain_agents

                chat_model = await self._new_chat_model()
                tools = _local_langchain_tools()

                if not hasattr(langchain_agents, "create_agent"):
                    raise RuntimeError("The installed LangChain version does not expose create_agent().")

                agent_executor = langchain_agents.create_agent(
                    model=chat_model,
                    tools=tools,
                    system_prompt=self._system_prompt(),
                    debug=True,
                    name="master-thesis-agent",
                )

                self._agent_chat_model = chat_model
                self._tools = tools
                self._agent_executor = agent_executor
                self._agent_backend = "langchain_v1"
                return self._agent_chat_model
            except Exception:
                self._agent_chat_model = None
                self._tools = []
                self._agent_executor = None
                self._agent_backend = "uninitialized"
                raise

    def _system_prompt(self) -> str:
        return _default_llm_prompt(self.args)

    def _assistant_response(self, content: str, events: list[dict[str, str]] | None = None) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": content,
            "events": events or [],
        }

    def _tool_event_summary(self, event: dict[str, str]) -> str:
        label = event.get("label", "tool")
        detail = (event.get("detail") or "").strip()
        if not detail:
            return f"Tool `{label}` completed."

        parsed: Any | None = None
        if detail.startswith("{") or detail.startswith("["):
            try:
                parsed = json.loads(detail)
            except json.JSONDecodeError:
                parsed = None

        if label == "fetch_latest_verified_model_bundle":
            return f"Tool `{label}` completed.\n\n{detail}"

        if label == "generate_random_chestmnist_image" and isinstance(parsed, dict):
            selection = parsed.get("selection", {})
            files = selection.get("files", {})
            return "\n".join(
                [
                    f"Tool `{label}` completed.",
                    "",
                    f"stage={parsed.get('stage', 'unknown')}",
                    f"source_index={selection.get('source_index', 'unknown')}",
                    f"true_label={selection.get('true_label', 'unknown')}",
                    f"single_image_npy={files.get('single_image_npy', 'unknown')}",
                    f"ezkl_input_json={files.get('ezkl_input_json', 'unknown')}",
                ]
            )

        if label == "generate_zk_inference_proof" and isinstance(parsed, dict):
            ezkl = parsed.get("ezkl", {})
            artifacts = ezkl.get("artifacts", {})
            return "\n".join(
                [
                    f"Tool `{label}` completed.",
                    "",
                    f"stage={parsed.get('stage', 'unknown')}",
                    f"proof={artifacts.get('proof', 'unknown')}",
                    f"witness={artifacts.get('witness', 'unknown')}",
                    f"settings={artifacts.get('settings', 'unknown')}",
                    f"verification_key={artifacts.get('verification_key', 'unknown')}",
                ]
            )

        return f"Tool `{label}` completed.\n\n{detail}"

    def _fallback_content_from_events(self, events: list[dict[str, str]]) -> str:
        tool_events = [event for event in events if event.get("type") == "tool"]
        if not tool_events:
            return "The agent reached the chat timeout before producing a final answer."
        if len(tool_events) == 1:
            return self._tool_event_summary(tool_events[0]).strip()
        parts = [self._tool_event_summary(event).strip() for event in tool_events]
        return "\n\n".join(parts)

    def _extract_agent_reply(self, response: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
        if self._agent_backend == "langchain_v1":
            tool_events: list[dict[str, str]] = []
            messages = response.get("messages", [])
            assistant_text = ""
            for message in messages:
                msg_type = getattr(message, "type", None)
                if msg_type is None and isinstance(message, dict):
                    msg_type = message.get("type") or message.get("role")
                if msg_type == "tool":
                    label = getattr(message, "name", None)
                    if label is None and isinstance(message, dict):
                        label = message.get("name")
                    tool_events.append(
                        {
                            "type": "tool",
                            "label": label or "tool",
                            "detail": _message_text(message),
                        }
                    )
                elif msg_type in {"ai", "assistant"}:
                    assistant_text = _message_text(message)
            return assistant_text or "The agent completed without a text response.", tool_events

        tool_events = []
        for action, observation in response.get("intermediate_steps", []):
            tool_events.append(
                {
                    "type": "tool",
                    "label": action.tool,
                    "detail": f"Input: {action.tool_input}",
                }
            )
        return response["output"], tool_events

    def _assistant_claims_tool_completion(self, content: str, tool_events: list[dict[str, str]]) -> bool:
        if tool_events:
            return False
        normalized = content.strip()
        if not normalized:
            return False
        if normalized.startswith("Tool `"):
            return True
        lowered = normalized.lower()
        return any(skill_name in lowered for skill_name in PUBLIC_SKILL_NAMES)

    def _infer_requested_skill(self, user_message: str) -> str | None:
        lowered = user_message.strip().lower()
        if not lowered:
            return None
        if "fetch_latest_verified_model_bundle" in lowered:
            return "fetch_latest_verified_model_bundle"
        if "generate_random_chestmnist_image" in lowered:
            return "generate_random_chestmnist_image"
        if "generate_zk_inference_proof" in lowered:
            return "generate_zk_inference_proof"
        if "bundle" in lowered and any(token in lowered for token in ("fetch", "latest", "verified")):
            return "fetch_latest_verified_model_bundle"
        if "chestmnist" in lowered or ("random" in lowered and "image" in lowered):
            return "generate_random_chestmnist_image"
        if "proof" in lowered or "ezkl" in lowered:
            return "generate_zk_inference_proof"
        return None

    def _execute_skill_fallback(
        self,
        session_state: dict[str, Any],
        user_message: str,
    ) -> dict[str, Any] | None:
        skill_name = self._infer_requested_skill(user_message)
        if skill_name is None:
            return None

        if skill_name == "fetch_latest_verified_model_bundle":
            from mcp_server import fetch_latest_verified_model_bundle

            payload = json.loads(fetch_latest_verified_model_bundle())
            _remember_verified_bundle_for_state(session_state, payload)
            detail = format_verified_bundle_summary(payload)
            return self._assistant_response(
                f"Tool `{skill_name}` completed.\n\n{detail}",
                [{"type": "tool", "label": skill_name, "detail": detail}],
            )

        if skill_name == "generate_random_chestmnist_image":
            from mcp_server import generate_random_chestmnist_image

            requested_index = resolve_preferred_sample_index(session_state, None)
            payload = json.loads(
                generate_random_chestmnist_image(
                    index=requested_index,
                    query_dir="zk_inference/single_query",
                    input_json=str(Path("zk_inference/out") / "input.json"),
                )
            )
            _remember_generated_sample_for_state(session_state, payload)
            detail = format_selection_summary(payload)
            return self._assistant_response(
                f"Tool `{skill_name}` completed.\n\n{detail}",
                [{"type": "tool", "label": skill_name, "detail": detail}],
            )

        if skill_name == "generate_zk_inference_proof":
            from mcp_server import generate_zk_inference_proof

            payload = json.loads(
                generate_zk_inference_proof(
                    workdir="zk_inference/out",
                    model="model_logits.onnx",
                    data="input.json",
                )
            )
            detail = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
            return self._assistant_response(
                self._tool_event_summary({"type": "tool", "label": skill_name, "detail": detail}),
                [{"type": "tool", "label": skill_name, "detail": detail}],
            )

        return None

    async def _run_agent_with_timeout(
        self,
        session_messages: list[dict[str, Any]],
        session_state: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any] | None, bool]:
        latest_response: dict[str, Any] | None = None
        timed_out = False
        try:
            async with asyncio.timeout(timeout_seconds):
                async for chunk in self._agent_executor.astream(
                    {"messages": _history_messages(session_messages, session_state=session_state)},
                    stream_mode="values",
                ):
                    if isinstance(chunk, dict):
                        latest_response = chunk
        except TimeoutError:
            timed_out = True
        return latest_response, timed_out

    def create_session(self, title: str | None = None) -> dict[str, Any]:
        session_id = uuid.uuid4().hex[:12]
        session = {
            "id": session_id,
            "title": title or "New session",
            "messages": [],
            "state": {"id": session_id},
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

    def _public_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": session["id"],
            "title": session["title"],
            "messages": session["messages"],
        }

    async def chat(self, session_id: str, user_message: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session["title"] == "New session":
            session["title"] = user_message[:60] or "New session"
        session["messages"].append({"role": "user", "content": user_message})

        try:
            await self.ensure_agent_model()
            print(f"[route] agent_planner message={user_message!r}", file=sys.stderr)
            session_state = session.setdefault("state", {})
            session_state.setdefault("id", session_id)
            token = CURRENT_SESSION_STATE.set(session_state)
            try:
                response, timed_out = await self._run_agent_with_timeout(
                    session["messages"],
                    session_state,
                    timeout_seconds=float(os.environ.get("AGENT_CHAT_TIMEOUT_SECONDS", "300")),
                )
            finally:
                CURRENT_SESSION_STATE.reset(token)
            if response is not None:
                content, tool_events = self._extract_agent_reply(response)
                if self._assistant_claims_tool_completion(content, tool_events):
                    fallback_message = self._execute_skill_fallback(session_state, user_message)
                    if fallback_message is not None:
                        assistant_message = fallback_message
                    else:
                        assistant_message = self._assistant_response(
                            "The model claimed a tool result without executing a real skill call. "
                            "Please retry with an explicit skill request.",
                            [{"type": "error", "label": "Hallucinated Tool Call", "detail": content}],
                        )
                elif timed_out and tool_events:
                    assistant_message = self._assistant_response(
                        self._fallback_content_from_events(tool_events),
                        tool_events,
                    )
                elif content == "The agent completed without a text response." and tool_events:
                    assistant_message = self._assistant_response(
                        self._fallback_content_from_events(tool_events),
                        tool_events,
                    )
                elif timed_out:
                    assistant_message = self._assistant_response(
                        "The local LLM did not answer before the chat timeout. "
                        "Please try a shorter prompt or ask for a specific artifact/tool.",
                        [{"type": "error", "label": "Chat Timeout", "detail": "Local Ollama response timed out"}],
                    )
                else:
                    assistant_message = self._assistant_response(content, tool_events)
            elif timed_out:
                assistant_message = self._assistant_response(
                    "The local LLM did not answer before the chat timeout. "
                    "Please try a shorter prompt or ask for a specific artifact/tool.",
                    [{"type": "error", "label": "Chat Timeout", "detail": "Local Ollama response timed out"}],
                )
            else:
                raise RuntimeError("The agent did not return any response payload.")
        except Exception as exc:
            print(f"[agent] chat request failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            assistant_message = self._assistant_response(
                f"The agent could not complete this request: {exc}",
                [{"type": "error", "label": "Agent Error", "detail": str(exc)}],
            )
        session["messages"].append({"role": "assistant", **assistant_message})
        return self._public_session(session)

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
        from fastapi import FastAPI, HTTPException, Response
        from fastapi.responses import HTMLResponse
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("Server mode requires FastAPI and uvicorn in agent/requirements.txt") from exc

    from mcp_server import reset_mcp_tool_call_metrics

    reset_mcp_tool_call_metrics()
    runtime = AgentRuntime(args)
    app = FastAPI(title="Master Thesis Agent")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return API_HOME_HTML

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": runtime.list_sessions()}

    @app.post("/api/sessions")
    async def create_session(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = payload or {}
        if body.get("reset_metrics"):
            from mcp_server import reset_mcp_tool_call_metrics

            reset_mcp_tool_call_metrics()
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
        from mcp_server import reset_mcp_tool_call_metrics

        runtime.clear_sessions()
        reset_mcp_tool_call_metrics()
        return {"ok": True}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            session = runtime.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown session") from exc
        return {"session": runtime._public_session(session)}

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
        help="Start a persistent web chat service backed by the same MCP/Ollama agent.",
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
