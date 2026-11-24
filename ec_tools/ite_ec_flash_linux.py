#!/usr/bin/env python3
"""Linux IT8390E-256 internal eFlash verifier & flasher for Razer Blade RZ09-0328 (SSID 0x2011).

Implements the exact PM1 0xDC Follow-Mode protocol verified in both:
  - Razer_EC_Flash_v1.03.exe (RWIo::Enable_EC_Flash / EC_Write_Enable / vEnterFollowMode / Erase / Program / Verify)
  - RZ09-0328 stock EC ROM ec-rom-00000-3ffff.bin (0x17192 -> 0x1728c -> 0x0259a..0x02842)

Modes (safe by default — NEVER erases or writes unless --flash AND --i-accept-brick-risk are passed):
  --self-test                 Offline mock-EC protocol & 256 KiB erase/program/verify test (works on Windows/Linux).
  --check-only                Read-only Linux Super I/O (0x2E/0x2F), 0x300 mailbox (SSID/ver), and PCI 0:31:0 reg 0x82.
  --verify-only --stock FILE  Read-only PM1 0xDC Follow-Mode test: enters Follow Mode, verifies SPI ID == FF FF FE,
                              reads all 256 KiB via 0x0B, compares SHA-256 to FILE, exits cleanly via 0xFC, restores 0x62/0x66.
  --flash FILE --i-accept-brick-risk
                              Full 256 KiB erase (0xD7), AAI-word program (0xAD), full 0x0B verify (with in-Follow-Mode
                              sector retry if a mismatch occurs before exiting idle), 0xFC exit, and 0x62/0x66 restore.

Requirements on Linux Live USB (e.g., Ubuntu Live USB):
  - Run as root (sudo python3 ite_ec_flash_linux.py ...).
  - Secure Boot disabled (Linux kernel lockdown blocks /dev/port when Secure Boot is enabled).
  - AC power adapter plugged in and battery charged (>50%).
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import struct
import sys
import time

ROM_SIZE = 0x40000  # 262,144 bytes (256 KiB)
SECTOR_SIZE = 0x400  # 1 KiB (0xD7 ITE eFlash sector erase)
BLOCK_SIZE = 0x1000  # 4 KiB AAI-word program chunk (matches Razer_EC_Flash_v1.03)

EXPECTED_CHIP_ID = 0x8390
EXPECTED_REG22 = 0x82
EXPECTED_SSID = 0x2011
EXPECTED_SPI_ID = (0xFF, 0xFF, 0xFE)
ITE_HEADER_SIG = bytes.fromhex("a5 a5 a5 a5 a5 a5 a5 b5 85 12 5a 5a aa 00 55 55")
ITE_HEADER_SIG_MIRROR = bytes.fromhex("a5 a5 a5 a5 a5 a5 a5 b5 85 12 5a 5a aa ff 55 55")


def has_valid_ite_sig(image: bytes) -> bool:
    return len(image) == ROM_SIZE and image[0x80:0x90] in (ITE_HEADER_SIG, ITE_HEADER_SIG_MIRROR)

SIO_IDX = 0x2E
SIO_DAT = 0x2F
MBOX_IDX = 0x300
MBOX_DAT = 0x301
KBC_CMD = 0x64
PM1_DATA = 0x202
PM1_CMD = 0x206  # Status read / Command write (remapped LDN 0x11)
PCI_LPC_CFG = Path("/sys/bus/pci/devices/0000:00:1f.0/config")


class FlashError(RuntimeError):
    pass


class LinuxPortBackend:
    """Direct Linux /dev/port + sysfs PCI config backend (requires root, Secure Boot / lockdown off)."""

    def __init__(self) -> None:
        self._port_fd: int | None = None
        self._pci_fd: int | None = None

    def open(self) -> None:
        if sys.platform != "linux":
            raise FlashError("Hardware port access requires Linux (use --self-test on Windows).")
        if os.geteuid() != 0:
            raise FlashError("Root privileges required (run with sudo).")
        try:
            self._port_fd = os.open("/dev/port", os.O_RDWR | os.O_SYNC)
        except OSError as exc:
            raise FlashError(
                f"Cannot open /dev/port ({exc}). On Linux Live USB, ensure Secure Boot / kernel lockdown is disabled."
            ) from exc
        if not PCI_LPC_CFG.exists():
            self.close()
            raise FlashError(f"Intel PCH LPC config not found at {PCI_LPC_CFG}")
        self._pci_fd = os.open(str(PCI_LPC_CFG), os.O_RDWR | os.O_SYNC)
        vid = struct.unpack("<H", os.pread(self._pci_fd, 2, 0x00))[0]
        if vid != 0x8086:
            self.close()
            raise FlashError(f"Expected Intel LPC bridge (0x8086) at 0000:00:1f.0, got 0x{vid:04X}")

    def close(self) -> None:
        if self._pci_fd is not None:
            os.close(self._pci_fd)
            self._pci_fd = None
        if self._port_fd is not None:
            os.close(self._port_fd)
            self._port_fd = None

    def inb(self, port: int) -> int:
        assert self._port_fd is not None
        return os.pread(self._port_fd, 1, port)[0]

    def outb(self, port: int, val: int) -> None:
        assert self._port_fd is not None
        os.pwrite(self._port_fd, bytes([val & 0xFF]), port)

    def read_pci_u16(self, offset: int) -> int:
        assert self._pci_fd is not None
        return struct.unpack("<H", os.pread(self._pci_fd, 2, offset))[0]

    def write_pci_u16(self, offset: int, val: int) -> None:
        assert self._pci_fd is not None
        os.pwrite(self._pci_fd, struct.pack("<H", val & 0xFFFF), offset)


class IteFollowModeClient:
    """Exact IT8390E-256 PM1 0xDC Follow-Mode transport matching Razer_EC_Flash_v1.03 + EC v01.00 0x0259a."""

    def __init__(self, io: LinuxPortBackend, sleep_fn=time.sleep) -> None:
        self.io = io
        self.sleep = sleep_fn
        self._saved_pci_82: int | None = None
        self._saved_ldn: int | None = None
        self._pm1_remapped = False
        self._in_idle = False
        self._kbc_disabled = False
        self._flash_dirty = False

    def read_sio(self, reg: int) -> int:
        self.io.outb(SIO_IDX, reg)
        return self.io.inb(SIO_DAT)

    def write_sio(self, reg: int, val: int) -> None:
        self.io.outb(SIO_IDX, reg)
        self.io.outb(SIO_DAT, val)

    def read_mbox(self, idx: int) -> int:
        self.io.outb(MBOX_IDX, idx)
        return self.io.inb(MBOX_DAT)

    def check_identity(self) -> dict:
        chip_id = (self.read_sio(0x20) << 8) | self.read_sio(0x21)
        reg22 = self.read_sio(0x22)
        ver_bytes = [self.read_mbox(i) for i in (6, 7, 8, 9)]
        ver_str = f"{chr(ver_bytes[0])}{chr(ver_bytes[1])}.{chr(ver_bytes[2])}{chr(ver_bytes[3])}" if all(
            32 <= b < 127 for b in ver_bytes
        ) else str(ver_bytes)
        ssid = (self.read_mbox(12) << 8) | self.read_mbox(11)
        pci_82 = self.io.read_pci_u16(0x82)

        saved_ldn = self.read_sio(0x07)
        try:
            self.write_sio(0x07, 0x11)
            ldn11_act = self.read_sio(0x30)
            ldn11_bar0 = (self.read_sio(0x60) << 8) | self.read_sio(0x61)
            ldn11_bar1 = (self.read_sio(0x62) << 8) | self.read_sio(0x63)
        finally:
            self.write_sio(0x07, saved_ldn)

        return {
            "chip_id": chip_id,
            "reg22": reg22,
            "ec_version": ver_str,
            "ssid": ssid,
            "pci_0_31_0_reg82": pci_82,
            "ldn11_active": ldn11_act,
            "ldn11_bar0": ldn11_bar0,
            "ldn11_bar1": ldn11_bar1,
        }

    def require_rz09_0328(self) -> dict:
        info = self.check_identity()
        if info["chip_id"] != EXPECTED_CHIP_ID or info["reg22"] != EXPECTED_REG22:
            raise FlashError(
                f"Unexpected EC chip: ID=0x{info['chip_id']:04X}, reg22=0x{info['reg22']:02X} "
                f"(expected 0x{EXPECTED_CHIP_ID:04X} / 0x{EXPECTED_REG22:02X})"
            )
        if info["ssid"] != EXPECTED_SSID:
            raise FlashError(f"Board SSID mismatch: 0x{info['ssid']:04X} != 0x{EXPECTED_SSID:04X} (RZ09-0328)")
        return info

    def _wait_pm1_obe(self, timeout_s: float = 2.0) -> None:
        deadline = time.monotonic() + timeout_s
        while self.io.inb(PM1_CMD) & 0x01:
            _ = self.io.inb(PM1_DATA)
            if time.monotonic() > deadline:
                raise FlashError("Timeout waiting for PM1 OBE (0x206 bit 0 clear)")

    def _wait_pm1_ibe(self, timeout_s: float = 2.0) -> None:
        deadline = time.monotonic() + timeout_s
        while self.io.inb(PM1_CMD) & 0x02:
            if time.monotonic() > deadline:
                raise FlashError("Timeout waiting for PM1 IBE (0x206 bit 1 clear)")

    def _wait_pm1_obf(self, timeout_s: float = 2.0) -> None:
        deadline = time.monotonic() + timeout_s
        while (self.io.inb(PM1_CMD) & 0x01) == 0:
            if time.monotonic() > deadline:
                raise FlashError("Timeout waiting for PM1 OBF (0x206 bit 0 set)")

    def send_pm1_cmd(self, cmd: int) -> None:
        self._wait_pm1_obe()
        self._wait_pm1_ibe()
        self.io.outb(PM1_CMD, cmd)
        self._wait_pm1_ibe()

    def read_pm1_data(self) -> int:
        self._wait_pm1_obf()
        return self.io.inb(PM1_DATA)

    def enter_flash_mode(self, already_in_follow_mode: bool = False) -> tuple[int, int, int]:
        """Remap LDN 0x11 PM1 to 0x202/0x206, enter PM1 0xDC RAM-idle loop, and verify SPI ID == FF FF FE."""
        if already_in_follow_mode:
            self._pm1_remapped = True
            self._in_idle = True
            spi_id = self.read_spi_id()
            if spi_id != EXPECTED_SPI_ID:
                raise FlashError(f"Expected active Follow Mode SPI ID {EXPECTED_SPI_ID}, got {spi_id}")
            return spi_id

        # Disable PS/2 keyboard & aux interrupts (matches Razer_EC_Flash DisableIrq1/12)
        self.io.outb(KBC_CMD, 0xAD)
        self.io.outb(KBC_CMD, 0xA7)
        self._kbc_disabled = True

        # Enable 0x200..0x207 LPC decode (GAMEL_LPC_EN bit 9 = 0x0200) while preserving existing decodes
        self._saved_pci_82 = self.io.read_pci_u16(0x82)
        self.io.write_pci_u16(0x82, self._saved_pci_82 | 0x0200)

        # Configure Super I/O LDN 0x0F (SMFI) and remap LDN 0x11 (PM1) from 0x62/0x66 -> 0x202/0x206
        self._saved_ldn = self.read_sio(0x07)
        self.write_sio(0x07, 0x0F)
        self.write_sio(0x60, 0x00)
        self.write_sio(0x61, 0x00)
        self.write_sio(0x30, 0x01)
        self.write_sio(0x23, 0x01)

        self.write_sio(0x07, 0x11)
        self.write_sio(0x30, 0x00)
        self.write_sio(0x60, 0x02)
        self.write_sio(0x61, 0x02)
        self.write_sio(0x62, 0x02)
        self.write_sio(0x63, 0x06)
        self.write_sio(0x30, 0x01)
        self._pm1_remapped = True

        # Enter EC RAM-resident flash-idle loop (0x17192 -> 0x1728c -> 0x0259a, expects ACK 0x33)
        self.send_pm1_cmd(0xDC)
        deadline = time.monotonic() + 3.0
        while True:
            ack = self.read_pm1_data()
            if ack == 0x33:
                break
            if time.monotonic() > deadline:
                raise FlashError(f"Did not receive 0x33 ACK from PM1 0xDC (last byte 0x{ack:02X})")
        self._in_idle = True
        self.sleep(0.2)

        spi_id = self.read_spi_id()
        if spi_id != EXPECTED_SPI_ID:
            raise FlashError(
                f"Unexpected SPI ID {[hex(x) for x in spi_id]} != {[hex(x) for x in EXPECTED_SPI_ID]} "
                "(this script targets direct internal eFlash FF FF FE; boards wired with an external mirror SPI "
                "such as GD25Q64 should use the patched Razer_EC_Flash_v1.03_safe.exe with 0x0008D=0xFF)."
            )
        return spi_id

    def exit_flash_mode(self) -> None:
        """Exit Follow Mode + PM1 0xFC warm restart and restore LDN 0x11 (0x62/0x66) + PCI reg 0x82.

        Safety: if flash erase started and full 256 KiB verification has NOT succeeded yet
        (_flash_dirty is True), refuses to send 0xFC so the EC remains alive in RAM-resident
        Follow Mode on 0x202/0x206 for immediate recovery via --already-in-follow-mode.
        """
        if self._flash_dirty:
            print(
                "\n[SAFETY] Refusing to send PM1 0xFC because flash verification did not pass! "
                "EC remains in RAM-resident Follow Mode on 0x202/0x206.\n"
                "DO NOT REBOOT OR POWER OFF! Re-run with --already-in-follow-mode to retry or flash stock."
            )
            return
        try:
            if self._in_idle:
                self.enter_follow()
                self.exit_follow()
                self.send_pm1_cmd(0xFC)
                self._in_idle = False
                self.sleep(0.2)
        finally:
            if self._pm1_remapped:
                self.write_sio(0x07, 0x11)
                self.write_sio(0x30, 0x00)
                self.write_sio(0x60, 0x00)
                self.write_sio(0x61, 0x62)
                self.write_sio(0x62, 0x00)
                self.write_sio(0x63, 0x66)
                self.write_sio(0x30, 0x01)
                self._pm1_remapped = False
            if self._saved_ldn is not None:
                self.write_sio(0x07, self._saved_ldn)
                self._saved_ldn = None
            if self._saved_pci_82 is not None:
                self.io.write_pci_u16(0x82, self._saved_pci_82)
                self._saved_pci_82 = None
            if self._kbc_disabled:
                self.io.outb(KBC_CMD, 0xAE)
                self.io.outb(KBC_CMD, 0xA8)
                self._kbc_disabled = False

    def enter_follow(self) -> None:
        self.send_pm1_cmd(0x01)

    def exit_follow(self) -> None:
        self.send_pm1_cmd(0x05)

    def send_spi_cmd(self, cmd: int) -> None:
        self.send_pm1_cmd(0x02)
        self.send_pm1_cmd(cmd)

    def send_spi_byte(self, val: int) -> None:
        self.send_pm1_cmd(0x03)
        self.send_pm1_cmd(val)

    def read_spi_byte(self) -> int:
        self.send_pm1_cmd(0x04)
        return self.read_pm1_data()

    def spi_wait_free(self, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            self.enter_follow()
            self.send_spi_cmd(0x05)
            st = self.read_spi_byte()
            if (st & 0x01) == 0:
                break
            if time.monotonic() > deadline:
                raise FlashError(f"Timeout waiting for eFlash WIP clear (status=0x{st:02X})")
        self.enter_follow()

    def read_spi_id(self) -> tuple[int, int, int]:
        self.enter_follow()
        self.send_spi_cmd(0x9F)
        b0 = self.read_spi_byte()
        b1 = self.read_spi_byte()
        b2 = self.read_spi_byte()
        self.enter_follow()
        return (b0, b1, b2)

    def read_eflash(self, start: int = 0, length: int = ROM_SIZE, progress: bool = False) -> bytes:
        self.enter_follow()
        self.send_spi_cmd(0x0B)
        self.send_spi_byte((start >> 16) & 0xFF)
        self.send_spi_byte((start >> 8) & 0xFF)
        self.send_spi_byte(start & 0xFF)
        self.send_spi_byte(0x00)  # Fast Read dummy byte
        out = bytearray(length)
        for i in range(length):
            out[i] = self.read_spi_byte()
            if progress and (i & 0x3FFF) == 0:
                pct = (i * 100) // length
                print(f"\rRead/Verify ...... {pct:3d}%", end="", flush=True)
        if progress:
            print("\rRead/Verify ...... 100%")
        self.enter_follow()
        return bytes(out)

    def erase_sector_1k(self, addr: int) -> None:
        if addr % SECTOR_SIZE != 0 or not (0 <= addr < ROM_SIZE):
            raise ValueError(f"Bad 1 KiB sector address: 0x{addr:05X}")
        self.enter_follow()
        self.send_spi_cmd(0x06)  # WREN
        self.enter_follow()
        self.send_spi_cmd(0xD7)  # ITE eFlash 1 KiB Sector Erase
        self.send_spi_byte((addr >> 16) & 0xFF)
        self.send_spi_byte((addr >> 8) & 0xFF)
        self.send_spi_byte(addr & 0xFF)
        self.spi_wait_free()

    def program_block_aai(self, base: int, chunk: bytes) -> None:
        if len(chunk) == 0 or (len(chunk) % 2) != 0:
            raise ValueError(f"AAI word program requires even non-empty length, got {len(chunk)}")
        self.enter_follow()
        self.send_spi_cmd(0x06)  # WREN
        self.enter_follow()
        self.send_spi_cmd(0xAD)  # AAI Word Program
        self.send_spi_byte((base >> 16) & 0xFF)
        self.send_spi_byte((base >> 8) & 0xFF)
        self.send_spi_byte(base & 0xFF)
        last_off = len(chunk) - 2
        for off in range(0, len(chunk), 2):
            self.send_spi_byte(chunk[off])
            self.send_spi_byte(chunk[off + 1])
            self.spi_wait_free()
            self.enter_follow()
            if off != last_off:
                self.send_spi_cmd(0xAD)
        self.spi_wait_free()
        self.enter_follow()
        self.send_spi_cmd(0x04)  # WRDI
        self.spi_wait_free()

    def flash_and_verify(self, image: bytes, max_retries: int = 2, progress: bool = True) -> None:
        if len(image) != ROM_SIZE:
            raise ValueError(f"Image must be exactly {ROM_SIZE} bytes")
        if not has_valid_ite_sig(image):
            raise ValueError("Refusing to flash: ITE eFlash header signature at 0x00080 does not match!")

        # 1. Full 256 KiB sector erase (256 x 1 KiB, matches Razer_EC_Flash)
        self._flash_dirty = True
        for addr in range(0, ROM_SIZE, SECTOR_SIZE):
            self.erase_sector_1k(addr)
            if progress and (addr & 0xFFF) == 0:
                print(f"\rErase ............ {(addr * 100) // ROM_SIZE:3d}%", end="", flush=True)
        if progress:
            print("\rErase ............ 100%")

        # 2. Full 256 KiB AAI-word program (64 x 4 KiB blocks, matches Razer_EC_Flash)
        for base in range(0, ROM_SIZE, BLOCK_SIZE):
            self.program_block_aai(base, image[base : base + BLOCK_SIZE])
            if progress:
                print(f"\rProgram .......... {((base + BLOCK_SIZE) * 100) // ROM_SIZE:3d}%", end="", flush=True)
        if progress:
            print("\rProgram .......... 100%")

        # 3. Full 256 KiB byte-for-byte verify with in-Follow-Mode sector retry!
        #    Staying inside 0x0259a RAM-idle loop on mismatch avoids exiting into a broken image.
        for attempt in range(max_retries + 1):
            readback = self.read_eflash(0, ROM_SIZE, progress=progress)
            if readback == image:
                self._flash_dirty = False
                return
            bad_sectors = sorted({
                (i // SECTOR_SIZE) * SECTOR_SIZE
                for i in range(ROM_SIZE)
                if readback[i] != image[i]
            })
            if attempt >= max_retries:
                raise FlashError(
                    f"VERIFY FAILED after {max_retries + 1} attempts! Bad sectors: "
                    f"{[hex(s) for s in bad_sectors[:8]]}. DO NOT REBOOT; re-run flash while EC is in Follow Mode!"
                )
            if progress:
                print(f"\nVerify mismatch in {len(bad_sectors)} sector(s); retrying in Follow Mode (attempt {attempt + 2})...")
            for sec in bad_sectors:
                self.erase_sector_1k(sec)
                self.program_block_aai(sec, image[sec : sec + SECTOR_SIZE])


def check_ac_online() -> bool | None:
    ps_root = Path("/sys/class/power_supply")
    if not ps_root.exists():
        return None
    for child in ps_root.iterdir():
        typ = child / "type"
        onl = child / "online"
        try:
            if typ.exists() and typ.read_text().strip() == "Mains" and onl.exists():
                return onl.read_text().strip() == "1"
        except OSError:
            continue
    return None


class _MockIteEcBackend:
    """In-memory IT8390E-256 + 0x0259a Follow-Mode simulator for offline --self-test."""

    def __init__(self, initial_rom: bytes, inject_bad_byte_once: tuple[int, int] | None = None) -> None:
        self.flash = bytearray(initial_rom)
        self.inject_once = inject_bad_byte_once
        self.pci_82 = 0x3D0F
        self.sio_idx = 0
        self.sio_ldn = 0
        self.sio_global = {0x20: 0x83, 0x21: 0x90, 0x22: 0x82, 0x23: 0x01}
        self.sio_ldns = {
            0x0F: {0x30: 1, 0x60: 0, 0x61: 0},
            0x11: {0x30: 1, 0x60: 0x00, 0x61: 0x62, 0x62: 0x00, 0x63: 0x66},
        }
        self.mbox_idx = 0
        self.mbox = {6: ord("0"), 7: ord("1"), 8: ord("0"), 9: ord("0"), 11: 0x11, 12: 0x20}
        self.kbc_cmds: list[int] = []
        self.in_idle = False
        self.pm1_out: list[int] = []
        self.pm1_expect_data = False
        self.spi_cmd: int | None = None
        self.spi_buf: list[int] = []
        self.wren = False
        self.aai_addr: int | None = None
        self.read_ptr = 0
        self.read_queue: list[int] = []

    def read_pci_u16(self, offset: int) -> int:
        assert offset == 0x82
        return self.pci_82

    def write_pci_u16(self, offset: int, val: int) -> None:
        assert offset == 0x82
        self.pci_82 = val & 0xFFFF

    def inb(self, port: int) -> int:
        if port == SIO_DAT:
            if self.sio_idx == 0x07:
                return self.sio_ldn
            if self.sio_idx < 0x30:
                return self.sio_global.get(self.sio_idx, 0)
            return self.sio_ldns.get(self.sio_ldn, {}).get(self.sio_idx, 0)
        if port == MBOX_DAT:
            return self.mbox.get(self.mbox_idx, 0)
        if port == PM1_CMD:
            assert (self.pci_82 & 0x0200) != 0 and self.sio_ldns[0x11][0x62] == 0x02 and self.sio_ldns[0x11][0x63] == 0x06
            return 0x01 if self.pm1_out else 0x00
        if port == PM1_DATA:
            return self.pm1_out.pop(0)
        raise AssertionError(f"Unexpected inb(0x{port:X})")

    def outb(self, port: int, val: int) -> None:
        val &= 0xFF
        if port == KBC_CMD:
            self.kbc_cmds.append(val)
            return
        if port == SIO_IDX:
            self.sio_idx = val
            return
        if port == SIO_DAT:
            if self.sio_idx == 0x07:
                self.sio_ldn = val
            elif self.sio_idx < 0x30:
                self.sio_global[self.sio_idx] = val
            else:
                self.sio_ldns.setdefault(self.sio_ldn, {})[self.sio_idx] = val
            return
        if port == MBOX_IDX:
            self.mbox_idx = val
            return
        if port == PM1_CMD:
            assert (self.pci_82 & 0x0200) != 0 and self.sio_ldns[0x11][0x62] == 0x02 and self.sio_ldns[0x11][0x63] == 0x06
            if not self.in_idle:
                if val == 0xDC:
                    self.in_idle = True
                    self.pm1_out.append(0x33)
                    return
                raise AssertionError(f"Unexpected PM1 cmd before 0xDC: 0x{val:02X}")
            if self.pm1_expect_data:
                self.pm1_expect_data = False
                self._on_spi_tx(val)
                return
            if val == 0x01:
                self.spi_cmd = None
                self.spi_buf.clear()
                self.read_queue.clear()
            elif val == 0x02:
                self.spi_cmd = None
                self.spi_buf.clear()
                self.pm1_expect_data = True
            elif val == 0x03:
                self.pm1_expect_data = True
            elif val == 0x04:
                self.pm1_out.append(self._on_spi_rx())
            elif val == 0x05:
                self.spi_cmd = None
            elif val == 0xFC:
                self.in_idle = False
            else:
                raise AssertionError(f"Unexpected Follow-Mode PM1 cmd: 0x{val:02X}")
            return
        raise AssertionError(f"Unexpected outb(0x{port:X}, 0x{val:02X})")

    def _on_spi_tx(self, b: int) -> None:
        if self.spi_cmd is None:
            self.spi_cmd = b
            self.spi_buf.clear()
            if b == 0x06:
                self.wren = True
            elif b == 0x04:
                self.wren = False
                self.aai_addr = None
            elif b == 0x9F:
                self.read_queue = list(EXPECTED_SPI_ID)
            elif b == 0x05:
                self.read_queue = [0x00]
            return
        self.spi_buf.append(b)
        if self.spi_cmd == 0xD7 and len(self.spi_buf) == 3:
            assert self.wren, "0xD7 without WREN"
            addr = (self.spi_buf[0] << 16) | (self.spi_buf[1] << 8) | self.spi_buf[2]
            self.flash[addr : addr + SECTOR_SIZE] = b"\xff" * SECTOR_SIZE
            self.wren = False
        elif self.spi_cmd == 0xAD:
            if self.aai_addr is None and len(self.spi_buf) == 5:
                assert self.wren, "0xAD without WREN"
                addr = (self.spi_buf[0] << 16) | (self.spi_buf[1] << 8) | self.spi_buf[2]
                self._prog_pair(addr, self.spi_buf[3], self.spi_buf[4])
                self.aai_addr = addr + 2
            elif self.aai_addr is not None and len(self.spi_buf) == 2:
                assert self.wren, "0xAD cont without WREN"
                self._prog_pair(self.aai_addr, self.spi_buf[0], self.spi_buf[1])
                self.aai_addr += 2
        elif self.spi_cmd == 0x0B and len(self.spi_buf) == 4:
            self.read_ptr = (self.spi_buf[0] << 16) | (self.spi_buf[1] << 8) | self.spi_buf[2]

    def _prog_pair(self, addr: int, b0: int, b1: int) -> None:
        # Real flash programming can only clear bits (AND)
        self.flash[addr] &= b0
        self.flash[addr + 1] &= b1
        if self.inject_once is not None and addr <= self.inject_once[0] <= addr + 1:
            idx, bad_val = self.inject_once
            self.flash[idx] = bad_val
            self.inject_once = None

    def _on_spi_rx(self) -> int:
        if self.read_queue:
            return self.read_queue.pop(0)
        if self.spi_cmd == 0x0B:
            val = self.flash[self.read_ptr % ROM_SIZE]
            self.read_ptr += 1
            return val
        return 0xFF


def self_test() -> None:
    repo = Path(__file__).resolve().parent.parent
    stock_path = repo / "firmware" / "rz09-0328-ec-v01.00" / "ec-rom-00000-3ffff.bin"
    mod_path = repo / "firmware" / "rz09-0328-ec-v01.00-mod-fan-0-5500" / "rz09-0328-ec-v01.00-fan-0-5500.bin"
    if stock_path.is_file():
        stock = stock_path.read_bytes()
        target = mod_path.read_bytes() if mod_path.is_file() else stock
    else:
        # Synthesize deterministic 256 KiB test images when proprietary ROMs are not present
        buf_stock = bytearray((i * 37 + 13) & 0xFF for i in range(ROM_SIZE))
        buf_stock[0x80:0x90] = ITE_HEADER_SIG
        buf_stock[0x12120] = 0x02
        buf_target = bytearray(buf_stock)
        buf_target[0x80:0x90] = ITE_HEADER_SIG_MIRROR
        buf_target[0x12120] = 0x03
        stock = bytes(buf_stock)
        target = bytes(buf_target)

    # Inject a simulated 1-byte bitflip at 0x12120 during first program pass to prove in-Follow-Mode retry works!
    mock = _MockIteEcBackend(stock, inject_bad_byte_once=(0x12120, 0x00))
    client = IteFollowModeClient(mock, sleep_fn=lambda _s: None)
    info = client.require_rz09_0328()
    assert info["ssid"] == EXPECTED_SSID and info["chip_id"] == EXPECTED_CHIP_ID

    spi_id = client.enter_flash_mode()
    assert spi_id == EXPECTED_SPI_ID
    assert client.read_eflash(0, ROM_SIZE, progress=False) == stock
    client.flash_and_verify(target, max_retries=2, progress=False)
    assert bytes(mock.flash) == target
    client.exit_flash_mode()

    # Verify full hardware state restoration
    assert mock.pci_82 == 0x3D0F
    assert mock.sio_ldns[0x11][0x60] == 0x00 and mock.sio_ldns[0x11][0x61] == 0x62
    assert mock.sio_ldns[0x11][0x62] == 0x00 and mock.sio_ldns[0x11][0x63] == 0x66
    assert mock.in_idle is False
    assert mock.kbc_cmds == [0xAD, 0xA7, 0xAE, 0xA8]

    # Verify fail-safe: if verify fails with max_retries=0, exit_flash_mode() REFUSES to send 0xFC,
    # keeping EC alive in Follow Mode so --already-in-follow-mode recovery can re-flash stock cleanly!
    mock2 = _MockIteEcBackend(stock, inject_bad_byte_once=(0x12120, 0x00))
    c2 = IteFollowModeClient(mock2, sleep_fn=lambda _s: None)
    c2.enter_flash_mode()
    failed = False
    try:
        c2.flash_and_verify(target, max_retries=0, progress=False)
    except FlashError:
        failed = True
    finally:
        c2.exit_flash_mode()
    assert failed and mock2.in_idle is True, "Must keep EC in Follow Mode when verify fails!"
    # Now recover using already_in_follow_mode=True and flash stock cleanly
    c3 = IteFollowModeClient(mock2, sleep_fn=lambda _s: None)
    c3.enter_flash_mode(already_in_follow_mode=True)
    c3.flash_and_verify(stock, max_retries=0, progress=False)
    c3.exit_flash_mode()
    assert mock2.in_idle is False and bytes(mock2.flash) == stock
    print(
        "Self-test passed: IT8390E PM1 0xDC Follow-Mode protocol, 0xD7 1 KiB erase, 0xAD AAI program, "
        "0x0B 256 KiB verify, in-Follow-Mode retry, fail-safe no-0xFC guard + recovery, and 0x62/0x66 restoration verified. "
        "No hardware accessed."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--self-test", action="store_true", help="Run offline mock-EC self-test (no hardware accessed)")
    group.add_argument("--check-only", action="store_true", help="Read-only Super I/O, SSID, and PCI check on Linux")
    group.add_argument("--verify-only", action="store_true", help="Read-only PM1 0xDC Follow-Mode 256 KiB readback & SHA-256 verify")
    group.add_argument("--flash", type=Path, help="Path to 256 KiB EC ROM image to flash (requires --i-accept-brick-risk)")
    parser.add_argument("--stock", type=Path, help="Expected 256 KiB ROM for --verify-only comparison")
    parser.add_argument("--i-accept-brick-risk", action="store_true", help="Explicit confirmation required for --flash")
    parser.add_argument("--already-in-follow-mode", action="store_true", help="Recovery mode: EC is already in Follow Mode on 0x202/0x206")
    parser.add_argument("--allow-battery", action="store_true", help="Override AC-adapter check (NOT recommended)")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    io = LinuxPortBackend()
    io.open()
    client = IteFollowModeClient(io)
    try:
        if not args.already_in_follow_mode:
            info = client.require_rz09_0328()
            print(
                f"Board verified: Chip=0x{info['chip_id']:04X} (reg22=0x{info['reg22']:02X}), "
                f"EC={info['ec_version']}, SSID=0x{info['ssid']:04X}, "
                f"PCI 0:31:0[0x82]=0x{info['pci_0_31_0_reg82']:04X}, "
                f"LDN11=0x{info['ldn11_bar0']:04X}/0x{info['ldn11_bar1']:04X}"
            )
        if args.check_only:
            return 0

        if args.verify_only:
            if args.stock is None:
                parser.error("--verify-only requires --stock <path/to/ec-rom-00000-3ffff.bin>")
            expected = args.stock.read_bytes()
            if len(expected) != ROM_SIZE:
                raise FlashError(f"Expected {ROM_SIZE}-byte stock file, got {len(expected)}")
            try:
                spi_id = client.enter_flash_mode(already_in_follow_mode=args.already_in_follow_mode)
                print(f"Entered PM1 0xDC Follow Mode cleanly! SPI ID = {[hex(x) for x in spi_id]}")
                live = client.read_eflash(0, ROM_SIZE, progress=True)
            finally:
                client.exit_flash_mode()
            live_sha = hashlib.sha256(live).hexdigest()
            exp_sha = hashlib.sha256(expected).hexdigest()
            print(f"Live eFlash SHA-256 : {live_sha}")
            print(f"File        SHA-256 : {exp_sha}")
            if live_sha != exp_sha:
                raise FlashError("Readback SHA-256 does NOT match --stock file!")
            print("VERIFY-ONLY PASSED 100%: PM1 0xDC Follow Mode transport works cleanly on this board.")
            return 0

        if args.flash is not None:
            if not args.i_accept_brick_risk:
                raise FlashError("Refusing to flash without --i-accept-brick-risk.")
            ac = check_ac_online()
            if ac is False and not args.allow_battery:
                raise FlashError("AC adapter not detected online. Plug in Razer AC power before flashing.")
            image = args.flash.read_bytes()
            if not has_valid_ite_sig(image):
                raise FlashError("Invalid 256 KiB IT8390E ROM image or missing 0x00080 signature.")
            print(f"Flashing {args.flash} (SHA-256: {hashlib.sha256(image).hexdigest()})...")
            try:
                spi_id = client.enter_flash_mode(already_in_follow_mode=args.already_in_follow_mode)
                print(f"SPI ID verified: {[hex(x) for x in spi_id]}")
                client.flash_and_verify(image, max_retries=2, progress=True)
            finally:
                client.exit_flash_mode()
            print("FLASH & 256 KiB VERIFY SUCCEEDED 100%! Power off / reboot now to activate the new EC image.")
            return 0
    finally:
        io.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
