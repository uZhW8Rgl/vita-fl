"""Compatibility facade for the shared SCITT receipt implementation."""

from agent_receipts.scitt import (
    SCITT_CONTENT_TYPE,
    SCITT_EKU,
    SCITT_TOOL_RECEIPT_CONTENT_TYPE,
    SCITT_ZK_CONTENT_TYPE,
    ScittRegistrationError,
    register_verified_evidence,
)

__all__ = [
    "SCITT_CONTENT_TYPE",
    "SCITT_EKU",
    "SCITT_TOOL_RECEIPT_CONTENT_TYPE",
    "SCITT_ZK_CONTENT_TYPE",
    "ScittRegistrationError",
    "register_verified_evidence",
]
