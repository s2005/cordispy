"""Verify the exact boundary of built cordispy distributions."""

from __future__ import annotations

import argparse
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


def _archive_paths(names: list[str], *, label: str) -> set[str]:
    paths: set[str] = set()
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{label} contains an unsafe path: {name}")
        paths.add(name)
    return paths


def _project_version(repository: Path) -> str:
    with (repository / "pyproject.toml").open("rb") as stream:
        document = tomllib.load(stream)
    version = document["project"]["version"]
    if not isinstance(version, str):
        raise ValueError("project.version must be a string")
    return version


def _source_package_files(repository: Path) -> set[str]:
    package = repository / "src" / "cordispy"
    return {
        path.relative_to(package.parent).as_posix()
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def _verify_wheel(wheel: Path, *, repository: Path, version: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = _archive_paths(archive.namelist(), label=wheel.name)

    dist_info = f"cordispy-{version}.dist-info"
    required_metadata = {
        f"{dist_info}/METADATA",
        f"{dist_info}/RECORD",
        f"{dist_info}/WHEEL",
        f"{dist_info}/licenses/LICENSE",
    }
    expected = _source_package_files(repository) | required_metadata
    if names != expected:
        missing = sorted(expected - names)
        unexpected = sorted(names - expected)
        raise ValueError(f"{wheel.name} content mismatch; missing={missing!r}, unexpected={unexpected!r}")


def _verify_sdist(sdist: Path, *, version: str) -> None:
    root = f"cordispy-{version}"
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = archive.getmembers()
        names = _archive_paths([member.name for member in members], label=sdist.name)
        links = [member.name for member in members if member.issym() or member.islnk()]
    if links:
        raise ValueError(f"{sdist.name} contains links: {links!r}")
    if any(PurePosixPath(name).parts[0] != root for name in names):
        raise ValueError(f"{sdist.name} contains a path outside {root}/")
    required = {
        f"{root}/LICENSE",
        f"{root}/PKG-INFO",
        f"{root}/README.md",
        f"{root}/pyproject.toml",
    }
    missing = sorted(required - names)
    if missing:
        raise ValueError(f"{sdist.name} is missing required files: {missing!r}")


def verify_artifacts(*, dist_dir: Path, repository: Path) -> None:
    """Verify one wheel and one source distribution in ``dist_dir``."""
    version = _project_version(repository)
    expected_wheel = dist_dir / f"cordispy-{version}-py3-none-any.whl"
    expected_sdist = dist_dir / f"cordispy-{version}.tar.gz"
    files = {path for path in dist_dir.iterdir() if path.is_file()}
    expected = {expected_wheel, expected_sdist}
    allowed_control_files = {dist_dir / ".gitignore"}
    if files - allowed_control_files != expected:
        missing = sorted(path.name for path in expected - files)
        unexpected = sorted(path.name for path in files - expected - allowed_control_files)
        raise ValueError(f"distribution set mismatch; missing={missing!r}, unexpected={unexpected!r}")
    _verify_wheel(expected_wheel, repository=repository, version=version)
    _verify_sdist(expected_sdist, version=version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist-dir",
        required=True,
        type=Path,
        help="directory containing exactly one cordispy wheel and source distribution",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    dist_dir = args.dist_dir.resolve(strict=True)
    repository = Path(__file__).resolve().parents[1]
    verify_artifacts(dist_dir=dist_dir, repository=repository)
    print(f"Verified cordispy artifacts in {dist_dir}")


if __name__ == "__main__":
    main()
