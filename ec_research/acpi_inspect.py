"""Read-only Windows ACPI table inventory and AML keyword inspection.

Uses Kernel32 EnumSystemFirmwareTables/GetSystemFirmwareTable and read-only
Windows registry table-cache access: no driver, ACPI method evaluation, EC I/O,
firmware writes, or hardware changes.

Microsoft references:
https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-enumsystemfirmwaretables
https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemfirmwaretable

Windows enumerates duplicate table signatures but this API returns only the
first table for each signature. Keyword hits in AML are heuristic, not an AML
decompiler or proof that a method/device is exposed or callable.
"""

import argparse
import collections
import ctypes
from ctypes import wintypes
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import struct


PROVIDER = int.from_bytes(b"ACPI", "big")
TARGETS = [
    "FAN", "FAN0", "FAN1", "FAN2", "FAN3", "FAN4", "FAN_", "_FSL", "_FST", "_FPS",
    "_FIF", "_ON_", "_OFF", "_TMP", "_PSV", "_CRT", "EC__", "EC0_",
    "H_EC", "PNP0C09", "PNP0C0B", "RPM", "PWM", "SFAN", "GFAN",
    "AC0F", "AC1F", "FNCL", "PENV", "ECMD", "ETMD", "FANL", "FANH",
] + [f"_AC{i}" for i in range(10)] + [f"_AL{i}" for i in range(10)]


def read_sized(function, *args):
    size = function(*args, None, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size)
        copied = function(*args, buffer, size)
        if not copied:
            raise ctypes.WinError(ctypes.get_last_error())
        if copied <= size:
            return buffer.raw[:copied]
        size = copied
    raise RuntimeError("Firmware table size changed repeatedly")


def printable(data):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in data)


def header(data):
    if len(data) < 36:
        return {"error": "table shorter than ACPI description header"}
    sig, length, revision, checksum, oemid, tableid, oemrev, creator, creatorrev = (
        struct.unpack_from("<4sIBB6s8sIII", data)
    )
    return {
        "signature": sig.decode("ascii", "replace"),
        "declared_length": length,
        "actual_length": len(data),
        "revision": revision,
        "oem_id": oemid.decode("ascii", "replace").rstrip(),
        "oem_table_id": tableid.decode("ascii", "replace").rstrip(),
        "oem_revision": oemrev,
        "creator_id": creator.to_bytes(4, "little").decode("ascii", "replace"),
        "creator_revision": creatorrev,
        "checksum_valid": sum(data[:length]) % 256 == 0 if length <= len(data) else False,
    }


def inspect_aml(data):
    hits = {}
    for target in TARGETS:
        occurrences = []
        for match in re.finditer(re.escape(target.encode("ascii")), data):
            offset = match.start()
            occurrences.append({
                "offset": offset,
                "offset_hex": hex(offset),
                "context_ascii": printable(data[max(0, offset - 24):offset + len(target) + 40]),
                "context_hex": data[max(0, offset - 12):offset + len(target) + 20].hex(" "),
            })
        if occurrences:
            hits[target] = occurrences
    strings = [m.group().decode("ascii") for m in re.finditer(rb"[\x20-\x7e]{4,}", data)]
    related_strings = [s for s in strings if re.search("FAN|THERM|TEMP|COOL|RPM|PWM|PNP0C09|PNP0C0B", s, re.I)]
    return {
        "warning": "Raw byte keyword search only; hits can be references, declarations, or incidental bytes.",
        "hits": hits,
        "related_ascii_strings": related_strings,
    }


def registry_ssdts(output):
    """Read Windows' cached SSDT copies, including duplicate signatures.

    The registry layout is observational rather than a documented stable API;
    validate each retrieved binary as an SSDT and retain its source key/hash.
    Only SSD* keys are inspected, never licensing or unrelated registry data.
    """
    import winreg

    result = []

    def walk(path, label):
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ) as key:
            subkeys, values, _ = winreg.QueryInfoKey(key)
            for index in range(values):
                value_name, data, kind = winreg.EnumValue(key, index)
                if kind != winreg.REG_BINARY or not isinstance(data, bytes) or data[:4] != b"SSDT":
                    continue
                item = header(data)
                item["source"] = "Windows ACPI registry cache"
                item["registry_path"] = f"HKLM\\{path}\\{value_name}"
                item["sha256"] = hashlib.sha256(data).hexdigest()
                suffix = re.sub(r"[^A-Za-z0-9_-]", "_", item.get("oem_table_id", "table")).strip("_")
                dump_path = output / f"registry_{label}_{suffix}.aml"
                dump_path.write_bytes(data)
                item["dump_path"] = str(dump_path.resolve())
                item["analysis"] = inspect_aml(data)
                result.append(item)
            for index in range(subkeys):
                walk(path + "\\" + winreg.EnumKey(key, index), label)

    root = r"HARDWARE\ACPI"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root, 0, winreg.KEY_READ) as key:
        for index in range(winreg.QueryInfoKey(key)[0]):
            name = winreg.EnumKey(key, index)
            if re.fullmatch(r"SSD[0-9A-F]|SSDT", name, flags=re.I):
                walk(root + "\\" + name, name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent.parent / "diagnostics" / "acpi")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This inspector requires Windows")
    args.output.mkdir(parents=True, exist_ok=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    enum = kernel.EnumSystemFirmwareTables
    enum.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    enum.restype = wintypes.UINT
    get = kernel.GetSystemFirmwareTable
    get.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    get.restype = wintypes.UINT
    ids_raw = read_sized(enum, PROVIDER)
    if len(ids_raw) % 4:
        raise RuntimeError("Enumeration returned non-DWORD data")
    ids = [item[0] for item in struct.iter_unpack("<I", ids_raw)]
    counts = collections.Counter(ids)
    report = {
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "read_only": True,
        "api": "Kernel32 EnumSystemFirmwareTables/GetSystemFirmwareTable",
        "limits": [
            "Only the first table for each duplicate signature can be read through this API.",
            "AML keyword searches do not evaluate methods or establish that fan controls are callable.",
            "Kernel32 reads DSDT, first SSDT, and FACP; duplicate SSDTs are supplemented from Windows registry cache.",
            "The registry cache is an undocumented observational supplement, not a stable Windows table retrieval API.",
            "MSDM licensing data and unrelated firmware tables are not read.",
        ],
        "enumerated_count": len(ids),
        "inventory": [],
        "tables": [],
    }
    for table_id, count in counts.items():
        signature = table_id.to_bytes(4, "little").decode("ascii", "replace")
        report["inventory"].append({"signature": signature, "table_id_hex": hex(table_id), "instances": count})
    # DSDT may not be included in some systems' enumeration: explicitly request it.
    selected = [b"DSDT", b"SSDT", b"FACP"]
    for signature_bytes in selected:
        table_id = int.from_bytes(signature_bytes, "little")
        signature = signature_bytes.decode("ascii")
        item = {"requested_signature": signature, "enumerated_instances": counts[table_id]}
        try:
            data = read_sized(get, PROVIDER, table_id)
            item.update(header(data))
            item["sha256"] = hashlib.sha256(data).hexdigest()
            dump_path = args.output / f"{signature}.aml"
            dump_path.write_bytes(data)
            item["dump_path"] = str(dump_path.resolve())
            if signature in ("DSDT", "SSDT"):
                item["analysis"] = inspect_aml(data)
            report["tables"].append(item)
        except OSError as exc:
            item["error"] = str(exc)
            report["tables"].append(item)
    try:
        report["registry_ssdts"] = registry_ssdts(args.output)
    except OSError as exc:
        report["registry_error"] = str(exc)
    report_path = args.output / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = {
        "enumerated_count": len(ids),
        "inventory": report["inventory"],
        "tables": [{k: v for k, v in t.items() if k != "analysis"} | {
            "keyword_counts": {k: len(v) for k, v in t.get("analysis", {}).get("hits", {}).items()}
        } for t in report["tables"]],
        "report_path": str(report_path.resolve()),
        "registry_ssdts": [{k: v for k, v in t.items() if k != "analysis"} | {
            "keyword_counts": {k: len(v) for k, v in t.get("analysis", {}).get("hits", {}).items()}
        } for t in report.get("registry_ssdts", [])],
        "limits": report["limits"],
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
