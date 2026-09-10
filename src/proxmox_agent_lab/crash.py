"""Offline, build-pinned crash address symbolization.

This module intentionally does not read a guest, attach a debugger, or try to
unwind a dump.  It turns addresses which an operator already extracted from a
crash log into source locations, provided the operator supplies an exact build
manifest.  The manifest is the boundary between an address in ASLR-relocated
memory and an object-relative address which ``llvm-symbolizer`` understands.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Iterable

from .errors import LabError


MANIFEST_SCHEMA = "proxmox-agent-lab.crash-manifest/v1"
REPORT_SCHEMA = "proxmox-agent-lab.crash-report/v1"
MAX_FRAMES = 1024
MAX_INPUT_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 60

_HEX = re.compile(r"^(?:0[xX])?[0-9a-fA-F]+$")
_MODULE_OFFSET = re.compile(
    r"(?P<module>[^\s+!]+)[+!](?P<offset>0[xX][0-9a-fA-F]+|[0-9]+)"
)
_ADDRESS = re.compile(r"(?<![0-9A-Za-z_])(?P<address>0[xX][0-9a-fA-F]+)(?![0-9A-Za-z_])")


class CrashError(LabError):
    """A malformed crash artifact or unusable local symbolization tool."""


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CrashError(f"{field} must be an integer, not a boolean")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _HEX.fullmatch(value.strip()):
        text = value.strip()
        number = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    else:
        raise CrashError(f"{field} must be a decimal or hexadecimal integer")
    if number < 0:
        raise CrashError(f"{field} must not be negative")
    return number


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _named_values(values: Iterable[str], option: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        name, marker, item = value.partition("=")
        if not marker or not name or not item:
            raise CrashError(f"{option} entries must have the form NAME=VALUE")
        if name in parsed:
            raise CrashError(f"duplicate {option} entry for module {name!r}")
        parsed[name] = item
    return parsed


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def create_manifest(
    modules: dict[str, str], runtime_bases: dict[str, str], runtime_sizes: dict[str, str],
    pdbs: dict[str, str], provenance: dict[str, str],
) -> dict[str, Any]:
    """Build a manifest from local artifacts, calculating every pinned hash."""
    if not modules:
        raise CrashError("at least one --module NAME=PATH is required")
    missing = set(modules).symmetric_difference(runtime_bases)
    if missing:
        raise CrashError("every module needs exactly one --runtime-base NAME=ADDRESS")
    missing = set(modules).symmetric_difference(runtime_sizes)
    if missing:
        raise CrashError("every module needs exactly one --runtime-size NAME=BYTES")
    extra_pdbs = set(pdbs).difference(modules)
    if extra_pdbs:
        raise CrashError(f"--pdb names not present in --module: {', '.join(sorted(extra_pdbs))}")

    records: list[dict[str, Any]] = []
    for name in sorted(modules):
        path = Path(modules[name]).expanduser().resolve()
        if not path.is_file():
            raise CrashError(f"module {name!r} is not a regular file: {path}")
        size = _integer(runtime_sizes[name], f"runtime size for {name}")
        if not size:
            raise CrashError(f"runtime size for {name} must be greater than zero")
        record: dict[str, Any] = {
            "name": name,
            "path": str(path),
            "sha256": _sha256_file(path),
            "runtime_base": _integer(runtime_bases[name], f"runtime base for {name}"),
            "runtime_size": size,
        }
        if name in pdbs:
            pdb = Path(pdbs[name]).expanduser().resolve()
            if not pdb.is_file():
                raise CrashError(f"PDB for module {name!r} is not a regular file: {pdb}")
            record["pdb"] = {"path": str(pdb), "sha256": _sha256_file(pdb)}
        records.append(record)
    return {"schema": MANIFEST_SCHEMA, "provenance": provenance, "modules": records}


def _path_from_manifest(value: str, manifest_path: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def load_manifest(path_text: str) -> tuple[Path, dict[str, Any]]:
    path = Path(path_text).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CrashError(f"manifest does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CrashError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != MANIFEST_SCHEMA:
        raise CrashError(f"manifest must have schema {MANIFEST_SCHEMA!r}")
    if not isinstance(document.get("provenance"), dict):
        raise CrashError("manifest provenance must be an object")
    modules = document.get("modules")
    if not isinstance(modules, list) or not modules:
        raise CrashError("manifest modules must be a non-empty list")
    names: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for number, source in enumerate(modules, 1):
        if not isinstance(source, dict):
            raise CrashError(f"manifest module {number} must be an object")
        name = source.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise CrashError("every manifest module needs a unique non-empty name")
        names.add(name)
        binary = source.get("path")
        expected = source.get("sha256")
        if not isinstance(binary, str) or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise CrashError(f"module {name!r} needs path and a 64-character sha256")
        size = _integer(source.get("runtime_size"), f"runtime size for {name}")
        if not size:
            raise CrashError(f"runtime size for {name} must be greater than zero")
        record = dict(source)
        record["runtime_base"] = _integer(source.get("runtime_base"), f"runtime base for {name}")
        record["runtime_size"] = size
        record["path"] = str(_path_from_manifest(binary, path))
        record["sha256"] = expected.lower()
        pdb = source.get("pdb")
        if pdb is not None:
            if not isinstance(pdb, dict) or not isinstance(pdb.get("path"), str) or not isinstance(pdb.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", pdb["sha256"]):
                raise CrashError(f"PDB for module {name!r} needs path and a 64-character sha256")
            record["pdb"] = {"path": str(_path_from_manifest(pdb["path"], path)), "sha256": pdb["sha256"].lower()}
        cleaned.append(record)
    document["modules"] = cleaned
    return path, document


def verify_modules(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify all binary and optional PDB hashes before a tool can run."""
    verified: list[dict[str, Any]] = []
    for module in manifest["modules"]:
        binary = Path(module["path"])
        if not binary.is_file():
            raise CrashError(f"verified binary is missing for {module['name']!r}: {binary}")
        actual = _sha256_file(binary)
        if actual != module["sha256"]:
            raise CrashError(f"SHA-256 mismatch for module {module['name']!r}; refusing symbolization")
        result: dict[str, Any] = {"name": module["name"], "binary_sha256": actual, "verified": True}
        pdb = module.get("pdb")
        if pdb:
            pdb_path = Path(pdb["path"])
            if not pdb_path.is_file():
                raise CrashError(f"verified PDB is missing for {module['name']!r}: {pdb_path}")
            pdb_actual = _sha256_file(pdb_path)
            if pdb_actual != pdb["sha256"]:
                raise CrashError(f"SHA-256 mismatch for PDB of {module['name']!r}; refusing symbolization")
            result["pdb_sha256"] = pdb_actual
            result["pdb_hash_verified_only"] = True
        verified.append(result)
    return verified


def parse_frame(raw: str, line: int | None = None) -> dict[str, Any]:
    """Parse an explicit address or the common ``module+offset`` log form."""
    text = raw.strip()
    exact = _MODULE_OFFSET.fullmatch(text)
    if exact:
        return {"raw": raw, "line": line, "kind": "module_offset", "module": exact["module"], "offset": _integer(exact["offset"], "frame offset")}
    if _HEX.fullmatch(text):
        return {"raw": raw, "line": line, "kind": "address", "address": _integer(text, "frame address")}
    module = _MODULE_OFFSET.search(raw)
    if module:
        return {"raw": raw, "line": line, "kind": "module_offset", "module": module["module"], "offset": _integer(module["offset"], "frame offset")}
    address = _ADDRESS.search(raw)
    if address:
        return {"raw": raw, "line": line, "kind": "address", "address": _integer(address["address"], "frame address")}
    return {"raw": raw, "line": line, "kind": "unparsed"}


def read_frames(addresses: Iterable[str], input_path: str | None) -> list[dict[str, Any]]:
    frames = [parse_frame(value) for value in addresses]
    if input_path:
        path = Path(input_path).expanduser()
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CrashError(f"cannot read crash input {path}: {exc}") from exc
        if len(data.encode("utf-8")) > MAX_INPUT_BYTES:
            raise CrashError(f"crash input exceeds {MAX_INPUT_BYTES} bytes")
        frames.extend(parse_frame(value, line) for line, value in enumerate(data.splitlines(), 1) if value.strip())
    if not frames:
        raise CrashError("provide --address and/or --input")
    if len(frames) > MAX_FRAMES:
        raise CrashError(f"refusing more than {MAX_FRAMES} frames")
    return frames


def _locate_frame(frame: dict[str, Any], modules: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(frame)
    if frame["kind"] == "unparsed":
        result.update(state="unresolved", reason="no_address_or_module_offset_found")
        return result
    if frame["kind"] == "module_offset":
        matches = [module for module in modules if module["name"] == frame["module"]]
        if not matches:
            result.update(state="unresolved", reason="unknown_module")
            return result
        module = matches[0]
        if frame["offset"] >= module["runtime_size"]:
            result.update(state="unresolved", reason="module_offset_out_of_range")
            return result
        result.update(module=module["name"], runtime_address=module["runtime_base"] + frame["offset"], object_address=frame["offset"], state="pending")
        return result
    matches = [module for module in modules if module["runtime_base"] <= frame["address"] < module["runtime_base"] + module["runtime_size"]]
    if not matches:
        result.update(state="unresolved", reason="address_not_in_manifest")
        return result
    if len(matches) > 1:
        result.update(state="unresolved", reason="ambiguous_module_mapping", candidates=[module["name"] for module in matches])
        return result
    module = matches[0]
    result.update(module=module["name"], runtime_address=frame["address"], object_address=frame["address"] - module["runtime_base"], state="pending")
    return result


def _decode_symbolizer_output(text: str, expected: int) -> list[Any]:
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        # Some LLVM versions produce one JSON value per input line.
        try:
            decoded = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise CrashError("llvm-symbolizer produced malformed JSON") from exc
    if isinstance(decoded, dict):
        decoded = [decoded]
    if not isinstance(decoded, list) or len(decoded) != expected:
        raise CrashError(f"llvm-symbolizer returned {len(decoded) if isinstance(decoded, list) else 'non-list'} records for {expected} addresses")
    return decoded


def _run_symbolizer(executable: str, module: dict[str, Any], addresses: list[int], timeout: float) -> tuple[list[Any], dict[str, Any]]:
    argv = [executable, "--relative-address", "--obj", module["path"], "--output-style=JSON"]
    if module.get("pdb"):
        argv.extend(["--pdb", module["pdb"]["path"]])
    try:
        completed = subprocess.run(
            argv, input="".join(f"0x{address:x}\n" for address in addresses), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CrashError(f"llvm-symbolizer is unavailable: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CrashError(f"llvm-symbolizer timed out after {timeout:g} seconds") from exc
    tool = {"argv": argv, "returncode": completed.returncode, "stderr": completed.stderr}
    if completed.returncode:
        raise CrashError(f"llvm-symbolizer exited with status {completed.returncode}: {completed.stderr.strip() or 'no stderr'}")
    return _decode_symbolizer_output(completed.stdout, len(addresses)), tool


def symbolize(manifest_path: str, frames: list[dict[str, Any]], executable: str, timeout: float) -> dict[str, Any]:
    if timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise CrashError(f"--timeout must be greater than zero and at most {MAX_TIMEOUT_SECONDS} seconds")
    manifest_file, manifest = load_manifest(manifest_path)
    verification = verify_modules(manifest)
    tool_path = shutil.which(executable) if os.path.sep not in executable else executable
    if not tool_path:
        raise CrashError(f"llvm-symbolizer is unavailable: {executable}")
    report_frames = [_locate_frame(frame, manifest["modules"]) for frame in frames]
    symbolizer: list[dict[str, Any]] = []
    for module in manifest["modules"]:
        targets = [frame for frame in report_frames if frame.get("state") == "pending" and frame.get("module") == module["name"]]
        if not targets:
            continue
        try:
            responses, invocation = _run_symbolizer(tool_path, module, [frame["object_address"] for frame in targets], timeout)
        except CrashError as exc:
            for frame in targets:
                frame.update(state="unresolved", reason="symbolizer_failure", detail=str(exc))
            symbolizer.append({"module": module["name"], "error": str(exc)})
            continue
        invocation["module"] = module["name"]
        symbolizer.append(invocation)
        for frame, response in zip(targets, responses):
            frame.update(state="symbolized", symbolizer_response=response)
    return {
        "schema": REPORT_SCHEMA,
        "manifest": {"path": str(manifest_file), "sha256": _sha256_file(manifest_file)},
        "provenance": manifest["provenance"],
        "verification": verification,
        "frames": report_frames,
        "symbolizer": symbolizer,
        "limitations": [
            "Addresses are mapped only by the manifest runtime base and runtime size; this command does not unwind arbitrary dumps.",
            "PDB SHA-256 verification proves the supplied PDB file was not changed. It does not prove that PDB identity matches the PE binary.",
            "ELF DWARF and PE/PDB results depend on the installed llvm-symbolizer and the debug information available to it.",
        ],
    }


def _display_address(value: Any) -> str:
    return f"0x{value:x}" if isinstance(value, int) else ""


def markdown_report(report: dict[str, Any]) -> str:
    """Render a small human report while retaining JSON as the source evidence."""
    lines = ["# Crash symbolization report", "", "| Frame | Mapping | Result |", "| --- | --- | --- |"]
    for frame in report["frames"]:
        mapping = frame.get("module", "")
        if "object_address" in frame:
            mapping = f"{mapping}+{_display_address(frame['object_address'])}"
        result = frame["state"] if frame["state"] == "symbolized" else frame.get("reason", frame["state"])
        raw = str(frame["raw"]).replace("|", "\\|")
        lines.append(f"| {raw} | {mapping} | {result} |")
    lines.extend(["", "## Limits", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def cmd_manifest_create(lab: Any, args: Any) -> None:
    manifest = create_manifest(
        _named_values(args.module, "--module"), _named_values(args.runtime_base, "--runtime-base"),
        _named_values(args.runtime_size, "--runtime-size"), _named_values(args.pdb, "--pdb"),
        {"source_revision": args.source_revision or "unknown", "reference": args.reference or "user-supplied local artifacts", "supplied_by": args.supplied_by or "operator"},
    )
    output = Path(args.out).expanduser().resolve()
    _atomic_json(output, manifest)
    print(json.dumps({"manifest": str(output), "schema": MANIFEST_SCHEMA, "modules": len(manifest["modules"])}, sort_keys=True))


def cmd_symbolize(lab: Any, args: Any) -> None:
    report = symbolize(args.manifest, read_frames(args.address, args.input), args.symbolizer, args.timeout)
    if args.json_out:
        _atomic_json(Path(args.json_out).expanduser().resolve(), report)
    if args.markdown_out:
        destination = Path(args.markdown_out).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind

    crash = sub.add_parser("crash", help="offline, build-pinned crash address symbolization")
    crash_sub = crash.add_subparsers(dest="crash_command", required=True)
    create = crash_sub.add_parser("manifest-create", help="hash local modules and record ASLR runtime mappings")
    create.add_argument("--module", action="append", default=[], metavar="NAME=PATH", help="module binary (repeatable)")
    create.add_argument("--runtime-base", action="append", default=[], metavar="NAME=ADDRESS", help="module runtime load base (repeatable)")
    create.add_argument("--runtime-size", action="append", default=[], metavar="NAME=BYTES", help="mapped image size (repeatable)")
    create.add_argument("--pdb", action="append", default=[], metavar="NAME=PATH", help="optional PE PDB (hash is verified but identity is not inferred)")
    create.add_argument("--source-revision", help="build source revision supplied by the operator")
    create.add_argument("--reference", help="user-supplied build/release reference")
    create.add_argument("--supplied-by", help="who supplied the artifacts")
    create.add_argument("--out", required=True, help="manifest JSON path")
    create.set_defaults(func=_bind(lab, cmd_manifest_create))
    symbols = crash_sub.add_parser("symbolize", help="symbolize explicit addresses or crash-log frames locally")
    symbols.add_argument("--manifest", required=True, help="manifest produced by crash manifest-create")
    symbols.add_argument("--address", action="append", default=[], help="address or module+offset frame (repeatable)")
    symbols.add_argument("--input", help="text crash log; addresses and module+offset frames are extracted line by line")
    symbols.add_argument("--symbolizer", default="llvm-symbolizer", help="local llvm-symbolizer executable or path")
    symbols.add_argument("--timeout", type=float, default=15, help="per-module tool timeout in seconds (max 60)")
    symbols.add_argument("--json-out", help="also write the JSON evidence report to this path")
    symbols.add_argument("--markdown-out", help="also write a brief Markdown report to this path")
    symbols.set_defaults(func=_bind(lab, cmd_symbolize))
