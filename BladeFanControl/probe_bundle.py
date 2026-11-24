"""Run the bundled EC probe and save its report as the controller user."""

import base64
import ctypes
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from secrets import token_hex
import socket
import subprocess
import tempfile
import time
import zipfile


PROBE_TIMEOUT_SECONDS = 150
MAX_REPORT_BYTES = 2 * 1024 * 1024

# Run only the hashed helper from an Administrators/SYSTEM-owned directory.
# The one-file GUI and its unpacked files are never launched with elevation.
_ELEVATED_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
$staging = $null
$status = 'error'
try {
    $security = [Security.AccessControl.DirectorySecurity]::new()
    $security.SetSecurityDescriptorSddlForm('O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)')
    $root = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
    if (-not $root) { throw 'Windows Program Files directory is unavailable.' }
    $privatePath = [IO.Path]::Combine($root, 'BladeFanControl-probe-' + [Guid]::NewGuid().ToString('N'))
    $staging = [IO.Directory]::CreateDirectory($privatePath, $security).FullName

    $copy = [IO.Path]::Combine($staging, 'probe.zip')
    [IO.File]::Copy($payloadPath, $copy)
    $actual = (Get-FileHash -LiteralPath $copy -Algorithm SHA256).Hash
    if ($actual -ne $expectedHash) { throw 'Bundled EC probe checksum mismatch.' }
    [void][Reflection.Assembly]::Load('System.IO.Compression.FileSystem, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089')
    [IO.Compression.ZipFile]::ExtractToDirectory($copy, $staging)

    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = [IO.Path]::Combine($staging, 'BladeEcProbe.exe')
    $start.Arguments = 'verify-live'
    $start.WorkingDirectory = $staging
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [Text.Encoding]::UTF8
    $start.StandardErrorEncoding = [Text.Encoding]::UTF8
    $windows = [IO.Directory]::GetParent([Environment]::SystemDirectory).FullName
    $start.EnvironmentVariables['SystemRoot'] = $windows
    $start.EnvironmentVariables['PATH'] = [Environment]::SystemDirectory + ';' + $windows
    $start.EnvironmentVariables['TEMP'] = $staging
    $start.EnvironmentVariables['TMP'] = $staging
    $worker = [Diagnostics.Process]::Start($start)
    try {
        $stdout = $worker.StandardOutput.ReadToEndAsync()
        $stderr = $worker.StandardError.ReadToEndAsync()
        $worker.WaitForExit()
        if ($worker.ExitCode -ne 0) { throw "EC probe exited $($worker.ExitCode). $($stderr.Result)" }
        $body = $stdout.Result
        $status = 'ok'
    } finally {
        $worker.Dispose()
    }
} catch {
    $body = $_.Exception.Message
} finally {
    if ($staging -and [IO.Path]::GetDirectoryName($staging) -eq $root) {
        try { [IO.Directory]::Delete($staging, $true) }
        catch {
            if ($status -eq 'ok') {
                $body = "EC probe completed, but temporary helper cleanup failed. $($_.Exception.Message)"
                $status = 'error'
            }
        }
    }
}
$client = [Net.Sockets.TcpClient]::new()
try {
    $client.Connect([Net.IPAddress]::Loopback, $port)
    $writer = [IO.StreamWriter]::new($client.GetStream(), [Text.UTF8Encoding]::new($false))
    try {
        $writer.WriteLine($token)
        $writer.WriteLine($status)
        $writer.Write($body)
    } finally {
        $writer.Dispose()
    }
} finally {
    $client.Dispose()
}
"""


def local_diagnostics() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "BladeFanControl" / "diagnostics"


def _payload() -> tuple[Path, str]:
    from _probe_payload import SHA256

    return Path(__file__).resolve().with_name("BladeEcProbe.zip"), SHA256


def check_bundle() -> None:
    """Start the packaged helper in its offline mode."""
    archive, expected = _payload()
    if sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Bundled EC probe checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="BladeFanControl-check-") as directory:
        with zipfile.ZipFile(archive) as package:
            package.extractall(directory)
        worker = Path(directory) / "BladeEcProbe.exe"
        result = subprocess.run(
            [str(worker), "check"], cwd=directory, capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=30,
        )
        if result.returncode or not json.loads(result.stdout)["passed"]:
            detail = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"Bundled EC probe failed its offline check. {detail}")


def _powershell() -> Path:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    directory = ctypes.create_unicode_buffer(32768)
    if not 0 < kernel.GetSystemDirectoryW(directory, len(directory)) < len(directory):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(directory.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _start_probe(archive: Path, digest: str, port: int, token: str) -> subprocess.Popen:
    powershell = _powershell()
    script = (
        f"$payloadPath = {_ps_literal(str(archive))}\n$expectedHash = {_ps_literal(digest)}\n"
        f"$port = {port}\n$token = {_ps_literal(token)}\n{_ELEVATED_SCRIPT}"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    launch = (
        "$ErrorActionPreference = 'Stop'; $env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules'); "
        f"$p = Start-Process -FilePath {_ps_literal(str(powershell))} "
        f"-ArgumentList '-NoProfile -NonInteractive -EncodedCommand {encoded}' "
        f"-WorkingDirectory {_ps_literal(str(powershell.parent))} "
        "-Verb RunAs -WindowStyle Hidden -PassThru -Wait; exit $p.ExitCode"
    )
    # Use the system DLL search path for PowerShell, away from the GUI bundle's temporary files.
    set_dll_directory = ctypes.WinDLL("kernel32", use_last_error=True).SetDllDirectoryW
    set_dll_directory.argtypes = [ctypes.c_wchar_p]
    if not set_dll_directory(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return subprocess.Popen(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", launch],
            cwd=powershell.parent, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    finally:
        set_dll_directory(str(archive.parent))


def _wait_for_report(listener: socket.socket, launcher: subprocess.Popen, token: str) -> dict:
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    listener.settimeout(0.25)
    while time.monotonic() < deadline:
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            if launcher.poll() is not None:
                detail = (launcher.communicate()[0] or b"").decode(errors="replace").strip()
                raise RuntimeError(f"The administrator prompt was canceled or the probe could not start. {detail[-300:]}")
            continue
        with connection, connection.makefile("rb") as reply:
            connection.settimeout(5)
            if reply.readline(128).strip() != token.encode("ascii"):
                continue
            status = reply.readline(16).strip()
            body = reply.read(MAX_REPORT_BYTES + 1)
        if len(body) > MAX_REPORT_BYTES:
            raise RuntimeError("EC probe report exceeds the expected size")
        if status != b"ok":
            raise RuntimeError(body.decode("utf-8", errors="replace").strip())
        report = json.loads(body)
        if not isinstance(report, dict) or not report.get("complete") or not report.get("verify_live"):
            raise RuntimeError("EC probe returned an incomplete report")
        return report
    raise RuntimeError("Timed out waiting for the live EC probe")


def run_live_probe() -> Path:
    archive, expected = _payload()
    token = token_hex(32)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with _start_probe(archive, expected, listener.getsockname()[1], token) as launcher:
            try:
                report = _wait_for_report(listener, launcher, token)
            finally:
                try:
                    launcher.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    launcher.kill()

    diagnostics = local_diagnostics()
    diagnostics.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=f"verify-live-{datetime.now():%Y%m%d-%H%M%S}-", dir=diagnostics))
    report_path = run / "probe.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report_path
