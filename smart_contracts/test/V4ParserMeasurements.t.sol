// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {V4Parser} from "../src/attestation/tdx/QuoteV4Auth/V4Parser.sol";
import {V4Struct} from "../src/attestation/tdx/QuoteV4Auth/V4Struct.sol";

contract V4ParserMeasurementsTest is Test {
    function testParsesAllDstackBootMeasurementsAtTdReportOffsets() public pure {
        bytes memory rawBody = new bytes(584);
        _fill(rawBody, 136, 0x11); // MRTD
        _fill(rawBody, 328, 0x22); // RTMR0
        _fill(rawBody, 376, 0x33); // RTMR1
        _fill(rawBody, 424, 0x44); // RTMR2
        _fill(rawBody, 472, 0x55); // RTMR3

        V4Struct.Body memory body = V4Parser.parseBody(rawBody);
        assertEq(body.mrtd, _expected(0x11));
        assertEq(body.rtmr0, _expected(0x22));
        assertEq(body.rtmr1, _expected(0x33));
        assertEq(body.rtmr2, _expected(0x44));
        assertEq(body.rtmr3, _expected(0x55));
    }

    function _fill(bytes memory target, uint256 offset, bytes1 value) private pure {
        for (uint256 i = 0; i < 48; i++) target[offset + i] = value;
    }

    function _expected(bytes1 value) private pure returns (bytes memory output) {
        output = new bytes(48);
        _fill(output, 0, value);
    }
}
