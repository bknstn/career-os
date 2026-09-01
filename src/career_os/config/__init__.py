"""Validated file-backed configuration models."""

from career_os.config.skills import (
    AmbiguousAliasRule,
    GoldenExtractionCase,
    GoldenExtractionCorpus,
    SkillTaxonomy,
    SkillTaxonomySkill,
    load_golden_extraction_corpus,
    load_skill_taxonomy,
)
from career_os.config.sources import SourcesConfig, load_sources_config
from career_os.config.target_universes import TargetUniverse, load_target_universe

__all__ = [
    "AmbiguousAliasRule",
    "GoldenExtractionCase",
    "GoldenExtractionCorpus",
    "SkillTaxonomy",
    "SkillTaxonomySkill",
    "SourcesConfig",
    "TargetUniverse",
    "load_golden_extraction_corpus",
    "load_skill_taxonomy",
    "load_sources_config",
    "load_target_universe",
]
