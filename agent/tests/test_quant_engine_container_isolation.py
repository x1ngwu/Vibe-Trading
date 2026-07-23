from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from src.quant_engine import EngineIdentity, build_request, canonical_json, compute_snapshot_sha256


CONTAINER_GATE_ENABLED = os.getenv("VIBE_QE0_CONTAINER_GATE") == "1"
CONTAINER_IMAGE = os.getenv("VIBE_QE0_CONTAINER_IMAGE", "python:3.11-slim")
FAKE_ENGINE = EngineIdentity(name="fake", commit="a" * 40)

pytestmark = pytest.mark.skipif(
    not CONTAINER_GATE_ENABLED,
    reason="set VIBE_QE0_CONTAINER_GATE=1 to run Docker kernel-isolation evidence",
)


@pytest.fixture(scope="module")
def immutable_image() -> str:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when VIBE_QE0_CONTAINER_GATE=1")
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", CONTAINER_IMAGE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stderr
    image_id = inspected.stdout.strip()
    assert image_id.startswith("sha256:") and len(image_id) == 71
    return image_id


@pytest.fixture()
def snapshot_root(tmp_path: Path) -> Path:
    root = tmp_path / "snapshots"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    snapshot = root / "snapshot.json"
    snapshot.write_text('{"bars":[]}\n', encoding="utf-8")
    snapshot.chmod(0o666)
    return root


def _docker_base(image: str, snapshot_root: Path) -> list[str]:
    repository = Path(__file__).resolve().parents[2]
    return [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--user",
        "65534:65534",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "--mount",
        f"type=bind,src={repository},dst=/workspace,readonly",
        "--mount",
        f"type=bind,src={snapshot_root},dst=/snapshots,readonly",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONPATH=/workspace/agent/engine_workers/common",
        "--env",
        "VIBE_QUANT_NETWORK_DISABLED=1",
        "--env",
        "VIBE_QUANT_SNAPSHOT_ROOT=/snapshots",
        "--env",
        "TZ=Asia/Shanghai",
        "--env",
        "LC_ALL=C.UTF-8",
        "--env",
        "PYTHONHASHSEED=0",
        "--env",
        "OMP_NUM_THREADS=1",
        "--env",
        "OPENBLAS_NUM_THREADS=1",
        "--env",
        "MKL_NUM_THREADS=1",
        "--env",
        "NUMEXPR_NUM_THREADS=1",
        image,
    ]


def test_container_kernel_network_and_readonly_snapshot(
    immutable_image: str,
    snapshot_root: Path,
) -> None:
    probe = """
import errno
import json
from pathlib import Path
import socket

result = {}
try:
    Path('/tmp/write-probe').write_text('ok')
    result['tmp_write'] = True
except OSError as exc:
    result['tmp_write'] = False
    result['tmp_errno'] = exc.errno
try:
    Path('/snapshots/snapshot.json').write_text('tampered')
    result['snapshot_write'] = True
except OSError as exc:
    result['snapshot_write'] = False
    result['snapshot_errno'] = exc.errno
try:
    socket.create_connection(('1.1.1.1', 53), timeout=1).close()
    result['external_connect'] = True
except OSError as exc:
    result['external_connect'] = False
    result['network_errno'] = exc.errno
for line in Path('/proc/self/status').read_text().splitlines():
    if line.startswith(('CapEff:', 'NoNewPrivs:', 'Seccomp:')):
        key, value = line.split(':', 1)
        result[key] = value.strip()
print(json.dumps(result, sort_keys=True))
"""
    completed = subprocess.run(
        [*_docker_base(immutable_image, snapshot_root), "python", "-c", probe],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["tmp_write"] is True
    assert result["snapshot_write"] is False
    assert result["snapshot_errno"] == errno.EROFS
    assert result["external_connect"] is False
    assert isinstance(result["network_errno"], int)
    assert result["CapEff"] == "0000000000000000"
    assert result["NoNewPrivs"] == "1"
    assert result["Seccomp"] == "2"


def test_worker_reads_content_bound_snapshot_in_hardened_container(
    immutable_image: str,
    snapshot_root: Path,
) -> None:
    snapshot = snapshot_root / "snapshot.json"
    digest = compute_snapshot_sha256(snapshot)
    request = build_request(
        request_id="qe0.container-isolation",
        engine=FAKE_ENGINE,
        operation="echo",
        payload={"kernel_gate": True},
        snapshot_path="/snapshots/snapshot.json",
        snapshot_sha256=digest,
    )
    completed = subprocess.run(
        [
            *_docker_base(immutable_image, snapshot_root),
            "python",
            "/workspace/agent/tests/fixtures/fake_quant_worker.py",
        ],
        input=canonical_json(request) + "\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert response["status"] == "ok", response
    assert response["result"] == {
        "payload": {"kernel_gate": True},
        "snapshot": {"path": "/snapshots/snapshot.json", "sha256": digest},
    }
