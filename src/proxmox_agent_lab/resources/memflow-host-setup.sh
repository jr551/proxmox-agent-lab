#!/usr/bin/env bash
# Prepare a Proxmox host for memflow introspection.
# Installed to /usr/local/sbin/pxl-memflow-setup and executed by
# 'proxmox-lab memflow host-setup --host-change-authorized'. Runs as root.
#
# Installs a Rust toolchain (if absent), builds the pxl-memflow tool
# (memflow + memflow-qemu + memflow-win32), and installs it alongside the
# pxl-memflow-run helper and the pxl-gdb stepper. No kernel changes, no
# reboot. Offline decompilation is not installed here.
set -euo pipefail

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf '!  %s\n' "$*" >&2; }
die()  { printf 'x  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this as root on the Proxmox host"
command -v qemu-system-x86_64 >/dev/null 2>&1 || \
  warn "qemu-system-x86_64 not found; is this the hypervisor?"

step "Rust toolchain"
if ! command -v cargo >/dev/null 2>&1 && [ ! -x "$HOME/.cargo/bin/cargo" ]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
fi
. "$HOME/.cargo/env"
say "  cargo $(cargo --version | awk '{print $2}')"

step "build dependencies"
export DEBIAN_FRONTEND=noninteractive
# A Proxmox enterprise repo without a subscription makes 'update' exit non-zero
# even though the Debian repos we need refreshed fine; do not abort on it.
apt-get update -qq || warn "apt-get update reported errors (unsubscribed enterprise repo?); continuing"
apt-get install -y -qq pkg-config build-essential python3-capstone >/dev/null 2>&1 \
  || warn "apt build deps install reported errors; continuing"

step "build pxl-memflow"
mkdir -p /root/pxl-memflow/src
cat > /root/pxl-memflow/Cargo.toml <<'TOML'
[package]
name = "pxl-memflow"
version = "0.1.0"
edition = "2021"

[dependencies]
memflow = "0.2"
memflow-qemu = "0.2"
memflow-win32 = "0.2"
serde_json = "1"
hex = "0.4"

[[bin]]
name = "pxl-memflow"
path = "src/main.rs"
TOML
cat > /root/pxl-memflow/src/main.rs <<'RUST'
// Agentless introspection of a running QEMU guest via memflow. Reads/writes
// the guest's kernel virtual memory and lists Windows processes. The guest is
// addressed by its QEMU pid (robust on Proxmox, which launches QEMU directly
// rather than through libvirt).
use memflow::prelude::v1::*;
use memflow_win32::prelude::v1::*;
use serde_json::json;
use std::env;

fn parse_addr(s: &str) -> u64 {
    let s = s.trim();
    let parsed = if let Some(h) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        u64::from_str_radix(h, 16)
    } else {
        s.parse()
    };
    match parsed {
        Ok(addr) => addr,
        Err(_) => {
            eprintln!("address is not a plain integer");
            std::process::exit(64);
        }
    }
}

const MAX_IO: usize = 16 * 1024 * 1024;

fn require_len(raw: &str) -> usize {
    let len: usize = raw.parse().unwrap_or(0);
    if len == 0 || len > MAX_IO {
        eprintln!("len must be between 1 and {MAX_IO}");
        std::process::exit(64);
    }
    len
}

fn require_bytes(bytes: &[u8]) {
    if bytes.is_empty() || bytes.len() > MAX_IO {
        eprintln!("payload must be between 1 and {MAX_IO} bytes");
        std::process::exit(64);
    }
}

fn connect(target: &str) -> Result<impl PhysicalMemory> {
    let args = ConnectorArgs::new(Some(target), Default::default(), None);
    memflow_qemu::create_connector(&args)
}

fn build(target: &str) -> Result<impl MemoryView + Os> {
    // Build the connector inline here (not via connect()): the Win32 cache
    // layers need the concrete connector's full trait set, which an opaque
    // `impl PhysicalMemory` return would erase.
    let args = ConnectorArgs::new(Some(target), Default::default(), None);
    let connector = memflow_qemu::create_connector(&args)?;
    Win32Kernel::builder(connector).build_default_caches().build()
}

fn run(args: &[String]) -> Result<()> {
    let cmd = args.get(0).map(String::as_str).unwrap_or("");
    let target = args.get(1).map(String::as_str).unwrap_or("");
    // Physical-memory commands go straight through the QEMU connector with no
    // OS layer, so raw RAM read/write/scan works on any guest, not just
    // Windows.
    match cmd {
        "phys-read" => {
            let mut conn = connect(target)?;
            let mut view = conn.phys_view();
            let addr = parse_addr(&args[2]);
            let len = require_len(&args[3]);
            let mut buf = vec![0u8; len];
            view.read_raw_into(Address::from(addr), &mut buf).data_part()?;
            println!("{}", json!({
                "addr": format!("{:#x}", addr), "len": len, "hex": hex::encode(&buf)
            }));
            return Ok(());
        }
        "phys-write" => {
            let mut conn = connect(target)?;
            let mut view = conn.phys_view();
            let addr = parse_addr(&args[2]);
            let bytes = hex::decode(args[3].trim())
                .map_err(|_| Error(ErrorOrigin::Other, ErrorKind::Configuration))?;
            require_bytes(&bytes);
            view.write_raw(Address::from(addr), &bytes).data_part()?;
            println!("{}", json!({
                "addr": format!("{:#x}", addr), "written": bytes.len()
            }));
            return Ok(());
        }
        "phys-scan" => {
            let mut conn = connect(target)?;
            let max = conn.metadata().max_address.to_umem();
            let mut view = conn.phys_view();
            let needle = hex::decode(args[2].trim())
                .map_err(|_| Error(ErrorOrigin::Other, ErrorKind::Configuration))?;
            if needle.is_empty() || needle.len() > 4096 {
                eprintln!("needle must be between 1 and 4096 bytes");
                std::process::exit(64);
            }
            let maxhits: usize = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(8);
            let chunk: usize = 4 * 1024 * 1024;
            let overlap = needle.len().saturating_sub(1);
            let n = needle.len();
            let mut hits: Vec<String> = Vec::new();
            let mut base: u64 = 0;
            while (base as u128) < (max as u128) && hits.len() < maxhits {
                let want = std::cmp::min(
                    (chunk + overlap) as u128, (max as u128) - base as u128) as usize;
                let mut b = vec![0u8; want];
                // Physical RAM has holes; a failed window is skipped, not fatal.
                let _ = view.read_raw_into(Address::from(base), &mut b);
                if b.len() >= n {
                    let mut i = 0;
                    while i + n <= b.len() {
                        if &b[i..i + n] == needle.as_slice() {
                            hits.push(format!("{:#x}", base + i as u64));
                            if hits.len() >= maxhits { break; }
                        }
                        i += 1;
                    }
                }
                base += chunk as u64;
            }
            println!("{}", json!({"needle_len": n, "hits": hits}));
            return Ok(());
        }
        _ => {}
    }
    let mut kernel = build(target)?;
    match cmd {
        "check" => println!("{}", json!({"introspectable": true})),
        "process-list" => {
            let list = kernel.process_info_list()?;
            let rows: Vec<_> = list.iter()
                .map(|p| json!({"pid": p.pid, "name": p.name.to_string()}))
                .collect();
            println!("{}", serde_json::to_string(&rows).unwrap());
        }
        "read" => {
            let addr = parse_addr(&args[2]);
            let len = require_len(&args[3]);
            let mut buf = vec![0u8; len];
            kernel.read_raw_into(Address::from(addr), &mut buf).data_part()?;
            println!("{}", json!({
                "addr": format!("{:#x}", addr), "len": len,
                "hex": hex::encode(&buf)
            }));
        }
        "write" => {
            let addr = parse_addr(&args[2]);
            let bytes = hex::decode(args[3].trim())
                .map_err(|_| Error(ErrorOrigin::Other, ErrorKind::Configuration))?;
            require_bytes(&bytes);
            kernel.write_raw(Address::from(addr), &bytes).data_part()?;
            println!("{}", json!({
                "addr": format!("{:#x}", addr), "written": bytes.len()
            }));
        }
        _ => {
            eprintln!("usage: pxl-memflow <check|process-list|read|write|phys-read|phys-write|phys-scan> <target> [args]");
            std::process::exit(64);
        }
    }
    Ok(())
}

fn main() {
    let argv: Vec<String> = env::args().skip(1).collect();
    if let Err(e) = run(&argv) {
        eprintln!("{}", e);
        std::process::exit(3);
    }
}
RUST
( cd /root/pxl-memflow && cargo build --release >/dev/null )
install -m 0755 /root/pxl-memflow/target/release/pxl-memflow /usr/local/bin/pxl-memflow
say "  installed /usr/local/bin/pxl-memflow"

step "pxl-memflow-run helper"
install -m 0755 /dev/stdin /usr/local/bin/pxl-memflow-run <<'HELP'
#!/usr/bin/env bash
# Map a Proxmox VMID to its QEMU pid, then introspect it with pxl-memflow.
# 'registers' reads the vCPU state from the QEMU monitor (memflow's /proc
# connector sees RAM only, not registers).
set -euo pipefail
QMP_DIR=/var/run/qemu-server
cmd="${1:-}"; vmid="${2:-}"
pid_for(){ local f="$QMP_DIR/$1.pid"; [ -f "$f" ] && cat "$f" || return 1; }
need_vmid(){ [ -n "$vmid" ] || { echo "vmid required" >&2; exit 64; }; }
case "$cmd" in
  doctor)
    have_bin=false; command -v pxl-memflow >/dev/null 2>&1 && have_bin=true
    proc_ok=false; [ -r /proc/self/mem ] && proc_ok=true
    printf '{"tool_installed": %s, "proc_readable": %s}\n' "$have_bin" "$proc_ok"
    ;;
  check|process-list)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow "$cmd" "$pid"
    ;;
  read)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow read "$pid" "${3:?addr}" "${4:?len}"
    ;;
  write)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow write "$pid" "${3:?addr}" "${4:?hex}"
    ;;
  phys-read)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow phys-read "$pid" "${3:?addr}" "${4:?len}"
    ;;
  phys-write)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow phys-write "$pid" "${3:?addr}" "${4:?hex}"
    ;;
  scan)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow phys-scan "$pid" "${3:?hex}" "${4:-8}"
    ;;
  registers)
    need_vmid; pid_for "$vmid" >/dev/null || { echo "VMID $vmid is not running" >&2; exit 3; }
    echo "info registers" | qm monitor "$vmid" 2>/dev/null | python3 -c '
import re, sys, json
regs = {}
for m in re.finditer(r"\b([A-Z][A-Z0-9]{1,4})\s*=\s*([0-9a-fA-F]+)", sys.stdin.read()):
    regs.setdefault(m.group(1), m.group(2))
print(json.dumps(regs))
'
    ;;
  debug-trace)
    need_vmid; pid_for "$vmid" >/dev/null || { echo "VMID $vmid is not running" >&2; exit 3; }
    port=$((23000 + vmid % 1000))
    exec pxl-gdb "$port" "$vmid" trace "${3:?steps}" "${4:-}"
    ;;
  debug-break)
    need_vmid; pid_for "$vmid" >/dev/null || { echo "VMID $vmid is not running" >&2; exit 3; }
    port=$((23000 + vmid % 1000))
    exec pxl-gdb "$port" "$vmid" break "${3:?addr}" "${4:-15}"
    ;;
  *) echo "usage: pxl-memflow-run {doctor|check|process-list|read|write|phys-read|phys-write|scan|registers|debug-trace|debug-break} [vmid] [args]" >&2; exit 64;;
esac
HELP
say "  installed /usr/local/bin/pxl-memflow-run"

step "pxl-gdb helper (live stepping via the QEMU gdbstub)"
install -m 0755 /dev/stdin /usr/local/bin/pxl-gdb <<'PYEOF'
#!/usr/bin/env python3
"""Minimal GDB-remote (RSP) client for QEMU's built-in gdbstub.

Drives single-step (into), step-over, breakpoints and continue against a
running guest, so we get live debugging with no patched kernel. Enables the
stub via the QEMU monitor if it is not already listening. Prints JSON.
"""
import json, re, socket, subprocess, sys, time

RIP_REGNUM = 0x10  # x86-64 gdb regnum for RIP

def monitor(vmid, cmd):
    subprocess.run(["qm", "monitor", str(vmid)], input=cmd + "\n",
                   text=True, capture_output=True, timeout=15)

class RSP:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.s.settimeout(10)
    def _send(self, data):
        cksum = sum(data.encode()) & 0xff
        self.s.sendall(b"$" + data.encode() + b"#" + b"%02x" % cksum)
        self.s.recv(1)
    def _recv(self):
        buf = b""
        while True:
            c = self.s.recv(1)
            if not c:
                break
            if c == b"$":
                buf = b""
            elif c == b"#":
                self.s.recv(2)
                break
            else:
                buf += c
        self.s.sendall(b"+")
        return buf.decode(errors="replace")
    def cmd(self, data):
        self._send(data)
        return self._recv()
    def read_reg(self, num):
        r = self.cmd("p%x" % num)
        return int.from_bytes(bytes.fromhex(r), "little") \
            if re.fullmatch(r"[0-9a-fA-F]+", r) else None
    def read_mem(self, addr, length):
        r = self.cmd("m%x,%x" % (addr, length))
        return bytes.fromhex(r) if re.fullmatch(r"[0-9a-fA-F]+", r) else b""
    def step(self):
        return self.cmd("s")
    def set_bp(self, addr):
        return self.cmd("Z0,%x,1" % addr)
    def clr_bp(self, addr):
        return self.cmd("z0,%x,1" % addr)
    def cont(self, timeout=10):
        self._send("c")
        self.s.settimeout(timeout)
        try:
            return self._recv()
        except socket.timeout:
            return ""
        finally:
            self.s.settimeout(10)
    def detach(self):
        try:
            self.cmd("D")
        except Exception:
            pass
        self.s.close()

def disasm_one(md, code, addr):
    for insn in md.disasm(code, addr):
        return insn.mnemonic, insn.op_str, insn.size
    return "?", "", 1

def qemu_pid(vmid):
    with open("/var/run/qemu-server/%s.pid" % vmid) as fh:
        return fh.read().strip()

def listening_ports(pid):
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
    ports = []
    for line in out.splitlines():
        if ("pid=%s," % pid) not in line:
            continue
        parts = line.split()
        if len(parts) >= 4:
            m = re.search(r":(\d+)$", parts[3])
            if m:
                ports.append(int(m.group(1)))
    return sorted(set(ports))

def is_rsp(port):
    # QEMU allows only one gdbstub, on a port we may not have chosen, so probe
    # each of the qemu process's listeners: the gdbstub answers '?' with a stop
    # packet, nothing else will.
    try:
        r = RSP(port); r.s.settimeout(2)
        reply = r.cmd("?"); r.s.close()
        return bool(reply) and reply[0] in "STWXsO"
    except OSError:
        return False

def connect_stub(vmid, hint):
    pid = qemu_pid(vmid)
    for p in listening_ports(pid):
        if is_rsp(p):
            return RSP(p)
    monitor(vmid, "gdbserver tcp::%d" % hint); time.sleep(1)
    return RSP(hint)

def main():
    import capstone
    port = int(sys.argv[1]); vmid = sys.argv[2]; op = sys.argv[3]
    rsp = connect_stub(vmid, port)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    try:
        if op == "trace":
            n = int(sys.argv[4]); over = (len(sys.argv) > 5 and sys.argv[5] == "over")
            steps = []
            for _ in range(n):
                rip = rsp.read_reg(RIP_REGNUM)
                mn, ops, size = disasm_one(md, rsp.read_mem(rip, 16), rip)
                steps.append({"rip": "%#x" % rip, "insn": (mn + " " + ops).strip()})
                if over and mn.startswith("call"):
                    ret = rip + size
                    rsp.set_bp(ret); rsp.cont(); rsp.clr_bp(ret)
                else:
                    rsp.step()
            print(json.dumps({"steps": steps}))
        elif op == "break":
            addr = int(sys.argv[4], 0)
            timeout = int(sys.argv[5]) if len(sys.argv) > 5 else 15
            rsp.set_bp(addr); rsp.cont(timeout)
            rip = rsp.read_reg(RIP_REGNUM); rsp.clr_bp(addr)
            print(json.dumps({"stopped_at": "%#x" % rip, "hit": rip == addr}))
        else:
            print(json.dumps({"error": "unknown op"})); sys.exit(64)
    finally:
        rsp.detach()

if __name__ == "__main__":
    main()
PYEOF
say "  installed /usr/local/bin/pxl-gdb"

step "Done"
say "  Verify from your controller: proxmox-lab memflow doctor"
