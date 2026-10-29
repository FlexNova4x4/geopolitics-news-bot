"""Persistent state with a local-file mode and a GitHub branch-backed mode.

GitHub Actions uses a separate `bot-state` branch so runtime state commits don't spam `main`.
Secrets are never written to the state file.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

API_ROOT = "https://api.github.com"
STATE_PATH = Path("data/state.local.json")
DEFAULT_STATE: dict[str, Any] = {"schema_version": 1, "items": {}, "last_cycle_at": None}


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, repo: str = "", token: str = "", branch: str = "bot-state", max_items: int = 5000):
        self.repo = repo
        self.token = token
        self.branch = branch
        self.max_items = max_items
        self.remote = bool(repo and token)
        self.session = requests.Session()
        if self.remote:
            self.session.headers.update({"Authorization": f"Bearer {token}",
                                         "Accept": "application/vnd.github+json",
                                         "X-GitHub-Api-Version": "2022-11-28"})
        self.state: dict[str, Any] = DEFAULT_STATE.copy()
        self.state["items"] = {}
        self.load()

    def _api(self, method: str, url: str, **kwargs) -> requests.Response:
        response = self.session.request(method, url, timeout=20, **kwargs)
        if response.status_code >= 400:
            message = ""
            try:
                message = response.json().get("message", "")
            except Exception:
                pass
            raise StateError(f"GitHub state API {method} {response.status_code}: {message or response.text[:250]}")
        return response

    @property
    def state_url(self) -> str:
        return f"{API_ROOT}/repos/{self.repo}/contents/data/state.json"

    def _ensure_branch(self) -> None:
        branch_url = f"{API_ROOT}/repos/{self.repo}/branches/{quote(self.branch, safe='')}"
        response = self.session.get(branch_url, timeout=20)
        if response.status_code == 200:
            return
        if response.status_code != 404:
            raise StateError(f"Cannot check state branch ({response.status_code}): {response.text[:250]}")

        repo_info = self._api("GET", f"{API_ROOT}/repos/{self.repo}").json()
        default_branch = repo_info.get("default_branch") or "main"
        ref_url = f"{API_ROOT}/repos/{self.repo}/git/ref/heads/{quote(default_branch, safe='')}"
        ref = self._api("GET", ref_url).json()
        create_url = f"{API_ROOT}/repos/{self.repo}/git/refs"
        create_response = self.session.post(create_url, json={"ref": f"refs/heads/{self.branch}", "sha": ref["object"]["sha"]}, timeout=20)
        if create_response.status_code not in (201, 422):
            raise StateError(f"Cannot create state branch ({create_response.status_code}): {create_response.text[:250]}")

    def _get_remote_file(self) -> tuple[dict[str, Any], str | None]:
        url = self.state_url + "?ref=" + quote(self.branch, safe="")
        response = self.session.get(url, timeout=20)
        if response.status_code == 404:
            return DEFAULT_STATE.copy(), None
        if response.status_code >= 400:
            raise StateError(f"Cannot read state file ({response.status_code}): {response.text[:250]}")
        payload = response.json()
        try:
            content = payload.get("content", "")
            if payload.get("encoding") == "none" or not content:
                # GitHub's Contents API omits inline content for files over
                # 1 MB. Request the raw representation instead of parsing "".
                raw_response = self.session.get(
                    url,
                    timeout=20,
                    headers={"Accept": "application/vnd.github.raw+json"},
                )
                if raw_response.status_code >= 400:
                    raise ValueError(
                        f"raw state download failed with HTTP {raw_response.status_code}"
                    )
                raw = raw_response.text
            else:
                raw = base64.b64decode(content).decode("utf-8")
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("items", {}), dict):
                raise ValueError("invalid state structure")
            value.setdefault("schema_version", 1)
            value.setdefault("items", {})
            value.setdefault("last_cycle_at", None)
            return value, payload.get("sha")
        except Exception as exc:
            raise StateError(f"State file could not be decoded safely: {exc}") from exc

    def load(self) -> None:
        if self.remote:
            self._ensure_branch()
            value, sha = self._get_remote_file()
            self.state = value
            if sha is None:
                self.save()
            return
        try:
            if STATE_PATH.exists():
                value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(value, dict) and isinstance(value.get("items", {}), dict):
                    self.state = value
                    self.state.setdefault("items", {})
                    self.state.setdefault("last_cycle_at", None)
        except (OSError, json.JSONDecodeError):
            raise StateError("Local state file is unreadable; refusing to start to avoid duplicate posts.")

    def _prune(self) -> None:
        items = self.state.get("items", {})
        cutoff = datetime.now(timezone.utc) - timedelta(days=45)
        retained = []
        for key, value in items.items():
            try:
                when = datetime.fromisoformat(str(value.get("updated_at", "")).replace("Z", "+00:00"))
                if when >= cutoff:
                    retained.append((key, value))
            except Exception:
                retained.append((key, value))
        retained.sort(key=lambda kv: str(kv[1].get("updated_at", "")), reverse=True)
        self.state["items"] = dict(retained[:self.max_items])
        self.state["last_cycle_at"] = datetime.now(timezone.utc).isoformat()

    def save(self) -> None:
        self._prune()
        encoded = json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if not self.remote:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(encoded, encoding="utf-8")
            os.replace(tmp, STATE_PATH)
            return

        content = base64.b64encode(encoded.encode("utf-8")).decode("ascii")
        last_error: Exception | None = None
        for attempt in range(3):
            current, sha = self._get_remote_file()
            body: dict[str, Any] = {"message": "bot-state: persist processed news keys", "content": content,
                                    "branch": self.branch}
            if sha:
                body["sha"] = sha
            response = self.session.put(self.state_url, json=body, timeout=25)
            if response.status_code in (200, 201):
                return
            last_error = StateError(f"Cannot persist state ({response.status_code}): {response.text[:250]}")
            if response.status_code not in (409, 422):
                break
            time.sleep(1 + attempt)
        raise StateError(str(last_error or "Could not persist remote state"))

    def has_key(self, key: str) -> bool:
        return key in self.state.get("items", {})

    def reserve(self, stories: list, post_keys: set[str]) -> None:
        """Record every evaluated candidate before publishing; crash recovery prefers no duplicates."""
        items = self.state.setdefault("items", {})
        for story in stories:
            if story.key in items:
                continue
            status = "reserved" if story.key in post_keys else "filtered"
            items[story.key] = story.to_state_dict(status)
        self.save()

    def set_status(self, key: str, status: str, message_id: int | None = None) -> None:
        item = self.state.setdefault("items", {}).get(key)
        if not item:
            return
        item["status"] = status
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        if message_id is not None:
            item["telegram_message_id"] = message_id
