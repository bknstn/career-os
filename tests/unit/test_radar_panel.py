"""Validation and content digest tests for the P3 panel manifest boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from career_os.config.radar_panel import RadarPanelManifest
from career_os.config.sources import (
    ConfigurationError,
    GreenhouseSourceConfig,
    LeverSourceConfig,
    SourcesConfig,
)


def test_manifest_digest_ignores_yaml_formatting_and_source_order(tmp_path: Path) -> None:
    sources = SourcesConfig(
        sources=[
            LeverSourceConfig(key="lever", kind="lever", company_name="B", site="b"),
            GreenhouseSourceConfig(
                key="greenhouse", kind="greenhouse", company_name="A", board_token="a"
            ),
        ]
    )
    first = tmp_path / "panel-a.yaml"
    first.write_text(
        "panel_version: radar-v1\nsources:\n  - source_key: greenhouse\n"
        "    company_key: a\n    source_kind: greenhouse\n"
        "  - source_key: lever\n    company_key: b\n    source_kind: lever\n",
        encoding="utf-8",
    )
    second = tmp_path / "panel-b.yaml"
    second.write_text(
        "sources: [ { source_kind: lever, company_key: b, source_key: lever },\n"
        "           { source_kind: greenhouse, company_key: a, source_key: greenhouse } ]\n"
        "panel_version: radar-v1\n",
        encoding="utf-8",
    )

    left = RadarPanelManifest.load(first, sources)
    right = RadarPanelManifest.load(second, sources)

    assert left.canonical_manifest == right.canonical_manifest
    assert left.manifest_digest == right.manifest_digest
    assert left.source_config_digests == right.source_config_digests
    assert left.panel_digest == right.panel_digest


def test_panel_digest_covers_typed_config_for_every_declared_source(tmp_path: Path) -> None:
    manifest_path = tmp_path / "panel.yaml"
    manifest_path.write_text(
        "panel_version: radar-v1\nsources:\n  - source_key: greenhouse\n"
        "    company_key: a\n    source_kind: greenhouse\n"
        "  - source_key: lever\n    company_key: b\n    source_kind: lever\n",
        encoding="utf-8",
    )
    original = SourcesConfig(
        sources=[
            GreenhouseSourceConfig(
                key="greenhouse", kind="greenhouse", company_name="A", board_token="a"
            ),
            LeverSourceConfig(key="lever", kind="lever", company_name="B", site="b"),
        ]
    )
    changed = SourcesConfig(
        sources=[
            original.sources[0],
            LeverSourceConfig(key="lever", kind="lever", company_name="B", site="b-new"),
        ]
    )

    assert RadarPanelManifest.load(manifest_path, original).panel_digest != (
        RadarPanelManifest.load(manifest_path, changed).panel_digest
    )


def test_semantic_manifest_identity_load_does_not_require_current_source_config(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "panel.yaml"
    manifest_path.write_text(
        "panel_version: frozen-v1\nsources:\n  - source_key: retired-board\n"
        "    company_key: company\n    source_kind: greenhouse\n",
        encoding="utf-8",
    )

    identity = RadarPanelManifest.load_identity(manifest_path)

    assert identity.panel_version == "frozen-v1"
    assert identity.sources[0].source_key == "retired-board"
    assert identity.canonical_manifest["sources"] == [
        {
            "company_key": "company",
            "source_key": "retired-board",
            "source_kind": "greenhouse",
        }
    ]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            "panel_version: v1\nsources:\n  - source_key: missing\n"
            "    company_key: a\n    source_kind: greenhouse\n",
            "not configured",
        ),
        (
            "panel_version: v1\nsources:\n  - source_key: example\n"
            "    company_key: a\n    source_kind: lever\n",
            "kind does not match",
        ),
        (
            "panel_version: v1\nsources:\n  - source_key: example\n"
            "    company_key: a\n    source_kind: greenhouse\n"
            "  - source_key: example\n    company_key: b\n    source_kind: greenhouse\n",
            "duplicate source_key",
        ),
        (
            "panel_version: v1\nsources:\n  - source_key: example\n"
            "    company_key: a\n    source_kind: greenhouse\n"
            "    unexpected: value\n",
            "Extra inputs are not permitted",
        ),
        ("panel_version: v1\npanel_version: v2\nsources: []\n", "Malformed YAML"),
    ],
)
def test_manifest_rejects_unknown_mismatched_duplicate_and_extra_fields(
    tmp_path: Path, document: str, message: str
) -> None:
    manifest_path = tmp_path / "panel.yaml"
    manifest_path.write_text(document, encoding="utf-8")
    sources = SourcesConfig(
        sources=[
            GreenhouseSourceConfig(
                key="example", kind="greenhouse", company_name="Example", board_token="board"
            )
        ]
    )

    with pytest.raises(ConfigurationError, match=message):
        RadarPanelManifest.load(manifest_path, sources)


def test_manifest_rejects_non_utf8_input(tmp_path: Path) -> None:
    manifest_path = tmp_path / "panel.yaml"
    manifest_path.write_bytes(b"\xff")
    sources = SourcesConfig(
        sources=[
            GreenhouseSourceConfig(
                key="example", kind="greenhouse", company_name="Example", board_token="board"
            )
        ]
    )

    with pytest.raises(ConfigurationError, match="not valid UTF-8"):
        RadarPanelManifest.load(manifest_path, sources)


def test_explicit_source_selection_must_stay_inside_declared_panel(tmp_path: Path) -> None:
    manifest_path = tmp_path / "panel.yaml"
    manifest_path.write_text(
        "panel_version: v1\nsources:\n  - source_key: example\n"
        "    company_key: a\n    source_kind: greenhouse\n",
        encoding="utf-8",
    )
    sources = SourcesConfig(
        sources=[
            GreenhouseSourceConfig(
                key="example", kind="greenhouse", company_name="Example", board_token="board"
            ),
            LeverSourceConfig(key="outside", kind="lever", company_name="Other", site="other"),
        ]
    )
    panel = RadarPanelManifest.load(manifest_path, sources)

    with pytest.raises(ConfigurationError, match="outside the Radar panel"):
        panel.require_sources((sources.sources[1],))
