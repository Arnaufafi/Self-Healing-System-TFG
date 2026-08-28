"""GitHub adapter — REST API + ``git push``, with no ``gh`` CLI dependency.

Implements :class:`~src.core.ports.github.GitHubPort` using only the standard
library (``urllib``) for the API call and a ``git push`` subprocess for the
branch upload.  The HTTP transport is injectable so the adapter is unit-testable
without network access.

Authentication uses a GitHub token (a classic PAT with ``repo`` scope, or a
fine-grained token with *Pull requests: write*).  The adapter **never merges** a
PR; human review is the merge gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.core.ports import GitHubPort

_LOGGER = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    """Raised when a push or pull-request operation fails."""


# Injectable transport: (url, payload, headers) -> (status_code, parsed_json)
HttpPost = Callable[[str, Mapping[str, Any], Mapping[str, str]], "tuple[int, dict]"]


def _default_http_post(
    url: str, payload: Mapping[str, Any], headers: Mapping[str, str], timeout: float = 30.0
) -> tuple[int, dict]:
    """POST *payload* as JSON; return ``(status, parsed_body)``. No exceptions for 4xx/5xx."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"message": raw}
        return exc.code, parsed


class GitHubAdapter(GitHubPort):
    """Pushes a branch and opens a PR via the GitHub REST API."""

    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.github.com",
        http_post: HttpPost = _default_http_post,
    ) -> None:
        """Store the *token* and the (injectable) HTTP transport."""
        self._token = token
        self._api = api_base.rstrip("/")
        self._http_post = http_post

    async def push_branch(
        self, *, workspace_path: str, repo: str, branch: str, force: bool = True
    ) -> None:
        """See :meth:`GitHubPort.push_branch`."""
        remote = f"https://x-access-token:{self._token}@github.com/{repo}.git"
        args = ["git", "-C", workspace_path, "push", remote, f"HEAD:refs/heads/{branch}"]
        if force:
            args.append("--force")
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            # Never leak the token in error messages / logs.
            msg = (err or b"").decode("utf-8", errors="replace").replace(self._token, "***")
            raise GitHubError(f"git push failed (rc={proc.returncode}): {msg[:500]}")
        _LOGGER.info("github.push.done", extra={"repo": repo, "branch": branch})

    async def open_pull_request(
        self, *, repo: str, base: str, head: str, title: str, body: str, draft: bool = False
    ) -> str:
        """See :meth:`GitHubPort.open_pull_request`."""
        url = f"{self._api}/repos/{repo}/pulls"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "self-healing-system",
            "Content-Type": "application/json",
        }
        payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}
        status, data = await asyncio.to_thread(self._http_post, url, payload, headers)
        if status == 201:
            pr_url = str(data.get("html_url", ""))
            _LOGGER.info("github.pr.created", extra={"url": pr_url})
            return pr_url
        raise GitHubError(
            f"pull-request creation failed (HTTP {status}): {data.get('message', data)!s}"
        )


def build_pr_body(
    *,
    reproduce_cmd: Sequence[str],
    resolved: Sequence[Any],
    telemetry: dict | None,
    commit_subjects: Sequence[str],
) -> str:
    """Compose a reviewer-friendly Markdown PR body from the heal artefacts."""
    cmd = " ".join(reproduce_cmd) if reproduce_cmd else "the failing command"
    lines = [
        "## 🤖 Automated fix — Self-Healing System",
        "",
        f"This pull request was generated autonomously to repair `{cmd}`.",
        "",
        "> ⚠️ **Human review required — do not auto-merge.** "
        "The change was produced and validated by an autonomous agent inside an "
        "isolated, network-disabled sandbox.",
        "",
    ]
    if commit_subjects:
        lines.append("### Changes")
        lines.extend(f"- {s}" for s in commit_subjects)
        lines.append("")
    if resolved:
        lines.append(f"### Errors healed ({len(resolved)})")
        for r in resolved:
            test_path = getattr(r, "test_path", None)
            suffix = f" — regression test `{test_path}`" if test_path else ""
            lines.append(f"- `{getattr(r, 'signature', r)!s}`{suffix}")
        lines.append("")
    if telemetry and telemetry.get("llm"):
        total = telemetry["llm"].get("total", {})
        lines.append("### Cost &amp; tokens")
        lines.append(
            f"- **${float(total.get('cost_usd', 0)):.4f}** · "
            f"{int(total.get('prompt_tokens', 0))} prompt + "
            f"{int(total.get('completion_tokens', 0))} completion tokens · "
            f"{int(total.get('calls', 0))} LLM calls"
        )
        for agent, bucket in (telemetry["llm"].get("by_agent") or {}).items():
            lines.append(
                f"  - {agent}: ${float(bucket.get('cost_usd', 0)):.4f} "
                f"({int(bucket.get('calls', 0))} calls)"
            )
        lines.append("")
    lines.append("---")
    lines.append("*Self-Healing System — autonomous multi-agent code repair.*")
    return "\n".join(lines)


__all__ = ["GitHubAdapter", "GitHubError", "build_pr_body"]
