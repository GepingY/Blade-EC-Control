# Prepared PawnIO EC snapshot package

Prepared on 2026-11-17. Downloads and offline checks only. The installer was not
executed, the driver was not installed/started, and EC ports were not accessed.

## Official installer

- Project website: <https://pawnio.eu/>, whose Download Now link points to
  `namazso/PawnIO.Setup` releases.
- Version: 2.2.0, published 2026-03-15.
- Release: <https://github.com/namazso/PawnIO.Setup/releases/tag/2.2.0>
- Binary: <https://github.com/namazso/PawnIO.Setup/releases/download/2.2.0/PawnIO_setup.exe>
- SHA256: `1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032`
- Hash matches the official GitHub asset digest.
- Windows Authenticode: **Valid**, signature verified.
- Signer: `E=admin@namazso.eu, CN=namazso.eu, O=namazso, L=Debrecen, C=HU`.
- Signer certificate thumbprint: `F380DCC9F706E2756A5047B832FFE719E1BC35F5`.
- Timestamp: Microsoft Public RSA Time Stamping Authority.

Full release metadata is `installer-release.json`; local signature evidence is
`installer-authenticode.json`. Installation would add a kernel driver and needs
a Windows administrator token. This package deliberately does not automate
installation or select the unsigned/unrestricted edition.

## Official signed EC module

- Release: <https://github.com/namazso/PawnIO.Modules/releases/tag/0.2.12>
- Published 2026-11-17; GitHub's live release API returned this version.
- Archive: `release_0_2_12.zip`.
- Archive SHA256: `09698897ce19b92f1043a2e322cfcf0b1fe823e0b576971b178a65244fb0af9d`.
- Archive hash matches the official GitHub asset digest.
- Only `LpcACPIEC.bin` was extracted.
- Module SHA256: `c38fd116e7aff4d1fdb0a494e296be0a6708e5a22fc72f14587442fb7f8f7906`.
- The module uses PawnIO's own signed-module format; it is not a Windows PE file
  with a separate Authenticode signature. Official PawnIO validates module
  signatures when loading them.

`source-provenance.json` pins source URLs, commit IDs, and hashes for the C API,
implementation, IOCTL header, and exact module release source. Copies are stored
beside this README for review. No sources were compiled or drivers loaded.

## Collector review

`ec_snapshot.py --check` (also the default with no arguments) reads files and
installation registry entries only. It loads no DLL, opens no driver, touches no
ports, and acquires no hardware mutex. It verifies package hashes, source hashes,
and C API/module signatures, then simulates all 256 register reads. The recorded
check passed; PawnIO is currently reported missing.

After installation is separately approved, explicit `--snapshot` requires the
installed official signed driver, a running service, and an elevated 64-bit
Python process. The script will not start the service itself. It takes one
snapshot of registers 0x00-0xFF, with 20ms between bytes, no bank switches, no
retries, and bounded status waits. The `Global\Access_EC` mutex corresponds to
the source-required `\BaseNamedObjects\Access_EC`; it is released in `finally`.
Output includes a partial map and failure stage/offset if the operation fails.

The current module exposes raw port reads/writes. An EC register read therefore
requires a READ command (`0x80`) to port `0x66` and an address byte to port `0x62`,
then a read from `0x62`. The script permits only these fixed read-transaction
framing writes. It contains no EC WRITE (`0x81`) command path, fan-test writes,
arbitrary commands, configurable ports, bank writes, or persistent service.

The mutex coordinates participating applications, not necessarily ACPI.sys or
firmware. Unexpected pending output causes an immediate abort without draining
it. A full EC snapshot is not inherently risk-free: device-specific registers
can have read side effects. This collector does not interpret any fan address or
claim that read-only protocol use guarantees absence of platform effects.

## Source references

- <https://github.com/namazso/PawnIO.Modules/wiki/Using-PawnIO-Modules>
- <https://github.com/namazso/PawnIO.Modules/blob/0.2.12/LpcACPIEC.p>
- <https://github.com/namazso/PawnIO/blob/9d52965895588b0ffa3703b72eec75ba19f4ccc0/PawnIOLib/include/PawnIOLib.h>
- <https://github.com/namazso/PawnIO/blob/9d52965895588b0ffa3703b72eec75ba19f4ccc0/PawnIOLib/PawnIOLib.cpp>
