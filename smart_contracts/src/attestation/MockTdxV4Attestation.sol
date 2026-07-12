// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Local-Anvil-only verifier used to exercise DeviceRegistry's report-data binding.
/// @dev The 64-byte input is treated as TDREPORT.REPORTDATA. This contract proves no TEE claim
///      and must never be configured in a Phala or other production deployment.
contract MockTdxV4Attestation {
    uint256 private constant REPORT_DATA_LENGTH = 64;

    function verifyAndAttestOnChain(bytes calldata input) external pure returns (bytes memory output) {
        return _mockOutput(input);
    }

    function verifyAndAttestOnChainWithRtmr3Events(bytes calldata input, bytes[] calldata)
        external
        pure
        returns (bytes memory output)
    {
        return _mockOutput(input);
    }

    function verifyAndAttestOnChainWithRtmr3Events(bytes calldata input, bytes[] calldata, bytes32 composeHash)
        external
        pure
        returns (bytes memory output)
    {
        require(composeHash != bytes32(0), "mock compose hash required");
        return _mockOutput(input);
    }

    function _mockOutput(bytes calldata reportData) private pure returns (bytes memory) {
        require(reportData.length == REPORT_DATA_LENGTH, "mock report data must be 64 bytes");
        return abi.encodePacked(bytes1(0), new bytes(48), reportData, bytes6(0));
    }
}
