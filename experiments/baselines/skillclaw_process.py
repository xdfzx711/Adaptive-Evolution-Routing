"""Lifecycle of the private native proxy/workflow process."""

import json
import os
from pathlib import Path
import secrets
import subprocess
from time import monotonic, sleep

import httpx
from dotenv import dotenv_values

from plugins.agents.codex import stop_process_tree


class NativeProcess:
    def __init__(self, config, root, execution_model):
        self.root, self.config = Path(root), config
        self.proxy_key = secrets.token_urlsafe(32)
        self.control_key = secrets.token_urlsafe(32)
        self.environment = dict(os.environ)
        if config.dotenv_path:
            for name, value in dotenv_values(config.dotenv_path, interpolate=False).items():
                if value is not None:
                    self.environment.setdefault(name, value)
        endpoints = [config.execution_api] + ([config.evolution_api] if config.mode == "learn" else [])
        for endpoint in endpoints:
            for field in ("api_key_env", "base_url_env", "model_env"):
                name = getattr(endpoint, field)
                if name and not self.environment.get(name):
                    raise ValueError("Missing environment variable: " + name)
        if config.mode == "learn" and not config.evolution_api.model_env:
            raise ValueError("evolution_api.model_env is required in learn mode")
        self.environment.update(AER_SKILLCLAW_PROXY_KEY=self.proxy_key,
            AER_SKILLCLAW_CONTROL_KEY=self.control_key, SKILLCLAW_HOME=str(self.root / "home"),
            PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        settings = {**config.model_dump(), "root": str(self.root), "execution_model": execution_model}
        self.settings_path = self.root / "native-settings.json"
        self.settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        self.process = self.log = self.http = None
        self.url = ""

    def __enter__(self):
        self.log = (self.root / "native.log").open("wb")
        try:
            self.process = subprocess.Popen([self.config.native_python,
                str(Path(__file__).with_name("skillclaw_worker.py")), str(self.settings_path)],
                cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}))
            self.http = httpx.Client(trust_env=False)
            deadline = monotonic() + 45
            while monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Native SkillClaw startup failed; inspect " + str(self.root / "native.log"))
                endpoint = self.root / "endpoint.json"
                if endpoint.is_file():
                    try:
                        self.url = json.loads(endpoint.read_text(encoding="utf-8"))["url"]
                        if self.http.get(self.url + "/healthz", timeout=1).is_success:
                            return self
                    except (httpx.HTTPError, ValueError):
                        pass
                sleep(0.1)
            raise TimeoutError("Native SkillClaw startup timed out")
        except BaseException:
            self.close()
            raise

    def request(self, command, payload=None):
        response = self.http.post(self.url + "/aer-native/" + command,
            headers={"Authorization": "Bearer " + self.control_key}, json=payload or {},
            timeout=self.config.cycle_timeout_seconds if command == "cycle" else 45)
        if not response.is_success:
            # Do not echo provider errors/credentials into the experiment report.
            raise RuntimeError(f"Native SkillClaw {command} failed (HTTP {response.status_code}); inspect native.log")
        return response.json()

    def close(self):
        if self.process is not None and self.process.poll() is None:
            stop_process_tree(self.process)
            self.process.wait(timeout=10)
        if self.http is not None:
            self.http.close()
        if self.log is not None:
            self.log.close()

    def __exit__(self, *_):
        self.close()
