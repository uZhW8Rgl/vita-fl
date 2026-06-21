#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import re
import subprocess
from pathlib import Path


DEFAULT_MNEMONIC = "test test test test test test test test test test test junk"
HARDENED = 0x80000000
SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_G = (
    55066263022277343669578718895168534326250603453777594175500187360389116729240,
    32670510020758816978083085130507043184471273380659243275938904335757337482424,
)
KNOWN_FIRST_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
KNOWN_FIRST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

KECCAK_ROUNDS = [
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
]
KECCAK_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]


def rotl64(value: int, shift: int) -> int:
    shift %= 64
    return ((value << shift) | (value >> (64 - shift))) & 0xFFFFFFFFFFFFFFFF


def keccak_f1600(state: list[int]) -> None:
    for round_constant in KECCAK_ROUNDS:
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]

        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = rotl64(state[x + 5 * y], KECCAK_ROT[x][y])

        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = b[x + 5 * y] ^ ((~b[((x + 1) % 5) + 5 * y]) & b[((x + 2) % 5) + 5 * y])
                state[x + 5 * y] &= 0xFFFFFFFFFFFFFFFF

        state[0] ^= round_constant


def keccak256(data: bytes) -> bytes:
    rate = 136
    padded = data + b"\x01"
    padded += b"\x00" * ((rate - 1 - len(padded) % rate) % rate)
    padded += b"\x80"

    state = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[i * 8 : (i + 1) * 8], "little")
        keccak_f1600(state)

    return b"".join(word.to_bytes(8, "little") for word in state)[:32]


def point_add(
    left: tuple[int, int] | None,
    right: tuple[int, int] | None,
) -> tuple[int, int] | None:
    if left is None:
        return right
    if right is None:
        return left

    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % SECP256K1_P == 0:
        return None

    if left == right:
        slope = (3 * x1 * x1) * pow(2 * y1, -1, SECP256K1_P)
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, SECP256K1_P)
    slope %= SECP256K1_P

    x3 = (slope * slope - x1 - x2) % SECP256K1_P
    y3 = (slope * (x1 - x3) - y1) % SECP256K1_P
    return x3, y3


def scalar_mult(scalar: int, point: tuple[int, int] = SECP256K1_G) -> tuple[int, int]:
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    while scalar:
        if scalar & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        scalar >>= 1
    if result is None:
        raise ValueError("Invalid secp256k1 scalar")
    return result


def public_key_bytes(private_key: int, *, compressed: bool) -> bytes:
    x, y = scalar_mult(private_key)
    x_bytes = x.to_bytes(32, "big")
    y_bytes = y.to_bytes(32, "big")
    if compressed:
        return (b"\x02" if y % 2 == 0 else b"\x03") + x_bytes
    return b"\x04" + x_bytes + y_bytes


def child_private_key(private_key: int, chain_code: bytes, index: int) -> tuple[int, bytes]:
    if index >= HARDENED:
        data = b"\x00" + private_key.to_bytes(32, "big") + index.to_bytes(4, "big")
    else:
        data = public_key_bytes(private_key, compressed=True) + index.to_bytes(4, "big")
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    child = (int.from_bytes(digest[:32], "big") + private_key) % SECP256K1_N
    if child == 0:
        raise ValueError("Derived invalid zero private key")
    return child, digest[32:]


def derive_anvil_private_key(index: int, mnemonic: str = DEFAULT_MNEMONIC) -> int:
    seed = hashlib.pbkdf2_hmac("sha512", mnemonic.encode("utf-8"), b"mnemonic", 2048, 64)
    digest = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    private_key = int.from_bytes(digest[:32], "big")
    chain_code = digest[32:]

    path = [44 + HARDENED, 60 + HARDENED, 0 + HARDENED, 0, index]
    for path_index in path:
        private_key, chain_code = child_private_key(private_key, chain_code, path_index)
    return private_key


def checksum_address(address_bytes: bytes) -> str:
    lower = address_bytes.hex()
    digest = keccak256(lower.encode("ascii")).hex()
    checked = "".join(char.upper() if int(digest[i], 16) >= 8 else char for i, char in enumerate(lower))
    return f"0x{checked}"


def derive_anvil_account(index: int) -> tuple[str, str]:
    private_key = derive_anvil_private_key(index)
    public_key = public_key_bytes(private_key, compressed=False)[1:]
    address = checksum_address(keccak256(public_key)[-20:])
    return address, f"0x{private_key:064x}"


def key_stem(index: int) -> str:
    return "key" if index == 0 else f"key_{index}"


def private_key_path(keys_dir: Path, index: int) -> Path:
    return keys_dir / ("private_key.pem" if index == 0 else f"private_key_{index}.pem")


def public_key_path(keys_dir: Path, index: int) -> Path:
    return keys_dir / ("public_key.pem" if index == 0 else f"public_key_{index}.pem")


def public_der_path(keys_dir: Path, index: int) -> Path:
    return keys_dir / ("public_key.der" if index == 0 else f"public_key_{index}.der")


def run_openssl(args: list[str]) -> None:
    subprocess.run(["openssl", *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_rsa_keypair(keys_dir: Path, index: int, *, force: bool = False) -> bool:
    private_path = private_key_path(keys_dir, index)
    public_path = public_key_path(keys_dir, index)
    der_path = public_der_path(keys_dir, index)

    if not force and private_path.exists() and public_path.exists() and der_path.exists():
        return False

    keys_dir.mkdir(parents=True, exist_ok=True)
    run_openssl(["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private_path)])
    run_openssl(["pkey", "-in", str(private_path), "-pubout", "-out", str(public_path)])
    run_openssl(["pkey", "-pubin", "-in", str(public_path), "-outform", "DER", "-out", str(der_path)])
    private_path.chmod(0o644)
    public_path.chmod(0o644)
    der_path.chmod(0o644)
    return True


def suffix(index: int) -> str:
    return "" if index == 0 else f"_{index}"


def render_worker_services(worker_count: int) -> str:
    blocks: list[str] = []
    for index in range(worker_count):
        suff = suffix(index)
        blocks.append(
            f"""  VM-{index}:
    <<: *worker-common
    container_name: ${{W{index}_ACCOUNT_ADDRESS}}
    environment:
      <<: *worker-env
      ACCOUNT_ADDRESS: ${{W{index}_ACCOUNT_ADDRESS}}
      PRIVATE_KEY: ${{W{index}_PRIVATE_KEY}}
      DEVICE_ID: ${{W{index}_DEVICE_ID}}
      TRAIN_IMAGES_SRC: /dfl/config/training_data/train-images-{index}.idx3-ubyte
      TRAIN_LABELS_SRC: /dfl/config/training_data/train-labels-{index}.idx1-ubyte
      TRAIN_DATA_SRC: /dfl/config/chestmnist/training_data/train-data-{index}.npz
      RSA_PRIVATE_KEY_FILE: /dfl/keys/private_key{suff}.pem
      RSA_PUBLIC_KEY_FILE: /dfl/keys/public_key{suff}.pem
"""
        )
    return "\n".join(blocks)


def update_compose(compose_path: Path, worker_count: int) -> None:
    text = compose_path.read_text(encoding="utf-8")
    text = text.replace('      - "20"\n      - --hardfork', '      - ${ANVIL_ACCOUNT_COUNT:-20}\n      - --hardfork')
    text = re.sub(r"\n\s+RSA_PRIVATE_KEY: \$\{W0_RSA_PRIVATE_KEY\}\n\s+RSA_PUBLIC_KEY: \$\{W0_RSA_PUBLIC_KEY\}", "", text)

    marker = "\n  VM-0:\n"
    volumes_marker = "\nvolumes:\n"
    if marker not in text or volumes_marker not in text:
        raise ValueError("Could not locate VM service block or volumes block in compose.yml")
    before = text[: text.index(marker)]
    after = text[text.index(volumes_marker) :]
    compose_path.write_text(before + "\n" + render_worker_services(worker_count) + after, encoding="utf-8")


def parse_env_key(line: str) -> str | None:
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    return match.group(1) if match else None


def worker_env_line(key: str) -> bool:
    return bool(re.fullmatch(r"W\d+_(ACCOUNT_ADDRESS|PRIVATE_KEY|DEVICE_ID|RSA_PRIVATE_KEY|RSA_PUBLIC_KEY)", key))


def update_env_file(env_path: Path, accounts: list[tuple[str, str]], worker_count: int) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    generated_marker = "# Worker accounts generated from the Anvil/Hardhat default mnemonic."
    value_map = {
        "WORKER_COUNT": str(worker_count),
        "ANVIL_ACCOUNT_COUNT": str(worker_count),
    }
    seen: set[str] = set()
    kept: list[str] = []

    def append_kept(line: str) -> None:
        if line == "" and (not kept or kept[-1] == ""):
            return
        kept.append(line)

    for line in lines:
        if re.match(r"^\s*#\s*Worker\s+\d+\s*$", line):
            continue
        if line.strip() == generated_marker:
            continue
        key = parse_env_key(line)
        if key and worker_env_line(key):
            continue
        if key in value_map:
            append_kept(f"{key}={value_map[key]}")
            seen.add(key)
            continue
        append_kept(line)

    if kept and kept[-1] != "":
        kept.append("")
    for key in ("ANVIL_ACCOUNT_COUNT", "WORKER_COUNT"):
        if key not in seen:
            kept.append(f"{key}={value_map[key]}")

    kept.append("")
    kept.append(generated_marker)
    for index, (address, private_key) in enumerate(accounts):
        kept.extend(
            [
                "",
                f"# Worker {index}",
                f"W{index}_ACCOUNT_ADDRESS={address}",
                f"W{index}_PRIVATE_KEY={private_key}",
                f"W{index}_DEVICE_ID={index}",
            ]
        )

    env_path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare local DFL worker configuration for larger experiments.")
    parser.add_argument("--workers", type=int, default=500, help="Total number of worker services/accounts/keys")
    parser.add_argument("--keys-dir", type=Path, default=Path("data/rsa_keys"))
    parser.add_argument("--compose", type=Path, default=Path("compose.yml"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--env-example", type=Path, default=Path(".env.example"))
    parser.add_argument("--skip-compose", action="store_true")
    parser.add_argument("--skip-env", action="store_true")
    parser.add_argument("--skip-keys", action="store_true")
    parser.add_argument("--force-keys", action="store_true", help="Regenerate existing RSA key files")
    args = parser.parse_args()

    if args.workers < 1:
        raise ValueError("--workers must be at least 1")

    first_address, first_private_key = derive_anvil_account(0)
    if first_address != KNOWN_FIRST_ADDRESS or first_private_key != KNOWN_FIRST_PRIVATE_KEY:
        raise RuntimeError("Anvil account derivation check failed; refusing to write experiment config")

    accounts = [derive_anvil_account(index) for index in range(args.workers)]

    if not args.skip_keys:
        generated = 0
        for index in range(args.workers):
            if ensure_rsa_keypair(args.keys_dir, index, force=args.force_keys):
                generated += 1
        print(f"RSA keypairs ready: {args.workers} total ({generated} generated)")

    if not args.skip_compose:
        update_compose(args.compose, args.workers)
        print(f"Updated {args.compose} with {args.workers} VM services")

    if not args.skip_env:
        update_env_file(args.env_example, accounts, args.workers)
        print(f"Updated {args.env_example} with {args.workers} worker accounts")
        update_env_file(args.env, accounts, args.workers)
        print(f"Updated {args.env} with {args.workers} worker accounts")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
