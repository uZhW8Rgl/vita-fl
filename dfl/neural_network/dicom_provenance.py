from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PROVENANCE_VERSION = "DICOM-ALIGNED-CHESTMNIST-V1"
DATASET_VERSION = "CHESTMNIST-1"
DEVICE_ROLE = "XRAY_DEVICE"
RADIOLOGIST_ROLE = "RADIOLOGIST"
IMAGE_SIGNATURE_PROFILE = "DICOM-CREATOR-RSA-SHA256"
LABEL_SIGNATURE_PROFILE = "DICOM-AUTHORIZATION-RSA-SHA256"
EXPLICIT_VR_LITTLE_ENDIAN_UID = "1.2.840.10008.1.2.1"
SECONDARY_CAPTURE_IMAGE_STORAGE_UID = "1.2.840.10008.5.1.4.1.1.7"
BASIC_TEXT_SR_STORAGE_UID = "1.2.840.10008.5.1.4.1.1.88.11"
CHESTMNIST_LABEL_COUNT = 14
SIGNER_ID_SIZE = 32


@dataclass(frozen=True)
class SampleIdentity:
    study_instance_uid: str
    series_instance_uid: str
    image_sop_instance_uid: str
    image_signature_uid: str
    label_sop_instance_uid: str


def _uid(domain: str, *values: object) -> str:
    material = "\x1f".join((PROVENANCE_VERSION, domain, *(str(value) for value in values))).encode("utf-8")
    return f"2.25.{int.from_bytes(hashlib.sha256(material).digest()[:16], 'big')}"


def _even(value: bytes, padding_byte: bytes) -> bytes:
    return value if len(value) % 2 == 0 else value + padding_byte


def _text(vr: str, value: str) -> bytes:
    encoded = value.encode("ascii")
    return _even(encoded, b"\x00" if vr == "UI" else b" ")


def _element(group: int, element: int, vr: str, value: bytes) -> bytes:
    tag = struct.pack("<HH", group, element)
    if vr in {"OB", "OD", "OF", "OL", "OW", "SQ", "UC", "UR", "UT", "UN"}:
        return tag + vr.encode("ascii") + b"\x00\x00" + struct.pack("<I", len(value)) + value
    if len(value) > 0xFFFF:
        raise ValueError(f"DICOM {vr} value is too long")
    return tag + vr.encode("ascii") + struct.pack("<H", len(value)) + value


def _encode_elements(elements: Iterable[tuple[int, int, str, bytes]]) -> bytes:
    ordered = sorted(elements, key=lambda item: (item[0], item[1]))
    return b"".join(_element(*item) for item in ordered)


def _validate_image_and_labels(image: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    image_array = np.asarray(image)
    if image_array.shape == (28, 28, 1):
        image_array = image_array[..., 0]
    if image_array.shape != (28, 28):
        raise ValueError(f"expected one 28x28 ChestMNIST image, got {image_array.shape}")
    if image_array.dtype != np.uint8:
        raise ValueError(f"ChestMNIST provenance requires uint8 pixels, got {image_array.dtype}")

    label_array = np.asarray(labels, dtype=np.uint8).reshape(-1)
    if label_array.shape != (CHESTMNIST_LABEL_COUNT,):
        raise ValueError(f"expected {CHESTMNIST_LABEL_COUNT} labels, got {label_array.shape}")
    if not np.isin(label_array, np.array([0, 1], dtype=np.uint8)).all():
        raise ValueError("ChestMNIST labels must be binary")
    return np.ascontiguousarray(image_array), np.ascontiguousarray(label_array)


def image_mac_bytes(image: np.ndarray, sample_index: int, device_id: str) -> tuple[bytes, SampleIdentity]:
    image_array, _ = _validate_image_and_labels(image, np.zeros(CHESTMNIST_LABEL_COUNT, dtype=np.uint8))
    study_uid = _uid("study", DATASET_VERSION)
    series_uid = _uid("series", DATASET_VERSION, device_id)
    image_sop_uid = _uid("image-sop", DATASET_VERSION, sample_index)
    acquisition_date = "20250101"
    acquisition_seconds = sample_index % 86400
    acquisition_time = (
        f"{acquisition_seconds // 3600:02d}{(acquisition_seconds % 3600) // 60:02d}{acquisition_seconds % 60:02d}"
    )

    elements = (
        (0x0008, 0x0012, "DA", _text("DA", acquisition_date)),
        (0x0008, 0x0013, "TM", _text("TM", acquisition_time)),
        (0x0008, 0x0016, "UI", _text("UI", SECONDARY_CAPTURE_IMAGE_STORAGE_UID)),
        (0x0008, 0x0018, "UI", _text("UI", image_sop_uid)),
        (0x0008, 0x0060, "CS", _text("CS", "DX")),
        (0x0008, 0x0064, "CS", _text("CS", "WSD")),
        (0x0008, 0x0070, "LO", _text("LO", "Master Thesis Synthetic Imaging")),
        (0x0008, 0x1090, "LO", _text("LO", "ChestMNIST X-Ray Simulator")),
        (0x0011, 0x0010, "LO", _text("LO", "MASTERTHESIS")),
        (0x0011, 0x1001, "UL", struct.pack("<I", sample_index)),
        (0x0011, 0x1002, "LO", _text("LO", DATASET_VERSION)),
        (0x0018, 0x1000, "LO", _text("LO", device_id)),
        (0x0020, 0x000D, "UI", _text("UI", study_uid)),
        (0x0020, 0x000E, "UI", _text("UI", series_uid)),
        (0x0028, 0x0002, "US", struct.pack("<H", 1)),
        (0x0028, 0x0004, "CS", _text("CS", "MONOCHROME2")),
        (0x0028, 0x0010, "US", struct.pack("<H", 28)),
        (0x0028, 0x0011, "US", struct.pack("<H", 28)),
        (0x0028, 0x0100, "US", struct.pack("<H", 8)),
        (0x0028, 0x0101, "US", struct.pack("<H", 8)),
        (0x0028, 0x0102, "US", struct.pack("<H", 7)),
        (0x0028, 0x0103, "US", struct.pack("<H", 0)),
        (0x7FE0, 0x0010, "OB", _even(image_array.tobytes(order="C"), b"\x00")),
    )
    mac_bytes = _encode_elements(elements)
    image_signature_uid = _uid("image-signature", image_sop_uid, hashlib.sha256(mac_bytes).hexdigest())
    identity = SampleIdentity(
        study_instance_uid=study_uid,
        series_instance_uid=series_uid,
        image_sop_instance_uid=image_sop_uid,
        image_signature_uid=image_signature_uid,
        label_sop_instance_uid=_uid("label-sop", DATASET_VERSION, sample_index),
    )
    return mac_bytes, identity


def label_mac_bytes(
    labels: np.ndarray,
    sample_index: int,
    radiologist_id: str,
    identity: SampleIdentity,
) -> bytes:
    _, label_array = _validate_image_and_labels(np.zeros((28, 28), dtype=np.uint8), labels)
    observation_date = "20250102"
    observation_seconds = sample_index % 86400
    observation_time = (
        f"{observation_seconds // 3600:02d}{(observation_seconds % 3600) // 60:02d}{observation_seconds % 60:02d}"
    )
    elements = (
        (0x0008, 0x0012, "DA", _text("DA", observation_date)),
        (0x0008, 0x0013, "TM", _text("TM", observation_time)),
        (0x0008, 0x0016, "UI", _text("UI", BASIC_TEXT_SR_STORAGE_UID)),
        (0x0008, 0x0018, "UI", _text("UI", identity.label_sop_instance_uid)),
        (0x0008, 0x0060, "CS", _text("CS", "SR")),
        (0x0011, 0x0010, "LO", _text("LO", "MASTERTHESIS")),
        (0x0011, 0x1010, "UI", _text("UI", identity.image_sop_instance_uid)),
        (0x0011, 0x1011, "UI", _text("UI", identity.image_signature_uid)),
        (0x0011, 0x1012, "LO", _text("LO", DATASET_VERSION)),
        (0x0011, 0x1013, "LO", _text("LO", "99MTHESIS-CHESTMNIST-LABELS-V1")),
        (0x0011, 0x1014, "OB", _even(label_array.tobytes(order="C"), b"\x00")),
        (0x0020, 0x000D, "UI", _text("UI", identity.study_instance_uid)),
        (0x0020, 0x000E, "UI", _text("UI", _uid("label-series", DATASET_VERSION))),
        (0x0040, 0xA073, "SQ", b""),
        (0x0040, 0xA491, "CS", _text("CS", "COMPLETE")),
        (0x0040, 0xA493, "CS", _text("CS", "VERIFIED")),
        (0x0040, 0xA730, "SQ", b""),
        (0x0040, 0xDB73, "UL", struct.pack("<I", 1)),
        (0x0041, 0x0010, "LO", _text("LO", "MASTERTHESIS")),
        (0x0041, 0x1001, "LO", _text("LO", radiologist_id)),
    )
    return _encode_elements(elements)


def sign_sample(
    image: np.ndarray,
    labels: np.ndarray,
    sample_index: int,
    device_id: str,
    device_private_key: rsa.RSAPrivateKey,
    radiologist_id: str,
    radiologist_private_key: rsa.RSAPrivateKey,
) -> tuple[bytes, bytes]:
    image_bytes, identity = image_mac_bytes(image, sample_index, device_id)
    label_bytes = label_mac_bytes(labels, sample_index, radiologist_id, identity)
    image_signature = device_private_key.sign(image_bytes, padding.PKCS1v15(), hashes.SHA256())
    label_signature = radiologist_private_key.sign(label_bytes, padding.PKCS1v15(), hashes.SHA256())
    return image_signature, label_signature


def load_rsa_private_key(path: Path) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError(f"{path} must contain an RSA key of at least 2048 bits")
    return key


def _decode_fixed_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.rstrip(b"\x00").decode("ascii")
    if isinstance(value, np.bytes_):
        return bytes(value).rstrip(b"\x00").decode("ascii")
    return str(value)


def _trusted_public_keys(snapshot: dict[str, Any]) -> dict[str, tuple[str, rsa.RSAPublicKey]]:
    trusted: dict[str, tuple[str, rsa.RSAPublicKey]] = {}
    for signer in snapshot.get("signers", []):
        if not signer.get("active", False):
            continue
        signer_id = str(signer["signer_id"])
        role = str(signer["role"])
        public_der = bytes.fromhex(str(signer["public_key_der_hex"]).removeprefix("0x"))
        certificate_der = bytes.fromhex(str(signer["certificate_der_hex"]).removeprefix("0x"))
        expected_fingerprint = str(signer["certificate_fingerprint"]).lower().removeprefix("0x")
        if hashlib.sha256(certificate_der).hexdigest() != expected_fingerprint:
            raise ValueError(f"on-chain certificate fingerprint mismatch for {signer_id}")

        certificate = x509.load_der_x509_certificate(certificate_der)
        certificate_key = certificate.public_key()
        public_key = serialization.load_der_public_key(public_der)
        if not isinstance(public_key, rsa.RSAPublicKey) or not isinstance(certificate_key, rsa.RSAPublicKey):
            raise ValueError(f"{signer_id} does not use an RSA public key")
        if public_key.key_size < 2048:
            raise ValueError(f"{signer_id} RSA key is smaller than 2048 bits")
        certificate_public_der = certificate_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if certificate_public_der != public_der:
            raise ValueError(f"certificate/public-key mismatch for {signer_id}")
        certificate_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            padding.PKCS1v15(),
            certificate.signature_hash_algorithm,
        )
        trusted[signer_id] = (role, public_key)
    return trusted


def verify_training_provenance(path: Path, signer_snapshot: dict[str, Any]) -> dict[str, int | str]:
    trusted = _trusted_public_keys(signer_snapshot)
    device_count = sum(role == DEVICE_ROLE for role, _ in trusted.values())
    radiologist_count = sum(role == RADIOLOGIST_ROLE for role, _ in trusted.values())
    if device_count != 5:
        raise ValueError("on-chain signer snapshot must contain exactly 5 active X-ray devices")
    if radiologist_count == 0:
        raise ValueError("on-chain signer snapshot must contain at least one active radiologist")

    required = {
        "images",
        "labels",
        "sample_indices",
        "device_signer_ids",
        "device_signatures",
        "radiologist_signer_ids",
        "radiologist_signatures",
        "provenance_version",
    }
    with np.load(path, allow_pickle=False) as bundle:
        missing = sorted(required.difference(bundle.files))
        if missing:
            raise ValueError(f"{path} lacks signed provenance fields: {', '.join(missing)}")
        images = bundle["images"]
        labels = bundle["labels"]
        sample_indices = bundle["sample_indices"]
        device_ids = bundle["device_signer_ids"]
        device_signatures = bundle["device_signatures"]
        radiologist_ids = bundle["radiologist_signer_ids"]
        radiologist_signatures = bundle["radiologist_signatures"]
        provenance_version = _decode_fixed_text(bundle["provenance_version"].item())

    if provenance_version != PROVENANCE_VERSION:
        raise ValueError(f"unsupported provenance version: {provenance_version}")
    count = int(images.shape[0])
    arrays = (labels, sample_indices, device_ids, device_signatures, radiologist_ids, radiologist_signatures)
    if any(int(array.shape[0]) != count for array in arrays):
        raise ValueError("signed provenance arrays have inconsistent sample counts")

    for index in range(count):
        sample_index = int(sample_indices[index])
        device_id = _decode_fixed_text(device_ids[index])
        radiologist_id = _decode_fixed_text(radiologist_ids[index])
        device_entry = trusted.get(device_id)
        radiologist_entry = trusted.get(radiologist_id)
        if device_entry is None or device_entry[0] != DEVICE_ROLE:
            raise ValueError(f"sample {sample_index} uses an unapproved X-ray device: {device_id}")
        if radiologist_entry is None or radiologist_entry[0] != RADIOLOGIST_ROLE:
            raise ValueError(f"sample {sample_index} uses an unapproved radiologist: {radiologist_id}")

        image_bytes, identity = image_mac_bytes(images[index], sample_index, device_id)
        label_bytes = label_mac_bytes(labels[index], sample_index, radiologist_id, identity)
        try:
            device_entry[1].verify(
                bytes(np.asarray(device_signatures[index], dtype=np.uint8)),
                image_bytes,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            radiologist_entry[1].verify(
                bytes(np.asarray(radiologist_signatures[index], dtype=np.uint8)),
                label_bytes,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception as exc:
            raise ValueError(f"DICOM provenance verification failed for sample {sample_index}") from exc

    return {
        "verified_samples": count,
        "active_device_keys": device_count,
        "active_radiologist_keys": radiologist_count,
        "key_set_version": str(signer_snapshot.get("key_set_version", "")),
        "block_number": str(signer_snapshot.get("block_number", "")),
    }
