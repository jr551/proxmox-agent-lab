"""Bounded, portable recording and replay of this runner's file I/O.

This is deliberately an operation recorder, not a kernel or block-device
tracer.  Its JSON Lines trace contains no paths: only ordered reads, writes,
flushes, offsets, checksums and deterministic payload descriptions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import stat
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterable

from .errors import LabError


TRACE_FORMAT = "proxmox-agent-lab-io-trace"
TRACE_VERSION = 1
MAX_OPERATIONS = 100_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_OPERATION_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_TRACE_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
_CHUNK = 64 * 1024


class TraceError(LabError):
    """A trace or scratch file is unsafe, malformed, or irreproducible."""


@dataclass(frozen=True)
class Limits:
    max_operations: int = MAX_OPERATIONS
    max_file_bytes: int = MAX_FILE_BYTES
    max_operation_bytes: int = MAX_OPERATION_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_seconds: float = 3600.0


DEFAULT_LIMITS = Limits()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _payload(seed: int, size: int) -> bytes:
    if not isinstance(seed, int) or not 0 <= seed < 2 ** 63:
        raise TraceError("payload seed must be a non-negative 63-bit integer")
    if not isinstance(size, int) or not 0 <= size <= MAX_OPERATION_BYTES:
        raise TraceError("payload size is outside the allowed range")
    result = bytearray()
    counter = 0
    while len(result) < size:
        result.extend(hashlib.sha256(f"{seed}:{counter}".encode("ascii")).digest())
        counter += 1
    return bytes(result[:size])


def _zero_digest(size: int) -> str:
    digest = hashlib.sha256()
    zeros = b"\0" * min(_CHUNK, size)
    while size:
        length = min(size, len(zeros))
        digest.update(zeros[:length])
        size -= length
    return digest.hexdigest()


def _file_digest(handle: BinaryIO, size: int) -> str:
    handle.seek(0)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        data = handle.read(min(_CHUNK, remaining))
        if not data:
            raise TraceError("scratch file became shorter during validation")
        digest.update(data)
        remaining -= len(data)
    return digest.hexdigest()


def _absolute_path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise TraceError(f"{label} must be an absolute path without '..'")
    return path


def _new_file(path: Path, label: str, mode: str) -> BinaryIO:
    if path.exists() or path.is_symlink():
        raise TraceError(f"{label} already exists; refusing to overwrite it")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise TraceError(f"{label} parent must be an existing non-symlink directory")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except OSError as exc:
        raise TraceError(f"could not create {label}: {exc}") from exc
    return os.fdopen(fd, mode, encoding="utf-8", newline="\n") if "t" in mode else os.fdopen(fd, mode, buffering=0)


def _can_create(path: Path, label: str) -> None:
    """Check create-exclusive output before an operation can modify scratch data."""
    if path.exists() or path.is_symlink():
        raise TraceError(f"{label} already exists; refusing to overwrite it")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise TraceError(f"{label} parent must be an existing non-symlink directory")


def _scratch(path: Path, *, create: bool) -> BinaryIO:
    if create:
        return _new_file(path, "scratch file", "r+b")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise TraceError(f"could not inspect scratch file: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TraceError("scratch file must be a regular file, never a device or symlink")
    try:
        return path.open("r+b", buffering=0)
    except OSError as exc:
        raise TraceError(f"could not open scratch file: {exc}") from exc


class _Writer:
    """Create-exclusive JSONL writer that commits every record immediately."""

    def __init__(self, path: Path, label: str = "trace file") -> None:
        self.handle = _new_file(path, label, "wt")

    def write(self, record: dict[str, Any]) -> None:
        self.handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def close(self) -> None:
        self.handle.close()


def _parameters(seed: int, file_size: int, operations: int, block_size: int,
                limits: Limits) -> None:
    if not isinstance(seed, int) or not 0 <= seed < 2 ** 63:
        raise TraceError("seed must be a non-negative 63-bit integer")
    if not isinstance(file_size, int) or not 0 < file_size <= limits.max_file_bytes:
        raise TraceError("file size is outside the allowed range")
    if not isinstance(operations, int) or not 0 < operations <= limits.max_operations:
        raise TraceError("operation count is outside the allowed range")
    if not isinstance(block_size, int) or not 0 < block_size <= min(file_size, limits.max_operation_bytes):
        raise TraceError("block size is outside the allowed range")
    if operations * block_size > limits.max_total_bytes:
        raise TraceError("planned I/O exceeds the total-byte limit")


def _header(seed: int, file_size: int, operations: int, block_size: int,
            mode: str) -> dict[str, Any]:
    return {
        "record": "header", "format": TRACE_FORMAT, "version": TRACE_VERSION,
        "mode": mode,
        "initial_state": {"size": file_size, "sha256": _zero_digest(file_size)},
        "workload": {"seed": seed, "file_size": file_size, "operations": operations,
                     "block_size": block_size},
    }


def _plan(seed: int, file_size: int, operations: int, block_size: int) -> Iterable[dict[str, Any]]:
    source = random.Random(seed)
    for sequence in range(operations):
        if sequence and sequence % 11 == 0:
            yield {"record": "intent", "sequence": sequence, "operation": "flush"}
            continue
        size = block_size
        offset = source.randrange(file_size - size + 1)
        offset -= offset % min(4096, size)
        if sequence % 3 == 1:
            yield {"record": "intent", "sequence": sequence, "operation": "read",
                   "offset": offset, "size": size}
        else:
            payload_seed = source.randrange(2 ** 63)
            payload = _payload(payload_seed, size)
            yield {"record": "intent", "sequence": sequence, "operation": "write",
                   "offset": offset, "size": size,
                   "payload": {"kind": "seeded-sha256", "seed": payload_seed},
                   "expected_checksum": _digest(payload)}


def generate_trace(trace_path: str | Path, *, seed: int, file_size: int,
                   operations: int, block_size: int, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Create an unexecuted portable plan; existing files are never replaced."""
    _parameters(seed, file_size, operations, block_size, limits)
    trace = _absolute_path(trace_path, "trace file")
    writer = _Writer(trace)
    try:
        writer.write(_header(seed, file_size, operations, block_size, "plan"))
        for operation in _plan(seed, file_size, operations, block_size):
            writer.write(operation)
    finally:
        writer.close()
    return {"trace": str(trace), "operations": operations, "status": "generated"}


def _run_operation(handle: BinaryIO, operation: dict[str, Any]) -> str | None:
    if operation["operation"] == "flush":
        handle.flush()
        os.fsync(handle.fileno())
        return None
    handle.seek(operation["offset"])
    if operation["operation"] == "write":
        payload = _payload(operation["payload"]["seed"], operation["size"])
        written = handle.write(payload)
        if written != len(payload):
            raise OSError(f"short write ({written} of {len(payload)} bytes)")
        return _digest(payload)
    data = handle.read(operation["size"])
    if len(data) != operation["size"]:
        raise OSError(f"short read ({len(data)} of {operation['size']} bytes)")
    return _digest(data)


def record_workload(trace_path: str | Path, scratch_path: str | Path, *, seed: int,
                    file_size: int, operations: int, block_size: int,
                    limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Execute a fresh scratch-file workload, persisting intent before each I/O."""
    _parameters(seed, file_size, operations, block_size, limits)
    trace = _absolute_path(trace_path, "trace file")
    scratch_path_obj = _absolute_path(scratch_path, "scratch file")
    writer = _Writer(trace)
    handle: BinaryIO | None = None
    completed = 0
    failure: str | None = None
    started = time.monotonic()
    try:
        handle = _scratch(scratch_path_obj, create=True)
        handle.truncate(file_size)
        handle.flush()
        os.fsync(handle.fileno())
        writer.write(_header(seed, file_size, operations, block_size, "record"))
        for operation in _plan(seed, file_size, operations, block_size):
            writer.write(operation)  # A crash after this point leaves useful inflight evidence.
            try:
                if time.monotonic() - started > limits.max_seconds:
                    raise TimeoutError("workload timeout")
                checksum = _run_operation(handle, operation)
                outcome: dict[str, Any] = {"record": "outcome", "sequence": operation["sequence"], "status": "ok"}
                if checksum is not None:
                    outcome["checksum"] = checksum
                writer.write(outcome)
                completed += 1
            except (OSError, ValueError, TimeoutError) as exc:
                failure = f"{type(exc).__name__}: {exc}"
                writer.write({"record": "outcome", "sequence": operation["sequence"],
                              "status": "error", "error": failure})
                break
    finally:
        if handle is not None:
            handle.close()
        writer.close()
    return {"trace": str(trace), "scratch": str(scratch_path_obj),
            "operations_completed": completed, "status": "failed" if failure else "recorded",
            "error": failure}


def _sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _check_header(header: dict[str, Any], limits: Limits) -> None:
    if header.get("format") != TRACE_FORMAT or header.get("version") != TRACE_VERSION:
        raise TraceError("unsupported I/O trace format or version")
    if header.get("mode") not in ("plan", "record"):
        raise TraceError("trace header mode is invalid")
    initial, workload = header.get("initial_state"), header.get("workload")
    if not isinstance(initial, dict) or not isinstance(workload, dict) or not _sha(initial.get("sha256")):
        raise TraceError("trace header is missing initial state")
    _parameters(workload.get("seed"), workload.get("file_size"), workload.get("operations"),
                workload.get("block_size"), limits)
    if initial.get("size") != workload["file_size"]:
        raise TraceError("trace initial-state and workload sizes disagree")


def _check_intent(intent: dict[str, Any], sequence: int, header: dict[str, Any], limits: Limits) -> int:
    operation = intent.get("operation")
    if intent.get("record") != "intent" or intent.get("sequence") != sequence or operation not in {"read", "write", "flush"}:
        raise TraceError("trace operation type or sequence is invalid")
    if operation == "flush":
        if set(intent) != {"record", "sequence", "operation"}:
            raise TraceError("flush operation contains unexpected fields")
        return 0
    size, offset = intent.get("size"), intent.get("offset")
    file_size = header["initial_state"]["size"]
    if not isinstance(size, int) or not isinstance(offset, int) or not 0 < size <= limits.max_operation_bytes or not 0 <= offset or offset + size > file_size:
        raise TraceError("trace offset or size is invalid")
    if operation == "read":
        if set(intent) != {"record", "sequence", "operation", "offset", "size"}:
            raise TraceError("read operation contains unexpected fields")
    else:
        payload = intent.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"kind", "seed"} or payload.get("kind") != "seeded-sha256":
            raise TraceError("write payload is invalid")
        if not _sha(intent.get("expected_checksum")) or _digest(_payload(payload.get("seed"), size)) != intent["expected_checksum"]:
            raise TraceError("write checksum does not describe its payload")
        if set(intent) != {"record", "sequence", "operation", "offset", "size", "payload", "expected_checksum"}:
            raise TraceError("write operation contains unexpected fields")
    return size


def _check_outcome(outcome: dict[str, Any], sequence: int) -> None:
    if outcome.get("record") != "outcome" or outcome.get("sequence") != sequence or outcome.get("status") not in ("ok", "error"):
        raise TraceError("trace outcome is invalid")
    if set(outcome) - {"record", "sequence", "status", "checksum", "error"}:
        raise TraceError("trace outcome contains unexpected fields")
    if outcome["status"] == "error" and (not isinstance(outcome.get("error"), str) or len(outcome["error"]) > 4096):
        raise TraceError("failed trace outcome lacks a bounded error")
    if "checksum" in outcome and not _sha(outcome["checksum"]):
        raise TraceError("trace outcome checksum is invalid")


def _load_trace(trace_path: str | Path, limits: Limits = DEFAULT_LIMITS) -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, dict[str, Any]]]:
    trace = _absolute_path(trace_path, "trace file")
    try:
        info = os.lstat(trace)
    except OSError as exc:
        raise TraceError(f"could not inspect trace file: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TRACE_BYTES:
        raise TraceError("trace file must be a bounded regular non-symlink file")
    header: dict[str, Any] | None = None
    intents: list[dict[str, Any]] = []
    outcomes: dict[int, dict[str, Any]] = {}
    waiting: int | None = None
    failed = False
    total = 0
    try:
        with trace.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if len(line) > MAX_LINE_BYTES:
                    raise TraceError(f"trace line {line_number} is too long")
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TraceError(f"trace line {line_number} is not JSON") from exc
                if not isinstance(record, dict):
                    raise TraceError(f"trace line {line_number} is not an object")
                if header is None:
                    if line_number != 1 or record.get("record") != "header":
                        raise TraceError("first trace record must be a header")
                    _check_header(record, limits)
                    header = record
                    continue
                if record.get("record") == "intent":
                    if failed:
                        raise TraceError("recorded trace continues after a failed operation")
                    if waiting is not None and header["mode"] == "record":
                        raise TraceError("recorded trace has an intent without an outcome")
                    total += _check_intent(record, len(intents), header, limits)
                    if total > limits.max_total_bytes or len(intents) >= limits.max_operations:
                        raise TraceError("trace exceeds configured I/O limits")
                    intents.append(record)
                    waiting = record["sequence"]
                elif record.get("record") == "outcome":
                    if header["mode"] == "plan":
                        raise TraceError("planned trace cannot contain outcomes")
                    if waiting is None or record.get("sequence") != waiting:
                        raise TraceError("outcome does not match the preceding intent")
                    _check_outcome(record, waiting)
                    outcomes[waiting] = record
                    failed = record["status"] == "error"
                    waiting = None
                else:
                    raise TraceError("trace contains an unknown record type")
    except UnicodeDecodeError as exc:
        raise TraceError("trace file is not UTF-8") from exc
    if header is None:
        raise TraceError("trace file is empty")
    return header, intents, outcomes


def analyze_trace(trace_path: str | Path, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    header, intents, outcomes = _load_trace(trace_path, limits)
    return {"trace": str(_absolute_path(trace_path, "trace file")), "valid": True,
            "mode": header["mode"], "initial_state": header["initial_state"],
            "operations": {name: sum(item["operation"] == name for item in intents) for name in ("read", "write", "flush")},
            "intent_count": len(intents), "outcome_count": len(outcomes),
            "inflight": len(intents) > len(outcomes),
            "recorded_failures": sum(item["status"] == "error" for item in outcomes.values())}


def _write_result(path: str | Path, report: dict[str, Any]) -> None:
    writer = _Writer(_absolute_path(path, "result file"), "result file")
    try:
        writer.write(report)
    finally:
        writer.close()


def replay_trace(trace_path: str | Path, scratch_path: str | Path, result_path: str | Path, *,
                 create_scratch: bool, confirm_disposable: bool,
                 limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Replay against a confirmed regular scratch file and write a separate result."""
    if not confirm_disposable:
        raise TraceError("replay requires --confirm-disposable")
    header, intents, expected = _load_trace(trace_path, limits)
    scratch_path_obj = _absolute_path(scratch_path, "scratch file")
    trace_path_obj = _absolute_path(trace_path, "trace file")
    result_path_obj = _absolute_path(result_path, "result file")
    if len({trace_path_obj, scratch_path_obj, result_path_obj}) != 3:
        raise TraceError("trace, scratch, and result paths must be distinct")
    _can_create(result_path_obj, "result file")
    initial = header["initial_state"]
    handle = _scratch(scratch_path_obj, create=create_scratch)
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        if create_scratch:
            handle.truncate(initial["size"])
            handle.flush()
            os.fsync(handle.fileno())
        size = os.fstat(handle.fileno()).st_size
        if size != initial["size"] or _file_digest(handle, size) != initial["sha256"]:
            raise TraceError("scratch file does not match trace initial state")
        for operation in intents:
            try:
                if time.monotonic() - started > limits.max_seconds:
                    raise TimeoutError("replay timeout")
                checksum = _run_operation(handle, operation)
                outcome = expected.get(operation["sequence"])
                if outcome and outcome.get("status") == "ok" and "checksum" in outcome and checksum != outcome["checksum"]:
                    raise OSError("checksum differs from captured operation")
                result: dict[str, Any] = {"sequence": operation["sequence"], "status": "ok"}
                if checksum is not None:
                    result["checksum"] = checksum
                results.append(result)
            except (OSError, ValueError, TimeoutError) as exc:
                results.append({"sequence": operation["sequence"], "status": "error", "error": f"{type(exc).__name__}: {exc}"})
                break
    finally:
        handle.close()
    failed = next((item for item in results if item["status"] == "error"), None)
    report = {"record": "replay-result", "format": TRACE_FORMAT, "version": TRACE_VERSION,
              "status": "failed" if failed else "ok", "trace": str(_absolute_path(trace_path, "trace file")),
              "scratch": str(scratch_path_obj), "operations_completed": len(results) - bool(failed), "results": results}
    _write_result(result_path_obj, report)
    return {"result": str(result_path_obj), "status": report["status"],
            "operations_completed": report["operations_completed"], "error": failed.get("error") if failed else None}


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def cmd_generate(_lab: Any, args: Any) -> None:
    _emit(generate_trace(args.trace, seed=args.seed, file_size=args.file_size, operations=args.operations, block_size=args.block_size))


def cmd_record(_lab: Any, args: Any) -> None:
    _emit(record_workload(args.trace, args.scratch, seed=args.seed, file_size=args.file_size, operations=args.operations, block_size=args.block_size))


def cmd_replay(_lab: Any, args: Any) -> None:
    _emit(replay_trace(args.trace, args.scratch, args.result, create_scratch=args.create_scratch, confirm_disposable=args.confirm_disposable))


def cmd_analyze(_lab: Any, args: Any) -> None:
    _emit(analyze_trace(args.trace))


def _add_commands(commands: Any, bind: Any) -> None:
    """Install the four commands on either the package CLI or direct runner."""
    for name, callback in (("generate", cmd_generate), ("record", cmd_record)):
        command = commands.add_parser(name)
        command.add_argument("--trace", required=True)
        if name == "record":
            command.add_argument("--scratch", required=True)
        command.add_argument("--seed", required=True, type=int)
        command.add_argument("--file-size", required=True, type=int)
        command.add_argument("--operations", required=True, type=int)
        command.add_argument("--block-size", required=True, type=int)
        command.set_defaults(func=bind(callback))
    replay = commands.add_parser("replay")
    replay.add_argument("--trace", required=True)
    replay.add_argument("--scratch", required=True)
    replay.add_argument("--result", required=True)
    replay.add_argument("--create-scratch", action="store_true")
    replay.add_argument("--confirm-disposable", action="store_true")
    replay.set_defaults(func=bind(cmd_replay))
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--trace", required=True)
    analyze.set_defaults(func=bind(cmd_analyze))


def register(sub: Any, lab: Any) -> None:
    """Register ``io-workload``; commands remain local until an explicit guest runner is used."""
    from .cli import _bind
    parser = sub.add_parser("io-workload", help="record/replay bounded portable scratch-file I/O")
    _add_commands(parser.add_subparsers(dest="io_workload_command", required=True),
                  lambda callback: _bind(lab, callback))


def main(argv: Iterable[str] | None = None) -> int:
    """Guest-local entry point for an installed package on Windows or Linux."""
    parser = argparse.ArgumentParser(prog="python -m proxmox_agent_lab.ioworkload")
    commands = parser.add_subparsers(dest="command", required=True)
    _add_commands(commands, lambda callback: lambda args: callback(None, args))
    args = parser.parse_args(list(argv) if argv is not None else None)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
