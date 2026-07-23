"""Build and verify immutable wheelhouse evidence for isolated engine workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import sys
import sysconfig
import tempfile
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any, Sequence


MANIFEST_SCHEMA = "vibe.worker-artifacts.v1"
_DIRECT_REQUIREMENT_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s+@\s+(.+)")
_PINNED_REQUIREMENT_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")
_IMMUTABLE_GIT_URL_RE = re.compile(r"git\+https://[^\s]+@[0-9a-fA-F]{40}")


class ArtifactManifestError(ValueError):
    """Raised when a lock, wheelhouse, or manifest is not fail-closed."""


@dataclass(frozen=True)
class LockedRequirement:
    name: str
    normalized_name: str
    version: str | None
    source: str


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_regular_file(path: Path) -> tuple[int, str]:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ArtifactManifestError(f"artifact cannot be inspected: {path.name}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ArtifactManifestError(f"artifact must be a regular non-symlink file: {path.name}")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise ArtifactManifestError(f"artifact cannot be read: {path.name}") from exc
    return size, digest.hexdigest()


def _read_lock(lock_path: Path) -> list[LockedRequirement]:
    try:
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ArtifactManifestError("lock file cannot be read") from exc

    requirements: list[LockedRequirement] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pinned = _PINNED_REQUIREMENT_RE.fullmatch(line)
        direct = _DIRECT_REQUIREMENT_RE.fullmatch(line)
        if pinned:
            name, version = pinned.groups()
        elif direct:
            name, url = direct.groups()
            if not _IMMUTABLE_GIT_URL_RE.fullmatch(url):
                raise ArtifactManifestError(
                    f"lock line {line_number} direct reference must pin a 40-character git commit"
                )
            version = None
        else:
            raise ArtifactManifestError(
                f"lock line {line_number} must be an exact version or immutable direct reference"
            )
        normalized_name = _normalize_name(name)
        if normalized_name in seen:
            raise ArtifactManifestError(f"duplicate locked requirement: {name}")
        seen.add(normalized_name)
        requirements.append(
            LockedRequirement(
                name=name,
                normalized_name=normalized_name,
                version=version,
                source=line,
            )
        )
    if not requirements:
        raise ArtifactManifestError("lock file contains no requirements")
    return requirements


def _read_wheel_metadata(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_entries = [
                info
                for info in archive.infolist()
                if info.filename.count("/") == 1 and info.filename.endswith(".dist-info/METADATA")
            ]
            if len(metadata_entries) != 1:
                raise ArtifactManifestError(f"wheel must contain exactly one METADATA file: {path.name}")
            for info in archive.infolist():
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ArtifactManifestError(f"wheel contains a special entry: {path.name}")
                if info.flag_bits & 0x1:
                    raise ArtifactManifestError(f"encrypted wheel entries are not allowed: {path.name}")
            metadata_info = metadata_entries[0]
            if metadata_info.file_size > 1024 * 1024:
                raise ArtifactManifestError(f"wheel METADATA is too large: {path.name}")
            metadata_text = archive.read(metadata_info).decode("utf-8")
    except ArtifactManifestError:
        raise
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise ArtifactManifestError(f"wheel cannot be inspected: {path.name}") from exc

    message = Parser().parsestr(metadata_text)
    name = message.get("Name", "").strip()
    version = message.get("Version", "").strip()
    if not name or not version or "\n" in name or "\n" in version:
        raise ArtifactManifestError(f"wheel metadata lacks a valid Name/Version: {path.name}")
    return name, version


def _environment_identity() -> dict[str, str]:
    return {
        "implementation": sys.implementation.name,
        "python": platform.python_version(),
        "abi": sysconfig.get_config_var("SOABI") or "unknown",
        "platform": sysconfig.get_platform(),
    }


def build_manifest(lock_path: Path, wheelhouse: Path) -> dict[str, Any]:
    """Return deterministic manifest data for one exact lock and wheelhouse."""

    requirements = _read_lock(lock_path)
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise ArtifactManifestError("wheelhouse must be a non-symlink directory")

    wheels: dict[str, dict[str, Any]] = {}
    for path in sorted(wheelhouse.iterdir(), key=lambda item: item.name):
        if path.suffix != ".whl":
            raise ArtifactManifestError(f"wheelhouse contains a non-wheel entry: {path.name}")
        size, digest = _hash_regular_file(path)
        name, version = _read_wheel_metadata(path)
        normalized_name = _normalize_name(name)
        if normalized_name in wheels:
            raise ArtifactManifestError(f"multiple wheels found for requirement: {name}")
        wheels[normalized_name] = {
            "name": name,
            "version": version,
            "filename": path.name,
            "size": size,
            "sha256": digest,
        }

    expected_names = {requirement.normalized_name for requirement in requirements}
    missing = sorted(expected_names - wheels.keys())
    extra = sorted(wheels.keys() - expected_names)
    if missing:
        raise ArtifactManifestError(f"wheelhouse is missing locked artifacts: {', '.join(missing)}")
    if extra:
        raise ArtifactManifestError(f"wheelhouse contains unlocked artifacts: {', '.join(extra)}")

    artifacts: list[dict[str, Any]] = []
    for requirement in requirements:
        artifact = wheels[requirement.normalized_name]
        if requirement.version is not None and artifact["version"] != requirement.version:
            raise ArtifactManifestError(
                f"wheel version mismatch for {requirement.name}: "
                f"expected {requirement.version}, got {artifact['version']}"
            )
        artifacts.append({**artifact, "source_requirement": requirement.source})

    lock_size, lock_digest = _hash_regular_file(lock_path)
    return {
        "schema": MANIFEST_SCHEMA,
        "lock": {
            "filename": lock_path.name,
            "size": lock_size,
            "sha256": lock_digest,
        },
        "environment": _environment_identity(),
        "artifacts": sorted(artifacts, key=lambda artifact: _normalize_name(artifact["name"])),
    }


def render_hashed_requirements(manifest: dict[str, Any]) -> str:
    """Render a no-VCS requirements file accepted by pip --require-hashes."""

    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ArtifactManifestError("unsupported artifact manifest schema")
    lines = [
        "# Generated from an isolated worker artifact manifest.",
        "# Install only with --no-index --find-links <wheelhouse> --require-hashes.",
    ]
    for artifact in manifest.get("artifacts", []):
        name = artifact.get("name")
        version = artifact.get("version")
        digest = artifact.get("sha256")
        if not all(isinstance(value, str) and value for value in (name, version, digest)):
            raise ArtifactManifestError("manifest artifact is incomplete")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ArtifactManifestError("manifest artifact has an invalid SHA-256")
        lines.append(f"{name}=={version} --hash=sha256:{digest}")
    return "\n".join(lines) + "\n"


def verify_manifest(manifest_path: Path, lock_path: Path, wheelhouse: Path) -> dict[str, Any]:
    """Rebuild and compare all manifest evidence using the current interpreter."""

    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactManifestError("artifact manifest cannot be read") from exc
    if not isinstance(stored, dict) or stored.get("schema") != MANIFEST_SCHEMA:
        raise ArtifactManifestError("unsupported artifact manifest schema")
    actual = build_manifest(lock_path, wheelhouse)
    if stored != actual:
        raise ArtifactManifestError("artifact manifest does not match lock, environment, or wheelhouse")
    return actual


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _build_command(args: argparse.Namespace) -> None:
    manifest = build_manifest(args.lock, args.wheelhouse)
    _atomic_write(args.manifest, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _atomic_write(args.requirements, render_hashed_requirements(manifest))


def _verify_command(args: argparse.Namespace) -> None:
    verify_manifest(args.manifest, args.lock, args.wheelhouse)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build manifest and hashed requirements")
    verify = subparsers.add_parser("verify", help="verify existing manifest")
    for command in (build, verify):
        command.add_argument("--lock", type=Path, required=True)
        command.add_argument("--wheelhouse", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--requirements", type=Path, required=True)
    build.set_defaults(handler=_build_command)
    verify.set_defaults(handler=_verify_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except ArtifactManifestError as exc:
        print(f"artifact manifest error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
