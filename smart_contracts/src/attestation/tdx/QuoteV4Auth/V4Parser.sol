// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {BytesUtils} from "automata-dcap-v3-attestation/utils/BytesUtils.sol";
import {V3Parser} from "automata-dcap-v3-attestation/v3/QuoteV3Auth/V3Parser.sol";
import {V3Struct} from "automata-dcap-v3-attestation/v3/QuoteV3Auth/V3Struct.sol";
import {V4Struct} from "./V4Struct.sol";
import {Base64} from "solady/utils/Base64.sol";
import {LibString} from "solady/utils/LibString.sol";

library V4Parser {
    using BytesUtils for bytes;

    string constant HEADER = "-----BEGIN CERTIFICATE-----";
    string constant FOOTER = "-----END CERTIFICATE-----";
    uint256 constant HEADER_LENGTH = 27;
    uint256 constant FOOTER_LENGTH = 25;

    uint256 constant HEADER_SIZE = 48;
    uint256 constant BODY_SIZE = 584;
    uint256 constant SIGNATURE_LENGTH_SIZE = 4;
    bytes2 constant SUPPORTED_QUOTE_VERSION = 0x0400;
    bytes2 constant SUPPORTED_ATTESTATION_KEY_TYPE = 0x0200;
    bytes4 constant SUPPORTED_TEE_TYPE = 0x81000000;
    bytes16 constant VALID_QE_VENDOR_ID = 0x939a7233f79c4ca9940a0db3957f0607;

    function parseInput(bytes memory quote)
        internal
        pure
        returns (bool success, V4Struct.ParsedV4Quote memory parsedQuote)
    {
        if (quote.length <= HEADER_SIZE + BODY_SIZE + SIGNATURE_LENGTH_SIZE) {
            return (false, parsedQuote);
        }

        bytes memory rawHeader = quote.substring(0, HEADER_SIZE);
        (bool headerSuccess, V4Struct.Header memory header) = parseAndVerifyHeader(rawHeader);
        if (!headerSuccess) {
            return (false, parsedQuote);
        }

        bytes memory rawBody = quote.substring(HEADER_SIZE, BODY_SIZE);
        V4Struct.Body memory body = parseBody(rawBody);

        uint256 signatureSectionLength =
            littleEndianDecode(quote.substring(HEADER_SIZE + BODY_SIZE, SIGNATURE_LENGTH_SIZE));
        uint256 expectedQuoteLength = HEADER_SIZE + BODY_SIZE + SIGNATURE_LENGTH_SIZE + signatureSectionLength;
        if (quote.length < expectedQuoteLength) {
            return (false, parsedQuote);
        }
        if (quote.length > expectedQuoteLength && !_isZeroPadded(quote, expectedQuoteLength)) {
            return (false, parsedQuote);
        }

        bytes memory signatureData = quote.substring(HEADER_SIZE + BODY_SIZE + SIGNATURE_LENGTH_SIZE, signatureSectionLength);
        (
            bool authDataSuccess,
            bytes memory quoteSignature,
            bytes memory attestationKey,
            bytes memory rawQeReport,
            V4Struct.QEReportCertificationData memory qeReportCertificationData
        ) = parseQuoteSignatureData(signatureData);
        if (!authDataSuccess) {
            return (false, parsedQuote);
        }

        parsedQuote.header = header;
        parsedQuote.body = body;
        parsedQuote.signedData = quote.substring(0, HEADER_SIZE + BODY_SIZE);
        parsedQuote.quoteSignature = quoteSignature;
        parsedQuote.attestationKey = attestationKey;
        parsedQuote.rawQeReport = rawQeReport;
        parsedQuote.qeReportCertificationData = qeReportCertificationData;
        success = true;
    }

    function validateParsedInput(V4Struct.ParsedV4Quote memory parsedQuote) internal pure {
        require(parsedQuote.header.version == SUPPORTED_QUOTE_VERSION, "unsupported quote version");
        require(parsedQuote.header.attestationKeyType == SUPPORTED_ATTESTATION_KEY_TYPE, "unsupported attestation key");
        require(parsedQuote.header.teeType == SUPPORTED_TEE_TYPE, "unsupported tee type");
        require(parsedQuote.header.qeVendorId == VALID_QE_VENDOR_ID, "invalid QE vendor");
        require(parsedQuote.signedData.length == HEADER_SIZE + BODY_SIZE, "invalid signed data length");
        require(parsedQuote.rawQeReport.length == 384, "invalid QE report length");
        require(parsedQuote.body.mrtd.length == 48, "invalid mrtd length");
        require(parsedQuote.body.rtmr0.length == 48, "invalid rtmr0 length");
        require(parsedQuote.body.rtmr1.length == 48, "invalid rtmr1 length");
        require(parsedQuote.body.rtmr2.length == 48, "invalid rtmr2 length");
        require(parsedQuote.body.rtmr3.length == 48, "invalid rtmr3 length");
        require(parsedQuote.body.reportData.length == 64, "invalid report data length");
        require(
            parsedQuote.quoteSignature.length == 64 && parsedQuote.attestationKey.length == 64
                && parsedQuote.qeReportCertificationData.qeReportSignature.length == 64,
            "invalid ECDSA signature format"
        );
        require(
            parsedQuote.qeReportCertificationData.qeAuthData.parsedDataSize
                == parsedQuote.qeReportCertificationData.qeAuthData.data.length,
            "invalid QEAuthData size"
        );
        require(
            parsedQuote.qeReportCertificationData.certification.certType == 5,
            "certType must be 5: Concatenated PCK Cert Chain (PEM formatted)"
        );
        require(parsedQuote.qeReportCertificationData.certification.decodedCertDataArray.length == 3, "3 certs in chain");

        bytes memory signedData = parsedQuote.signedData;
        bytes memory headerBytes = abi.encodePacked(
            parsedQuote.header.version,
            parsedQuote.header.attestationKeyType,
            parsedQuote.header.teeType,
            parsedQuote.header.reserved1,
            parsedQuote.header.reserved2,
            parsedQuote.header.qeVendorId,
            parsedQuote.header.userData
        );
        require(signedData.equals(0, headerBytes), "header mismatch");
        require(signedData.equals(HEADER_SIZE, abi.encodePacked(parsedQuote.body.teeTcbSvn)), "teeTcbSvn mismatch");
        require(signedData.equals(HEADER_SIZE + 136, parsedQuote.body.mrtd), "mrtd mismatch");
        require(signedData.equals(HEADER_SIZE + 328, parsedQuote.body.rtmr0), "rtmr0 mismatch");
        require(signedData.equals(HEADER_SIZE + 376, parsedQuote.body.rtmr1), "rtmr1 mismatch");
        require(signedData.equals(HEADER_SIZE + 424, parsedQuote.body.rtmr2), "rtmr2 mismatch");
        require(signedData.equals(HEADER_SIZE + 472, parsedQuote.body.rtmr3), "rtmr3 mismatch");
        require(signedData.equals(HEADER_SIZE + 520, parsedQuote.body.reportData), "report data mismatch");

        V3Struct.EnclaveReport memory qeReport = parsedQuote.qeReportCertificationData.qeReport;
        require(
            qeReport.reserved3.length == 96 && qeReport.reserved4.length == 60 && qeReport.reportData.length == 64,
            "QE report has wrong length"
        );

        bytes memory packedQeReport = V3Parser.packQEReport(qeReport);
        bytes memory rawQeReport = parsedQuote.rawQeReport;
        require(rawQeReport.equals(packedQeReport), "QE report mismatch");

        bytes32 expectedHash = bytes32(qeReport.reportData.substring(0, 32));
        bytes32 computedHash =
            sha256(abi.encodePacked(parsedQuote.attestationKey, parsedQuote.qeReportCertificationData.qeAuthData.data));
        require(expectedHash == computedHash, "QE auth hash mismatch");
    }

    function parseBody(bytes memory rawBody) internal pure returns (V4Struct.Body memory body) {
        body.teeTcbSvn = bytes16(rawBody.substring(0, 16));
        body.mrtd = rawBody.substring(136, 48);
        body.rtmr0 = rawBody.substring(328, 48);
        body.rtmr1 = rawBody.substring(376, 48);
        body.rtmr2 = rawBody.substring(424, 48);
        body.rtmr3 = rawBody.substring(472, 48);
        body.reportData = rawBody.substring(520, 64);
    }

    function parseQuoteSignatureData(bytes memory signatureData)
        internal
        pure
        returns (
            bool success,
            bytes memory quoteSignature,
            bytes memory attestationKey,
            bytes memory rawQeReport,
            V4Struct.QEReportCertificationData memory qeReportCertificationData
        )
    {
        if (signatureData.length < 64 + 64 + 2 + 4) {
            return (false, quoteSignature, attestationKey, rawQeReport, qeReportCertificationData);
        }

        uint256 offset = 0;
        quoteSignature = signatureData.substring(offset, 64);
        offset += 64;
        attestationKey = signatureData.substring(offset, 64);
        offset += 64;

        uint16 certType = uint16(littleEndianDecode(signatureData.substring(offset, 2)));
        offset += 2;
        uint32 certDataSize = uint32(littleEndianDecode(signatureData.substring(offset, 4)));
        offset += 4;
        if (signatureData.length < offset + certDataSize) {
            return (false, quoteSignature, attestationKey, rawQeReport, qeReportCertificationData);
        }
        bytes memory certificationData = signatureData.substring(offset, certDataSize);

        if (certType == 6) {
            (
                success,
                rawQeReport,
                qeReportCertificationData
            ) = parseQeReportCertificationData(certificationData, attestationKey);
            return (success, quoteSignature, attestationKey, rawQeReport, qeReportCertificationData);
        }

        if (certType == 5) {
            qeReportCertificationData.certification = V4Struct.CertificationData({
                certType: certType,
                certDataSize: certDataSize,
                decodedCertDataArray: _decodePemChain(certificationData, 3)
            });
            success = qeReportCertificationData.certification.decodedCertDataArray.length == 3;
            return (success, quoteSignature, attestationKey, rawQeReport, qeReportCertificationData);
        }
    }

    function parseQeReportCertificationData(bytes memory certificationData, bytes memory attestationKey)
        internal
        pure
        returns (
            bool success,
            bytes memory rawQeReport,
            V4Struct.QEReportCertificationData memory qeReportCertificationData
        )
    {
        if (certificationData.length < 384 + 64 + 2 + 2 + 4) {
            return (false, rawQeReport, qeReportCertificationData);
        }

        uint256 offset = 0;
        rawQeReport = certificationData.substring(offset, 384);
        offset += 384;
        qeReportCertificationData.qeReport = V3Parser.parseEnclaveReport(rawQeReport);
        qeReportCertificationData.qeReportSignature = certificationData.substring(offset, 64);
        offset += 64;

        uint16 qeAuthDataSize = uint16(littleEndianDecode(certificationData.substring(offset, 2)));
        offset += 2;
        if (certificationData.length < offset + qeAuthDataSize + 2 + 4) {
            return (false, rawQeReport, qeReportCertificationData);
        }

        qeReportCertificationData.qeAuthData = V3Struct.QEAuthData({
            parsedDataSize: qeAuthDataSize,
            data: certificationData.substring(offset, qeAuthDataSize)
        });
        offset += qeAuthDataSize;

        uint16 innerCertType = uint16(littleEndianDecode(certificationData.substring(offset, 2)));
        offset += 2;
        uint32 innerCertSize = uint32(littleEndianDecode(certificationData.substring(offset, 4)));
        offset += 4;
        if (certificationData.length < offset + innerCertSize) {
            return (false, rawQeReport, qeReportCertificationData);
        }

        bytes memory innerCertificationData = certificationData.substring(offset, innerCertSize);
        bytes[] memory decodedChain;
        if (innerCertType == 5) {
            decodedChain = _decodePemChain(innerCertificationData, 3);
        }

        qeReportCertificationData.certification = V4Struct.CertificationData({
            certType: innerCertType,
            certDataSize: innerCertSize,
            decodedCertDataArray: decodedChain
        });

        bytes32 expectedHash = bytes32(qeReportCertificationData.qeReport.reportData.substring(0, 32));
        bytes32 computedHash =
            sha256(abi.encodePacked(attestationKey, qeReportCertificationData.qeAuthData.data));

        success = innerCertType == 5 && decodedChain.length == 3 && expectedHash == computedHash;
    }

    function parseAndVerifyHeader(bytes memory rawHeader)
        internal
        pure
        returns (bool success, V4Struct.Header memory header)
    {
        bytes2 version = bytes2(rawHeader.substring(0, 2));
        if (version != SUPPORTED_QUOTE_VERSION) {
            return (false, header);
        }

        bytes2 attestationKeyType = bytes2(rawHeader.substring(2, 2));
        if (attestationKeyType != SUPPORTED_ATTESTATION_KEY_TYPE) {
            return (false, header);
        }

        bytes4 teeType = bytes4(rawHeader.substring(4, 4));
        if (teeType != SUPPORTED_TEE_TYPE) {
            return (false, header);
        }

        bytes16 qeVendorId = bytes16(rawHeader.substring(12, 16));
        if (qeVendorId != VALID_QE_VENDOR_ID) {
            return (false, header);
        }

        header = V4Struct.Header({
            version: version,
            attestationKeyType: attestationKeyType,
            teeType: teeType,
            reserved1: bytes2(rawHeader.substring(8, 2)),
            reserved2: bytes2(rawHeader.substring(10, 2)),
            qeVendorId: qeVendorId,
            userData: bytes20(rawHeader.substring(28, 20))
        });
        success = true;
    }

    function littleEndianDecode(bytes memory encoded) internal pure returns (uint256 decoded) {
        for (uint256 i = 0; i < encoded.length; i++) {
            uint256 digits = uint256(uint8(bytes1(encoded[i])));
            uint256 upperDigit = digits / 16;
            uint256 lowerDigit = digits % 16;

            uint256 acc = lowerDigit * (16 ** (2 * i));
            acc += upperDigit * (16 ** ((2 * i) + 1));

            decoded += acc;
        }
    }

    function _decodePemChain(bytes memory pemChain, uint256 size) private pure returns (bytes[] memory certs) {
        certs = new bytes[](size);
        string memory pemChainStr = string(pemChain);
        uint256 index = 0;
        uint256 len = pemChain.length;

        for (uint256 i = 0; i < size; i++) {
            string memory input = i == 0 ? pemChainStr : LibString.slice(pemChainStr, index, index + len);
            (bool removed, bytes memory certBody, uint256 increment) = _removeHeadersAndFooters(input);
            if (!removed) {
                delete certs;
                return certs;
            }

            certs[i] = Base64.decode(string(certBody));
            index += increment;
        }
    }

    function _removeHeadersAndFooters(string memory pemData)
        private
        pure
        returns (bool success, bytes memory extracted, uint256 endIndex)
    {
        uint256 beginPos = LibString.indexOf(pemData, HEADER);
        uint256 endPos = LibString.indexOf(pemData, FOOTER);

        if (beginPos == LibString.NOT_FOUND || endPos == LibString.NOT_FOUND) {
            return (false, extracted, endIndex);
        }

        uint256 contentStart = beginPos + HEADER_LENGTH;
        bytes memory delimiter = hex"0a";
        string memory contentSlice = LibString.slice(pemData, contentStart, endPos);
        string[] memory split = LibString.split(contentSlice, string(delimiter));
        string memory contentStr;

        for (uint256 i = 0; i < split.length; i++) {
            contentStr = LibString.concat(contentStr, split[i]);
        }

        extracted = bytes(contentStr);
        endIndex = endPos + FOOTER_LENGTH;
        success = true;
    }

    function _isZeroPadded(bytes memory quote, uint256 start) private pure returns (bool) {
        for (uint256 i = start; i < quote.length; i++) {
            if (quote[i] != bytes1(0)) {
                return false;
            }
        }
        return true;
    }
}
