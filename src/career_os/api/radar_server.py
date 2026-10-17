"""Loopback-only launcher for the Career Radar web MVP."""

from __future__ import annotations

import argparse
from functools import lru_cache
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from career_os.api.app import ApiDependencies, create_app
from career_os.application.operations import CollectionOperationalPolicy
from career_os.config.skills import SkillTaxonomy, load_skill_taxonomy
from career_os.db import create_database_engine, create_session_factory
from career_os.settings import load_settings

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_RADAR_DOTENV = _PROJECT_ROOT / ".env.radar.local"
_SKILLS_TAXONOMY = _PROJECT_ROOT / "config/taxonomy/skills.v1.yaml"


def create_radar_app() -> FastAPI:
    """Build the API with Radar's explicit optional dotenv file and lazy DB access."""
    settings = load_settings(env_file=_RADAR_DOTENV if _RADAR_DOTENV.is_file() else None)

    @lru_cache(maxsize=1)
    def session_factory_provider() -> sessionmaker[Session]:
        return create_session_factory(create_database_engine(settings))

    @lru_cache(maxsize=1)
    def taxonomy_provider() -> SkillTaxonomy:
        return load_skill_taxonomy(_SKILLS_TAXONOMY)

    def operational_policy_provider() -> CollectionOperationalPolicy:
        return CollectionOperationalPolicy(
            max_fetch_drop_ratio=settings.max_fetch_drop_ratio,
            circuit_failure_threshold=settings.circuit_failure_threshold,
            circuit_cooldown_seconds=settings.circuit_cooldown_seconds,
            freshness_target_seconds=settings.freshness_target_seconds,
        )

    return create_app(
        ApiDependencies(
            session_factory_provider=session_factory_provider,
            taxonomy_provider=taxonomy_provider,
            operational_policy_provider=operational_policy_provider,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Career Radar MVP")
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    if not 1024 <= arguments.port <= 65_535:
        parser.error("--port must be between 1024 and 65535")
    uvicorn.run(create_radar_app(), host="127.0.0.1", port=arguments.port)


if __name__ == "__main__":
    main()
