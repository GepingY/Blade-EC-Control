"""One slow, read-only 256-byte ACPI EC snapshot through official PawnIO.

Default/--check: offline file/API/protocol self-check; never loads a DLL, opens a
driver, acquires a hardware mutex, or accesses EC ports. --snapshot is explicit.
This script NEVER installs/starts a driver, changes EC banks, or sends EC_WRITE.

Semantic EC reads require two port writes: EC_READ (0x80) to command port 0x66,
then the register address to data port 0x62. These are read-transaction framing,
not EC RAM writes. No arbitrary port-write operation is exposed by this script.
The module itself supports writes; its wider interface is not used here.

The Global\\Access_EC mutex coordinates participating monitoring tools, not
necessarily firmware or the Windows EC driver. Unexpected pending output aborts
the snapshot rather than consuming it. No retries or output-buffer draining.
"""

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = (ROOT / "tools" / "pawnio") if (ROOT / "tools" / "pawnio").is_dir() else (ROOT / "diagnostics" / "tools" / "pawnio")
MODULE_SHA256 = "c38fd116e7aff4d1fdb0a494e296be0a6708e5a22fc72f14587442fb7f8f7906"
ARCHIVE_SHA256 = "09698897ce19b92f1043a2e322cfcf0b1fe823e0b576971b178a65244fb0af9d"
INSTALLER_SHA256 = "1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032"
DATA_PORT, STATUS_PORT, READ_COMMAND = 0x62, 0x66, 0x80
OUTPUT_FULL, INPUT_FULL = 0x01, 0x02
READ_DELAY = 0.020
POLL_DELAY = 0.001
TRANSACTION_TIMEOUT = 0.100
SNAPSHOT_TIMEOUT = 15.0


class SnapshotError(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hresult(result, operation):
    # HRESULT is a signed 32-bit LONG even in a 64-bit process.
    if result < 0:
        raise SnapshotError(f"{operation} failed: HRESULT 0x{result & 0xffffffff:08X}")


def installed_paths():
    if os.name != "nt":
        return {"installed": False, "reason": "Windows is required"}
    import winreg
    result = {"installed": False}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            location = winreg.QueryValueEx(key, "InstallLocation")[0]
            result["library_path"] = str(Path(location) / "PawnIOLib.dll")
            try:
                result["display_version"] = winreg.QueryValueEx(key, "DisplayVersion")[0]
            except OSError:
                pass
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\PawnIO", 0, winreg.KEY_READ) as key:
            image = os.path.expandvars(winreg.QueryValueEx(key, "ImagePath")[0]).strip('"')
            service_type = winreg.QueryValueEx(key, "Type")[0]
        if image.startswith("\\??\\"):
            image = image[4:]
        if image.lower().startswith("\\systemroot\\"):
            image = os.environ["SystemRoot"] + image[len("\\SystemRoot"):]
        if not Path(image).is_absolute():
            image = str(Path(os.environ["SystemRoot"]) / image)
        result.update(driver_path=image, service_type=service_type)
        if service_type != 1 or Path(image).name.lower() != "pawnio.sys":
            raise SnapshotError("Expected the official PawnIO kernel driver service/image")
        if not Path(result["library_path"]).is_file() or not Path(image).is_file():
            raise SnapshotError("PawnIO registry entries exist but installed DLL/driver files are missing")
        result["installed"] = True
    except (OSError, SnapshotError) as exc:
        result["reason"] = f"Official PawnIO is not installed/usable: {exc}"
    return result


def offline_check():
    """No DLL loading or driver calls; only local file and registry reads."""
    checks = []
    for name, expected in (("LpcACPIEC.bin", MODULE_SHA256),
                           ("release_0_2_12.zip", ARCHIVE_SHA256),
                           ("PawnIO_setup.exe", INSTALLER_SHA256)):
        path = PACKAGE / name
        digest = sha256(path) if path.is_file() else None
        checks.append({"file": str(path), "sha256": digest, "matches_pinned_release": digest == expected})
    header_path = PACKAGE / "PawnIOLib.h"
    module_src_path = PACKAGE / "LpcACPIEC.p"
    prov_path = PACKAGE / "source-provenance.json"
    header = header_path.read_text(encoding="utf-8") if header_path.is_file() else ""
    module_source = module_src_path.read_text(encoding="utf-8") if module_src_path.is_file() else ""
    expected_functions = ("pawnio_open", "pawnio_load", "pawnio_execute", "pawnio_close")
    api_valid = all(f"PAWNIOAPI {name}(" in header for name in expected_functions)
    module_valid = all(s in module_source for s in (
        "DEFINE_IOCTL_SIZED(ioctl_pio_read, 1, 1)",
        "DEFINE_IOCTL_SIZED(ioctl_pio_write, 2, 0)",
        "port == 0x62 || port == 0x66", "\\BaseNamedObjects\\Access_EC"))
    if prov_path.is_file():
        provenance = json.loads(prov_path.read_text(encoding="utf-8-sig"))
        source_checks = [{"file": item["Name"], "source_url": item["Url"],
                          "matches_recorded_sha256": (PACKAGE / item["Name"]).is_file() and sha256(PACKAGE / item["Name"]) == item["Sha256"].lower()}
                         for item in provenance["Sources"]]
    else:
        source_checks = []
    tested_reads = protocol_selftest()
    return {
        "mode": "offline_check", "driver_opened": False, "hardware_accessed": False,
        "package_files": checks, "c_api_declarations_valid": api_valid,
        "source_files": source_checks,
        "module_interface_valid": module_valid, "simulated_register_reads": tested_reads,
        "passed": (all(c["matches_pinned_release"] for c in checks) and api_valid and module_valid
                   and all(c["matches_recorded_sha256"] for c in source_checks)),
        "installed": installed_paths(),
        "transaction": {"command_port": "0x66", "command": "0x80 READ only",
                        "address_port": "0x62", "data_read_port": "0x62",
                        "registers": "0x00..0xFF once", "inter_read_delay_ms": 20,
                        "no_ec_ram_writes": True, "no_bank_changes": True},
    }


class ReadProtocol:
    """Only whole EC_READ transactions; transport framing is not user supplied."""
    def __init__(self, read_port, send_read_command, send_read_address, sleeper=time.sleep):
        self._read_port = read_port
        self._send_command = send_read_command
        self._send_address = send_read_address
        self._sleep = sleeper
        self.trace = None

    def _phase(self, name):
        self.trace["phase"] = name

    def _record_status(self, status):
        # Record only the status reads already required by the protocol. No
        # extra port/data reads are added for diagnostics. Bound output size.
        self.trace["last_status"] = status
        self.trace["last_status_hex"] = f"0x{status:02X}"
        self.trace["status_read_count"] += 1
        sample = {"elapsed_ms": round((time.monotonic() - self._started) * 1000, 3),
                  "phase": self.trace["phase"], "status": status,
                  "status_hex": f"0x{status:02X}",
                  "obf": bool(status & OUTPUT_FULL), "ibf": bool(status & INPUT_FULL)}
        self.trace["status_samples"].append(sample)
        if len(self.trace["status_samples"]) > 16:
            del self.trace["status_samples"][0]

    def _error(self, message):
        status = self.trace["last_status_hex"]
        error = SnapshotError(f"{message}; wait phase={self.trace['phase']}, last status={status}")
        error.ec_trace = self.trace
        return error

    def _wait(self, want_output, deadline):
        while time.monotonic() < deadline:
            status = self._read_port(STATUS_PORT)
            self._record_status(status)
            if not 0 <= status <= 255:
                raise self._error("Invalid EC status byte")
            if want_output:
                if status & OUTPUT_FULL:
                    return
            else:
                if status & OUTPUT_FULL:
                    raise self._error("EC has unexpected pending output; stopped without draining it")
                if not status & INPUT_FULL:
                    return
            self._sleep(POLL_DELAY)
        raise self._error("EC read transaction timed out; no retry attempted")

    def read_register(self, address):
        if not isinstance(address, int) or not 0 <= address <= 255:
            raise SnapshotError("Register address must be a byte")
        self._started = time.monotonic()
        self.trace = {"register": address, "register_hex": f"0x{address:02X}",
                      "phase": "before_read_command_wait_input_clear",
                      "read_command_sent": False, "read_address_sent": False,
                      "data_port_read": False, "last_status": None,
                      "last_status_hex": None, "status_read_count": 0,
                      "status_samples": [], "samples_limit": 16}
        deadline = self._started + TRANSACTION_TIMEOUT
        try:
            self._wait(False, deadline)
            self._phase("send_read_command_0x80")
            self._send_command()  # hard-coded port0x66,value0x80
            self.trace["read_command_sent"] = True
            self._phase("after_read_command_wait_input_clear")
            self._wait(False, deadline)
            self._phase("send_read_address")
            self._send_address(address)  # one address byte, never a data payload
            self.trace["read_address_sent"] = True
            self._phase("after_read_address_wait_output_full")
            self._wait(True, deadline)
            self._phase("read_data_port")
            value = self._read_port(DATA_PORT)
            self.trace["data_port_read"] = True
            if not 0 <= value <= 255:
                raise self._error("Invalid EC data byte")
            self._phase("complete")
            return value
        except BaseException as exc:
            self.trace["elapsed_ms"] = round((time.monotonic() - self._started) * 1000, 3)
            exc.ec_trace = self.trace
            raise


def protocol_selftest():
    """Exercise all256 addresses against a simulated EC, never native code."""
    for address in range(256):
        events = []
        states = iter((0, 0, OUTPUT_FULL))
        def fake_read(port):
            if port == STATUS_PORT:
                return next(states)
            if port == DATA_PORT:
                return address ^ 0x5A
            raise AssertionError("Unexpected port")
        protocol = ReadProtocol(fake_read,
                                lambda: events.append((STATUS_PORT, READ_COMMAND)),
                                lambda a: events.append((DATA_PORT, a)),
                                lambda _: None)
        assert protocol.read_register(address) == address ^ 0x5A
        assert events == [(0x66, 0x80), (0x62, address)]
    blocked = []
    protocol = ReadProtocol(lambda _: OUTPUT_FULL, lambda: blocked.append(1), lambda _: blocked.append(2))
    try:
        protocol.read_register(0)
        raise AssertionError("Pending output was not rejected")
    except SnapshotError:
        assert not blocked
    return 256


def verify_signature(path):
    # Environment passing avoids interpolating registry paths into PowerShell code.
    env = dict(os.environ, BLADE_PAWNIO_VERIFY=str(path))
    command = ("$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath $env:BLADE_PAWNIO_VERIFY; "
               "[pscustomobject]@{Status=$s.Status.ToString();Signer=$s.SignerCertificate.Subject;"
               "Thumbprint=$s.SignerCertificate.Thumbprint} | ConvertTo-Json -Compress")
    done = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" /
                               "v1.0" / "powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", command],
                          env=env, capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode:
        raise SnapshotError("Could not verify installed driver Authenticode signature")
    return json.loads(done.stdout.lstrip("\ufeff"))


def require_running_driver():
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    advapi.OpenSCManagerW.restype = wintypes.HANDLE
    advapi.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    advapi.OpenServiceW.restype = wintypes.HANDLE
    class ServiceStatus(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            "type", "state", "controls", "win32_exit", "service_exit", "checkpoint", "wait_hint")]
    advapi.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(ServiceStatus)]
    advapi.QueryServiceStatus.restype = wintypes.BOOL
    advapi.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    advapi.CloseServiceHandle.restype = wintypes.BOOL
    manager = advapi.OpenSCManagerW(None, None, 1)  # CONNECT only
    if not manager:
        raise ctypes.WinError(ctypes.get_last_error())
    service = None
    try:
        service = advapi.OpenServiceW(manager, "PawnIO", 4)  # QUERY_STATUS only
        if not service:
            raise ctypes.WinError(ctypes.get_last_error())
        status = ServiceStatus()
        if not advapi.QueryServiceStatus(service, ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        if status.state != 4:
            raise SnapshotError("PawnIO driver is not running; this collector never starts services")
    finally:
        if service:
            advapi.CloseServiceHandle(service)
        advapi.CloseServiceHandle(manager)


class EcMutex:
    def __enter__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.ReleaseMutex.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateMutexW(None, False, "Global\\Access_EC")
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        waited = self.kernel.WaitForSingleObject(self.handle, 3000)
        if waited != 0:
            if waited == 0x80:  # abandoned mutex: acquired, but abort conservatively
                self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            raise SnapshotError(f"Could not acquire a clean Access_EC mutex (wait0x{waited:X})")
        return self

    def __exit__(self, *_):
        try:
            if not self.kernel.ReleaseMutex(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(self.handle)


class PawnEcReader:
    def __init__(self, library_path):
        self.lib = ctypes.WinDLL(str(Path(library_path).resolve()))
        self.handle = wintypes.HANDLE()
        self.lib.pawnio_open.argtypes = [ctypes.POINTER(wintypes.HANDLE)]
        self.lib.pawnio_open.restype = ctypes.c_int32
        self.lib.pawnio_load.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t]
        self.lib.pawnio_load.restype = ctypes.c_int32
        self.lib.pawnio_execute.argtypes = [wintypes.HANDLE, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.lib.pawnio_execute.restype = ctypes.c_int32
        self.lib.pawnio_close.argtypes = [wintypes.HANDLE]
        self.lib.pawnio_close.restype = ctypes.c_int32

    def __enter__(self):
        hresult(self.lib.pawnio_open(ctypes.byref(self.handle)), "pawnio_open")
        try:
            data = (PACKAGE / "LpcACPIEC.bin").read_bytes()
            if hashlib.sha256(data).hexdigest() != MODULE_SHA256:
                raise SnapshotError("EC module differs from the pinned official release")
            blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            hresult(self.lib.pawnio_load(self.handle, blob, len(data)), "pawnio_load official LpcACPIEC")
        except BaseException:
            self.lib.pawnio_close(self.handle)
            raise
        return self

    def __exit__(self, *_):
        hresult(self.lib.pawnio_close(self.handle), "pawnio_close")

    def read_port(self, port):
        if port not in (DATA_PORT, STATUS_PORT):
            raise SnapshotError("Read outside standard ACPI EC ports rejected")
        inputs = (ctypes.c_uint64 * 1)(port)
        outputs = (ctypes.c_uint64 * 1)()
        count = ctypes.c_size_t()
        hresult(self.lib.pawnio_execute(self.handle, b"ioctl_pio_read", inputs, 1,
                                       outputs, 1, ctypes.byref(count)), "EC port read")
        if count.value != 1:
            raise SnapshotError("EC port read returned the wrong number of entries")
        return outputs[0]

    def send_read_command(self):
        # This is the ONLY command-port write anywhere in this collector.
        inputs = (ctypes.c_uint64 * 2)(STATUS_PORT, READ_COMMAND)
        count = ctypes.c_size_t()
        hresult(self.lib.pawnio_execute(self.handle, b"ioctl_pio_write", inputs, 2,
                                       None, 0, ctypes.byref(count)), "EC READ command framing")
        if count.value:
            raise SnapshotError("EC command unexpectedly returned output entries")

    def send_read_address(self, address):
        if not isinstance(address, int) or not 0 <= address <= 255:
            raise SnapshotError("EC read address must be a byte")
        inputs = (ctypes.c_uint64 * 2)(DATA_PORT, address)
        count = ctypes.c_size_t()
        hresult(self.lib.pawnio_execute(self.handle, b"ioctl_pio_write", inputs, 2,
                                       None, 0, ctypes.byref(count)), "EC READ address framing")
        if count.value:
            raise SnapshotError("EC address unexpectedly returned output entries")


def take_snapshot(check, output):
    if not check["passed"]:
        raise SnapshotError("Offline package/API/protocol checks failed; no driver opened")
    installed = check["installed"]
    if not installed.get("installed"):
        raise SnapshotError(installed.get("reason", "Install the official signed PawnIO first"))
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise SnapshotError("Use 64-bit Python with this64-bit laptop")
    if not ctypes.WinDLL("shell32").IsUserAnAdmin():
        raise SnapshotError("An elevated Windows administrator token is required; no driver opened")
    signature = verify_signature(installed["driver_path"])
    if signature.get("Status") != "Valid" or not any(
        publisher in signature.get("Signer", "") for publisher in (
            "Microsoft Windows Hardware Compatibility Publisher", "CN=namazso.eu")):
        raise SnapshotError(f"Installed PawnIO is not verified as officially signed: {signature}")
    require_running_driver()
    values = []
    started = time.monotonic()
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "complete": False,
              "module_sha256": MODULE_SHA256, "driver_signature": signature,
              "installed": installed, "scope": "standard EC bank0 registers0x00..0xFF once",
              "ec_command": "0x80 READ only; command/address framing port writes",
              "mutex": "\\BaseNamedObjects\\Access_EC (Global\\Access_EC)",
              "inter_read_delay_ms": 20, "values": values,
              "limits": ("Mutex coordinates participating tools; does not guarantee Windows/firmware exclusion. "
                         "Firmware-specific EC registers can have read side effects; a fullmap is not inherently risk-free.")}
    stage = "open_driver_and_load_module"
    protocol = None
    try:
        with PawnEcReader(installed["library_path"]) as reader:
            protocol = ReadProtocol(reader.read_port, reader.send_read_command, reader.send_read_address)
            stage = "acquire_mutex"
            with EcMutex():
                for address in range(256):
                    stage = f"read_register_0x{address:02X}"
                    if time.monotonic() - started > SNAPSHOT_TIMEOUT:
                        raise SnapshotError("Snapshot total time limit reached; no retry")
                    values.append(protocol.read_register(address))
                    if address != 255:
                        time.sleep(READ_DELAY)
                stage = "release_mutex"
            stage = "close_driver"
        report["complete"] = True
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["failure_stage"] = stage
        if getattr(exc, "ec_trace", None) is not None:
            report["ec_transaction_trace"] = exc.ec_trace
        elif protocol is not None and protocol.trace is not None:
            report["ec_transaction_trace"] = protocol.trace
        if stage.startswith("read_register_"):
            report["failed_address"] = address
        raise
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["registers_read"] = len(values)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
        if report["complete"]:
            with output.with_suffix(".bin").open("xb") as stream:
                stream.write(bytes(values))
    print(json.dumps({"snapshot_path": str(output.resolve()), "complete": report["complete"],
                      "registers_read": len(values)}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Offline check only (default); no hardware access")
    mode.add_argument("--snapshot", action="store_true", help="Explicitly perform exactly one256-byte EC READ snapshot")
    parser.add_argument("--output", type=Path, help="New output JSON path; existing files are never overwritten")
    args = parser.parse_args()
    try:
        check = offline_check()
        if not args.snapshot:
            print(json.dumps(check, indent=2))
            return 0 if check["passed"] else 2
        output = args.output or (ROOT / "diagnostics" / ("ec-snapshot-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"))
        if output.exists() or output.with_suffix(".bin").exists():
            raise SnapshotError("Output path exists; choose a new path before any hardware access")
        take_snapshot(check, output)
        return 0
    except (SnapshotError, OSError, ValueError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
