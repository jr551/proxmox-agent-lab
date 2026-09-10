# Offline crash reports

`proxmox-lab crash` symbolizes addresses that have already been collected from
a guest crash log. It never starts a guest, reads guest memory, uploads an
artifact, or unwinds a dump. The only external program it uses is a local
`llvm-symbolizer`; install that separately if it is not already on `PATH`.

Create a manifest while the exact binaries from the crashed build are still
available. Each module requires its actual runtime ASLR base and mapped image
size. The size is the memory mapping extent from the crash environment, not
necessarily the on-disk file length.

```bash
proxmox-lab crash manifest-create \
  --module ntoskrnl=./ntoskrnl.exe \
  --runtime-base ntoskrnl=0x804d7000 \
  --runtime-size ntoskrnl=0x5a0000 \
  --pdb ntoskrnl=./ntoskrnl.pdb \
  --source-revision gdbb43bbaeb2 \
  --reference 'ReactOS local build' \
  --supplied-by operator \
  --out reactos-manifest.json
```

For ELF/DWARF builds, omit `--pdb`; `llvm-symbolizer` uses the ELF binary and
any debug information it can discover. For PE/PDB builds, `--pdb` passes the
explicit PDB path to LLVM. The command hashes every supplied binary and PDB.

Symbolize raw addresses, module offsets, or a text crash log:

```bash
proxmox-lab crash symbolize \
  --manifest reactos-manifest.json \
  --address 0x8061f2a0 \
  --address 'ntoskrnl+0x1482a0' \
  --input crash-console.log \
  --json-out evidence.json \
  --markdown-out report.md
```

The JSON report preserves each raw input line, the computed runtime and
object-relative addresses, the hash-verification record, the manifest
provenance, and LLVM's JSON response. `module+offset` and `module!offset` are
supported. For a raw runtime address the mapping is:

```text
object-relative address = runtime address - manifest runtime base
```

The tool verifies all recorded binary and PDB hashes before launching LLVM. A
hash mismatch, a missing binary, an unavailable tool, or invalid manifest
stops symbolization. Addresses outside all mappings, ambiguous overlapping
mappings, unknown modules, and out-of-range offsets remain in the report as
unresolved rather than being guessed.

There are deliberate limits. This is address-to-symbol lookup, not automatic
unwinding of arbitrary Linux core files, Windows minidumps, or guest RAM.
It cannot recover a stack from a screenshot or validate which build a log came
from. A PDB hash proves that the supplied PDB has not changed, but does not by
itself verify the PDB's GUID/age identity against the PE binary. Results also
depend on the LLVM version and the actual ELF DWARF or PE/PDB information
available locally.
