"""GitHub deployment adapter (REST API + git push, no ``gh`` CLI)."""

from src.infrastructure.github.github_adapter import (
    GitHubAdapter,
    GitHubError,
    build_pr_body,
)

__all__ = ["GitHubAdapter", "GitHubError", "build_pr_body"]
