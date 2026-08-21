"""Localhost-safe process entry point for the read-only API."""

from __future__ import annotations

import uvicorn


def main() -> None:
    """Serve only on loopback; public deployment is outside the unauthenticated MVP."""
    uvicorn.run("career_os.api.app:app", host="127.0.0.1", port=8000)
