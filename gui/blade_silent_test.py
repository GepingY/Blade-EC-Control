"""Bounded Silent-mode experiment for Blade 1532:0255; default is self-test.

Run only with --run. The only hardware writes allowed are 0x0D02
[1, zone, mode, 0], with zone 1/2 and mode Balanced(0)/Silent(5).
No RPM, CPU boost, power-plan, BIOS, EC-register or driver changes exist here.

Upstream HID protocol references:
https://github.com/jdsgnrinfo/blade-control/blob/main/src/BladeControl.Core/Services/PerformanceService.cs
https://github.com/jdsgnrinfo/blade-control/blob/main/src/BladeControl.Core/Models/PerformanceMode.cs
"""

from __future__ import annotations

import argparse
import ctypes as C
from datetime import datetime
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time

from blade_inspect import NativeHid, checksum, describe_reply, MIN_REPORT, TRANSACTION

HOLD_SECONDS = 120
POLL_SECONDS = 10
WATCHDOG_SECONDS = 140
CPU_LIMIT_C = 80
GPU_LIMIT_C = 75
READ_ALLOWLIST = frozenset({
    (0x0D82, (0, 1, 0, 0)),
    (0x0D82, (0, 2, 0, 0)),
    (0x0D85, (0, 0, 0)),
})
WRITE_ALLOWLIST = frozenset(
    (0x0D02, (1, zone, mode, 0))
    for zone in (1, 2) for mode in (0, 5)
)


def make_packet(command, arguments, length):
    arguments = tuple(arguments)
    if (command, arguments) not in READ_ALLOWLIST | WRITE_ALLOWLIST:
        raise ValueError("Command/arguments are outside this experiment's fixed allowlist")
    if not MIN_REPORT <= length <= 4096:
        raise ValueError("Unexpected feature-report length")
    data = bytearray(length)
    data[2] = TRANSACTION
    data[6] = len(arguments)
    data[7:9] = int(command).to_bytes(2, "big")
    data[9:9 + len(arguments)] = bytes(arguments)
    data[89] = checksum(data)
    return bytes(data)


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event, **fields):
        record = {"time": datetime.now().astimezone().isoformat(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
            stream.flush()
        if event != "exchange":
            print(json.dumps(record), flush=True)


class ExperimentHid:
    def __init__(self, journal, path=None):
        self.native = NativeHid()
        self.journal = journal
        self.handle = None
        self.length = None
        self.path = None
        paths = [path] if path else self.native.paths()
        for candidate in paths:
            if not candidate or "vid_1532" not in candidate.lower() or "pid_0255" not in candidate.lower():
                continue
            handle = None
            try:
                handle = self.native.open(candidate)
                caps = self.native.caps(handle)
                if caps["FeatureReportByteLength"] < MIN_REPORT:
                    continue
                self.handle, self.length, self.path = handle, caps["FeatureReportByteLength"], candidate
                handle = None
                self.journal.emit("endpoint_opened", path=candidate, caps=caps)
                return
            finally:
                if handle is not None:
                    self.native.kernel.CloseHandle(handle)
        raise RuntimeError("No 1532:0255 endpoint with a 91-byte feature report was found")

    def close(self):
        if self.handle is not None:
            self.native.kernel.CloseHandle(self.handle)
            self.handle = None

    def exchange(self, command, arguments, attempts=3):
        sent = make_packet(command, tuple(arguments), self.length)
        # A Silent write is sent once only. Recovery/query retries are bounded.
        if command == 0x0D02 and arguments[2] == 5:
            attempts = 1
        last = None
        for attempt in range(1, min(attempts, 3) + 1):
            outgoing = C.create_string_buffer(sent, len(sent))
            time.sleep(0.001)
            if not self.native.hid.HidD_SetFeature(self.handle, outgoing, len(sent)):
                error = self.native.error()
                self.journal.emit("exchange", command=f"0x{command:04x}", request_args=list(arguments),
                                  attempt=attempt, request_hex=sent.hex(" "), send_error=error)
                raise OSError(f"HID query/write send failed: {error}")
            time.sleep(0.002)
            incoming = C.create_string_buffer(self.length)
            if not self.native.hid.HidD_GetFeature(self.handle, incoming, self.length):
                error = self.native.error()
                self.journal.emit("exchange", command=f"0x{command:04x}", request_args=list(arguments),
                                  attempt=attempt, request_hex=sent.hex(" "), receive_error=error)
                raise OSError(f"HID reply failed: {error}")
            last = describe_reply(incoming.raw, command, sent)
            self.journal.emit("exchange", command=f"0x{command:04x}", request_args=list(arguments),
                              attempt=attempt, request_hex=sent.hex(" "), response=last)
            if last["valid"]:
                return last
            if last.get("valid_envelope") and last.get("status") == 5:
                raise RuntimeError(f"Controller does not support command 0x{command:04x}")
            if attempt < attempts:
                time.sleep(0.020 if last.get("status") == 1 else 0.500)
        raise RuntimeError(f"Command 0x{command:04x} had no matching checksum-valid success reply: {last}")

    def modes(self):
        result = []
        for zone in (1, 2):
            reply = self.exchange(0x0D82, (0, zone, 0, 0))
            raw_args = reply["all_argument_bytes"]
            if reply["data_size"] < 4 or raw_args[1] != zone:
                raise RuntimeError(f"Unexpected zone/length in mode reply for zone {zone}")
            result.append({"zone": zone, "mode": raw_args[2], "fan_flag": raw_args[3],
                           "response_valid": reply["valid"], "checksum_valid": reply["checksum_valid"]})
        return result

    def temperatures(self):
        reply = self.exchange(0x0D85, (0, 0, 0))
        raw_args = reply["all_argument_bytes"]
        cpu, gpu = raw_args[1:3]
        if reply["data_size"] < 3 or raw_args[0] != 2 or not (10 <= cpu <= 125 and 10 <= gpu <= 125):
            raise RuntimeError(f"EC temperature reply is unavailable/unrecognized: {raw_args[:4]}")
        return {"cpu_side_ec_c": cpu, "gpu_ec_c": gpu,
                "interpretation": "BladeControl sensor mapping; CPU-side EC reading is not a die measurement"}

    def set_zone(self, zone, mode):
        return self.exchange(0x0D02, (1, zone, mode, 0), attempts=1 if mode == 5 else 3)


def exact_modes(modes, expected):
    return (len(modes) == 2 and [m["zone"] for m in modes] == [1, 2]
            and all(m["mode"] == expected and m["fan_flag"] == 0
                    and m["response_valid"] and m["checksum_valid"] for m in modes))


def temperature_guard(temperatures):
    if temperatures["cpu_side_ec_c"] >= CPU_LIMIT_C or temperatures["gpu_ec_c"] >= GPU_LIMIT_C:
        raise RuntimeError(f"Conservative EC temperature limit reached: {temperatures}")


def restore_balanced(hid, journal):
    # Try both zones even if one fails, then verify both independently of ACKs.
    for recovery_round in range(1, 4):
        for zone in (1, 2):
            try:
                hid.set_zone(zone, 0)
            except Exception as error:
                journal.emit("restore_write_error", round=recovery_round, zone=zone, error=str(error))
        try:
            modes = hid.modes()
            journal.emit("restore_readback", round=recovery_round, modes=modes)
            if exact_modes(modes, 0):
                return modes
        except Exception as error:
            journal.emit("restore_read_error", round=recovery_round, error=str(error))
        time.sleep(0.2)
    return None


def cancellation_verified(config):
    try:
        data = json.loads(Path(config["cancel_path"]).read_text(encoding="utf-8"))
        return (data.get("token") == config["token"] and data.get("baseline_restored") is True
                and exact_modes(data.get("verification", []), 0))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def write_verified_cancel(config, modes):
    if not exact_modes(modes, 0):
        raise RuntimeError("Cannot cancel watchdog without exact verified Balanced/Auto restoration")
    content = {"token": config["token"], "baseline_restored": True, "verification": modes,
               "time": datetime.now().astimezone().isoformat()}
    cancel_path = Path(config["cancel_path"])
    temporary = cancel_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(content, indent=2), encoding="utf-8")
    temporary.replace(cancel_path)


def watchdog(config_path):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    journal = Journal(config["watchdog_log"])
    journal.emit("watchdog_started", deadline_unix=config["deadline_unix"])
    Path(config["ready_path"]).write_text(config["token"], encoding="ascii")
    while time.time() < config["deadline_unix"]:
        if cancellation_verified(config):
            journal.emit("watchdog_canceled_after_verified_restore")
            return 0
        time.sleep(min(0.1, max(0.001, config["deadline_unix"] - time.time())))
    if cancellation_verified(config):
        journal.emit("watchdog_canceled_after_verified_restore")
        return 0
    journal.emit("watchdog_deadline_restore_started")
    hid = None
    try:
        hid = ExperimentHid(journal, config["endpoint_path"])
        modes = restore_balanced(hid, journal)
        if modes is None:
            raise RuntimeError("Watchdog could not verify Balanced/Auto; manual recovery required")
        write_verified_cancel(config, modes)
        journal.emit("watchdog_restore_verified", modes=modes)
        return 0
    except Exception as error:
        journal.emit("watchdog_restore_failed", error=str(error))
        return 2
    finally:
        if hid is not None:
            hid.close()


def self_test():
    assert make_packet(0x0D82, (0, 1, 0, 0), 91)[89] == 0x8A
    silent_packet = make_packet(0x0D02, (1, 1, 5, 0), 91)
    assert silent_packet[7:13] == bytes([0x0D, 2, 1, 1, 5, 0])
    assert silent_packet[89] == checksum(silent_packet)
    for command, arguments in ((0x0D01, (0, 1, 0)), (0x0D02, (1, 1, 5, 1)),
                               (0x0D02, (1, 3, 0, 0)), (0x0D02, (1, 1, 4, 0)),
                               (0x0D02, (0, 1, 5, 0))):
        try:
            make_packet(command, arguments, 91)
        except ValueError:
            pass
        else:
            raise AssertionError("Forbidden packet accepted")
    valid = [{"zone": z, "mode": 0, "fan_flag": 0, "response_valid": True, "checksum_valid": True} for z in (1, 2)]
    assert exact_modes(valid, 0) and not exact_modes(valid, 5)
    for changed in ("mode", "fan_flag", "checksum_valid", "response_valid"):
        broken = [dict(v) for v in valid]
        broken[0][changed] = 1 if changed in ("mode", "fan_flag") else False
        assert not exact_modes(broken, 0)
    try:
        temperature_guard({"cpu_side_ec_c": CPU_LIMIT_C, "gpu_ec_c": 50})
    except RuntimeError:
        pass
    else:
        raise AssertionError("Temperature limit not enforced")
    print("Self-test passed: allowlist, packet framing, exact readback, checksum guard, temperature guard. No hardware accessed.")


def run_experiment():
    if sys.platform != "win32":
        raise RuntimeError("This experiment requires Windows")
    run_directory = Path(__file__).resolve().parent.parent / "diagnostics" / "silent" / f"test-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    run_directory.mkdir(parents=True)
    journal = Journal(run_directory / "test.jsonl")
    journal.emit("test_started", duration_seconds=HOLD_SECONDS, poll_seconds=POLL_SECONDS,
                 cpu_ec_limit_c=CPU_LIMIT_C, gpu_ec_limit_c=GPU_LIMIT_C,
                 writes="Only performance mode Silent5 / Balanced0 with Auto fan flag0; no RPM writes")
    hid = None
    config = None
    armed = False
    write_attempted = False
    success = False
    restored = False
    try:
        hid = ExperimentHid(journal)
        baseline = hid.modes()
        temperatures = hid.temperatures()
        journal.emit("baseline", modes=baseline, temperatures=temperatures)
        if not exact_modes(baseline, 0):
            raise RuntimeError("Baseline is not exact Balanced0/Auto0 in both zones; refusing any setting write")
        temperature_guard(temperatures)

        config = {"token": secrets.token_hex(24), "deadline_unix": time.time() + WATCHDOG_SECONDS,
                  "endpoint_path": hid.path, "cancel_path": str(run_directory / "verified-restore.json"),
                  "ready_path": str(run_directory / "watchdog.ready"),
                  "watchdog_log": str(run_directory / "watchdog.jsonl")}
        config_path = run_directory / "watchdog-config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        with (run_directory / "watchdog-console.txt").open("w", encoding="utf-8") as console:
            helper = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watchdog", str(config_path)],
                                      stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
                                      close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        ready_deadline = time.monotonic() + 5
        while time.monotonic() < ready_deadline:
            if helper.poll() is not None:
                raise RuntimeError("Watchdog exited before readiness; refusing setting writes")
            try:
                if Path(config["ready_path"]).read_text(encoding="ascii") == config["token"]:
                    armed = True
                    break
            except OSError:
                pass
            time.sleep(0.05)
        if not armed:
            raise RuntimeError("Watchdog readiness timed out; refusing setting writes")
        journal.emit("watchdog_armed", pid=helper.pid, deadline_unix=config["deadline_unix"])

        # Recheck after launch so stale baseline state cannot authorize the test.
        baseline = hid.modes()
        if not exact_modes(baseline, 0):
            raise RuntimeError("Baseline changed before test; refusing Silent writes")
        for zone in (1, 2):
            write_attempted = True
            hid.set_zone(zone, 5)
        modes = hid.modes()
        journal.emit("silent_initial_readback", modes=modes)
        if not exact_modes(modes, 5):
            raise RuntimeError("Firmware did not hold exact Silent5/Auto0 in both zones")
        temperatures = hid.temperatures()
        journal.emit("observation", elapsed_seconds=0, modes=modes, temperatures=temperatures)
        temperature_guard(temperatures)
        start = time.monotonic()
        for elapsed in range(POLL_SECONDS, HOLD_SECONDS + 1, POLL_SECONDS):
            time.sleep(max(0, start + elapsed - time.monotonic()))
            if helper.poll() is not None:
                raise RuntimeError("Watchdog exited during test")
            modes = hid.modes()
            temperatures = hid.temperatures()
            journal.emit("observation", elapsed_seconds=round(time.monotonic() - start, 3),
                         modes=modes, temperatures=temperatures)
            if not exact_modes(modes, 5):
                raise RuntimeError("Silent/Auto state changed during observation")
            temperature_guard(temperatures)
        success = True
        journal.emit("observation_interval_completed")
    except KeyboardInterrupt:
        journal.emit("test_interrupted")
    except Exception as error:
        journal.emit("test_stopped", error=str(error))
    finally:
        # Once armed, verify restoration even if no Silent write was sent.
        # This is the only path which may cancel the independent watchdog.
        if hid is not None and (armed or write_attempted):
            modes = restore_balanced(hid, journal)
            restored = modes is not None
            if restored and config is not None:
                write_verified_cancel(config, modes)
                journal.emit("balanced_auto_restored_watchdog_canceled", modes=modes)
            else:
                journal.emit("restore_unverified_watchdog_remains_armed")
        if hid is not None:
            hid.close()
        journal.emit("test_finished", silent_held_for_full_interval=success,
                     baseline_restored=restored, run_directory=str(run_directory))
    return 0 if success and restored else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--run", action="store_true", help="Perform the bounded hardware experiment")
    group.add_argument("--self-test", action="store_true", help="Validate guards without hardware (default)")
    group.add_argument("--watchdog", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.watchdog:
        return watchdog(args.watchdog)
    self_test()
    return run_experiment() if args.run else 0


if __name__ == "__main__":
    raise SystemExit(main())
