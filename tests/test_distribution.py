"""Release workflow and artifact-boundary policy tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPOSITORY = Path(__file__).resolve().parents[1]
WORKFLOWS = REPOSITORY / ".github" / "workflows"
IMMUTABLE_ACTION = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _workflow(name: str) -> dict[str, Any]:
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _steps(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in document["jobs"].values() for step in job["steps"]]


def test_every_action_is_immutable_and_checkout_drops_credentials() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        document = _workflow(path.name)
        for step in _steps(document):
            action = step.get("uses")
            if action is None:
                continue
            assert IMMUTABLE_ACTION.fullmatch(action), f"mutable action in {path.name}: {action}"
            if action.startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False


def test_setup_uv_selects_the_required_executable_version() -> None:
    pyproject = (REPOSITORY / "pyproject.toml").read_text(encoding="utf-8")
    assert 'required-version = "==0.11.14"' in pyproject
    for path in WORKFLOWS.glob("*.yml"):
        document = _workflow(path.name)
        for step in _steps(document):
            action = step.get("uses", "")
            if action.startswith("astral-sh/setup-uv@"):
                assert step.get("with", {}).get("version") == "0.11.14"


def test_publish_credentials_are_isolated_from_the_build() -> None:
    document = _workflow("publish.yml")
    assert document["concurrency"]["cancel-in-progress"] is False
    assert set(document["jobs"]) == {"build", "publish"}

    build = document["jobs"]["build"]
    assert build["permissions"] == {"contents": "read"}
    assert all(step.get("with", {}).get("enable-cache") is not True for step in build["steps"])
    build_text = repr(build)
    assert "uvx" not in build_text
    assert "--build-constraint build-constraints.txt --require-hashes" in build_text
    assert 'merge-base --is-ancestor "$GITHUB_SHA" "origin/main"' in build_text

    publish = document["jobs"]["publish"]
    assert publish["permissions"] == {"actions": "read", "id-token": "write"}
    assert publish["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert publish["needs"] == "build"
    assert len(publish["steps"]) == 2
    assert publish["steps"][0]["uses"].startswith("actions/download-artifact@")
    assert publish["steps"][1]["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert publish["steps"][1]["with"]["attestations"] is True
    assert "checkout" not in repr(publish)
    assert "cache" not in repr(publish)


def test_security_policy_is_owned_and_packaged() -> None:
    security = (REPOSITORY / "SECURITY.md").read_text(encoding="utf-8")
    pyproject = (REPOSITORY / "pyproject.toml").read_text(encoding="utf-8")
    owners = (REPOSITORY / ".github" / "CODEOWNERS").read_text(encoding="utf-8")

    assert "GitHub private vulnerability reporting" in security
    assert "Loader.trusted" in security
    assert '"SECURITY.md"' in pyproject
    assert "/SECURITY.md @s2005" in owners


def test_security_maintenance_covers_actions_dependencies_source_and_secrets() -> None:
    security = _workflow("security.yml")
    scan_text = repr(security["jobs"]["scan"])
    assert "pip-audit" in scan_text
    assert "bandit" in scan_text
    assert "detect-secrets" in scan_text
    assert "verify_secret_report.py" in scan_text
    assert "zizmor" in scan_text
    assert "uvx" not in scan_text
