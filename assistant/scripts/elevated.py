"""elevated.py - the agent's administrator helper: a short allow-list of things Windows only lets an administrator do.

Why it exists (2026-11-04): the owner wants Chrome Remote Desktop ("Control PC") set up and its PIN set from Google
Chat. Windows keeps the Remote Desktop host's settings in a file only an administrator may write, and asks for that
approval on its own secure prompt, which no program can press. So the approval is asked for ONCE, for this helper:
a scheduled task that runs with the highest privileges, on demand only, and does nothing but the actions listed in
ALLOWED. After that one approval the agent can do those things by itself, from a Chat card, for good. A new
organization gets the helper during the PC install (the installer is the moment an owner is at the computer), so
they never meet the prompt later.

  ALLOWED    crd_status    is the Remote Desktop host installed / registered / does it have a PIN, and whose it is
             crd_pin       set the host's PIN (6-12 digits): the hash Chrome itself stores, then the host restarts
             crd_register  register this PC as a host with a one-time code from remotedesktop.google.com/headless
             crd_install   install Google's own host installer (downloaded from dl.google.com, signature checked)
             card_reset    a memory card in a USB reader that Windows left offline, hidden or "write protected" after
                           a card swap (2026-11-07): power-cycle the reader's USB device, bring the card's volume
                           online with a drive letter, and optionally copy ONE file from the organization's folder
                           to the card's root and hash-check it. USB disks only; never formats, deletes or partitions.

How a request travels: the agent (not elevated) writes <data>/elevated/request.json with a nonce, starts the task
(`schtasks /run`, which a user may do for their own task), the task runs `elevated.py serve` elevated, which reads the
request, refuses anything not in ALLOWED or with arguments of the wrong shape, acts, writes result.json and deletes
the request. A PIN lives in that file for the second it takes and is never logged.

  python assistant/scripts/elevated.py status          # is the helper installed
  python assistant/scripts/elevated.py install         # register it (Windows asks for the one approval now)
  python assistant/scripts/elevated.py serve           # (the task) carry out the waiting request
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
sys.path.insert(0, str(HERE))
from alib import DATA, load_json, save_json  # noqa: E402

DIR = Path(os.environ.get("ELEVATED_DIR") or (DATA / "elevated"))
REQ, RES = DIR / "request.json", DIR / "result.json"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CRD_DIR = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google" / "Chrome Remote Desktop"
CRD_DATA = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Google" / "Chrome Remote Desktop"
CRD_MSI = "https://dl.google.com/edgedl/chrome-remote-desktop/chromeremotedesktophost.msi"
CRD_REDIRECT = "https://remotedesktop.google.com/_/oauthredirect"
ALLOWED = ("crd_status", "crd_pin", "crd_register", "crd_install", "card_reset")
_PIN = re.compile(r"\d{6,12}")
_CODE = re.compile(r"[\w/.\-]{10,400}")
_NAME = re.compile(r"[\w .\-]{1,40}")


def _agent_name() -> str:
    try:
        return str((load_json(ROOT / "agent" / "profile.json", {}) or {}).get("name") or "Agent")
    except Exception:  # noqa: BLE001
        return "Agent"


def task_name() -> str:
    return f"{_agent_name()} Elevated Helper (assistant)"


def _ps(script: str, timeout: int = 60) -> tuple:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {str(e)[:120]}"


def installed() -> bool:
    """The helper task exists and runs with the highest privileges. Replaceable in tests."""
    name = task_name().replace("'", "''")
    rc, out = _ps(f"$t = Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue; if ($t) {{ $t.Principal.RunLevel }}", 30)
    return rc == 0 and "Highest" in out


def _task_xml() -> str:
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else py)
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "") if os.environ.get("USERDOMAIN") else os.environ.get("USERNAME", "")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>The agent's administrator helper: only the actions listed in assistant/scripts/elevated.py (ALLOWED), on demand.</Description></RegistrationInfo>
  <Triggers />
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>false</StartWhenAvailable><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit><Priority>7</Priority></Settings>
  <Actions Context="Author"><Exec><Command>{exe}</Command><Arguments>"{HERE / 'elevated.py'}" serve</Arguments><WorkingDirectory>{VAULT}</WorkingDirectory></Exec></Actions>
</Task>
"""


def install(wait_s: int = 150) -> dict:
    """Register the helper. Windows asks for the one administrator approval on its own prompt (up to `wait_s`).
    -> {"ok", "message"}."""
    if os.name != "nt":
        return {"ok": False, "message": "the helper is only for Windows"}
    if installed():
        return {"ok": True, "message": "already installed"}
    DIR.mkdir(parents=True, exist_ok=True)
    xml = DIR / "helper_task.xml"
    xml.write_text(_task_xml(), encoding="utf-16")
    name = task_name().replace("'", "''")
    script = (f"Start-Process -FilePath schtasks.exe -ArgumentList '/create','/tn','\"{name}\"','/xml','\"{xml}\"','/f' "
              f"-Verb RunAs -WindowStyle Hidden -Wait")
    rc, out = _ps(script, wait_s)
    if installed():
        return {"ok": True, "message": "installed"}
    return {"ok": False, "message": "Windows did not get the approval (the prompt was not answered, or it was declined)" + (f": {out[-120:]}" if out and rc else "")}


def request(action: str, args: dict | None = None, timeout: int = 180) -> dict:
    """Ask the helper to do one allowed thing. -> the helper's result ({"ok", ...}); {"ok": False, "state": "no_helper"}
    when it is not installed. Replaceable in tests."""
    if action not in ALLOWED:
        return {"ok": False, "message": f"{action} is not something the helper does"}
    if not installed():
        return {"ok": False, "state": "no_helper", "message": "the administrator helper is not installed yet"}
    DIR.mkdir(parents=True, exist_ok=True)
    nonce = base64.urlsafe_b64encode(os.urandom(12)).decode()
    try:
        RES.unlink()
    except OSError:
        pass
    REQ.write_text(json.dumps({"nonce": nonce, "action": action, "args": args or {}, "ts": time.time()}), encoding="utf-8")
    name = task_name()
    r = subprocess.run(["schtasks", "/run", "/tn", name], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if r.returncode != 0:
        _forget_request()
        return {"ok": False, "message": f"the helper did not start ({(r.stdout or r.stderr or '').strip()[-120:]})"}
    end = time.time() + timeout
    while time.time() < end:
        d = load_json(RES, {}) or {}
        if d.get("nonce") == nonce:
            try:
                RES.unlink()
            except OSError:
                pass
            d.pop("nonce", None)
            return d
        time.sleep(1)
    _forget_request()
    return {"ok": False, "message": "the helper did not answer in time"}


def _forget_request() -> None:
    try:
        REQ.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ the elevated side
def _host_json() -> dict:
    return json.loads((CRD_DATA / "host.json").read_text(encoding="utf-8"))


def pin_hash(host_id: str, pin: str) -> str:
    """What Chrome Remote Desktop stores for a PIN (remoting/host/pin_hash.cc): "hmac:" + base64(HMAC-SHA256(key=host id, pin))."""
    return "hmac:" + base64.b64encode(hmac.new(host_id.encode("utf-8"), pin.encode("utf-8"), hashlib.sha256).digest()).decode("ascii")


def _crd_status() -> dict:
    exe = next((p for p in CRD_DIR.glob("*/remoting_host.exe")), None)
    out = {"ok": True, "installed": bool(exe), "registered": False, "has_pin": False, "owner": "", "host_name": ""}
    try:
        d = _host_json()
        out.update({"registered": bool(d.get("host_id")), "has_pin": bool(d.get("host_secret_hash")),
                    "owner": str(d.get("host_owner") or d.get("host_owner_email") or ""), "host_name": str(d.get("host_name") or "")})
    except (OSError, ValueError):
        pass
    return out


def _restart_host() -> str:
    for cmd in (["net", "stop", "chromoting"], ["net", "start", "chromoting"]):
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        if cmd[1] == "start" and r.returncode != 0:
            return (r.stdout or r.stderr or "").strip()[-160:]
    return ""


def _crd_pin(args: dict) -> dict:
    pin = str(args.get("pin") or "")
    if not _PIN.fullmatch(pin):
        return {"ok": False, "message": "a PIN is 6 to 12 digits"}
    f = CRD_DATA / "host.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "state": "not_registered", "message": f"this PC is not registered with Chrome Remote Desktop yet ({type(e).__name__})"}
    if not d.get("host_id"):
        return {"ok": False, "state": "not_registered", "message": "this PC is not registered with Chrome Remote Desktop yet"}
    backup = f.with_suffix(".json.bak")
    try:
        backup.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass
    d["host_secret_hash"] = pin_hash(str(d["host_id"]), pin)
    f.write_text(json.dumps(d, indent=1), encoding="utf-8")
    err = _restart_host()
    st = _crd_status()
    return {"ok": not err, "message": "PIN set" if not err else f"PIN written, but the host did not restart: {err}", "owner": st["owner"], "host_name": st["host_name"]}


def _crd_register(args: dict) -> dict:
    code, pin = str(args.get("code") or ""), str(args.get("pin") or "")
    name = str(args.get("name") or os.environ.get("COMPUTERNAME") or "PC")
    if not (_CODE.fullmatch(code) and _PIN.fullmatch(pin) and _NAME.fullmatch(name)):
        return {"ok": False, "message": "the code, the PIN or the name is not in the expected shape"}
    exe = next((p for p in CRD_DIR.glob("*/remoting_start_host.exe")), None)
    if not exe:
        return {"ok": False, "state": "not_installed", "message": "the Remote Desktop host is not installed"}
    r = subprocess.run([str(exe), f"--code={code}", f"--redirect-url={CRD_REDIRECT}", f"--name={name}", f"--pin={pin}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, creationflags=NO_WINDOW)
    st = _crd_status()
    ok = r.returncode == 0 and st["registered"]
    return {"ok": ok, "message": "registered" if ok else f"registration did not go through ({(r.stdout or r.stderr or '').strip()[-160:]})", "owner": st["owner"], "host_name": st["host_name"]}


def _crd_install(args: dict) -> dict:
    import urllib.request
    DIR.mkdir(parents=True, exist_ok=True)
    msi = DIR / "chromeremotedesktophost.msi"
    try:
        with urllib.request.urlopen(CRD_MSI, timeout=120) as r:      # Google's own installer, from Google's own host
            msi.write_bytes(r.read())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"could not download Google's installer ({type(e).__name__})"}
    rc, out = _ps(f"$s = Get-AuthenticodeSignature -FilePath '{msi}'; $s.Status.ToString() + '|' + $s.SignerCertificate.Subject", 60)
    if not out.startswith("Valid|") or "Google LLC" not in out:
        try:
            msi.unlink()
        except OSError:
            pass
        return {"ok": False, "message": "the downloaded installer is not signed by Google; nothing was installed"}
    r = subprocess.run(["msiexec", "/i", str(msi), "/qn", "/norestart"], capture_output=True, text=True, timeout=600, creationflags=NO_WINDOW)
    try:
        msi.unlink()
    except OSError:
        pass
    st = _crd_status()
    return {"ok": st["installed"], "message": "installed" if st["installed"] else f"the installer ended with code {r.returncode}"}


# PowerShell for card_reset: $env:CARD_SRC = the file to copy (may be empty). Prints one JSON object on its last line.
_CARD_PS = r"""
$ErrorActionPreference = 'Continue'
$src = $env:CARD_SRC
$steps = @()
function CardLetter { $d = Get-Disk | Where-Object BusType -eq 'USB'; if (-not $d) { return $null }
  $p = $d | Get-Partition -ErrorAction SilentlyContinue | Where-Object DriveLetter; if ($p) { return ($p | Select-Object -First 1).DriveLetter }; return $null }
function TryWrite($l) { if (-not $src) { return $true }
  try { $dst = "${l}:\" + (Split-Path $src -Leaf); Copy-Item $src $dst -Force -ErrorAction Stop
        return ((Get-FileHash $src).Hash -eq (Get-FileHash $dst).Hash) } catch { $script:steps += "write failed on ${l}: $($_.Exception.Message)"; return $false } }
function Done($l) { $v = Get-Volume -DriveLetter $l -ErrorAction SilentlyContinue
  @{ ok = $true; letter = "$l"; label = "$($v.FileSystemLabel)"; copied = [bool]$src; steps = $steps } | ConvertTo-Json -Compress; exit 0 }
$l = CardLetter; if ($l -and (TryWrite $l)) { Done $l }
$usb = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -like 'USB\VID_*' -and $_.FriendlyName -match 'Mass Storage|UAS' } | Select-Object -First 1
if ($usb) { $steps += "reader cycled"; pnputil /disable-device "$($usb.InstanceId)" | Out-Null; Start-Sleep 4; pnputil /enable-device "$($usb.InstanceId)" | Out-Null; Start-Sleep 10
  $l = CardLetter; if ($l -and (TryWrite $l)) { Done $l } } else { $steps += "no USB reader found" }
$d = Get-Disk | Where-Object BusType -eq 'USB' | Select-Object -First 1
if ($d) {
  try { Set-Disk -Number $d.Number -IsReadOnly $false -ErrorAction Stop } catch {}
  try { Set-Disk -Number $d.Number -IsOffline $false -ErrorAction Stop } catch {}
  try { Update-Disk -Number $d.Number -ErrorAction Stop; $steps += "disk rescanned" } catch {}
  Start-Sleep 3
  $p = Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($p) {
    try { Set-Partition -DiskNumber $d.Number -PartitionNumber $p.PartitionNumber -IsOffline $false -ErrorAction Stop; $steps += "partition online" } catch { $steps += "partition online: $($_.Exception.Message)" }
    if (-not $p.DriveLetter) { try { Add-PartitionAccessPath -DiskNumber $d.Number -PartitionNumber $p.PartitionNumber -AssignDriveLetter -ErrorAction Stop; $steps += "letter assigned" } catch { $steps += "letter: $($_.Exception.Message)" } }
    $dp = Join-Path $env:TEMP 'card_reset_dp.txt'
    "rescan`r`nselect disk $($d.Number)`r`nselect partition $($p.PartitionNumber)`r`nonline volume`r`nassign" | Set-Content $dp -Encoding ascii
    $out = diskpart /s $dp 2>&1; Remove-Item $dp -ErrorAction SilentlyContinue
    $steps += "diskpart: " + (($out | Where-Object { $_ -match 'could not|error|assigned|online' }) -join ' | ') }
  Start-Sleep 3; $l = CardLetter; if ($l -and (TryWrite $l)) { Done $l }
  $scsi = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.Class -eq 'DiskDrive' -and $_.InstanceId -like 'SCSI\*' -and $_.FriendlyName -notmatch 'NVMe|SSD|ST\d' } | Select-Object -First 1
  if ($scsi) { pnputil /restart-device "$($scsi.InstanceId)" | Out-Null; Start-Sleep 8; $steps += "disk device restarted"; $l = CardLetter; if ($l -and (TryWrite $l)) { Done $l } }
}
$d = Get-Disk | Where-Object BusType -eq 'USB' | Select-Object -First 1
$p = if ($d) { Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | Select-Object -First 1 }
@{ ok = $false; letter = "$($p.DriveLetter)"; offline = [bool]$p.IsOffline; present = [bool]$d; steps = $steps } | ConvertTo-Json -Compress
"""


def _card_reset(args: dict) -> dict:
    """Bring a memory card in a USB reader back after Windows left it offline / write protected; optionally copy one file
    from inside the organization's folder to the card's root. -> {"ok", "letter", "label", "copied", "steps", "message"}."""
    src = str(args.get("src") or "")
    if src:
        p = Path(src)
        try:
            ok = p.is_file() and p.resolve().is_relative_to(VAULT.resolve()) and p.stat().st_size <= 50 * 1024 * 1024
        except (OSError, ValueError):
            ok = False
        if not ok:
            return {"ok": False, "message": "the file to copy must be a file inside the organization's folder (50 MB at most)"}
        src = str(p.resolve())
    env = dict(os.environ, CARD_SRC=src)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _CARD_PS], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=240, creationflags=NO_WINDOW, env=env)
        out = (r.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"{type(e).__name__}: {str(e)[:160]}"}
    last = out.splitlines()[-1] if out else ""
    try:
        res = json.loads(last)
    except ValueError:
        return {"ok": False, "message": f"the card step gave no answer ({(out or r.stderr or '').strip()[-160:]})"}
    res["message"] = ("card online" + (" and file copied" if res.get("copied") else "")) if res.get("ok") else "the card is still not usable"
    return res


def serve() -> int:
    """The task's side: carry out the one waiting request, if it is allowed and well-formed."""
    d = load_json(REQ, {}) or {}
    _forget_request()                                  # whatever happens, a PIN does not stay on disk
    action, nonce = str(d.get("action") or ""), str(d.get("nonce") or "")
    if not nonce or time.time() - float(d.get("ts") or 0) > 300:
        return 1
    try:
        if action not in ALLOWED:
            res = {"ok": False, "message": "not an allowed action"}
        else:
            res = {"crd_status": lambda a: _crd_status(), "crd_pin": _crd_pin, "crd_register": _crd_register, "crd_install": _crd_install,
                   "card_reset": _card_reset}[action](d.get("args") or {})
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "message": f"{type(e).__name__}: {str(e)[:160]}"}
    res["nonce"] = nonce
    DIR.mkdir(parents=True, exist_ok=True)
    save_json(RES, res)
    return 0


def main() -> int:
    a = sys.argv[1:]
    if a[:1] == ["serve"]:
        return serve()
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    if a[:1] == ["install"]:
        r = install()
        print(("INSTALLED " if r["ok"] else "NOT_INSTALLED ") + r["message"])
        return 0 if r["ok"] else 1
    print(json.dumps({"helper": task_name(), "installed": installed(), "allowed": list(ALLOWED)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
