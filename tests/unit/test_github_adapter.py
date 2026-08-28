"""Unit tests for the GitHub deployment adapter (no network).

Pins the PR-body content and the REST request the adapter builds — the HTTP
transport is injected, so these run fully offline.
"""

from __future__ import annotations

import pytest

from src.core.domain import ErrorSignature, ResolvedError
from src.infrastructure.github import GitHubAdapter, GitHubError, build_pr_body

# ---------------------------------------------------------------------------
# build_pr_body
# ---------------------------------------------------------------------------


def test_build_pr_body_has_review_notice_changes_and_metrics() -> None:
    resolved = [
        ResolvedError(
            signature=ErrorSignature(kind="crash"), commit_sha="abc1234", test_path="tests/t.py"
        )
    ]
    total = {"calls": 6, "prompt_tokens": 1000, "completion_tokens": 50, "cost_usd": 0.0035}
    telemetry = {
        "llm": {
            "total": total,
            "by_agent": {"corrector": {"calls": 4, "cost_usd": 0.0023}},
        }
    }
    body = build_pr_body(
        reproduce_cmd=("python", "main.py"),
        resolved=resolved,
        telemetry=telemetry,
        commit_subjects=["fix(m): correct the thing"],
    )
    assert "do not auto-merge" in body.lower()  # the safety gate
    assert "python main.py" in body            # what was repaired
    assert "fix(m): correct the thing" in body  # the change
    assert "tests/t.py" in body                 # the immunization test
    assert "$0.0035" in body                    # total cost
    assert "corrector" in body                  # per-agent breakdown


def test_build_pr_body_is_robust_without_telemetry_or_commits() -> None:
    body = build_pr_body(
        reproduce_cmd=("python", "main.py"), resolved=[], telemetry=None, commit_subjects=[]
    )
    assert "Self-Healing System" in body
    assert "do not auto-merge" in body.lower()


# ---------------------------------------------------------------------------
# open_pull_request (injected transport)
# ---------------------------------------------------------------------------


class _CaptureHttp:
    def __init__(self, status: int, data: dict) -> None:
        self.status, self.data, self.calls = status, data, []

    def __call__(self, url, payload, headers):
        self.calls.append((url, payload, headers))
        return self.status, self.data


@pytest.mark.asyncio
async def test_open_pull_request_success_builds_the_right_request() -> None:
    http = _CaptureHttp(201, {"html_url": "https://github.com/o/r/pull/7", "number": 7})
    gh = GitHubAdapter("ghp_secret", http_post=http)
    url = await gh.open_pull_request(
        repo="o/r", base="main", head="selfheal/x", title="t", body="b", draft=True
    )
    assert url == "https://github.com/o/r/pull/7"
    req_url, payload, headers = http.calls[0]
    assert req_url == "https://api.github.com/repos/o/r/pulls"
    assert payload == {
        "title": "t", "head": "selfheal/x", "base": "main", "body": "b", "draft": True,
    }
    assert headers["Authorization"] == "Bearer ghp_secret"
    assert headers["Accept"] == "application/vnd.github+json"


@pytest.mark.asyncio
async def test_open_pull_request_failure_raises_github_error() -> None:
    http = _CaptureHttp(422, {"message": "A pull request already exists"})
    gh = GitHubAdapter("ghp_secret", http_post=http)
    with pytest.raises(GitHubError) as exc_info:
        await gh.open_pull_request(repo="o/r", base="main", head="h", title="t", body="b")
    assert "422" in str(exc_info.value)
