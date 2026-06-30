// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "solady/auth/Ownable.sol";
import {Sha384} from "./Sha384.sol";
import {V4Parser} from "./tdx/QuoteV4Auth/V4Parser.sol";
import {V4Struct} from "./tdx/QuoteV4Auth/V4Struct.sol";

contract PhalaRtmr3ReplayPolicy is Ownable {
    bytes32 public expectedComposeHash;
    bytes public expectedComposeEventDigest;

    event ExpectedComposeHashUpdated(bytes32 expectedComposeHash);
    event ExpectedComposeEventDigestUpdated(bytes expectedComposeEventDigest);

    error Failed_To_Verify_Replay();

    constructor() {
        _initializeOwner(msg.sender);
    }

    function setExpectedComposeHash(bytes32 _expectedComposeHash) external onlyOwner {
        expectedComposeHash = _expectedComposeHash;
        emit ExpectedComposeHashUpdated(_expectedComposeHash);
    }

    function setExpectedComposeEventDigest(bytes calldata _expectedComposeEventDigest) external onlyOwner {
        require(
            _expectedComposeEventDigest.length == 0 || _expectedComposeEventDigest.length == 48,
            "expected compose event digest must be 48 bytes"
        );
        expectedComposeEventDigest = _expectedComposeEventDigest;
        emit ExpectedComposeEventDigestUpdated(_expectedComposeEventDigest);
    }

    function verifyReplayFromQuote(bytes calldata quote, bytes[] calldata rtmr3EventDigests) external view {
        (bool success, V4Struct.ParsedV4Quote memory parsedQuote) = V4Parser.parseInput(bytes(quote));
        if (!success || !_rtmr3EventsPolicySatisfied(parsedQuote.body.rtmr3, rtmr3EventDigests)) {
            revert Failed_To_Verify_Replay();
        }
    }

    function _rtmr3EventsPolicySatisfied(bytes memory quoteRtmr3, bytes[] calldata eventDigests)
        private
        view
        returns (bool)
    {
        if (eventDigests.length == 0) {
            return false;
        }

        bool foundExpectedComposeEvent = expectedComposeEventDigest.length == 0;
        bytes memory replayedRtmr = new bytes(48);
        for (uint256 i = 0; i < eventDigests.length; i++) {
            if (eventDigests[i].length != 48) {
                return false;
            }
            if (
                expectedComposeEventDigest.length > 0
                    && keccak256(eventDigests[i]) == keccak256(expectedComposeEventDigest)
            ) {
                foundExpectedComposeEvent = true;
            }
            replayedRtmr = Sha384.hashRtmrExtend(replayedRtmr, eventDigests[i]);
        }

        return foundExpectedComposeEvent && keccak256(replayedRtmr) == keccak256(quoteRtmr3);
    }
}
