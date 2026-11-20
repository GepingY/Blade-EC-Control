"""Read-only Blade 1532:0255 HID queries using Windows built-in APIs.

No driver/package is installed. HidD_SetFeature sends only the explicit query
allowlist below; this tool cannot send setting commands or arbitrary packets.
Transport/layout reference (retrieved 2026-11-17):
https://github.com/jdsgnrinfo/blade-control/tree/main/src/BladeControl.Core/Hardware
The matching three source files are saved under diagnostics/hid/sources.
Unlike that reference, validate reply checksums and preserve mismatched replies.
Reply data_size is recorded, never required to equal request data_size.
"""

from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime
from functools import reduce
import json
from pathlib import Path
import sys
import time

VID, PID = 0x1532, 0x0255
KNOWN_BLADE_PIDS: tuple[int, ...] = (
    0x0255,  # Blade 15 Base (Early 2020, RZ09-0328)
    0x0253,  # Blade 15 Advanced (Early 2020, RZ09-0330)
    0x0233, 0x023A, 0x023B, 0x0240, 0x0246, 0x024A, 0x024C, 0x024D,
    0x0252, 0x0256, 0x0259, 0x025A,
    0x026D, 0x026E, 0x026F, 0x0270, 0x0276, 0x0279, 0x027A,
    0x028A, 0x028B, 0x028C, 0x028D, 0x028E,
    0x029D, 0x029E, 0x029F, 0x02A0, 0x02A2, 0x02A4,
    0x02B8, 0x02B9, 0x02BA,
)


def parse_pid_from_path(path: str) -> int | None:
    """Extract 16-bit USB PID from a Windows HID device path (e.g. '...vid_1532&pid_0255...')."""
    low = path.lower()
    idx = low.find("pid_")
    if idx < 0 or idx + 8 > len(low):
        return None
    try:
        return int(low[idx + 4 : idx + 8], 16)
    except ValueError:
        return None
TRANSACTION = 0x1F
MIN_REPORT = 91
QUERIES = (
    ("endpoint_device_mode", 0x0084, (0, 0)),
    ("fan_mode_zone_1", 0x0D82, (0, 1, 0, 0)),
    ("fan_mode_zone_2", 0x0D82, (0, 2, 0, 0)),
    ("fan_stored_target_zone_1", 0x0D81, (0, 1, 0)),
    ("fan_stored_target_zone_2", 0x0D81, (0, 2, 0)),
    ("ec_temperature_request_length_1", 0x0D85, (0,)),
    ("ec_temperature_request_length_3", 0x0D85, (0, 0, 0)),
    ("controller_firmware", 0x0081, (0, 0)),
)
ALLOWLIST = frozenset((cmd, args) for _, cmd, args in QUERIES)


def checksum(report: bytes | bytearray) -> int:
    return reduce(int.__xor__, report[3:89], 0)


def request(command: int, arguments: tuple[int, ...], length: int) -> bytes:
    if (command, arguments) not in ALLOWLIST:
        raise ValueError("Only the fixed read-only command/argument allowlist is allowed")
    if not MIN_REPORT <= length <= 4096:
        raise ValueError("Unexpected HID feature report length")
    result = bytearray(length)
    result[2] = TRANSACTION
    result[6] = len(arguments)
    result[7:9] = command.to_bytes(2, "big")
    result[9:9 + len(arguments)] = bytes(arguments)
    result[89] = checksum(result)
    return bytes(result)


def describe_reply(raw: bytes, command: int, sent: bytes) -> dict:
    if len(raw) < MIN_REPORT:
        return {"raw_hex": raw.hex(" "), "valid": False, "reason": "short response"}
    result = {
        "raw_hex": raw.hex(" "),
        "report_id": raw[0],
        "status": raw[1],
        "transaction_id": raw[2],
        "remaining_packets": int.from_bytes(raw[3:5], "big"),
        "protocol_type": raw[5],
        "data_size": raw[6],
        "command": f"0x{int.from_bytes(raw[7:9], 'big'):04x}",
        "args": list(raw[9:9 + min(raw[6], 80)]),
        "all_argument_bytes": list(raw[9:89]),
        "checksum_received": raw[89],
        "checksum_calculated": checksum(raw),
        "reserved": raw[90],
        "data_size_matches_request": raw[6] == sent[6],
    }
    result["checksum_valid"] = result["checksum_received"] == result["checksum_calculated"]
    result["command_matches"] = int.from_bytes(raw[7:9], "big") == command
    result["transaction_matches"] = raw[2] == TRANSACTION
    result["valid_envelope"] = (
        raw[0] == 0 and raw[2] == TRANSACTION and raw[6] <= 80
        and result["command_matches"] and result["checksum_valid"]
    )
    result["valid"] = result["valid_envelope"] and raw[1] == 2
    return result


class GUID(C.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", C.c_ubyte * 8)]


class InterfaceData(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("InterfaceClassGuid", GUID), ("Flags", W.DWORD), ("Reserved", C.c_size_t)]


class HidCaps(C.Structure):
    _fields_ = [(name, W.USHORT) for name in (
        "Usage", "UsagePage", "InputReportByteLength", "OutputReportByteLength", "FeatureReportByteLength"
    )] + [("Reserved", W.USHORT * 17)] + [(name, W.USHORT) for name in (
        "NumberLinkCollectionNodes", "NumberInputButtonCaps", "NumberInputValueCaps", "NumberInputDataIndices",
        "NumberOutputButtonCaps", "NumberOutputValueCaps", "NumberOutputDataIndices",
        "NumberFeatureButtonCaps", "NumberFeatureValueCaps", "NumberFeatureDataIndices"
    )]


class NativeHid:
    def __init__(self):
        self.hid = C.WinDLL("hid.dll", use_last_error=True)
        self.setup = C.WinDLL("setupapi.dll", use_last_error=True)
        self.kernel = C.WinDLL("kernel32.dll", use_last_error=True)
        signatures = (
            (self.hid, "HidD_GetHidGuid", None, [C.POINTER(GUID)]),
            (self.hid, "HidD_GetPreparsedData", C.c_ubyte, [W.HANDLE, C.POINTER(C.c_void_p)]),
            (self.hid, "HidD_FreePreparsedData", C.c_ubyte, [C.c_void_p]),
            (self.hid, "HidP_GetCaps", C.c_long, [C.c_void_p, C.POINTER(HidCaps)]),
            (self.hid, "HidD_SetFeature", C.c_ubyte, [W.HANDLE, C.c_void_p, W.ULONG]),
            (self.hid, "HidD_GetFeature", C.c_ubyte, [W.HANDLE, C.c_void_p, W.ULONG]),
            (self.hid, "HidD_GetProductString", C.c_ubyte, [W.HANDLE, C.c_void_p, W.ULONG]),
            (self.setup, "SetupDiGetClassDevsW", W.HANDLE, [C.POINTER(GUID), W.LPCWSTR, W.HWND, W.DWORD]),
            (self.setup, "SetupDiEnumDeviceInterfaces", W.BOOL, [W.HANDLE, C.c_void_p, C.POINTER(GUID), W.DWORD, C.POINTER(InterfaceData)]),
            (self.setup, "SetupDiGetDeviceInterfaceDetailW", W.BOOL, [W.HANDLE, C.POINTER(InterfaceData), C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]),
            (self.setup, "SetupDiDestroyDeviceInfoList", W.BOOL, [W.HANDLE]),
            (self.kernel, "CreateFileW", W.HANDLE, [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]),
            (self.kernel, "CloseHandle", W.BOOL, [W.HANDLE]),
        )
        for dll, name, restype, argtypes in signatures:
            function = getattr(dll, name)
            function.restype, function.argtypes = restype, argtypes
        assert C.sizeof(HidCaps) == 64
        assert C.sizeof(InterfaceData) == (32 if C.sizeof(C.c_void_p) == 8 else 28)

    @staticmethod
    def error() -> dict:
        code = C.get_last_error()
        return {"winerror": code, "message": C.FormatError(code).strip()}

    def paths(self, vid: int = VID, pid: int | None = PID) -> list[str]:
        guid = GUID()
        self.hid.HidD_GetHidGuid(C.byref(guid))
        devices = self.setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x12)
        if devices in (None, C.c_void_p(-1).value):
            raise C.WinError(C.get_last_error())
        paths = []
        vid_tag = f"vid_{vid:04x}"
        pid_tag = f"pid_{pid:04x}" if pid is not None else None
        try:
            index = 0
            while True:
                data = InterfaceData()
                data.cbSize = C.sizeof(data)
                if not self.setup.SetupDiEnumDeviceInterfaces(devices, None, C.byref(guid), index, C.byref(data)):
                    if C.get_last_error() != 259:
                        raise C.WinError(C.get_last_error())
                    break
                index += 1
                required = W.DWORD()
                self.setup.SetupDiGetDeviceInterfaceDetailW(devices, C.byref(data), None, 0, C.byref(required), None)
                if not required.value:
                    continue
                detail = C.create_string_buffer(required.value)
                C.cast(detail, C.POINTER(W.DWORD))[0] = 8 if C.sizeof(C.c_void_p) == 8 else 6
                if self.setup.SetupDiGetDeviceInterfaceDetailW(devices, C.byref(data), detail, required, C.byref(required), None):
                    path = C.wstring_at(C.addressof(detail) + 4)
                    low = path.lower()
                    if vid_tag in low and (pid_tag is None or pid_tag in low):
                        paths.append(path)
        finally:
            self.setup.SetupDiDestroyDeviceInfoList(devices)
        return paths

    def product_string(self, handle) -> str:
        """Read UTF-16 USB product string for an open HID handle, or empty string if unavailable."""
        buf = C.create_unicode_buffer(128)
        if not self.hid.HidD_GetProductString(handle, buf, C.sizeof(buf)):
            return ""
        return buf.value.strip()

    def open(self, path: str):
        handle = self.kernel.CreateFileW(path, 0, 3, None, 3, 0, None)
        if handle in (None, C.c_void_p(-1).value):
            raise C.WinError(C.get_last_error())
        return handle

    def caps(self, handle) -> dict:
        preparsed = C.c_void_p()
        if not self.hid.HidD_GetPreparsedData(handle, C.byref(preparsed)):
            raise C.WinError(C.get_last_error())
        try:
            caps = HidCaps()
            status = self.hid.HidP_GetCaps(preparsed, C.byref(caps))
            if status != 0x00110000:
                raise OSError(f"HidP_GetCaps status 0x{status & 0xffffffff:08x}")
            return {key: int(getattr(caps, key)) for key in (
                "Usage", "UsagePage", "InputReportByteLength", "OutputReportByteLength", "FeatureReportByteLength"
            )}
        finally:
            self.hid.HidD_FreePreparsedData(preparsed)

    def exchange(self, handle, length: int, label: str, command: int, arguments: tuple[int, ...]) -> dict:
        sent = request(command, arguments, length)
        result = {"label": label, "command": f"0x{command:04x}", "request_args": list(arguments), "request_hex": sent.hex(" "), "attempts": []}
        # Match BladeControl's send/get delays and retry only known read queries.
        for attempt in range(1, 4):
            observation = {"attempt": attempt, "time": datetime.now().astimezone().isoformat()}
            outgoing = C.create_string_buffer(sent, length)
            time.sleep(0.001)
            if not self.hid.HidD_SetFeature(handle, outgoing, length):
                observation["send_error"] = self.error()
                result["attempts"].append(observation)
                break
            time.sleep(0.002)
            incoming = C.create_string_buffer(length)
            if not self.hid.HidD_GetFeature(handle, incoming, length):
                observation["receive_error"] = self.error()
                result["attempts"].append(observation)
                break
            decoded = describe_reply(incoming.raw, command, sent)
            observation["response"] = decoded
            result["attempts"].append(observation)
            if decoded["valid"] or (decoded.get("valid_envelope") and decoded["status"] == 5):
                break
            if attempt < 3:
                time.sleep(0.020 if decoded.get("status") == 1 else 0.500)
        last = result["attempts"][-1]
        result["success"] = bool(last.get("response", {}).get("valid"))
        if "response" in last:
            result["response_summary"] = {key: last["response"].get(key) for key in (
                "status", "data_size", "args", "checksum_valid", "command_matches", "transaction_matches", "data_size_matches_request"
            )}
        return result


def self_test():
    packet = request(0x0D82, (0, 1, 0, 0), 91)
    assert packet[89] == 0x8A  # Confirmed against prior hardware transcript.
    reply = bytearray(packet)
    reply[1] = 2
    reply[6] = 3  # A changed response length is legitimate and must be preserved.
    reply[89] = checksum(reply)
    assert describe_reply(bytes(reply), 0x0D82, packet)["valid"]
    reply[89] ^= 1
    assert not describe_reply(bytes(reply), 0x0D82, packet)["valid"]
    try:
        request(0x0D02, (0, 1, 0, 0), 91)
    except ValueError:
        pass
    else:
        raise AssertionError("Setting command escaped allowlist")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Validate packet framing without accessing HID")
    parser.add_argument("--pid", type=lambda s: int(s, 0), default=None, help="Razer Blade USB HID PID in hex (omit to scan all VID 0x1532 interfaces)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    self_test()
    if args.self_test:
        print("Packet framing, changed response size, checksum rejection and write-command guard passed.")
        return 0
    if sys.platform != "win32":
        parser.error("Native HID inspection requires Windows")
    output = args.output or Path(__file__).resolve().parent.parent / "diagnostics" / "hid" / f"inspection-{datetime.now():%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"created_at": datetime.now().astimezone().isoformat(), "read_only_query_allowlist": True,
              "vid": f"0x{VID:04x}", "pid": f"0x{args.pid:04x}" if args.pid is not None else "auto", "endpoints": []}

    def save():
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    hid = NativeHid()
    report["matching_paths"] = hid.paths(pid=args.pid)
    save()
    for path in report["matching_paths"]:
        endpoint = {"path": path, "queries": []}
        report["endpoints"].append(endpoint)
        handle = None
        try:
            handle = hid.open(path)
            endpoint["caps"] = caps = hid.caps(handle)
            length = caps["FeatureReportByteLength"]
            if length < MIN_REPORT:
                endpoint["skipped"] = "Feature report is shorter than the Razer packet"
                continue
            for label, command, arguments in QUERIES:
                query = hid.exchange(handle, length, label, command, arguments)
                endpoint["queries"].append(query)
                print(json.dumps({"endpoint": path, "label": label, "success": query["success"], "response": query.get("response_summary"), "attempts": len(query["attempts"])}), flush=True)
                save()
                if label == "endpoint_device_mode" and not query["success"]:
                    endpoint["skipped"] = "Known endpoint probe did not produce a valid success response"
                    break
        except OSError as error:
            endpoint["error"] = str(error)
        finally:
            if handle is not None:
                hid.kernel.CloseHandle(handle)
            save()
    print(f"Saved raw inspection: {output}", flush=True)
    return 0 if any(q["success"] for e in report["endpoints"] for q in e["queries"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
