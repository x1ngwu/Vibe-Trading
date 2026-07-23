from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

COMMON_DIR = Path(__file__).resolve().parents[1] / "engine_workers" / "common"
if str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

from artifact_manifest import (  # noqa: E402
    MANIFEST_SCHEMA,
    ArtifactManifestError,
    build_manifest,
    render_hashed_requirements,
    verify_manifest,
)


def _write_wheel(wheelhouse: Path, name: str, version: str, *, payload: bytes = b"ok") -> Path:
    distribution = name.replace("-", "_")
    path = wheelhouse / f"{distribution}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{distribution}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(f"{distribution}/__init__.py", payload)
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text(
        "# exact freeze\nalpha-lib==1.2.3\n"
        f"direct-lib @ git+https://example.invalid/direct.git@{'d' * 40}\n",
        encoding="utf-8",
    )
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _write_wheel(wheelhouse, "alpha-lib", "1.2.3")
    _write_wheel(wheelhouse, "direct-lib", "4.5.6")
    manifest_path = tmp_path / "artifact-manifest.json"
    return lock, wheelhouse, manifest_path


def test_manifest_binds_lock_environment_metadata_and_artifact_hashes(tmp_path: Path) -> None:
    lock, wheelhouse, manifest_path = _fixture(tmp_path)
    manifest = build_manifest(lock, wheelhouse)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert manifest["schema"] == MANIFEST_SCHEMA
    assert set(manifest["environment"]) == {"implementation", "python", "abi", "platform"}
    assert [artifact["name"] for artifact in manifest["artifacts"]] == ["alpha-lib", "direct-lib"]
    assert all(len(artifact["sha256"]) == 64 for artifact in manifest["artifacts"])
    assert verify_manifest(manifest_path, lock, wheelhouse) == manifest

    hashed = render_hashed_requirements(manifest)
    assert "alpha-lib==1.2.3 --hash=sha256:" in hashed
    assert "direct-lib==4.5.6 --hash=sha256:" in hashed
    assert "git+" not in hashed


def test_manifest_rejects_tampered_artifact(tmp_path: Path) -> None:
    lock, wheelhouse, manifest_path = _fixture(tmp_path)
    manifest_path.write_text(json.dumps(build_manifest(lock, wheelhouse)), encoding="utf-8")
    wheel = next(wheelhouse.glob("alpha_lib-*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"tampered")

    with pytest.raises(ArtifactManifestError, match="does not match"):
        verify_manifest(manifest_path, lock, wheelhouse)


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_manifest_rejects_missing_or_unlocked_artifacts(tmp_path: Path, change: str) -> None:
    lock, wheelhouse, _manifest_path = _fixture(tmp_path)
    if change == "missing":
        next(wheelhouse.glob("alpha_lib-*.whl")).unlink()
    else:
        _write_wheel(wheelhouse, "extra-lib", "9.9.9")

    with pytest.raises(ArtifactManifestError, match="missing locked|unlocked artifacts"):
        build_manifest(lock, wheelhouse)


def test_manifest_rejects_unpinned_lock_lines(tmp_path: Path) -> None:
    lock, wheelhouse, _manifest_path = _fixture(tmp_path)
    lock.write_text("alpha-lib>=1.0\n", encoding="utf-8")

    with pytest.raises(ArtifactManifestError, match="exact version"):
        build_manifest(lock, wheelhouse)


def test_manifest_rejects_moving_direct_reference(tmp_path: Path) -> None:
    lock, wheelhouse, _manifest_path = _fixture(tmp_path)
    lock.write_text(
        "alpha-lib==1.2.3\n"
        "direct-lib @ git+https://example.invalid/direct.git@main\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactManifestError, match="40-character git commit"):
        build_manifest(lock, wheelhouse)


def test_manifest_rejects_non_wheel_entries(tmp_path: Path) -> None:
    lock, wheelhouse, _manifest_path = _fixture(tmp_path)
    (wheelhouse / "unlocked.tar.gz").write_bytes(b"not allowed")

    with pytest.raises(ArtifactManifestError, match="non-wheel entry"):
        build_manifest(lock, wheelhouse)


def test_manifest_rejects_symlinked_wheel(tmp_path: Path) -> None:
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text("alpha-lib==1.2.3\n", encoding="utf-8")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    target = _write_wheel(tmp_path, "alpha-lib", "1.2.3")
    (wheelhouse / target.name).symlink_to(target)

    with pytest.raises(ArtifactManifestError, match="regular non-symlink"):
        build_manifest(lock, wheelhouse)


def test_manifest_rejects_recorded_environment_drift(tmp_path: Path) -> None:
    lock, wheelhouse, manifest_path = _fixture(tmp_path)
    manifest = build_manifest(lock, wheelhouse)
    manifest["environment"]["abi"] = "different-abi"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactManifestError, match="does not match"):
        verify_manifest(manifest_path, lock, wheelhouse)


def test_manifest_allows_vendored_dist_info_but_uses_top_level_metadata(tmp_path: Path) -> None:
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text("alpha-lib==1.2.3\n", encoding="utf-8")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = _write_wheel(wheelhouse, "alpha-lib", "1.2.3")
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(
            "alpha_lib/_vendor/helper-9.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: helper\nVersion: 9.0\n",
        )

    manifest = build_manifest(lock, wheelhouse)

    assert manifest["artifacts"][0]["name"] == "alpha-lib"
    assert manifest["artifacts"][0]["version"] == "1.2.3"
