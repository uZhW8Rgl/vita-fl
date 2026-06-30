// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {AutomataDcapTdxV4Attestation} from "../src/attestation/AutomataDcapTdxV4Attestation.sol";
import {V4Parser} from "../src/attestation/tdx/QuoteV4Auth/V4Parser.sol";
import {V4Struct} from "../src/attestation/tdx/QuoteV4Auth/V4Struct.sol";
import {P256Probe} from "./common/P256Probe.sol";

contract VerifyTDXV4Quote is Script, P256Probe {
    function run() external view {
        address dcapAddr = vm.envAddress("DCAP_TDX_V4_ADDRESS");
        string memory quotePath = vm.envOr("QUOTE_PATH", string("../data/phala_tdx_quote"));
        string memory fullPath = string.concat(vm.projectRoot(), "/", quotePath);
        string memory quoteHex = vm.readFile(fullPath);
        bytes memory quoteBytes = vm.parseBytes(string.concat("0x", quoteHex));
        address configuredVerifier = AutomataDcapTdxV4Attestation(dcapAddr).p256Verifier();
        (uint8 route, bool nativeSupported, bool fallbackSupported,) = _resolveP256Route(configuredVerifier);

        console2.log("P256 configured verifier:", configuredVerifier);
        console2.log("P256 native probe:", _boolLabel(nativeSupported));
        console2.log("P256 fallback probe:", _boolLabel(fallbackSupported));
        console2.log("P256 effective route:", _routeLabel(route));

        (bool parsedSuccessfully, V4Struct.ParsedV4Quote memory parsedQuote) = V4Parser.parseInput(quoteBytes);
        if (parsedSuccessfully) {
            bytes memory expectedRtmr3 = AutomataDcapTdxV4Attestation(dcapAddr).expectedRtmr3();
            console2.log("Quote RTMR3:");
            console2.logBytes(parsedQuote.body.rtmr3);
            if (expectedRtmr3.length > 0) {
                console2.log("Expected RTMR3 policy:");
                console2.logBytes(expectedRtmr3);
            } else {
                console2.log("Expected RTMR3 policy: disabled");
            }
            bytes32 expectedComposeHash = AutomataDcapTdxV4Attestation(dcapAddr).expectedComposeHash();
            if (expectedComposeHash != bytes32(0)) {
                console2.log("Expected Phala compose policy hash:");
                console2.logBytes32(expectedComposeHash);
            }
            try AutomataDcapTdxV4Attestation(dcapAddr).verifyParsedQuoteAndAttestOnChain(parsedQuote) returns (
                bytes memory output
            ) {
                console2.log("TDX V4 verification succeeded");
                console2.logBytes(output);
                if (output.length > 0) {
                    console2.log("TCB status:", uint8(output[0]));
                }
                return;
            } catch {}
        }

        bytes memory output = AutomataDcapTdxV4Attestation(dcapAddr).verifyAndAttestOnChain(quoteBytes);
        console2.log("TDX V4 verification succeeded");
        console2.logBytes(output);
        if (output.length > 0) {
            console2.log("TCB status:", uint8(output[0]));
        }
    }
}
