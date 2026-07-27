// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/// @notice Minimal strict secp256k1 signature recovery for raw 32-byte digests.
/// @dev Rejects malleable high-s signatures and non-canonical recovery identifiers.
library StrictECDSA {
    uint256 private constant SECP256K1N_DIV_2 = 0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0;

    function recover(bytes32 digest, bytes memory signature) internal pure returns (address signer) {
        require(signature.length == 65, "invalid signature length");

        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly ("memory-safe") {
            r := mload(add(signature, 0x20))
            s := mload(add(signature, 0x40))
            v := byte(0, mload(add(signature, 0x60)))
        }

        require(uint256(s) <= SECP256K1N_DIV_2, "non-canonical signature s");
        require(v == 27 || v == 28, "invalid signature v");
        signer = ecrecover(digest, v, r, s);
        require(signer != address(0), "invalid signature");
    }
}
