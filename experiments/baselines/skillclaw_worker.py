"""Isolated native SkillClaw process. No AER execution/evaluation imports."""

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sys


def build_app(settings):
    # Resolve these only in the native interpreter, after sys.path isolation.
    from fastapi import HTTPException, Request
    from skillclaw.config import SkillClawConfig
    from skillclaw.skill_manager import SkillManager
    from skillclaw.skill_hub import SkillHub
    from evolve_server.core.config import EvolveServerConfig
    from evolve_server.engines.workflow import EvolveServer
    from skillclaw_responses import ResponsesCollector

    root = Path(settings["root"])
    library = root / "library"
    learn = settings["mode"] == "learn"
    def endpoint(name, field):
        return os.environ[settings[name][field]]
    config = SkillClawConfig(claw_type="codex", configure_openclaw=False, use_prm=False,
        use_skills=True, skills_dir=str(library), retrieval_mode="template",
        llm_api_mode="responses", llm_provider="openai",
        llm_api_key=endpoint("execution_api", "api_key_env"),
        llm_api_base=endpoint("execution_api", "base_url_env"),
        llm_model_id=settings["execution_model"], served_model_name=settings["execution_model"],
        proxy_api_key=os.environ["AER_SKILLCLAW_PROXY_KEY"], proxy_host="127.0.0.1",
        record_dir=str(root / "native-records"),
        sharing_enabled=learn, sharing_upload_sessions=learn, sharing_backend="local",
        sharing_local_root=str(root / "shared"), sharing_group_id="baseline",
        sharing_session_upload_interval=0, sharing_skill_reload_mode="off")
    config.session_idle_close_seconds = 0  # Explicit AER task boundaries, no mid-task idle split.
    hub = SkillHub.from_config(config)
    if learn:
        pushed = hub.push_skills(str(library))
        expected = len(list(library.glob("*/SKILL.md")))
        if pushed["uploaded"] + pushed["skipped"] != expected:
            raise RuntimeError("Native SkillHub did not seed every initial skill")
    proxy = ResponsesCollector(config, skill_manager=SkillManager(str(library)))
    engine = None
    if learn:
        engine = EvolveServer(EvolveServerConfig(storage_backend="local", local_root=str(root / "shared"),
            group_id="baseline", llm_api_key=endpoint("evolution_api", "api_key_env"),
            llm_base_url=endpoint("evolution_api", "base_url_env"),
            llm_model=endpoint("evolution_api", "model_env"),
            llm_max_tokens=settings["llm_max_tokens"], llm_temperature=settings["llm_temperature"],
            use_session_judge=settings["use_session_judge"], use_skill_verifier=settings["use_skill_verifier"],
            publish_mode="direct", skill_reload_mode="off",
            history_path=str(root / "evolve_history.jsonl"), processed_log_path=str(root / "evolve_processed.json")))
    active = None
    lock = asyncio.Lock()
    secret = os.environ["AER_SKILLCLAW_CONTROL_KEY"]

    @proxy.app.post("/aer-native/{command}")
    async def control(command: str, request: Request):
        nonlocal active
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + secret):
            raise HTTPException(401, "Invalid baseline control credentials")
        async with lock:
            payload = await request.json()
            if command == "begin":
                if active is not None:
                    raise HTTPException(409, "A task is already active")
                manager = SkillManager(payload["skills_dir"])
                proxy.skill_manager = manager
                # Keep sync directed at the canonical library, never task workspaces.
                active = payload["session_id"]
                return {"session_id": active}
            if command == "finish":
                sid = payload["session_id"]
                if active != sid:
                    raise HTTPException(409, "Session does not match active task")
                turns = proxy._session_turns.get(sid, [])
                archive = root / "sessions" / (sid + ".json")
                archive.parent.mkdir(exist_ok=True)
                if not learn:
                    archive.write_text(json.dumps({"session_id": sid, "turns": turns}, ensure_ascii=False, indent=2), encoding="utf-8")
                await proxy._close_session(sid, reason="aer_task_completed")
                await proxy._await_background_tasks(30)
                active = None
                proxy.skill_manager = SkillManager(str(library))
                if learn:
                    queued = root / "shared" / "baseline" / "sessions" / (sid + ".json")
                    if not queued.is_file():
                        raise HTTPException(500, "Native session upload missing; inspect native.log")
                    archive.write_bytes(queued.read_bytes())
                if not turns:
                    raise HTTPException(500, "No task traffic reached the SkillClaw proxy")
                return {"session_archive": str(archive), "uploaded": learn,
                    "injected_skills": sorted({name for turn in turns for name in turn.get("injected_skills", [])}),
                    "read_skills": [ref for turn in turns for ref in turn.get("read_skills", [])],
                    "read_receipts": [ref for turn in turns for ref in turn.get("aer_read_receipts", [])],
                    "read_evidence": "full_content_in_tool_output; partial reads remain unknown"}
            if command == "cycle":
                if active is not None or engine is None:
                    raise HTTPException(409, "Evolution requires learn mode and no active task")
                summary = await engine.run_once()
                sync = await asyncio.to_thread(hub.pull_skills, str(library))
                if (sync.get("restored_from_backup") or
                    sync["downloaded"] + sync["skipped"] != sync["total_remote"]):
                    raise HTTPException(500, "Native skill sync failed; inspect native.log")
                proxy.skill_manager = SkillManager(str(library))
                return {"summary": summary, "sync": sync, "evolution_usage": None}
            raise HTTPException(404, "Unknown baseline command")

    return proxy.app


def main():
    settings = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sys.path.insert(0, settings["source"])
    root = Path(settings["root"])
    logging.basicConfig(level=logging.INFO)
    app = build_app(settings)
    import uvicorn
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        (root / "endpoint.json").write_text(json.dumps({"url": f"http://127.0.0.1:{port}"}), encoding="utf-8")
        uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
