"""Windows guest installation and first-boot setup.

Two ways to install, both supported:

* Interactive (default). Clone a retained Windows installation template, start
  it, and drive Setup over VNC with `console screenshot` / `click` / `type`.
  This is the path built for multimodal models -- they can simply look at the
  screen.
* Unattended (`--unattended`). Generate an `autounattend.xml`, wrap it in a
  tiny ISO, attach it, and let Setup run itself. Windows Setup can only read an
  answer file from removable media, so this one case still needs an ISO; every
  other file transfer uses the S3 scratch bucket.

  One caveat, measured rather than assumed: Setup opens on its language page
  and waits there even with a valid answer file attached. Everything after
  that page is automated by the answer file, so the install sends Enter until
  the disk starts filling. See `_dismiss_setup_ui`.

After Setup finishes, `windows finish` uses qemu-guest-agent to enable RDP and
OpenSSH and report the guest address.
"""

from __future__ import annotations

import json
from pathlib import Path
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
from typing import Any
from xml.sax.saxutils import escape

from . import console

WINDOWS_VERSIONS = ("2022", "2025")
DEFAULT_IMAGE_INDEX = 2  # Standard with Desktop Experience on the eval media


def _template_vmid(lab: Any, version: str, override: int | None = None) -> int:
    """Resolve site inventory without baking one lab's VMIDs into the package."""
    if override:
        return int(override)
    setting = f"template_{version}_vmid"
    vmid = int(lab.CONFIG.windows.get(setting, 0) or 0)
    if vmid <= 0:
        raise lab.LabError(
            f"Windows {version} template VMID is not configured. Set "
            f"[windows] {setting} or pass --template-vmid."
        )
    return vmid


def _driver_branch(version: str, override: str | None = None) -> str:
    return override or f"2k{version[-2:]}"

UNATTEND_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35" language="neutral"
               versionScope="nonSxS">
      <SetupUILanguage><UILanguage>{locale}</UILanguage></SetupUILanguage>
      <InputLocale>{locale}</InputLocale>
      <SystemLocale>{locale}</SystemLocale>
      <UILanguage>{locale}</UILanguage>
      <UserLocale>{locale}</UserLocale>
    </component>
    <component name="Microsoft-Windows-PnpCustomizationsWinPE"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35" language="neutral"
               versionScope="nonSxS">
      <DriverPaths>
        <PathAndCredentials wcm:action="add" wcm:keyValue="1"
                            xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Path>E:\\vioscsi\\{driver_branch}\\amd64</Path>
        </PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="2"
                            xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Path>E:\\NetKVM\\{driver_branch}\\amd64</Path>
        </PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="3"
                            xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Path>E:\\Balloon\\{driver_branch}\\amd64</Path>
        </PathAndCredentials>
        <!-- vioserial carries the qemu-guest-agent channel. Without it the
             agent service runs but the host cannot reach it, and Windows
             reports "PCI Simple Communications Controller" with error 28. -->
        <PathAndCredentials wcm:action="add" wcm:keyValue="4"
                            xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Path>E:\\vioserial\\{driver_branch}\\amd64</Path>
        </PathAndCredentials>
      </DriverPaths>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35" language="neutral"
               versionScope="nonSxS">
      <DiskConfiguration>
        <WillShowUI>OnError</WillShowUI>
        <Disk wcm:action="add"
              xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add">
              <Order>1</Order><Type>EFI</Type><Size>260</Size>
            </CreatePartition>
            <CreatePartition wcm:action="add">
              <Order>2</Order><Type>MSR</Type><Size>16</Size>
            </CreatePartition>
            <CreatePartition wcm:action="add">
              <Order>3</Order><Type>Primary</Type><Extend>true</Extend>
            </CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add">
              <Order>1</Order><PartitionID>1</PartitionID>
              <Format>FAT32</Format><Label>System</Label>
            </ModifyPartition>
            <ModifyPartition wcm:action="add">
              <Order>2</Order><PartitionID>2</PartitionID>
            </ModifyPartition>
            <ModifyPartition wcm:action="add">
              <Order>3</Order><PartitionID>3</PartitionID>
              <Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter>
            </ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add"
                      xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
              <Key>/IMAGE/INDEX</Key><Value>{image_index}</Value>
            </MetaData>
          </InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
        </OSImage>
      </ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <FullName>{owner}</FullName>
        <Organization>{owner}</Organization>
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35" language="neutral"
               versionScope="nonSxS">
      <ComputerName>{hostname}</ComputerName>
      <TimeZone>{timezone}</TimeZone>
    </component>
    <component name="Microsoft-Windows-TerminalServices-LocalSessionManager"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35" language="neutral"
               versionScope="nonSxS">
      <fDenyTSConnections>false</fDenyTSConnections>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35" language="neutral"
               versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
        <NetworkLocation>Work</NetworkLocation>
      </OOBE>
      <UserAccounts>
        <AdministratorPassword>
          <Value>{admin_password}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
      </UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled>
        <LogonCount>1</LogonCount>
        <Username>Administrator</Username>
        <Password><Value>{admin_password}</Value><PlainText>true</PlainText></Password>
      </AutoLogon>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add"
                            xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Order>1</Order>
          <Description>Trust the virtio-win signing CA</Description>
          <CommandLine>cmd /c for %d in (D E F G H) do @if exist %d:\\cert\\Virtio_Win_Red_Hat_CA.cer (certutil -addstore -f Root %d:\\cert\\Virtio_Win_Red_Hat_CA.cer &amp; certutil -addstore -f TrustedPublisher %d:\\cert\\Virtio_Win_Red_Hat_CA.cer)</CommandLine>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add"
                            xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Order>2</Order>
          <Description>QEMU guest agent</Description>
          <CommandLine>cmd /c for %d in (D E F G H) do @if exist %d:\\guest-agent\\qemu-ga-x86_64.msi msiexec /i %d:\\guest-agent\\qemu-ga-x86_64.msi /qn /norestart</CommandLine>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add"
                            xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Order>3</Order>
          <Description>Remote Desktop firewall rule</Description>
          <CommandLine>netsh advfirewall firewall set rule group="remote desktop" new enable=Yes</CommandLine>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add"
                            xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Order>4</Order>
          <Description>Allow ICMP</Description>
          <CommandLine>netsh advfirewall firewall add rule name="ICMP" protocol=icmpv4:8,any dir=in action=allow</CommandLine>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
"""


def generate_password(length: int = 20) -> str:
    """A password that satisfies the Windows complexity rules."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_="
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(not c.isalnum() for c in candidate)
        ):
            return candidate


def render_unattend(**values: Any) -> str:
    escaped = {key: escape(str(value)) for key, value in values.items()}
    return UNATTEND_TEMPLATE.format(**escaped)


def build_answer_iso(xml: str, target: Path) -> Path:
    """Wrap autounattend.xml in a small ISO.

    Windows Setup reads the answer file from removable media only, which is why
    this one file is not delivered through the S3 scratch bucket.
    """
    with tempfile.TemporaryDirectory() as staging:
        root = Path(staging) / "root"
        root.mkdir()
        (root / "autounattend.xml").write_text(xml, encoding="utf-8")
        if shutil.which("hdiutil"):
            command = [
                "hdiutil", "makehybrid", "-quiet", "-iso", "-joliet",
                "-default-volume-name", "UNATTEND", "-o", str(target), str(root),
            ]
        elif shutil.which("xorriso"):
            command = [
                "xorriso", "-as", "mkisofs", "-quiet", "-J", "-r",
                "-V", "UNATTEND", "-o", str(target), str(root),
            ]
        else:
            raise RuntimeError("neither hdiutil nor xorriso is available")
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(
                f"answer ISO build failed: {(result.stderr or result.stdout)[:400]}"
            )
    return target


def _clone(lab: Any, api: Any, source_vmid: int, vmid: int, name: str,
           full: bool, storage: str | None = None) -> str:
    payload: dict[str, Any] = {
        "newid": vmid,
        "name": name,
        "full": 1 if full else 0,
        "target": lab.NODE,
    }
    if storage:
        # Only a full clone can be redirected to another storage; Proxmox
        # rejects `storage` on a linked clone rather than ignoring it.
        if not full:
            raise lab.LabError(
                "--storage requires --full-clone; a linked clone must stay on "
                "the template's storage"
            )
        payload["storage"] = storage
    return api.call(
        "POST", f"/nodes/{lab.NODE}/qemu/{source_vmid}/clone", payload
    )


def _tap_boot_prompt(lab: Any, api: Any, vmid: int,
                     taps: int = 12, delay: float = 1.0) -> int:
    """Tap Enter across the UEFI "press any key to boot from CD" window.

    Best-effort and bounded: it uses the sendkey API (no VNC session needed),
    swallows errors so it can never fail the install, and sends only for the
    length of the boot window. Returns how many taps landed.
    """
    sent = 0
    for _ in range(taps):
        try:
            api.call("PUT", f"/nodes/{lab.NODE}/qemu/{vmid}/sendkey",
                     {"key": "ret"})
            sent += 1
        except lab.LabError:
            break
        time.sleep(delay)
    return sent


def _dismiss_setup_ui(lab: Any, api: Any, vmid: int,
                      budget: float = 300.0, delay: float = 10.0) -> int:
    """Get Setup past its language page, which no answer file dismisses.

    Measured on Server 2022 eval media: an answer file on a second drive
    supplies everything -- image index, EULA, partitioning, drivers -- but
    Setup still opens on "Language to install" and waits there forever.

    Enter does not clear it. Thirty of them changed nothing, because focus sits
    on the "Language to install" combo box and a combo box consumes Enter. The
    key that works is the Next button's accelerator, Alt+N, which moved the
    installer straight to copying files on the first press.

    Both are sent: Alt+N is what actually lands here, and Enter costs nothing
    and covers a dialog whose default button does respond to it. NOTE the
    accelerator is the *English* one -- a localised installer labels the button
    differently ("Weiter" wants Alt+W), so a non-en-US --locale may still need
    a click.

    Taps stop the moment the disk starts filling, so this cannot keep typing at
    a running installer. Best-effort: never fails the install.
    """
    deadline = time.monotonic() + budget
    baseline = _disk_written(lab, api, vmid)
    sent = 0
    while time.monotonic() < deadline:
        if _disk_written(lab, api, vmid) - baseline >= STALL_WRITE_BYTES:
            break  # it is installing; nothing left to dismiss
        try:
            for key in ("alt-n", "ret"):
                api.call("PUT", f"/nodes/{lab.NODE}/qemu/{vmid}/sendkey",
                         {"key": key})
                sent += 1
        except lab.LabError:
            break
        time.sleep(delay)
    return sent


def _prepare_unattended(lab: Any, api: Any, args: Any, name: str,
                        driver_branch: str, supplied_password: str | None) -> tuple[str | None, str | None]:
    """Build the answer file ISO and attach it. Returns (password, volume)."""
    if not args.unattended:
        return None, None
    password = supplied_password or generate_password()
    xml = render_unattend(
        locale=args.locale,
        timezone=args.timezone,
        hostname=args.hostname or name[:15],
        owner=args.owner,
        image_index=args.image_index,
        driver_branch=driver_branch,
        admin_password=password,
    )
    with tempfile.TemporaryDirectory() as staging:
        iso_path = Path(staging) / f"autounattend-{args.vmid}.iso"
        build_answer_iso(xml, iso_path)
        if not iso_path.exists():
            alternative = iso_path.with_suffix(".iso.cdr")
            if alternative.exists():
                alternative.rename(iso_path)
        volume = _upload_iso(lab, api, args.lease, iso_path)
    api.call(
        "PUT",
        f"/nodes/{lab.NODE}/qemu/{args.vmid}/config",
        {"ide3": f"{volume},media=cdrom"},
    )
    return password, volume


def _boot_with_taps(lab: Any, api: Any, args: Any) -> dict[str, Any]:
    """Start the guest and tap past the boot prompt + Setup language page."""
    result: dict[str, Any] = {}
    start_upid = api.call("POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/status/start")
    lab.wait_task(api, start_upid, timeout=120)
    result["started"] = True
    if args.unattended and args.boot_key:
        result["boot_key_taps"] = _tap_boot_prompt(lab, api, args.vmid)
        result["setup_ui_taps"] = _dismiss_setup_ui(lab, api, args.vmid)
    return result


def cmd_install(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    lease = lab.load_lease(args.lease)
    template_vmid = _template_vmid(lab, args.version, args.template_vmid)
    if args.vmid in lease["initial_vmids"]:
        raise lab.LabError(f"VMID {args.vmid} existed before this lease")
    name = args.name or f"win{args.version}-lab-{args.vmid}"
    driver_branch = _driver_branch(args.version, args.driver_branch)
    supplied_password: str | None = None
    if args.unattended and args.password_stdin:
        supplied_password = sys.stdin.readline().strip()
        if not supplied_password:
            raise lab.LabError(
                "--password-stdin received an empty Administrator password; "
                "omit the flag to have a strong one generated"
            )
    upid = _clone(lab, api, template_vmid, args.vmid, name,
                  args.full_clone, args.storage)
    lab.wait_task(api, upid, timeout=args.clone_timeout)
    lab.register_resource(lease, "qemu", args.vmid, args.policy, name)
    config: dict[str, Any] = {
        "cores": args.cores,
        "memory": args.memory,
        "tags": f"codex-lab;lease-{args.lease};windows",
        "onboot": 0,
    }
    api.call("PUT", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config", config)
    result: dict[str, Any] = {
        "vmid": args.vmid,
        "name": name,
        "version": args.version,
        "cloned_from": template_vmid,
        "answer_file_offered": bool(args.unattended),
    }
    password, volume = _prepare_unattended(
        lab, api, args, name, driver_branch, supplied_password
    )
    if password is not None:
        result["answer_iso"] = volume
        result["administrator_password"] = password
        result["password_note"] = "shown once here and never written to the audit ledger"
        result["answer_file_note"] = (
            "the language page is not covered by the answer file and is "
            "dismissed with Enter after Setup's UI loads; everything after it "
            "runs unattended"
        )
    if args.start:
        result.update(_boot_with_taps(lab, api, args))
    lab.audit(
        "windows-install-started",
        lease=args.lease,
        vmid=args.vmid,
        version=args.version,
        template=template_vmid,
        unattended=bool(args.unattended),
        sync=False,
    )
    result["next"] = (
        [
            "poll: proxmox-lab windows wait-agent "
            f"--lease {args.lease} --vmid {args.vmid}",
            "then: proxmox-lab windows finish "
            f"--lease {args.lease} --vmid {args.vmid}",
        ]
        if args.unattended
        else [
            f"watch: proxmox-lab console screenshot --vmid {args.vmid}",
            "press a key at the boot prompt: proxmox-lab console keys "
            f"--lease {args.lease} --vmid {args.vmid} enter",
            "drive Setup with console click / console type, then run "
            "'windows finish' once the desktop is up",
        ]
    )
    print(json.dumps(result, indent=2, sort_keys=True))


def _upload_iso(lab: Any, api: Any, lease_id: str, path: Path) -> str:
    """Upload a locally generated ISO to `local` and return its volume ID."""
    import argparse

    namespace = argparse.Namespace(
        lease=lease_id,
        storage="local",
        content="iso",
        file=str(path),
        timeout=600,
        task_timeout=600,
    )
    lab.cmd_upload(namespace)
    return f"local:iso/{path.name}"


STALL_WRITE_BYTES = 50 * 1024 * 1024


def cmd_wait_agent(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    deadline = time.monotonic() + args.timeout
    # An installing Windows writes gigabytes; one parked on a Setup prompt
    # writes essentially nothing. Waiting the full timeout to discover that
    # is a waste, so call it early and say what is actually wrong.
    # 0 disables the check; without this guard it would fire on the first pass.
    stall_at = time.monotonic() + args.stall_after if args.stall_after > 0 else 0.0
    baseline = _disk_written(lab, api, args.vmid)
    while time.monotonic() < deadline:
        if console.agent_ready(lab, api, args.vmid):
            print(json.dumps({"vmid": args.vmid, "agent_ready": True}))
            return
        if stall_at and time.monotonic() > stall_at:
            written = _disk_written(lab, api, args.vmid) - baseline
            if written < STALL_WRITE_BYTES:
                print(
                    f"warning: {args.stall_after}s in, the guest has written "
                    f"only {written // (1024 * 1024)} MB. It may be waiting "
                    "for input -- Setup's language page is the usual culprit "
                    "and no answer file dismisses it. Look with 'console "
                    f"screenshot --vmid {args.vmid}', and send Enter with "
                    f"'console keys --vmid {args.vmid} enter' if it is "
                    "sitting on a page. Still waiting.",
                    file=sys.stderr,
                )
            stall_at = 0.0  # warn once, then leave it alone
        time.sleep(args.interval)
    raise lab.LabError(
        f"qemu-guest-agent did not respond within {args.timeout}s; take a "
        f"screenshot (console screenshot --vmid {args.vmid}) to see where "
        "Setup actually is"
    )


def _disk_written(lab: Any, api: Any, vmid: int) -> int:
    """Proxmox's cumulative write counter. Advisory only.

    It has been observed reading 0 for a whole session on a writing qcow2
    guest over directory-backed storage, so both callers here are deliberately
    biased the safe way: a stalled counter makes `_kick_installer` keep tapping
    (harmless) and makes `wait-agent` print a warning while it carries on
    waiting. Neither may ever be turned into a hard stop on this number. To
    settle whether a guest is really writing, use 'guest disk-activity
    --ground-truth', which cross-checks it against QEMU's own block counters
    and the allocated size of the image file on the host.
    """
    try:
        status = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/status/current")
        return int(status.get("diskwrite") or 0)
    except Exception:
        return 0


def cmd_finish(lab: Any, args: Any) -> None:
    """Post-install setup over the guest agent: RDP, OpenSSH, address."""
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    if not console.agent_ready(lab, api, args.vmid):
        raise lab.LabError(
            "qemu-guest-agent is not responding; run 'windows wait-agent' first"
        )
    steps: dict[str, Any] = {}
    powershell = [
        (
            "enable_rdp",
            "Set-ItemProperty -Path 'HKLM:\\System\\CurrentControlSet\\Control\\"
            "Terminal Server' -Name fDenyTSConnections -Value 0; "
            "Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'",
        ),
    ]
    if args.openssh:
        powershell.append(
            (
                "install_openssh",
                "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; "
                "Start-Service sshd; Set-Service -Name sshd -StartupType Automatic; "
                "New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' "
                "-Enabled True -Direction Inbound -Protocol TCP -Action Allow "
                "-LocalPort 22 -ErrorAction SilentlyContinue",
            )
        )
    for label, script in powershell:
        steps[label] = console.agent_exec(
            lab,
            api,
            args.vmid,
            ["powershell.exe", "-NoProfile", "-Command", script],
            timeout=args.timeout,
        )
    addresses: list[str] = []
    try:
        interfaces = api.call(
            "GET", f"/nodes/{lab.NODE}/qemu/{args.vmid}/agent/network-get-interfaces"
        )
        for interface in (interfaces or {}).get("result", []):
            for address in interface.get("ip-addresses", []) or []:
                value = address.get("ip-address", "")
                if value and not value.startswith(("127.", "::1", "fe80")):
                    addresses.append(value)
    except lab.LabError:
        pass
    shredded = _shred_answer_iso(lab, api, args.vmid)
    lab.audit("windows-finished", lease=args.lease, vmid=args.vmid,
              addresses=addresses, answer_iso_removed=shredded, sync=False)
    print(json.dumps(
        {"vmid": args.vmid, "addresses": addresses, "steps": steps,
         "answer_iso_removed": shredded},
        indent=2,
        sort_keys=True,
    ))


def _shred_answer_iso(lab: Any, api: Any, vmid: int) -> str | None:
    """Detach and delete this guest's answer ISO.

    autounattend.xml carries the Administrator password in plain text --
    `<PlainText>true</PlainText>` is required for Setup to read it -- so the
    ISO is a credential sitting on shared storage. Setup has consumed it by
    the time the guest agent answers, so it should not outlive the install.

    Best-effort: a guest that is otherwise set up must not be reported as
    failed because the cleanup could not run.
    """
    try:
        config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/config")
    except lab.LabError:
        return None
    wanted = f"autounattend-{vmid}.iso"
    for key, value in list(config.items()):
        if not isinstance(value, str) or wanted not in value:
            continue
        volume = value.split(",", 1)[0]
        try:
            api.call("PUT", f"/nodes/{lab.NODE}/qemu/{vmid}/config",
                     {"delete": key})
            api.call("DELETE",
                     f"/nodes/{lab.NODE}/storage/local/content/{volume}")
            return volume
        except lab.LabError:
            return None
    return None


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind


    windows = sub.add_parser("windows", help="install and set up Windows guests")
    windows_sub = windows.add_subparsers(dest="windows_command", required=True)

    install = windows_sub.add_parser("install", help="clone and boot a Windows guest")
    install.add_argument("--lease", required=True)
    install.add_argument("--vmid", type=int, required=True)
    install.add_argument("--version", default="2025",
                         choices=WINDOWS_VERSIONS)
    install.add_argument(
        "--template-vmid", type=int,
        help="retained installer template to clone; overrides [windows] config",
    )
    install.add_argument("--name")
    install.add_argument("--cores", type=int, default=4)
    install.add_argument("--memory", type=int, default=8192)
    install.add_argument("--full-clone", action="store_true")
    install.add_argument("--storage",
                         help="full-clone target, e.g. usb-bulk for an "
                              "80 GiB Windows disk that would crowd local-lvm")
    install.add_argument("--policy", choices=("delete", "retain"), default="delete")
    install.add_argument("--no-start", dest="start", action="store_false")
    install.add_argument("--no-boot-key", dest="boot_key", action="store_false",
                         help="do not auto-tap Enter at the UEFI "
                              "boot-from-CD prompt (unattended installs)")
    install.add_argument("--clone-timeout", type=int, default=1800)
    install.add_argument("--unattended", action="store_true",
                         help="generate and attach an autounattend answer ISO")
    install.add_argument("--password-stdin", action="store_true",
                         help="read the Administrator password from stdin; it "
                              "must not be empty, since this creates the "
                              "account rather than logging into one")
    install.add_argument("--image-index", type=int, default=DEFAULT_IMAGE_INDEX)
    install.add_argument(
        "--driver-branch",
        help="virtio driver directory; defaults from --version (2k25 or 2k22)",
    )
    install.add_argument("--locale", default="en-GB")
    install.add_argument("--timezone", default="GMT Standard Time")
    install.add_argument("--hostname")
    install.add_argument("--owner", default="proxmox-agent-lab")
    install.set_defaults(func=_bind(lab, cmd_install))

    wait = windows_sub.add_parser("wait-agent", help="wait for qemu-guest-agent")
    wait.add_argument("--lease")
    wait.add_argument("--vmid", type=int, required=True)
    wait.add_argument("--timeout", type=int, default=3600)
    wait.add_argument("--interval", type=int, default=15)
    wait.add_argument("--stall-after", type=int, default=300,
                      help="give up early if the guest has written almost "
                           "nothing to disk by now, which means Setup is "
                           "waiting for input; 0 disables the check")
    wait.set_defaults(func=_bind(lab, cmd_wait_agent))

    finish = windows_sub.add_parser("finish", help="enable RDP/SSH and report IPs")
    finish.add_argument("--lease", required=True)
    finish.add_argument("--vmid", type=int, required=True)
    finish.add_argument("--no-openssh", dest="openssh", action="store_false")
    finish.add_argument("--timeout", type=int, default=900)
    finish.set_defaults(func=_bind(lab, cmd_finish))
