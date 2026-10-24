"""Adapter registry. Real-client adapters are listed with their verification status."""

from __future__ import annotations

from ..storage import InvalidInput
from .base import Adapter


def get_adapter(name: str, options: dict | None = None) -> Adapter:
    if name == "fake":
        from .fake import FakeAdapter
        return FakeAdapter(options)
    if name == "claude-agents":
        from .claude import ClaudeAgentsAdapter
        return ClaudeAgentsAdapter(options)
    if name == "claude-stream":
        from .claude import ClaudeStreamAdapter
        return ClaudeStreamAdapter(options)
    if name == "codex-local":
        from .codex_local import CodexLocalAdapter
        return CodexLocalAdapter(options)
    if name == "codex-app-server":
        from .codex import CodexAppServerAdapter
        return CodexAppServerAdapter(options)
    raise InvalidInput(f"unknown adapter {name!r}; available: {', '.join(ADAPTERS)}")


ADAPTERS = ("fake", "claude-agents", "claude-stream", "codex-app-server", "codex-local")
