"""Engine-neutral subprocess protocol used by the QE0 isolation PoC.

The package is deliberately not registered with the API or deployment paths in
QE0.  It only exposes the protocol and the local subprocess runner to tests and
PoC tooling.
"""

from .protocol import (
    PROTOCOL_NAME,
    SCHEMA_VERSION,
    EngineIdentity,
    ProtocolError,
    build_request,
    canonical_json,
    strict_json_loads,
    validate_request,
    validate_response,
)
from .runner import RunResult, WorkerConfig, WorkerExecutionError, WorkerRunner

__all__ = [
    "PROTOCOL_NAME",
    "SCHEMA_VERSION",
    "EngineIdentity",
    "ProtocolError",
    "RunResult",
    "WorkerConfig",
    "WorkerExecutionError",
    "WorkerRunner",
    "build_request",
    "canonical_json",
    "strict_json_loads",
    "validate_request",
    "validate_response",
]
