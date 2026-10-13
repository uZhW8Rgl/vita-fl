"""Read participant signing seeds from the supervisor's private, volatile mount."""

from __future__ import annotations

import os
import stat
from pathlib import Path

RUNTIME_KEY_DIRECTORY = Path("/run/vita-fl")
SELLO_RUNTIME_KEY = "sello-signing.seed"
AIR_RUNTIME_KEY = "air-signing.seed"
TMPFS_MAGIC = 0x01021994


def require_volatile_directory(directory: Path = RUNTIME_KEY_DIRECTORY) -> None:
    if directory.resolve() != directory:
        raise RuntimeError("Runtime key directory must not be a symbolic link")
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeError("Runtime key directory must be owned by this process user and private")
    # Linux mountinfo records the filesystem behind the resolved directory.
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        mount = before.split()[4].replace("\\040", " ").replace("\\134", "\\")
        if directory == Path(mount) or Path(mount) in directory.parents:
            mounts.append((len(mount), after.split()[0]))
    if not mounts or max(mounts)[1] != "tmpfs":
        raise RuntimeError("Runtime private keys require a tmpfs mount")
    if len(Path("/proc/swaps").read_text().strip().splitlines()) > 1:
        raise RuntimeError("Runtime private keys require swap to be disabled")


def read_runtime_signing_seed(name: str, directory: Path = RUNTIME_KEY_DIRECTORY) -> bytes:
    if name not in (SELLO_RUNTIME_KEY, AIR_RUNTIME_KEY):
        raise RuntimeError("Unknown runtime signing key")
    require_volatile_directory(directory)
    filename = directory / name
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError("Runtime signing seed must be a private regular file")
        seed = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(seed) != 32:
        raise RuntimeError("Runtime signing seed must contain 32 bytes")
    return seed
