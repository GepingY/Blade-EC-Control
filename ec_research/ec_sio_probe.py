"""Stage-2 read-only Super I/O, BRAM & safe I2EC MMIO probe for IT8390E via PawnIO LpcIO.

Default / --check: offline package, archive, module hash, C API, and mock-protocol
self-test; never loads a DLL, opens a driver, or accesses hardware ports.

--run (requires Windows Administrator / UAC): uses official WHCP-signed PawnIO +
the signed LpcIO.bin module from diagnostics/tools/pawnio/release_0_2_12.zip to:
  1. Read Super I/O ID/version registers (0x20..0x2F on 0x2E/0x2F, falling back
     to 0x4E/0x4F only if 0x2E/0x2F returns no device), scan LDN BARs 0x00..0x1F,
     and read vendor config regs (0xF0..0xFF) on LDNs 0x0F..0x12.
  2. Discover active BARs via LpcIO ioctl_find_bars, read 0x0300 mailbox
     version/SSID, and dump read-only BRAM 0x00..0x7F on active LDN 0x10 ports
     (0x0300/0x0301 and 0x0302/0x0303).
  3. Sample safe I2EC MMIO windows (BRAM 0x2200..0x22FF, SMFI ECINDAR0..3
     0x103B..0x103E, SMFI cfg 0x1060..0x106F, EC2I 0x1100..0x111F, LSIO
     0x1200..0x120F, plus 16-byte samples at 0x0000, 0x0100, 0x0300, 0x4000,
     0x8000, 0xC000, 0xF000). Strictly forbids reading 0x103F (ECINDDR).

Safety boundaries:
  - Never accesses ACPI EC ports 0x62/0x66.
  - Strictly forbids reading/writing I2EC 0x103F (ECINDDR), PM1 0xDC, or erase/write.
  - Never writes to BRAM command slots (only sets index on 0x0300/0x0302 and reads data).
  - Never touches PCI config space or loads WinRing0 / unsigned drivers.
  - Restores initial Super I/O LDN (and exits PnP mode if entered), and uninstalls
    PawnIO afterward if this run installed it (unless --keep-driver is passed).
"""

import argparse
import contextlib
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import ec_snapshot as ec

LPCIO_SHA256 = "b3896a1cab0d808fca31fe2ebcae045d59dac690da87b17c858bb8da357eb45e"
LPCIO_MEMBER = "LpcIO.bin"
ISABUS_MUTEX = "Global\\Access_ISABUS.HTP.Method"
SIO_SLOTS = ((0, 0x2E, (0x87, 0x01, 0x55, 0x55)), (1, 0x4E, (0x87, 0x01, 0x55, 0xAA)))
SRAM_SIZE = 0x1000
BRAM_BANK_SIZE = 0x80
ROM_SIZE = 0x40000
D0_CHUNK_SIZE = 32
D0_BATCH_BYTES = 0x1000
MAX_EST_DUMP_SECONDS = 360.0
I2EC_FORBIDDEN_ADDRS = frozenset({0x103F})
I2EC_MMIO_WINDOWS = (
    ("ecindar_103b_103e", 0x103B, 0x103F),
    ("smfi_cfg_1060_106f", 0x1060, 0x1070),
    ("ec2i_1100_111f", 0x1100, 0x1120),
    ("lsio_1200_120f", 0x1200, 0x1210),
    ("bram_2200_227f", 0x2200, 0x2280),
    ("bram_2280_22ff", 0x2280, 0x2300),
)
I2EC_SAMPLE_OFFSETS = (0x0000, 0x0100, 0x0300, 0x4000, 0x8000, 0xC000, 0xF000)
DETAIL_LDNS = (0x0F, 0x10, 0x11, 0x12)
DETAIL_LDN_REGS = (0x30, 0x60, 0x61, 0x62, 0x63, 0x70, 0x71, *range(0xF0, 0x100))


class SioProbeError(RuntimeError):
    pass


def resolve_pawnio_dir(pawnio_dir=None):
    if pawnio_dir is not None:
        return Path(pawnio_dir)
    for candidate in (ec.ROOT / "tools" / "pawnio", ec.ROOT / "diagnostics" / "tools" / "pawnio"):
        if candidate.is_dir():
            return candidate
    return ec.ROOT / "tools" / "pawnio"


def load_verified_lpcio_bytes(pawnio_dir=None):
    pkg = resolve_pawnio_dir(pawnio_dir)
    archive = pkg / "release_0_2_12.zip"
    if not archive.is_file():
        raise SioProbeError(
            f"Missing official PawnIO module archive: {archive}. "
            "Download release_0_2_12.zip from https://github.com/namazso/PawnIO.Modules/releases/tag/0.2.12"
        )
    if ec.sha256(archive) != ec.ARCHIVE_SHA256:
        raise SioProbeError("release_0_2_12.zip SHA-256 mismatch")
    with zipfile.ZipFile(archive) as zf:
        data = zf.read(LPCIO_MEMBER)
    digest = hashlib.sha256(data).hexdigest()
    if digest != LPCIO_SHA256:
        raise SioProbeError(f"LpcIO.bin SHA-256 mismatch: {digest}")
    return data


def offline_check(pawnio_dir=None):
    """Verify local PawnIO installer/LpcIO hashes (if present) and mock protocol self-test."""
    pkg = resolve_pawnio_dir(pawnio_dir)
    installed = ec.installed_paths()
    installer_path = pkg / "PawnIO_setup.exe"
    installer_sha = ec.sha256(installer_path) if installer_path.is_file() else None
    installer_ok = (installer_sha == ec.INSTALLER_SHA256) or bool(installed.get("installed"))

    lpcio_ok = False
    lpcio_digest = None
    lpcio_err = None
    try:
        blob = load_verified_lpcio_bytes(pkg)
        lpcio_digest = hashlib.sha256(blob).hexdigest()
        lpcio_ok = len(blob) == 18076
    except (OSError, KeyError, zipfile.BadZipFile, SioProbeError) as exc:
        lpcio_err = str(exc)

    selftest_ok = protocol_selftest()
    return {
        "mode": "offline_check",
        "driver_opened": False,
        "hardware_accessed": False,
        "pawnio_dir": str(pkg),
        "installer": {
            "path": str(installer_path),
            "sha256": installer_sha,
            "matches_pinned_or_already_installed": installer_ok,
        },
        "lpcio_module": {
            "archive": str(pkg / "release_0_2_12.zip"),
            "member": LPCIO_MEMBER,
            "sha256": lpcio_digest,
            "matches_pinned_release": lpcio_ok,
            "error": lpcio_err,
        },
        "installed": installed,
        "is_admin": bool(ctypes.WinDLL("shell32").IsUserAnAdmin()) if hasattr(ctypes, "WinDLL") else False,
        "protocol_selftest_passed": selftest_ok,
        "passed": bool(installer_ok and lpcio_ok and selftest_ok),
    }


class NamedMutex:
    """Acquire and release a named Windows synchronization mutex cleanly."""

    def __init__(self, name):
        self.name = name
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.ReleaseMutex.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = None

    def __enter__(self):
        self.handle = self.kernel.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        waited = self.kernel.WaitForSingleObject(self.handle, 3000)
        if waited != 0:
            if waited == 0x80:
                self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise SioProbeError(f"Could not acquire clean mutex {self.name} (wait=0x{waited:X})")
        return self

    def __exit__(self, *_):
        if self.handle:
            try:
                self.kernel.ReleaseMutex(self.handle)
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


class LpcIoClient:
    """Wrapper over PawnIOLib.dll loaded with the signed LpcIO.bin module."""

    def __init__(self, library_path, module_bytes):
        self.lib = ctypes.WinDLL(str(Path(library_path).resolve()))
        self.module_bytes = module_bytes
        self.handle = wintypes.HANDLE()
        self.lib.pawnio_open.argtypes = [ctypes.POINTER(wintypes.HANDLE)]
        self.lib.pawnio_open.restype = ctypes.c_int32
        self.lib.pawnio_load.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t]
        self.lib.pawnio_load.restype = ctypes.c_int32
        self.lib.pawnio_execute.argtypes = [
            wintypes.HANDLE, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.lib.pawnio_execute.restype = ctypes.c_int32
        self.lib.pawnio_close.argtypes = [wintypes.HANDLE]
        self.lib.pawnio_close.restype = ctypes.c_int32

    def __enter__(self):
        ec.hresult(self.lib.pawnio_open(ctypes.byref(self.handle)), "pawnio_open")
        try:
            blob = (ctypes.c_ubyte * len(self.module_bytes)).from_buffer_copy(self.module_bytes)
            ec.hresult(self.lib.pawnio_load(self.handle, blob, len(self.module_bytes)), "pawnio_load LpcIO")
        except BaseException:
            self.lib.pawnio_close(self.handle)
            raise
        return self

    def __exit__(self, *_):
        ec.hresult(self.lib.pawnio_close(self.handle), "pawnio_close")

    def _exec(self, name, args, out_count):
        in_arr = (ctypes.c_uint64 * len(args))(*args) if args else None
        out_arr = (ctypes.c_uint64 * out_count)() if out_count else None
        ret_count = ctypes.c_size_t()
        ec.hresult(
            self.lib.pawnio_execute(self.handle, name.encode("ascii"), in_arr, len(args), out_arr, out_count, ctypes.byref(ret_count)),
            name,
        )
        if ret_count.value != out_count:
            raise SioProbeError(f"{name} returned {ret_count.value} values, expected {out_count}")
        return list(out_arr) if out_count else []

    def select_slot(self, slot):
        self._exec("ioctl_select_slot", [slot], 0)

    def find_bars(self):
        self._exec("ioctl_find_bars", [], 0)

    def sio_inb(self, reg):
        return self._exec("ioctl_superio_inb", [reg & 0xFF], 1)[0] & 0xFF

    def sio_inw(self, reg):
        return self._exec("ioctl_superio_inw", [reg & 0xFF], 1)[0] & 0xFFFF

    def sio_outb(self, reg, val):
        self._exec("ioctl_superio_outb", [reg & 0xFF, val & 0xFF], 0)

    def pio_inb(self, port):
        return self._exec("ioctl_pio_inb", [port & 0xFFFF], 1)[0] & 0xFF

    def pio_outb(self, port, val):
        self._exec("ioctl_pio_outb", [port & 0xFFFF, val & 0xFF], 0)


class SioProbeSession:
    """Executes Stage-2 read-only Super I/O, BRAM, mailbox, and I2EC MMIO checks."""

    def __init__(self, io):
        self.io = io

    def _i2ec_read(self, addr):
        if addr in I2EC_FORBIDDEN_ADDRS:
            raise SioProbeError(f"Refusing forbidden I2EC read at 0x{addr:04X}")
        self.io.sio_outb(0x11, (addr >> 8) & 0xFF)
        self.io.sio_outb(0x10, addr & 0xFF)
        return self.io.sio_inb(0x12) & 0xFF

    def _read_global_regs(self):
        return {f"0x{r:02X}": f"0x{self.io.sio_inb(r):02X}" for r in range(0x20, 0x30)}

    def _scan_ldns(self):
        ldns = {}
        for ldn in range(0x20):
            self.io.sio_outb(0x07, ldn)
            echo = self.io.sio_inb(0x07)
            act = self.io.sio_inb(0x30)
            bar0 = self.io.sio_inw(0x60)
            bar1 = self.io.sio_inw(0x62)
            irq = self.io.sio_inb(0x70)
            if echo == ldn and (act not in (0x00, 0xFF) or bar0 not in (0x0000, 0xFFFF) or bar1 not in (0x0000, 0xFFFF)):
                ldns[f"0x{ldn:02X}"] = {
                    "activate": f"0x{act:02X}",
                    "bar0": f"0x{bar0:04X}",
                    "bar1": f"0x{bar1:04X}",
                    "irq": f"0x{irq:02X}",
                }
        return ldns

    def _read_ldn_details(self):
        details = {}
        for ldn in DETAIL_LDNS:
            self.io.sio_outb(0x07, ldn)
            if self.io.sio_inb(0x07) != ldn:
                continue
            details[f"0x{ldn:02X}"] = {
                f"0x{reg:02X}": f"0x{self.io.sio_inb(reg):02X}" for reg in DETAIL_LDN_REGS
            }
        return details

    @staticmethod
    def _has_bar_port(ldns, port):
        target = f"0x{port:04X}"
        return any(v["bar0"] == target or v["bar1"] == target for v in ldns.values())

    def _probe_mailbox_300(self, ldns):
        if not self._has_bar_port(ldns, 0x0300):
            return {"tested": False, "reason": "No LDN BAR reported at 0x0300"}
        ver_bytes = []
        for idx in (0x06, 0x07, 0x08, 0x09):
            self.io.pio_outb(0x0300, idx)
            ver_bytes.append(self.io.pio_inb(0x0301))
        self.io.pio_outb(0x0300, 0x0C)
        ssid_hi = self.io.pio_inb(0x0301)
        self.io.pio_outb(0x0300, 0x0B)
        ssid_lo = self.io.pio_inb(0x0301)
        ver_ascii = "".join(chr(b) if 0x20 <= b < 0x7F else "?" for b in ver_bytes)
        return {
            "tested": True,
            "version_raw": ver_bytes,
            "version_ascii": f"{ver_ascii[:2]}.{ver_ascii[2:]}",
            "ssid": f"0x{(ssid_hi << 8) | ssid_lo:04X}",
        }

    def _read_bram_bank(self, idx_port, count):
        raw = bytearray()
        for idx in range(count):
            self.io.pio_outb(idx_port, idx)
            raw.append(self.io.pio_inb(idx_port + 1))
        self.io.pio_outb(idx_port, 0x00)
        return bytes(raw)

    def _bram_read(self, idx_port, index):
        self.io.pio_outb(idx_port, index & 0x7F)
        return self.io.pio_inb(idx_port + 1)

    def _bram_write(self, idx_port, index, value):
        self.io.pio_outb(idx_port, index & 0x7F)
        self.io.pio_outb(idx_port + 1, value & 0xFF)

    def _d0_read_chunk(self, addr, length=D0_CHUNK_SIZE, timeout_s=1.0, sleep_fn=time.sleep):
        if not 1 <= length <= D0_CHUNK_SIZE:
            raise SioProbeError(f"Invalid BRAM 0xD0 chunk length: {length}")
        cmd = self._bram_read(0x0300, 0x13)
        if cmd != 0x00:
            raise SioProbeError(f"BRAM 0x0300 command register busy (0x{cmd:02X}) before 0xD0 at 0x{addr:08X}")
        self._bram_write(0x0300, 0x14, 0x00)
        self._bram_write(0x0300, 0x12, length)
        self._bram_write(0x0300, 0x1C, addr & 0xFF)
        self._bram_write(0x0300, 0x1D, (addr >> 8) & 0xFF)
        self._bram_write(0x0300, 0x1E, (addr >> 16) & 0xFF)
        self._bram_write(0x0300, 0x1F, (addr >> 24) & 0xFF)
        t0 = time.perf_counter()
        self._bram_write(0x0300, 0x13, 0xD0)

        polls = 0
        while True:
            cmd = self._bram_read(0x0300, 0x13)
            status = self._bram_read(0x0300, 0x14)
            polls += 1
            if cmd == 0x00 and status == 0x80:
                break
            if cmd == 0x00 and status in (0x82, 0x83, 0x84):
                raise SioProbeError(f"EC rejected BRAM 0xD0 at 0x{addr:08X}: status=0x{status:02X}")
            if time.perf_counter() - t0 >= timeout_s:
                raise SioProbeError(
                    f"Timeout waiting for BRAM 0xD0 at 0x{addr:08X} (cmd=0x{cmd:02X}, status=0x{status:02X}, polls={polls})"
                )
            if polls > 200:
                sleep_fn(0.001)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        payload = bytes(self._bram_read(0x0300, 0x20 + i) for i in range(length))
        return payload, {"cmd": cmd, "status": status, "polls": polls, "elapsed_ms": round(elapsed_ms, 3)}

    def _bram0_read_byte(self, addr, timeout_s=1.0, sleep_fn=time.sleep):
        if not 0 <= addr <= 0x00FFFFFF:
            raise SioProbeError(f"BRAM0 0x90 address out of 24-bit range: 0x{addr:X}")
        if addr == 0x00F0103F:
            raise SioProbeError(f"Forbidden read of 0x{addr:08X} (ECINDDR)")
        cmd = self._bram_read(0x0302, 0x12)
        if cmd != 0x00:
            raise SioProbeError(f"BRAM0 0x0302[0x12] busy (0x{cmd:02X}) before 0x90 at 0x{addr:06X}")
        self._bram_write(0x0302, 0x13, (addr >> 16) & 0xFF)
        self._bram_write(0x0302, 0x14, (addr >> 8) & 0xFF)
        self._bram_write(0x0302, 0x15, addr & 0xFF)
        t0 = time.perf_counter()
        self._bram_write(0x0302, 0x12, 0x90)

        self.io.pio_outb(0x0302, 0x12)
        polls = 0
        while True:
            cmd = self.io.pio_inb(0x0303)
            polls += 1
            if cmd == 0x00:
                break
            elapsed = time.perf_counter() - t0
            if elapsed >= timeout_s:
                raise SioProbeError(
                    f"Timeout waiting for BRAM0 0x90 at 0x{addr:06X} (cmd=0x{cmd:02X}, polls={polls})"
                )
            if elapsed >= 0.015:
                sleep_fn(0.001)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        val = self._bram_read(0x0302, 0x1C)
        return val, {"polls": polls, "elapsed_ms": round(elapsed_ms, 3)}

    def _bram0_read_bytes(self, addr, length, timeout_s=1.0, sleep_fn=time.sleep):
        out = bytearray(length)
        polls = 0
        t0 = time.perf_counter()
        for off in range(length):
            val, meta = self._bram0_read_byte(addr + off, timeout_s=timeout_s, sleep_fn=sleep_fn)
            out[off] = val
            polls += meta["polls"]
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return bytes(out), {"polls": polls, "elapsed_ms": round(elapsed_ms, 3)}

    def _probe_bram_ports(self, ldns):
        summary = {}
        dumps = {}
        for port, filename in ((0x0300, "bram-300.bin"), (0x0302, "bram-302.bin")):
            key = f"port_0x{port:04X}"
            if not self._has_bar_port(ldns, port):
                summary[key] = {"tested": False, "reason": f"No LDN BAR reported at 0x{port:04X}"}
                continue
            try:
                blob = self._read_bram_bank(port, BRAM_BANK_SIZE)
                dumps[filename] = blob
                summary[key] = {
                    "tested": True,
                    "size": len(blob),
                    "nonzero_count": sum(1 for b in blob if b != 0),
                    "first_64_hex": blob[:64].hex(" "),
                    "second_64_hex": blob[64:].hex(" "),
                }
            except Exception as exc:
                summary[key] = {"tested": True, "error": f"{type(exc).__name__}: {exc}"}
        return summary, dumps

    def _probe_i2ec(self):
        windows = {}
        dumps = {}
        for name, start, end in I2EC_MMIO_WINDOWS:
            blob = bytes(self._i2ec_read(addr) for addr in range(start, end))
            nonzero = sum(1 for b in blob if b != 0)
            nonff = sum(1 for b in blob if b != 0xFF)
            windows[name] = {
                "range": f"0x{start:04X}..0x{end - 1:04X}",
                "size": len(blob),
                "nonzero_count": nonzero,
                "nonff_count": nonff,
                "hex": blob.hex(" "),
            }
            if nonzero > 0 and nonff > 0:
                dumps[f"i2ec-{name}.bin"] = blob

        samples = {}
        for base in I2EC_SAMPLE_OFFSETS:
            block = [self._i2ec_read(base + i) for i in range(16)]
            samples[f"0x{base:04X}"] = [f"0x{b:02X}" for b in block]

        any_live = bool(dumps) or any(
            int(hx, 16) not in (0x00, 0xFF)
            for row in samples.values()
            for hx in row
        )
        return {"any_live": any_live, "windows": windows, "samples": samples}, dumps

    def _discover_slot(self, slot, base_port, pnp_key):
        self.io.select_slot(slot)
        chip_before = self.io.sio_inw(0x20)
        reg22_before = self.io.sio_inb(0x22)
        pnp_used = False
        chip_after = chip_before
        reg22_after = reg22_before
        if chip_before in (0x0000, 0xFFFF):
            for key_byte in pnp_key:
                self.io.pio_outb(base_port, key_byte)
            chip_after = self.io.sio_inw(0x20)
            reg22_after = self.io.sio_inb(0x22)
            pnp_used = chip_after not in (0x0000, 0xFFFF)

        slot_info = {
            "base_port": f"0x{base_port:02X}",
            "chip_id_before_key": f"0x{chip_before:04X}",
            "reg22_before_key": f"0x{reg22_before:02X}",
            "pnp_key_tried": chip_before in (0x0000, 0xFFFF),
            "pnp_key_unlocked": pnp_used,
            "chip_id": f"0x{chip_after:04X}",
            "reg22": f"0x{reg22_after:02X}",
        }
        return slot_info, pnp_used, chip_after

    def run(self):
        results = {"slots": {}, "selected_slot": None}
        bin_dumps = {}
        for slot, base_port, pnp_key in SIO_SLOTS:
            slot_info, pnp_used, chip_after = self._discover_slot(slot, base_port, pnp_key)
            if chip_after in (0x0000, 0xFFFF):
                results["slots"][f"slot_{slot}"] = slot_info
                continue

            saved_ldn = self.io.sio_inb(0x07)
            try:
                slot_info["global_regs_20_2F"] = self._read_global_regs()
                slot_info["ldns"] = self._scan_ldns()
                slot_info["ldn_details"] = self._read_ldn_details()
                i2ec_info, i2ec_dumps = self._probe_i2ec()
                slot_info["i2ec_mmio"] = i2ec_info
                bin_dumps.update(i2ec_dumps)
                try:
                    self.io.find_bars()
                    slot_info["find_bars_status"] = "ok"
                    slot_info["mailbox_300"] = self._probe_mailbox_300(slot_info["ldns"])
                    bram_info, bram_dumps = self._probe_bram_ports(slot_info["ldns"])
                    slot_info["bram_banks"] = bram_info
                    bin_dumps.update(bram_dumps)
                except Exception as exc:
                    slot_info["find_bars_status"] = f"error: {exc}"
            finally:
                self.io.sio_outb(0x07, saved_ldn)
                if pnp_used:
                    self.io.sio_outb(0x02, 0x02)

            results["slots"][f"slot_{slot}"] = slot_info
            results["selected_slot"] = slot
            break
        return results, bin_dumps

    def _clear_stale_d0_if_canary(self, initial_bram):
        if (
            initial_bram[0x13] == 0xD0
            and initial_bram[0x14] == 0x00
            and initial_bram[0x1C:0x20] == b"\x80\x00\x00\x00"
        ):
            self._bram_write(0x0300, 0x13, 0x00)
            self._bram_write(0x0300, 0x14, 0x80)
            self.io.pio_outb(0x0300, 0x00)
            return True
        return False

    def _cleanup_bram0_cmd90(self):
        if self._bram_read(0x0302, 0x12) == 0x90:
            for idx in (0x12, 0x13, 0x14, 0x15):
                self._bram_write(0x0302, idx, 0x00)
            self.io.pio_outb(0x0302, 0x00)
            return True
        self.io.pio_outb(0x0302, 0x00)
        return False

    def _probe_ec_regs_bram0(self, timeout_s=1.0, sleep_fn=time.sleep):
        regs = (
            ("isr9_f01124", 0x00F01124),
            ("ier9_f01125", 0x00F01125),
            ("bram_cfg_f02018", 0x00F02018),
            ("bram_idx0_f02019", 0x00F02019),
            ("bram_idx1_f0201a", 0x00F0201A),
            ("bram_ctrl_f0201b", 0x00F0201B),
            ("bram_1c_f0201c", 0x00F0201C),
            ("smfi3_f01063", 0x00F01063),
            ("ecindar0_f0103b", 0x00F0103B),
            ("ecindar1_f0103c", 0x00F0103C),
            ("ecindar2_f0103d", 0x00F0103D),
            ("ecindar3_f0103e", 0x00F0103E),
            ("ram_080145", 0x00080145),
            ("task13_08000d", 0x0008000D),
            ("sku_0802ed", 0x000802ED),
        )
        out = {}
        for name, addr in regs:
            val, meta = self._bram0_read_byte(addr, timeout_s=timeout_s, sleep_fn=sleep_fn)
            out[name] = {"addr": f"0x{addr:08X}", "val": f"0x{val:02X}", "elapsed_ms": meta["elapsed_ms"]}
        self.io.pio_outb(0x0302, 0x00)
        return out

    def verify_live_ec(self, lock_factory=contextlib.nullcontext, sleep_fn=time.sleep, timeout_s=1.0):
        """Fast (~0.35s) strictly read-only check of live EC ROM mode slots, patch bytes, and active SRAM state."""
        targets = (
            ("rom_header_0080", 0x00000080, 32),
            ("rom_cache_builder_11316", 0x00011316, 8),
            ("rom_cache_ver_chk_1211e", 0x0001211E, 4),
            ("rom_defaults_ver_chk_105cc", 0x000105CC, 4),
            ("rom_gpu_ctgp_0e548", 0x0000E548, 4),
            ("rom_gpu_ctgp_cutoff_0e56e", 0x0000E56E, 4),
            ("rom_bal_clamps_3d1c9", 0x0003D1C9, 6),
            ("rom_sil_clamps_3dbc9", 0x0003DBC9, 6),
            ("rom_sil_ac_pl_3dbc0", 0x0003DBC0, 2),
            ("rom_sil_dc_pl_3dbdb", 0x0003DBDB, 2),
            ("sram_cache_ver_8054f", 0x0008054F, 1),
            ("sram_defaults_ver_86004", 0x00086004, 2),
            ("sram_custom_low_pl_86900", 0x00086900, 4),
            ("sram_custom_med_pl_86908", 0x00086908, 4),
            ("sram_custom_high_pl_86910", 0x00086910, 4),
            ("sram_clamps_80555", 0x00080555, 6),
            ("sram_mode_8035f", 0x0008035F, 1),
            ("sram_custom_boost_808e0", 0x000808E0, 3),
            ("sram_psta_802aa", 0x000802AA, 1),
            ("sram_active_pl_805eb", 0x000805EB, 2),
            ("sram_manual_flag_8034d", 0x0008034D, 1),
            ("sram_req_rpm_80330", 0x00080330, 2),
            ("sram_target_rpm_80500", 0x00080500, 4),
            ("sram_postflash_80145", 0x00080145, 1),
            ("sram_skid_802ed", 0x000802ED, 1),
            ("mmio_flhctrl3r_f01063", 0x00F01063, 1),
            ("mmio_pwm1_f01807", 0x00F01807, 1),
            ("mmio_pwm2_f01803", 0x00F01803, 1),
            ("mmio_pwm_period_f01841", 0x00F01841, 1),
        )
        info = {"protocol": "bram0_0x0302_cmd_0x90", "reads": {}}
        with lock_factory():
            selected = None
            for slot, base_port, pnp_key in SIO_SLOTS:
                slot_info, pnp_used, chip_after = self._discover_slot(slot, base_port, pnp_key)
                if chip_after in (0x0000, 0xFFFF):
                    continue
                saved_ldn = self.io.sio_inb(0x07)
                try:
                    if chip_after != 0x8390:
                        raise SioProbeError(f"Expected IT8390 (0x8390), got 0x{chip_after:04X}")
                    ldns = self._scan_ldns()
                    if not self._has_bar_port(ldns, 0x0302):
                        raise SioProbeError("LDN 0x10 does not expose BAR 0x0302")
                    self.io.find_bars()
                    info["slot"] = slot
                    info["slot_info"] = slot_info
                    selected = slot
                finally:
                    self.io.sio_outb(0x07, saved_ldn)
                    if pnp_used:
                        self.io.sio_outb(0x02, 0x02)
                break
            if selected is None:
                raise SioProbeError("No responding IT8390 Super I/O slot found")

            info["mailbox_300"] = self._probe_mailbox_300(ldns)
            if self._bram_read(0x0302, 0x12) != 0x00:
                raise SioProbeError("BRAM0 0x0302[0x12] busy; aborting cleanly")

            raw_blobs = {}
            for name, addr, length in targets:
                blob, meta = self._bram0_read_bytes(addr, length, timeout_s=timeout_s, sleep_fn=sleep_fn)
                raw_blobs[name] = blob
                info["reads"][name] = {
                    "addr": f"0x{addr:08X}",
                    "len": length,
                    "hex": blob.hex(" "),
                    "elapsed_ms": meta["elapsed_ms"],
                }

            # Read live mode slot footers (0..6) from active SKU bank so GUI auto-detects any EC ROM mod.
            skid = raw_blobs["sram_skid_802ed"][0]
            sku_bank = 0x3A000 + skid * 0x1000 if 1 <= skid <= 4 else 0x3D000
            rom_slots = {}
            for s in range(7):
                slot_base = sku_bank + s * 0x200
                hdr, meta_h = self._bram0_read_bytes(slot_base, 5, timeout_s=timeout_s, sleep_fn=sleep_fn)
                tail_off = int.from_bytes(hdr[3:5], "little")
                if not (0x20 <= tail_off <= 0x1C0):
                    tail_off = 0x1A8
                f_base = slot_base + tail_off
                dgps_b, meta_d = self._bram0_read_bytes(f_base + 0x0B, 1, timeout_s=timeout_s, sleep_fn=sleep_fn)
                blk, meta_b = self._bram0_read_bytes(f_base + 0x18, 0x1D, timeout_s=timeout_s, sleep_fn=sleep_fn)
                info["reads"][f"rom_slot_{s}_footer"] = {
                    "addr": f"0x{f_base + 0x18:08X}",
                    "len": len(blk),
                    "dgps_hex": dgps_b.hex(),
                    "hex": blk.hex(" "),
                    "elapsed_ms": round(meta_h["elapsed_ms"] + meta_d["elapsed_ms"] + meta_b["elapsed_ms"], 3),
                }
                rom_slots[str(s)] = {
                    "ac_pl1_w": blk[0x00],
                    "ac_pl2_w": blk[0x01],
                    "manual_min_rpm": blk[0x09] * 100,
                    "manual_default_rpm": blk[0x0B] * 100,
                    "manual_max_rpm": blk[0x0E] * 100,
                    "throttle_pl1_w": blk[0x16],
                    "throttle_pl2_w": blk[0x19],
                    "ndtt_c": blk[0x1A],
                    "dc_pl1_w": blk[0x1B],
                    "dc_pl2_w": blk[0x1C],
                    "dgps_level": dgps_b[0],
                }
            self.io.pio_outb(0x0302, 0x00)

        chk = raw_blobs["rom_cache_ver_chk_1211e"]
        bld = raw_blobs["rom_cache_builder_11316"]
        def_chk = raw_blobs["rom_defaults_ver_chk_105cc"]
        gpu_ctgp = raw_blobs["rom_gpu_ctgp_0e548"]
        gpu_cutoff_word = int.from_bytes(raw_blobs["rom_gpu_ctgp_cutoff_0e56e"], "big")
        has_v2_gpu_ctgp = gpu_ctgp == bytes.fromhex("58 10 80 80")
        custom_gpu_90w_levels = []
        if has_v2_gpu_ctgp:
            # v2 uses `slti r15, r4, <cutoff>` (op6=0x2E, rt=15, ra=4) at 0x0E56E
            if ((gpu_cutoff_word >> 25) & 0x3F) == 0x2E and ((gpu_cutoff_word >> 15) & 0x1F) == 4:
                cutoff = gpu_cutoff_word & 0x7FFF
                custom_gpu_90w_levels = [lv for lv in (0, 1, 2) if lv < cutoff]
            else:
                custom_gpu_90w_levels = [0, 1]
        bal = raw_blobs["rom_bal_clamps_3d1c9"]
        sil = raw_blobs["rom_sil_clamps_3dbc9"]
        sil_ac = raw_blobs["rom_sil_ac_pl_3dbc0"]
        sil_dc = raw_blobs["rom_sil_dc_pl_3dbdb"]
        sram_c = raw_blobs["sram_clamps_80555"]
        t_rpm = raw_blobs["sram_target_rpm_80500"]
        psta = raw_blobs["sram_psta_802aa"][0]
        c_low = raw_blobs["sram_custom_low_pl_86900"]
        c_med = raw_blobs["sram_custom_med_pl_86908"]
        c_high = raw_blobs["sram_custom_high_pl_86910"]
        c_bst = raw_blobs["sram_custom_boost_808e0"]
        act_pl = raw_blobs["sram_active_pl_805eb"]
        info["summary"] = {
            "rom_sku_bank": f"0x{sku_bank:05X}",
            "rom_slots": rom_slots,
            "rom_has_v1_or_v2_fan_clamps": (bal[:2] == b"\x00\x00" and bal[3:] == b"\x37\x37\x37" and sil[:2] == b"\x00\x00" and sil[3:] == b"\x37\x37\x37"),
            "rom_has_v2_cache_ver_4": chk == bytes.fromhex("5a 08 04 0b") and bld == bytes.fromhex("44 93 d0 00 85 04 d5 12"),
            "rom_has_v2_defaults_ver_8": def_chk == bytes.fromhex("5a 00 08 10"),
            "rom_has_v2_gpu_ctgp_handler": has_v2_gpu_ctgp,
            "rom_custom_gpu_90w_cpu_levels": custom_gpu_90w_levels,
            "rom_silent_pl_ac_dc_w": {"ac": [sil_ac[0], sil_ac[1]], "dc": [sil_dc[0], sil_dc[1]]},
            "sram_cache_version_3fe000": raw_blobs["sram_cache_ver_8054f"][0],
            "sram_defaults_version_050000": int.from_bytes(raw_blobs["sram_defaults_ver_86004"], "little"),
            "sram_custom_pl_w": {
                "low": {"ac": [c_low[0], c_low[1]], "dc": [c_low[2], c_low[3]]},
                "med": {"ac": [c_med[0], c_med[1]], "dc": [c_med[2], c_med[3]]},
                "high": {"ac": [c_high[0], c_high[1]], "dc": [c_high[2], c_high[3]]},
            },
            "sram_active_mode": raw_blobs["sram_mode_8035f"][0],
            "sram_custom_boost_regs": {"cpu": c_bst[0], "gpu": c_bst[1], "prev_state": f"0x{c_bst[2]:02X}"},
            "sram_psta_802aa": f"0x{psta:02X}",
            "sram_psta_ctgp_90w": bool(psta & 0x08),
            "sram_active_peci_pl_w": {"pl1": act_pl[0], "pl2": act_pl[1]},
            "sram_manual_flag": f"0x{raw_blobs['sram_manual_flag_8034d'][0]:02X}",
            "sram_active_manual_min_rpm": sram_c[0] * 100,
            "sram_active_manual_default_rpm": sram_c[2] * 100,
            "sram_active_manual_max_rpm": sram_c[5] * 100,
            "sram_req_rpm": [raw_blobs["sram_req_rpm_80330"][0] * 100, raw_blobs["sram_req_rpm_80330"][1] * 100],
            "sram_computed_target_rpm": [int.from_bytes(t_rpm[0:2], "little"), int.from_bytes(t_rpm[2:4], "little")],
            "sram_postflash_80145": f"0x{raw_blobs['sram_postflash_80145'][0]:02X}",
            "mmio_flhctrl3r_f01063": f"0x{raw_blobs['mmio_flhctrl3r_f01063'][0]:02X}",
            "live_pwm": {
                "fan1": raw_blobs["mmio_pwm1_f01807"][0],
                "fan2": raw_blobs["mmio_pwm2_f01803"][0],
                "period": raw_blobs["mmio_pwm_period_f01841"][0],
            },
        }
        return info

    def dump_rom(
        self,
        lock_factory=contextlib.nullcontext,
        sleep_fn=time.sleep,
        progress_cb=None,
        protocol="bram0_90",
        max_dump_seconds=900.0,
        timeout_s=1.0,
        extra_artifacts=None,
    ):
        """Verify EC ROM read capability via canaries, then sweep full 256 KiB EC ROM."""
        if protocol not in ("bram0_90", "bram1_d0"):
            raise SioProbeError(f"Unsupported dump protocol: {protocol}")
        info = {
            "protocol": "bram0_0x0302_cmd_0x90" if protocol == "bram0_90" else "bram_0x0300_cmd_0xD0",
            "rom_size": ROM_SIZE,
            "chunk_size": 1 if protocol == "bram0_90" else D0_CHUNK_SIZE,
            "max_dump_seconds": max_dump_seconds,
        }
        with lock_factory():
            selected = None
            for slot, base_port, pnp_key in SIO_SLOTS:
                slot_info, pnp_used, chip_after = self._discover_slot(slot, base_port, pnp_key)
                if chip_after in (0x0000, 0xFFFF):
                    continue
                saved_ldn = self.io.sio_inb(0x07)
                try:
                    if chip_after != 0x8390:
                        raise SioProbeError(f"Expected IT8390 (0x8390), got 0x{chip_after:04X}")
                    ldns = self._scan_ldns()
                    req_port = 0x0302 if protocol == "bram0_90" else 0x0300
                    if not self._has_bar_port(ldns, req_port):
                        raise SioProbeError(f"LDN 0x10 does not expose BAR 0x{req_port:04X}")
                    self.io.find_bars()
                    info["slot"] = slot
                    info["slot_info"] = slot_info
                    selected = slot
                finally:
                    self.io.sio_outb(0x07, saved_ldn)
                    if pnp_used:
                        self.io.sio_outb(0x02, 0x02)
                break
            if selected is None:
                raise SioProbeError("No responding IT8390 Super I/O slot found")

            info["mailbox_before"] = self._probe_mailbox_300(ldns)
            initial_bram_300 = self._read_bram_bank(0x0300, BRAM_BANK_SIZE)
            info["initial_bram_300_64_hex"] = initial_bram_300[:64].hex(" ")
            info["cleared_stale_0x0300_d0"] = self._clear_stale_d0_if_canary(initial_bram_300)
            if protocol == "bram1_d0":
                if not info["cleared_stale_0x0300_d0"] and initial_bram_300[0x13] != 0x00:
                    raise SioProbeError(f"BRAM[0x13] already busy (0x{initial_bram_300[0x13]:02X}); aborting cleanly")
            else:
                initial_bram_302 = self._read_bram_bank(0x0302, BRAM_BANK_SIZE)
                info["initial_bram_302_64_hex"] = initial_bram_302[:64].hex(" ")
                if extra_artifacts is not None:
                    extra_artifacts["bram-302-initial.bin"] = initial_bram_302
                if initial_bram_302[0x12] != 0x00:
                    raise SioProbeError(
                        f"BRAM0 0x0302[0x12] already busy (0x{initial_bram_302[0x12]:02X}); aborting cleanly"
                    )

            canaries = {}
            canary_blobs = {}
            canary_targets = (("header_0080", 0x00000080), ("vectors_0000", 0x00000000), ("bank1_10000", 0x00010000))
            for name, addr in canary_targets:
                try:
                    if protocol == "bram0_90":
                        blob, meta = self._bram0_read_bytes(addr, 32, timeout_s=timeout_s, sleep_fn=sleep_fn)
                    else:
                        blob, meta = self._d0_read_chunk(addr, D0_CHUNK_SIZE, timeout_s=timeout_s, sleep_fn=sleep_fn)
                    canary_blobs[addr] = blob
                    canaries[name] = {
                        "addr": f"0x{addr:08X}",
                        "ok": True,
                        **meta,
                        "hex": blob.hex(" "),
                        "ascii": "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in blob),
                    }
                except Exception as exc:
                    canaries[name] = {"addr": f"0x{addr:08X}", "ok": False, "error": str(exc)}
                    if protocol == "bram0_90":
                        canaries[name]["cleaned_bram0_cmd90"] = self._cleanup_bram0_cmd90()
                        break
            self.io.pio_outb(0x0302 if protocol == "bram0_90" else 0x0300, 0x00)
            info["canaries"] = canaries

        if len(canary_blobs) != len(canary_targets) or not all(c.get("ok") for c in canaries.values()):
            info["full_dump_performed"] = False
            info["abort_reason"] = f"Canary {protocol} check failed or was rejected by EC"
            return info, None

        if protocol == "bram0_90":
            with lock_factory():
                info["ec_regs"] = self._probe_ec_regs_bram0(timeout_s=timeout_s, sleep_fn=sleep_fn)

        c_hdr = canary_blobs[0x00000080]
        c_vec = canary_blobs[0x00000000]
        header_valid = (b"ITE8390" in c_hdr) or (c_hdr[:7] == b"\xA5" * 7)
        vector_valid = any(b not in (0x00, 0xFF) for b in c_vec) and (c_vec != c_hdr)
        if protocol == "bram0_90":
            total_ms = sum(c["elapsed_ms"] for c in canaries.values())
            avg_byte_ms = total_ms / 96.0
            est_s = (avg_byte_ms * ROM_SIZE) / 1000.0
            info["canary_checks"] = {
                "header_signature_valid": header_valid,
                "vector_table_valid": vector_valid,
                "avg_byte_ms": round(avg_byte_ms, 4),
                "estimated_bytes_per_sec": round(1000.0 / avg_byte_ms, 1) if avg_byte_ms > 0 else 0.0,
                "estimated_total_seconds": round(est_s, 1),
            }
        else:
            avg_ms = sum(c["elapsed_ms"] for c in canaries.values()) / len(canaries)
            est_s = (avg_ms * (ROM_SIZE // D0_CHUNK_SIZE)) / 1000.0
            info["canary_checks"] = {
                "header_signature_valid": header_valid,
                "vector_table_valid": vector_valid,
                "avg_chunk_ms": round(avg_ms, 3),
                "estimated_total_seconds": round(est_s, 1),
            }

        if not (header_valid and vector_valid):
            info["full_dump_performed"] = False
            info["abort_reason"] = "Canary payload did not match expected IT8390 ROM signature"
            return info, None
        cap_s = max_dump_seconds if protocol == "bram0_90" else MAX_EST_DUMP_SECONDS
        if cap_s is not None and est_s > cap_s:
            if protocol == "bram0_90" and extra_artifacts is not None:
                sample = bytearray(256)
                with lock_factory():
                    sample[:32] = c_vec
                    rest, _ = self._bram0_read_bytes(0x20, 224, timeout_s=timeout_s, sleep_fn=sleep_fn)
                    sample[32:256] = rest
                    self.io.pio_outb(0x0302, 0x00)
                extra_artifacts["ec-rom-sample-00000-000ff.bin"] = bytes(sample)
                info["sample_0000_00ff_hex"] = bytes(sample).hex(" ")
            info["full_dump_performed"] = False
            info["abort_reason"] = (
                f"Estimated sweep time ({est_s:.1f}s) exceeds {cap_s:.0f}s safety cap"
                + (" (re-run with --force-slow to dump full 256 KiB)" if protocol == "bram0_90" else "")
            )
            return info, None

        rom = bytearray(ROM_SIZE)
        t_sweep0 = time.perf_counter()
        max_polls = 0
        if protocol == "bram0_90":
            batch_size = 256
            for batch_start in range(0, ROM_SIZE, batch_size):
                with lock_factory():
                    for off in range(batch_size):
                        val, meta = self._bram0_read_byte(batch_start + off, timeout_s=timeout_s, sleep_fn=sleep_fn)
                        rom[batch_start + off] = val
                        if meta["polls"] > max_polls:
                            max_polls = meta["polls"]
                    self.io.pio_outb(0x0302, 0x00)
                done_bytes = batch_start + batch_size
                if progress_cb and (done_bytes % 0x1000 == 0 or done_bytes == ROM_SIZE):
                    progress_cb(done_bytes, ROM_SIZE, time.perf_counter() - t_sweep0)
        else:
            for batch_start in range(0, ROM_SIZE, D0_BATCH_BYTES):
                with lock_factory():
                    for addr in range(batch_start, batch_start + D0_BATCH_BYTES, D0_CHUNK_SIZE):
                        chunk, meta = self._d0_read_chunk(addr, D0_CHUNK_SIZE, timeout_s=timeout_s, sleep_fn=sleep_fn)
                        rom[addr : addr + D0_CHUNK_SIZE] = chunk
                        if meta["polls"] > max_polls:
                            max_polls = meta["polls"]
                    self.io.pio_outb(0x0300, 0x00)
                done_bytes = batch_start + D0_BATCH_BYTES
                if progress_cb and (done_bytes % 0x4000 == 0 or done_bytes == ROM_SIZE):
                    progress_cb(done_bytes, ROM_SIZE, time.perf_counter() - t_sweep0)

        sweep_s = time.perf_counter() - t_sweep0
        with lock_factory():
            if protocol == "bram0_90":
                verify_hdr, _ = self._bram0_read_bytes(0x00000080, 32, timeout_s=timeout_s, sleep_fn=sleep_fn)
                verify_b1, _ = self._bram0_read_bytes(0x00010000, 32, timeout_s=timeout_s, sleep_fn=sleep_fn)
                self.io.pio_outb(0x0302, 0x00)
            else:
                verify_hdr, _ = self._d0_read_chunk(0x00000080, D0_CHUNK_SIZE, timeout_s=timeout_s, sleep_fn=sleep_fn)
                verify_b1, _ = self._d0_read_chunk(0x00010000, D0_CHUNK_SIZE, timeout_s=timeout_s, sleep_fn=sleep_fn)
                self.io.pio_outb(0x0300, 0x00)

        consistency = {
            "vectors_0000_match": bytes(rom[0x00:0x20]) == c_vec,
            "header_0080_match": bytes(rom[0x80:0xA0]) == c_hdr == verify_hdr,
            "bank1_10000_match": bytes(rom[0x10000:0x10020]) == canary_blobs[0x00010000] == verify_b1,
        }
        info["sweep_seconds"] = round(sweep_s, 3)
        info["max_polls_per_unit" if protocol == "bram0_90" else "max_polls_per_chunk"] = max_polls
        info["consistency_checks"] = consistency
        if not all(consistency.values()):
            raise SioProbeError(f"Post-sweep ROM consistency verification failed: {consistency}")

        info["full_dump_performed"] = True
        info["nonzero_bytes"] = sum(1 for b in rom if b != 0)
        info["nonff_bytes"] = sum(1 for b in rom if b != 0xFF)
        info["header_ascii_0090"] = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in rom[0x90:0xA0])
        return info, bytes(rom)


class _MockLpcIo:
    """In-memory Super I/O + I2EC MMIO + BRAM 0x300/0x302 (0xD0 and 0x90 ROM) simulator for --check."""

    def __init__(self, require_pnp_key=True, allow_bram_write=False, d0_supported=True, cmd90_supported=True):
        self.require_pnp_key = require_pnp_key
        self.allow_bram_write = allow_bram_write
        self.d0_supported = d0_supported
        self.cmd90_supported = cmd90_supported
        self.slot = 0
        self.unlocked = not require_pnp_key
        self.key_seq = []
        self.index = 0
        self.ldn = 0x03
        self.regs = {0x20: 0x83, 0x21: 0x90, 0x22: 0x82, 0x10: 0x00, 0x11: 0x00}
        self.ldn_regs = {
            0x0F: {0x30: 0x01, 0xF5: 0x00, 0xF6: 0x00},
            0x10: {0x30: 0x01, 0x60: 0x03, 0x61: 0x02, 0x62: 0x03, 0x63: 0x00, 0x70: 0x08},
            0x11: {0x30: 0x01, 0x60: 0x00, 0x61: 0x62, 0x62: 0x00, 0x63: 0x66},
        }
        self.mmio = {0x2286: ord("0"), 0x2287: ord("1"), 0x2288: ord("0"), 0x2289: ord("0"), 0x228B: 0x11, 0x228C: 0x20}
        self.ec_mem = {
            0x00F01124: 0x00,
            0x00F01125: 0x00,
            0x00F02018: 0x00,
            0x00F02019: 0x93,
            0x00F0201A: 0x00,
            0x00F0201B: 0x31,
            0x00F0201C: 0x01,
            0x00F01063: 0x18,
            0x00080145: 0xA5,
            0x0008000D: 0x00,
            0x000802ED: 0x03,
        }
        self.bram_idx = {0x0300: 0, 0x0302: 0}
        self.bram_300 = bytearray(BRAM_BANK_SIZE)
        for off, b in ((0x06, ord("0")), (0x07, ord("1")), (0x08, ord("0")), (0x09, ord("0")), (0x0B, 0x11), (0x0C, 0x20)):
            self.bram_300[off] = b
        self.bram_302 = bytearray((i ^ 0x5A) for i in range(BRAM_BANK_SIZE))
        self.bram_302[0x12:0x20] = bytes(0x0E)
        self.rom = bytearray(((i * 37 + (i >> 8) * 13) & 0xFF) for i in range(ROM_SIZE))
        self.rom[0x00:0x08] = b"\x48\x00\x28\x00\x48\x00\x02\x45"
        self.rom[0x80:0xA0] = b"\xa5\xa5\xa5\xa5\xa5\xa5\xa5\xb5\x85\x12\x5a\x5a\xaa\xff\x55\x55ITE8390EC-V01.00"
        self.bars_found = False

    def select_slot(self, slot):
        self.slot = slot

    def sio_inb(self, reg):
        if self.slot != 0 or not self.unlocked:
            return 0xFF
        if reg == 0x07:
            return self.ldn
        if reg == 0x12:
            addr = (self.regs[0x11] << 8) | self.regs[0x10]
            if addr == 0x103F:
                raise SioProbeError("Mock caught forbidden I2EC read at 0x103F")
            return self.mmio.get(addr, 0x00)
        if reg >= 0x30:
            return self.ldn_regs.get(self.ldn, {}).get(reg, 0x00)
        return self.regs.get(reg, 0x00)

    def sio_inw(self, reg):
        return (self.sio_inb(reg) << 8) | self.sio_inb(reg + 1)

    def sio_outb(self, reg, val):
        if self.slot != 0 or not self.unlocked:
            return
        if reg == 0x02 and val == 0x02 and self.require_pnp_key:
            self.unlocked = False
            return
        if reg == 0x07:
            self.ldn = val
        elif reg in (0x10, 0x11):
            self.regs[reg] = val

    def pio_outb(self, port, val):
        if port == 0x2E and self.slot == 0:
            self.key_seq = (self.key_seq + [val])[-4:]
            if self.key_seq == [0x87, 0x01, 0x55, 0x55]:
                self.unlocked = True
        elif port in (0x0300, 0x0302) and self.bars_found:
            self.bram_idx[port] = val & 0x7F
        elif port == 0x0301 and self.bars_found and self.allow_bram_write:
            idx = self.bram_idx[0x0300]
            self.bram_300[idx] = val & 0xFF
            if idx == 0x13 and (val & 0xFF) == 0xD0:
                if not self.d0_supported:
                    self.bram_300[0x14] = 0x83
                    self.bram_300[0x13] = 0x00
                else:
                    n = self.bram_300[0x12]
                    if n <= D0_CHUNK_SIZE:
                        addr = (
                            self.bram_300[0x1C]
                            | (self.bram_300[0x1D] << 8)
                            | (self.bram_300[0x1E] << 16)
                            | (self.bram_300[0x1F] << 24)
                        )
                        chunk = self.rom[addr : addr + n] if addr + n <= len(self.rom) else b"\xFF" * n
                        self.bram_300[0x20 : 0x20 + len(chunk)] = chunk
                    self.bram_300[0x14] = 0x80
                    self.bram_300[0x13] = 0x00
        elif port == 0x0303 and self.bars_found and self.allow_bram_write:
            idx = self.bram_idx[0x0302]
            self.bram_302[idx] = val & 0xFF
            if idx == 0x12 and (val & 0xFF) == 0x90 and self.cmd90_supported:
                addr = (self.bram_302[0x13] << 16) | (self.bram_302[0x14] << 8) | self.bram_302[0x15]
                if addr == 0x00F0103F:
                    raise SioProbeError("Mock caught forbidden ECINDDR 0x00F0103F read")
                if addr < len(self.rom):
                    out_b = self.rom[addr]
                else:
                    out_b = self.ec_mem.get(addr, 0x00)
                self.bram_302[0x1C] = out_b & 0xFF
                for clr in (0x12, 0x13, 0x14, 0x15):
                    self.bram_302[clr] = 0x00
        else:
            raise SioProbeError(f"Denied port write 0x{port:X}")

    def pio_inb(self, port):
        if port == 0x0301 and self.bars_found:
            return self.bram_300[self.bram_idx[0x0300]]
        if port == 0x0303 and self.bars_found:
            return self.bram_302[self.bram_idx[0x0302]]
        raise SioProbeError(f"Denied port read 0x{port:X}")

    def find_bars(self):
        if self.slot != 0 or not self.unlocked:
            raise SioProbeError("No Super I/O chip")
        self.bars_found = True


def protocol_selftest():
    for need_key in (True, False):
        mock = _MockLpcIo(require_pnp_key=need_key)
        res, dumps = SioProbeSession(mock).run()
        assert res["selected_slot"] == 0
        s0 = res["slots"]["slot_0"]
        assert s0["chip_id"] == "0x8390"
        assert s0["pnp_key_tried"] is need_key
        assert s0["mailbox_300"]["version_ascii"] == "01.00"
        assert s0["mailbox_300"]["ssid"] == "0x2011"
        assert s0["ldn_details"]["0x0F"]["0x30"] == "0x01"
        assert s0["bram_banks"]["port_0x0300"]["tested"] is True
        assert s0["bram_banks"]["port_0x0302"]["tested"] is True
        assert dumps["bram-300.bin"] == bytes(mock.bram_300)
        assert dumps["bram-302.bin"] == bytes(mock.bram_302)
        assert "i2ec-bram_2280_22ff.bin" in dumps
        assert all(w["range"] != "0x103B..0x103F" for w in s0["i2ec_mmio"]["windows"].values())
        assert mock.ldn == 0x03
        assert mock.unlocked is (not need_key)

    for need_key in (True, False):
        dump_mock = _MockLpcIo(require_pnp_key=need_key, allow_bram_write=True, d0_supported=True, cmd90_supported=True)
        # Simulate stale Stage-3 canary left in 0x0300
        dump_mock.bram_300[0x13] = 0xD0
        dump_mock.bram_300[0x14] = 0x00
        dump_mock.bram_300[0x1C:0x20] = b"\x80\x00\x00\x00"
        extra = {}
        dump_info, rom_blob = SioProbeSession(dump_mock).dump_rom(
            sleep_fn=lambda _: None, protocol="bram0_90", extra_artifacts=extra
        )
        assert dump_info["cleared_stale_0x0300_d0"] is True
        assert dump_mock.bram_300[0x13] == 0x00
        assert dump_mock.bram_300[0x14] == 0x80
        assert dump_info["full_dump_performed"] is True
        assert dump_info["header_ascii_0090"] == "ITE8390EC-V01.00"
        assert dump_info["ec_regs"]["ram_080145"]["val"] == "0xA5"
        assert rom_blob == bytes(dump_mock.rom)
        assert "bram-302-initial.bin" in extra
        assert dump_mock.unlocked is (not need_key)

        d0_mock = _MockLpcIo(require_pnp_key=need_key, allow_bram_write=True, d0_supported=True)
        d0_info, d0_blob = SioProbeSession(d0_mock).dump_rom(sleep_fn=lambda _: None, protocol="bram1_d0")
        assert d0_info["full_dump_performed"] is True
        assert d0_blob == bytes(d0_mock.rom)

    rej90_mock = _MockLpcIo(require_pnp_key=False, allow_bram_write=True, cmd90_supported=False)
    rej90_info, rej90_blob = SioProbeSession(rej90_mock).dump_rom(
        sleep_fn=lambda _: None, protocol="bram0_90", timeout_s=0.02
    )
    assert rej90_info["full_dump_performed"] is False
    assert rej90_info["canaries"]["header_0080"]["cleaned_bram0_cmd90"] is True
    assert rej90_mock.bram_302[0x12] == 0x00
    assert rej90_blob is None

    slow_mock = _MockLpcIo(require_pnp_key=False, allow_bram_write=True, cmd90_supported=True)
    slow_extra = {}
    slow_info, slow_blob = SioProbeSession(slow_mock).dump_rom(
        sleep_fn=lambda _: None, protocol="bram0_90", max_dump_seconds=0.0001, extra_artifacts=slow_extra
    )
    assert slow_info["full_dump_performed"] is False
    assert "ec-rom-sample-00000-000ff.bin" in slow_extra
    assert slow_extra["ec-rom-sample-00000-000ff.bin"] == bytes(slow_mock.rom[:256])
    assert slow_blob is None

    rej_mock = _MockLpcIo(require_pnp_key=False, allow_bram_write=True, d0_supported=False)
    rej_info, rej_blob = SioProbeSession(rej_mock).dump_rom(sleep_fn=lambda _: None, protocol="bram1_d0")
    assert rej_info["full_dump_performed"] is False
    assert rej_blob is None

    try:
        SioProbeSession(_MockLpcIo(require_pnp_key=False))._i2ec_read(0x103F)
        raise AssertionError("0x103F read must raise")
    except SioProbeError:
        pass
    try:
        SioProbeSession(_MockLpcIo(require_pnp_key=False, allow_bram_write=True))._bram0_read_byte(0x00F0103F)
        raise AssertionError("0x00F0103F read must raise")
    except SioProbeError:
        pass

    # Verify verify_live_ec() decodes all 7 mode slots + Custom 4 SRAM + v2 GPU CTGP cutoff cleanly
    live_mock = _MockLpcIo(require_pnp_key=False, allow_bram_write=True, cmd90_supported=True)
    live_mock.rom[0x0000E548 : 0x0000E54C] = bytes.fromhex("58 10 80 80")
    live_mock.rom[0x0000E56E : 0x0000E572] = ((0x2E << 25) | (15 << 20) | (4 << 15) | 2).to_bytes(4, "big")
    live_mock.rom[0x0001211E : 0x00012122] = bytes.fromhex("5a 08 04 0b")
    live_mock.rom[0x00011316 : 0x0001131E] = bytes.fromhex("44 93 d0 00 85 04 d5 12")
    live_mock.rom[0x000105CC : 0x000105D0] = bytes.fromhex("5a 00 08 10")
    for s in range(7):
        sb = 0x3D000 + s * 0x200
        live_mock.rom[sb + 3 : sb + 5] = (0x1A8).to_bytes(2, "little")
        fb = sb + 0x1A8
        live_mock.rom[fb + 0x0B] = 5 if s in (5, 6) else 1
        live_mock.rom[fb + 0x18 : fb + 0x1A] = bytes([25, 35] if s in (5, 6) else [35, 45])
        live_mock.rom[fb + 0x21 : fb + 0x27] = bytes([0, 0, 31 if s in (5, 6) else 49, 55, 55, 55])
        live_mock.rom[fb + 0x2E] = 25 if s in (5, 6) else 30
        live_mock.rom[fb + 0x31] = 35 if s in (5, 6) else 80
        live_mock.rom[fb + 0x32] = 97 if s in (5, 6) else 80
        live_mock.rom[fb + 0x33 : fb + 0x35] = bytes([25, 35] if s in (5, 6) else [25, 25])
    for off, vals in ((0x86900, (35, 45, 20, 20)), (0x86908, (60, 80, 25, 25)), (0x86910, (60, 90, 30, 30))):
        for idx, val in enumerate(vals):
            live_mock.ec_mem[off + idx] = val
    v_info = SioProbeSession(live_mock).verify_live_ec(sleep_fn=lambda _: None)
    v_sum = v_info["summary"]
    assert v_sum["rom_sku_bank"] == "0x3D000"
    assert v_sum["rom_has_v1_or_v2_fan_clamps"] is True
    assert v_sum["rom_has_v2_gpu_ctgp_handler"] is True
    assert v_sum["rom_custom_gpu_90w_cpu_levels"] == [0, 1]
    assert v_sum["rom_slots"]["5"]["ac_pl1_w"] == 25 and v_sum["rom_slots"]["5"]["dc_pl2_w"] == 35
    assert v_sum["rom_slots"]["0"]["manual_min_rpm"] == 0 and v_sum["rom_slots"]["0"]["manual_max_rpm"] == 5500
    assert v_sum["sram_custom_pl_w"]["med"]["ac"] == [60, 80]
    assert v_sum["sram_custom_pl_w"]["high"]["ac"] == [60, 90]
    return True


def _run_setup(args, log_path, pawnio_dir=None):
    installer = resolve_pawnio_dir(pawnio_dir) / "PawnIO_setup.exe"
    if ec.sha256(installer) != ec.INSTALLER_SHA256:
        raise SioProbeError("PawnIO_setup.exe SHA-256 mismatch")
    sig = ec.verify_signature(installer)
    if sig.get("Status") != "Valid" or "CN=namazso.eu" not in sig.get("Signer", ""):
        raise SioProbeError(f"Invalid PawnIO_setup.exe signature: {sig}")
    with log_path.open("w", encoding="utf-8") as stream:
        proc = subprocess.run(
            [str(installer), *args],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    if proc.returncode == 3010:
        raise SioProbeError("PawnIO setup requires a reboot (exit 3010)")
    if proc.returncode != 0:
        raise SioProbeError(f"PawnIO setup {args} exited with {proc.returncode}")
    return sig


def _with_lpcio_run(dir_prefix, scope, keep_driver, worker, pawnio_dir=None):
    check = offline_check(pawnio_dir=pawnio_dir)
    if not check["passed"]:
        raise SioProbeError(f"Offline checks failed: {check}")
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise SioProbeError("64-bit Python is required")
    if not ctypes.WinDLL("shell32").IsUserAnAdmin():
        raise SioProbeError("Windows Administrator privileges (UAC) are required; use EcSioProbe.cmd or EcRomDump.cmd")

    run_dir = ec.ROOT / "diagnostics" / (dir_prefix + datetime.now().strftime("%Y%m%d-%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "complete": False,
        "installed_before": ec.installed_paths(),
    }
    installed_by_us = False
    try:
        inst = report["installed_before"]
        if not inst.get("installed"):
            report["installer_signature"] = _run_setup(["-install", "-silent"], run_dir / "installer.log", pawnio_dir=pawnio_dir)
            installed_by_us = True
            inst = ec.installed_paths()
            if not inst.get("installed"):
                raise SioProbeError(f"PawnIO failed post-install check: {inst}")
        report["installed"] = inst
        drv_sig = ec.verify_signature(inst["driver_path"])
        report["driver_signature"] = drv_sig
        if drv_sig.get("Status") != "Valid" or not any(
            pub in drv_sig.get("Signer", "")
            for pub in ("Microsoft Windows Hardware Compatibility Publisher", "CN=namazso.eu")
        ):
            raise SioProbeError(f"Unexpected driver signature: {drv_sig}")
        ec.require_running_driver()

        lpcio_blob = load_verified_lpcio_bytes(pawnio_dir=pawnio_dir)
        with LpcIoClient(inst["library_path"], lpcio_blob) as client:
            worker(client, run_dir, report)
        report["complete"] = True
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if installed_by_us and not keep_driver:
            try:
                _run_setup(["-uninstall", "-silent"], run_dir / "uninstaller.log", pawnio_dir=pawnio_dir)
                report["cleaned_up_driver"] = True
            except Exception as exc:
                report["cleaned_up_driver"] = False
                report["cleanup_error"] = str(exc)
        (run_dir / "probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return run_dir, report


def run_probe(keep_driver=False, pawnio_dir=None):
    def worker(client, run_dir, report):
        with NamedMutex(ISABUS_MUTEX):
            probe_res, bin_dumps = SioProbeSession(client).run()
        report["probe"] = probe_res
        report["bin_dumps"] = {}
        for filename, blob in bin_dumps.items():
            dump_path = run_dir / filename
            dump_path.write_bytes(blob)
            report["bin_dumps"][filename] = {
                "path": str(dump_path),
                "size": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
            }

    run_dir, report = _with_lpcio_run(
        "sio-probe-",
        "Stage-2 read-only Super I/O 0x2E/0x2F, LDN 0x0F..0x12 regs, BRAM 0x0300/0x0302, safe I2EC MMIO (no 0x103F)",
        keep_driver,
        worker,
        pawnio_dir=pawnio_dir,
    )
    print(json.dumps({"run_dir": str(run_dir), "probe": report.get("probe")}, indent=2))
    return 0


def run_rom_dump(keep_driver=False, protocol="bram0_90", force_slow=False, pawnio_dir=None):
    def on_progress(done, total, elapsed_s):
        pct = (done * 100.0) / total
        rate = done / elapsed_s if elapsed_s > 0 else 0.0
        eta = (total - done) / rate if rate > 0 else 0.0
        print(f"[{done // 1024:3d}/{total // 1024} KiB] {pct:5.1f}% ({elapsed_s:.1f}s elapsed, ~{eta:.1f}s remaining)", flush=True)

    def worker(client, run_dir, report):
        proto_label = "BRAM0 0x0302 cmd 0x90" if protocol == "bram0_90" else "BRAM1 0x0300 cmd 0xD0"
        print(f"Running Stage-3 canary + 256 KiB EC ROM dump over {proto_label}...", flush=True)
        extra_artifacts = {}
        max_s = None if force_slow else 900.0
        dump_info, rom_blob = SioProbeSession(client).dump_rom(
            lock_factory=lambda: NamedMutex(ISABUS_MUTEX),
            sleep_fn=time.sleep,
            progress_cb=on_progress,
            protocol=protocol,
            max_dump_seconds=max_s,
            extra_artifacts=extra_artifacts,
        )
        report["rom_dump"] = dump_info
        if extra_artifacts:
            report["rom_dump"]["artifacts"] = {}
            for name, blob in extra_artifacts.items():
                art_path = run_dir / name
                art_path.write_bytes(blob)
                report["rom_dump"]["artifacts"][name] = {
                    "path": str(art_path),
                    "size": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                }
        if rom_blob is not None:
            rom_path = run_dir / "ec-rom-00000-3ffff.bin"
            rom_path.write_bytes(rom_blob)
            report["rom_dump"]["file"] = {
                "path": str(rom_path),
                "size": len(rom_blob),
                "sha256": hashlib.sha256(rom_blob).hexdigest(),
            }

    run_dir, report = _with_lpcio_run(
        "rom-dump-",
        f"Stage-3 {protocol} canary verification + read-only 256 KiB EC ROM dump (0x00000000..0x0003FFFF)",
        keep_driver,
        worker,
        pawnio_dir=pawnio_dir,
    )
    print(json.dumps({"run_dir": str(run_dir), "rom_dump": report.get("rom_dump")}, indent=2))
    return 0


def run_verify_live(keep_driver=False, pawnio_dir=None):
    def worker(client, run_dir, report):
        info = SioProbeSession(client).verify_live_ec(
            lock_factory=lambda: NamedMutex(ISABUS_MUTEX),
            sleep_fn=time.sleep,
        )
        report["verify_live"] = info

    run_dir, report = _with_lpcio_run(
        "verify-live-",
        "Fast read-only BRAM0 0x0302 cmd 0x90 check of live EC ROM patch bytes and active SRAM fan/cache state",
        keep_driver,
        worker,
        pawnio_dir=pawnio_dir,
    )
    print(json.dumps({"run_dir": str(run_dir), "verify_live": report.get("verify_live")}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Offline PawnIO asset + mock protocol check (default)")
    mode.add_argument("--self-test", action="store_true", help="Run offline mock-EC protocol self-test only (no PawnIO assets required)")
    mode.add_argument("--run", action="store_true", help="Run Stage-2 read-only probe via official PawnIO (requires Admin)")
    mode.add_argument("--verify-live", action="store_true", help="Fast read-only check of live EC ROM patch bytes & SRAM clamps via PawnIO (requires Admin)")
    mode.add_argument("--dump", action="store_true", help="Canary-verify BRAM and dump 256 KiB EC ROM via PawnIO (requires Admin)")
    parser.add_argument("--pawnio-dir", type=Path, default=None, help="Directory containing official PawnIO_setup.exe and release_0_2_12.zip (default: tools/pawnio)")
    parser.add_argument("--protocol", choices=("bram0_90", "bram1_d0"), default="bram0_90", help="BRAM read protocol for --dump")
    parser.add_argument("--force-slow", action="store_true", help="Allow full 256 KiB dump even if estimated duration exceeds 900s")
    parser.add_argument("--keep-driver", action="store_true", help="Leave PawnIO installed if this run installed it")
    args = parser.parse_args()
    try:
        if args.self_test:
            ok = protocol_selftest()
            print(json.dumps({"mode": "self_test", "driver_opened": False, "hardware_accessed": False, "protocol_selftest_passed": ok}, indent=2))
            return 0 if ok else 2
        if args.run:
            return run_probe(keep_driver=args.keep_driver, pawnio_dir=args.pawnio_dir)
        if args.verify_live:
            return run_verify_live(keep_driver=args.keep_driver, pawnio_dir=args.pawnio_dir)
        if args.dump:
            return run_rom_dump(keep_driver=args.keep_driver, protocol=args.protocol, force_slow=args.force_slow, pawnio_dir=args.pawnio_dir)
        res = offline_check(pawnio_dir=args.pawnio_dir)
        print(json.dumps(res, indent=2))
        return 0 if res["passed"] else 2
    except (SioProbeError, ec.SnapshotError, OSError, ValueError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
