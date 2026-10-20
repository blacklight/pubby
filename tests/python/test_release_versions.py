"""Regression coverage for the configured release version bump."""

from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
from tomlkit import parse

ROOT = Path(__file__).resolve().parents[2]


def read_toml(path):
    return parse(path.read_text())


def runtime_version(root):
    source = (root / "src/python/pubby/__init__.py").read_text()
    return re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE).group(1)


def test_release_versions_match():
    project = read_toml(ROOT / "pyproject.toml")
    version = project["project"]["version"]
    assert project["tool"]["bumpversion"]["current_version"] == version
    assert runtime_version(ROOT) == version
    assert read_toml(ROOT / "src/rust/Cargo.toml")["package"]["version"] == version
    lock = read_toml(ROOT / "Cargo.lock")
    assert (
        next(p for p in lock["package"] if p["name"] == "pubby-sdk")["version"]
        == version
    )


@pytest.mark.parametrize("part", ["patch", "minor", "major"])
def test_bump_keeps_release_versions_synchronized(tmp_path, part):
    for filename in (
        "pyproject.toml",
        "src/python/pubby/__init__.py",
        "src/rust/Cargo.toml",
        "Cargo.lock",
        "Cargo.toml",
        "tests/rust/Cargo.toml",
    ):
        target = tmp_path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / filename, target)

    project_path = tmp_path / "pyproject.toml"
    current = read_toml(project_path)["tool"]["bumpversion"]["current_version"]
    # A release consumes Unreleased in the real changelog, so use a local fixture.
    changelog = f"# Changelog\n\n## Unreleased\n\n## {current}\n"
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    major, minor, patch = map(int, current.split("."))
    expected = {
        "patch": f"{major}.{minor}.{patch + 1}",
        "minor": f"{major}.{minor + 1}.0",
        "major": f"{major + 1}.0.0",
    }[part]

    # Deliberately collide with the release version, independent of real dependencies.
    with (tmp_path / "Cargo.lock").open("a") as stream:
        stream.write(
            f'\n[[package]]\nname = "release-version-sentinel"\nversion = "{current}"\n'
        )
    with (tmp_path / "src/rust/Cargo.toml").open("a") as stream:
        stream.write(
            f'\n[dev-dependencies.release-version-sentinel]\nversion = "{current}"\n'
        )
    with project_path.open("a") as stream:
        stream.write(f'\n[tool.release-test]\nversion = "{current}"\n')

    project = read_toml(project_path)
    manifest_path = tmp_path / "src/rust/Cargo.toml"
    manifest = read_toml(manifest_path)
    lock_path = tmp_path / "Cargo.lock"
    lock = read_toml(lock_path)
    untouched = {
        filename: (tmp_path / filename).read_bytes()
        for filename in ("Cargo.toml", "tests/rust/Cargo.toml")
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bumpversion",
            "bump",
            "--no-commit",
            "--no-tag",
            part,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    project["project"]["version"] = expected
    project["tool"]["bumpversion"]["current_version"] = expected
    assert read_toml(project_path) == project
    assert project["tool"]["bumpversion"]["commit"] is True
    assert project["tool"]["bumpversion"]["tag"] is True
    assert runtime_version(tmp_path) == expected
    manifest["package"]["version"] = expected
    assert read_toml(manifest_path) == manifest
    next(p for p in lock["package"] if p["name"] == "pubby-sdk")["version"] = expected
    assert read_toml(lock_path) == lock
    for filename, contents in untouched.items():
        assert (tmp_path / filename).read_bytes() == contents
    assert (tmp_path / "CHANGELOG.md").read_text() == changelog.replace(
        "Unreleased", expected
    )
    assert not (tmp_path / ".git").exists()
