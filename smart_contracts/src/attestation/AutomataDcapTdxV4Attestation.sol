// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IAttestation} from "automata-dcap-v3-attestation/interfaces/IAttestation.sol";
import {
    EnclaveIdentityJsonObj,
    IdentityObj,
    EnclaveIdTcbStatus
} from "@automata-network/on-chain-pccs/helpers/EnclaveIdentityHelper.sol";
import {
    FmspcTcbHelper,
    TcbInfoJsonObj,
    TcbInfoBasic,
    TCBLevelsObj,
    TDXModule,
    TDXModuleIdentity,
    TCBStatus
} from "@automata-network/on-chain-pccs/helpers/FmspcTcbHelper.sol";
import {EnclaveIdentityDao} from "@automata-network/on-chain-pccs/bases/EnclaveIdentityDao.sol";
import {FmspcTcbDao} from "@automata-network/on-chain-pccs/bases/FmspcTcbDao.sol";
import {PCKHelper, X509CertObj} from "@automata-network/on-chain-pccs/helpers/PCKHelper.sol";
import {X509CRLHelper} from "@automata-network/on-chain-pccs/helpers/X509CRLHelper.sol";
import {PcsDao, CA} from "@automata-network/on-chain-pccs/bases/PcsDao.sol";
import {PEMCertChainBase, PCKCertTCB} from "automata-dcap-v3-attestation/base/PEMCertChainBase.sol";
import {Sha384} from "./Sha384.sol";
import {V4Parser} from "./tdx/QuoteV4Auth/V4Parser.sol";
import {V4Struct} from "./tdx/QuoteV4Auth/V4Struct.sol";
import {Ownable} from "solady/auth/Ownable.sol";
import {LibString} from "solady/utils/LibString.sol";

contract AutomataDcapTdxV4Attestation is IAttestation, PEMCertChainBase, Ownable {
    using LibString for string;

    EnclaveIdentityDao public enclaveIdDao;
    FmspcTcbDao public tcbDao;

    error Failed_To_Verify_Quote();
    error ZK_Verification_Not_Supported();

    uint8 internal constant DEBUG_STAGE_OK = 0;
    uint8 internal constant DEBUG_STAGE_PARSE_FAILED = 1;
    uint8 internal constant DEBUG_STAGE_QUOTE_SIGNATURE_FAILED = 2;
    uint8 internal constant DEBUG_STAGE_CERT_CHAIN_LENGTH_FAILED = 3;
    uint8 internal constant DEBUG_STAGE_CERT_CHAIN_FAILED = 4;
    uint8 internal constant DEBUG_STAGE_QE_REPORT_SIGNATURE_FAILED = 5;
    uint8 internal constant DEBUG_STAGE_QE_IDENTITY_FAILED = 6;
    uint8 internal constant DEBUG_STAGE_TCB_INFO_MISSING = 7;
    uint8 internal constant DEBUG_STAGE_TCB_LEVEL_FAILED = 8;
    uint8 internal constant DEBUG_STAGE_RTMR3_POLICY_FAILED = 9;

    bytes public expectedRtmr3;
    bytes32 public expectedComposeHash;
    bytes public expectedComposeEventDigest;
    mapping(bytes32 => bool) public expectedComposeEventDigestAllowed;
    uint256 public expectedComposeEventDigestAllowedCount;

    event ExpectedRtmr3Updated(bytes expectedRtmr3);
    event ExpectedComposeHashUpdated(bytes32 expectedComposeHash);
    event ExpectedComposeEventDigestUpdated(bytes expectedComposeEventDigest);
    event ExpectedComposeEventDigestAllowed(bytes expectedComposeEventDigest, bool allowed);

    constructor(
        address enclaveIdDaoAddr,
        address pckHelperAddr,
        address tcbDaoAddr,
        address crlHelperAddr,
        address pcsDaoAddr,
        address p256VerifierAddr
    ) PEMCertChainBase(pckHelperAddr, crlHelperAddr, pcsDaoAddr, p256VerifierAddr) {
        _initializeOwner(msg.sender);
        enclaveIdDao = EnclaveIdentityDao(enclaveIdDaoAddr);
        tcbDao = FmspcTcbDao(tcbDaoAddr);
    }

    function updateConfig(
        address enclaveIdDaoAddr,
        address pckHelperAddr,
        address tcbDaoAddr,
        address crlHelperAddr,
        address pcsDaoAddr,
        address p256VerifierAddr
    ) external onlyOwner {
        enclaveIdDao = EnclaveIdentityDao(enclaveIdDaoAddr);
        tcbDao = FmspcTcbDao(tcbDaoAddr);
        _setCertBaseConfig(pckHelperAddr, crlHelperAddr, pcsDaoAddr, p256VerifierAddr);
    }

    function setExpectedRtmr3(bytes calldata _expectedRtmr3) external onlyOwner {
        require(_expectedRtmr3.length == 0 || _expectedRtmr3.length == 48, "expected RTMR3 must be 48 bytes");
        expectedRtmr3 = _expectedRtmr3;
        emit ExpectedRtmr3Updated(_expectedRtmr3);
    }

    function setExpectedRtmr3FromQuote(bytes calldata quote) external onlyOwner {
        (bool success, V4Struct.ParsedV4Quote memory parsedQuote) = V4Parser.parseInput(bytes(quote));
        require(success, "failed to parse reference quote");
        V4Parser.validateParsedInput(parsedQuote);
        expectedRtmr3 = parsedQuote.body.rtmr3;
        emit ExpectedRtmr3Updated(parsedQuote.body.rtmr3);
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

    function setExpectedComposeEventDigestAllowed(bytes calldata _expectedComposeEventDigest, bool allowed)
        external
        onlyOwner
    {
        require(_expectedComposeEventDigest.length == 48, "expected compose event digest must be 48 bytes");
        bytes32 digestHash = keccak256(_expectedComposeEventDigest);
        bool wasAllowed = expectedComposeEventDigestAllowed[digestHash];

        if (wasAllowed != allowed) {
            expectedComposeEventDigestAllowed[digestHash] = allowed;
            if (allowed) {
                expectedComposeEventDigestAllowedCount++;
            } else {
                expectedComposeEventDigestAllowedCount--;
            }
        }

        emit ExpectedComposeEventDigestAllowed(_expectedComposeEventDigest, allowed);
    }

    function verifyAndAttestOnChain(bytes calldata input) external view override returns (bytes memory output) {
        bool verified;
        (verified, output) = _verify(input, true);
        if (!verified) {
            revert Failed_To_Verify_Quote();
        }
    }

    function verifyParsedQuoteAndAttestOnChain(V4Struct.ParsedV4Quote calldata parsedQuote)
        external
        view
        returns (bytes memory output)
    {
        // Parsed quotes can be prepared off-chain to avoid the most expensive on-chain parsing work.
        V4Struct.ParsedV4Quote memory parsedQuoteMemory = parsedQuote;
        V4Parser.validateParsedInput(parsedQuoteMemory);
        bool verified;
        (verified, output) = _verifyParsedQuote(parsedQuoteMemory, true);
        if (!verified) {
            revert Failed_To_Verify_Quote();
        }
    }

    function verifyAndAttestOnChainWithRtmr3Events(bytes calldata input, bytes[] calldata rtmr3EventDigests)
        external
        view
        returns (bytes memory output)
    {
        bool verified;
        (verified, output) = _verify(input, false);
        if (!verified) {
            revert Failed_To_Verify_Quote();
        }

        (bool success, V4Struct.ParsedV4Quote memory parsedQuote) = V4Parser.parseInput(bytes(input));
        if (!success || !_rtmr3EventsPolicySatisfied(parsedQuote.body.rtmr3, rtmr3EventDigests)) {
            revert Failed_To_Verify_Quote();
        }
    }

    function verifyAndAttestWithZKProof(bytes calldata, bytes calldata)
        external
        pure
        override
        returns (bytes memory)
    {
        revert ZK_Verification_Not_Supported();
    }

    function debugVerify(bytes calldata input)
        external
        view
        returns (
            uint8 stage,
            uint8 qeTcbStatus,
            uint8 tcbStatus,
            uint16 pcesvn,
            bytes6 fmspc,
            bytes16 teeTcbSvn,
            uint16 qeIsvProdId,
            uint16 qeIsvSvn
        )
    {
        (
            stage,
            qeTcbStatus,
            tcbStatus,
            pcesvn,
            fmspc,
            teeTcbSvn,
            qeIsvProdId,
            qeIsvSvn
        ) = _debugVerify(input);
    }

    function _verify(bytes calldata quote, bool enforceExactRtmr3) private view returns (bool verified, bytes memory output) {
        (bool success, V4Struct.ParsedV4Quote memory parsedQuote) = V4Parser.parseInput(bytes(quote));
        if (!success) {
            return (false, output);
        }
        return _verifyParsedQuote(parsedQuote, enforceExactRtmr3);
    }

    function _debugVerify(bytes calldata quote)
        private
        view
        returns (
            uint8 stage,
            uint8 qeTcbStatusCode,
            uint8 tcbStatusCode,
            uint16 pcesvn,
            bytes6 fmspc,
            bytes16 teeTcbSvn,
            uint16 qeIsvProdId,
            uint16 qeIsvSvn
        )
    {
        (bool success, V4Struct.ParsedV4Quote memory parsedQuote) = V4Parser.parseInput(bytes(quote));
        if (!success) {
            return (DEBUG_STAGE_PARSE_FAILED, 0, 0, 0, 0x000000000000, 0x0, 0, 0);
        }

        return _debugVerifyParsedQuote(parsedQuote, true);
    }

    function _verifyParsedQuote(V4Struct.ParsedV4Quote memory parsedQuote, bool enforceExactRtmr3)
        private
        view
        returns (bool verified, bytes memory output)
    {
        (
            uint8 stage,
            ,
            uint8 tcbStatusCode,
            ,
            bytes6 fmspcBytes,
            ,
            ,
        ) = _debugVerifyParsedQuote(parsedQuote, enforceExactRtmr3);
        if (stage != DEBUG_STAGE_OK) {
            return (false, output);
        }

        output = abi.encodePacked(TCBStatus(tcbStatusCode), parsedQuote.body.mrtd, parsedQuote.body.reportData, fmspcBytes);
        verified = true;
    }

    function _debugVerifyParsedQuote(V4Struct.ParsedV4Quote memory parsedQuote, bool enforceExactRtmr3)
        private
        view
        returns (
            uint8 stage,
            uint8 qeTcbStatusCode,
            uint8 tcbStatusCode,
            uint16 pcesvn,
            bytes6 fmspc,
            bytes16 teeTcbSvn,
            uint16 qeIsvProdId,
            uint16 qeIsvSvn
        )
    {
        teeTcbSvn = parsedQuote.body.teeTcbSvn;
        qeIsvProdId = parsedQuote.qeReportCertificationData.qeReport.isvProdId;
        qeIsvSvn = parsedQuote.qeReportCertificationData.qeReport.isvSvn;

        bool quoteSigVerified =
            _ecdsaVerify(sha256(parsedQuote.signedData), parsedQuote.quoteSignature, parsedQuote.attestationKey);
        if (!quoteSigVerified) {
            return (DEBUG_STAGE_QUOTE_SIGNATURE_FAILED, 0, 0, 0, 0x000000000000, teeTcbSvn, qeIsvProdId, qeIsvSvn);
        }

        if (parsedQuote.qeReportCertificationData.certification.decodedCertDataArray.length != 3) {
            return (DEBUG_STAGE_CERT_CHAIN_LENGTH_FAILED, 0, 0, 0, 0x000000000000, teeTcbSvn, qeIsvProdId, qeIsvSvn);
        }

        X509CertObj[] memory parsedCerts = new X509CertObj[](3);
        PCKCertTCB memory pckTcb;
        for (uint256 i = 0; i < 3; i++) {
            bytes memory der = parsedQuote.qeReportCertificationData.certification.decodedCertDataArray[i];
            parsedCerts[i] = pckHelper.parseX509DER(der);
            if (i == 0) {
                pckTcb = _parsePck(der, parsedCerts[i].extensionPtr);
            }
        }
        pcesvn = pckTcb.pcesvn;
        fmspc = bytes6(pckTcb.fmspcBytes);

        if (!_verifyCertChain(parsedCerts)) {
            return (DEBUG_STAGE_CERT_CHAIN_FAILED, 0, 0, pcesvn, fmspc, teeTcbSvn, qeIsvProdId, qeIsvSvn);
        }

        bool qeReportSigVerified = _ecdsaVerify(
            sha256(parsedQuote.rawQeReport),
            parsedQuote.qeReportCertificationData.qeReportSignature,
            parsedCerts[0].subjectPublicKey
        );
        if (!qeReportSigVerified) {
            return (
                DEBUG_STAGE_QE_REPORT_SIGNATURE_FAILED,
                0,
                0,
                pcesvn,
                fmspc,
                teeTcbSvn,
                qeIsvProdId,
                qeIsvSvn
            );
        }

        EnclaveIdTcbStatus qeTcbStatus;
        bool enclaveIdentityVerified;
        (enclaveIdentityVerified, qeTcbStatus) = _verifyQeReportWithTdIdentity(
            parsedQuote.qeReportCertificationData.qeReport.miscSelect,
            parsedQuote.qeReportCertificationData.qeReport.attributes,
            parsedQuote.qeReportCertificationData.qeReport.mrSigner,
            parsedQuote.qeReportCertificationData.qeReport.isvProdId,
            parsedQuote.qeReportCertificationData.qeReport.isvSvn
        );
        qeTcbStatusCode = uint8(qeTcbStatus);
        if (!enclaveIdentityVerified) {
            return (
                DEBUG_STAGE_QE_IDENTITY_FAILED,
                qeTcbStatusCode,
                0,
                pcesvn,
                fmspc,
                teeTcbSvn,
                qeIsvProdId,
                qeIsvSvn
            );
        }

        (
            bool tcbInfoFound,
            TCBLevelsObj[] memory tcbLevels,
            TDXModuleIdentity[] memory moduleIdentities
        ) = _getTdxTcbInfo(bytes6(pckTcb.fmspcBytes));
        if (!tcbInfoFound) {
            return (
                DEBUG_STAGE_TCB_INFO_MISSING,
                qeTcbStatusCode,
                0,
                pcesvn,
                fmspc,
                teeTcbSvn,
                qeIsvProdId,
                qeIsvSvn
            );
        }

        bool tcbVerified;
        TCBStatus tcbStatus;
        (tcbVerified, tcbStatus) = _checkTdxTcbLevels(
            qeTcbStatus,
            pckTcb,
            parsedQuote.body.teeTcbSvn,
            tcbLevels,
            moduleIdentities
        );
        tcbStatusCode = uint8(tcbStatus);
        if (!tcbVerified) {
            return (
                DEBUG_STAGE_TCB_LEVEL_FAILED,
                qeTcbStatusCode,
                tcbStatusCode,
                pcesvn,
                fmspc,
                teeTcbSvn,
                qeIsvProdId,
                qeIsvSvn
            );
        }

        if (enforceExactRtmr3 && !_rtmr3PolicySatisfied(parsedQuote.body.rtmr3)) {
            return (
                DEBUG_STAGE_RTMR3_POLICY_FAILED,
                qeTcbStatusCode,
                tcbStatusCode,
                pcesvn,
                fmspc,
                teeTcbSvn,
                qeIsvProdId,
                qeIsvSvn
            );
        }

        return (DEBUG_STAGE_OK, qeTcbStatusCode, tcbStatusCode, pcesvn, fmspc, teeTcbSvn, qeIsvProdId, qeIsvSvn);
    }

    function _rtmr3PolicySatisfied(bytes memory rtmr3) private view returns (bool) {
        if (expectedRtmr3.length == 0) {
            return true;
        }
        return keccak256(rtmr3) == keccak256(expectedRtmr3);
    }

    function _rtmr3EventsPolicySatisfied(bytes memory quoteRtmr3, bytes[] calldata eventDigests)
        private
        view
        returns (bool)
    {
        if (eventDigests.length == 0) {
            return false;
        }

        bool hasSingleExpectedComposeEvent = expectedComposeEventDigest.length > 0;
        bool hasAllowedComposeEvents = expectedComposeEventDigestAllowedCount > 0;
        bool foundExpectedComposeEvent = !hasSingleExpectedComposeEvent && !hasAllowedComposeEvents;
        bytes memory replayedRtmr = new bytes(48);
        for (uint256 i = 0; i < eventDigests.length; i++) {
            if (eventDigests[i].length != 48) {
                return false;
            }
            bytes32 eventDigestHash = keccak256(eventDigests[i]);
            if (hasSingleExpectedComposeEvent && eventDigestHash == keccak256(expectedComposeEventDigest)) {
                foundExpectedComposeEvent = true;
            }
            if (hasAllowedComposeEvents && expectedComposeEventDigestAllowed[eventDigestHash]) {
                foundExpectedComposeEvent = true;
            }
            replayedRtmr = Sha384.hashRtmrExtend(replayedRtmr, eventDigests[i]);
        }

        return foundExpectedComposeEvent && keccak256(replayedRtmr) == keccak256(quoteRtmr3);
    }

    function _verifyQeReportWithTdIdentity(
        bytes4 enclaveReportMiscselect,
        bytes16 enclaveReportAttributes,
        bytes32 enclaveReportMrsigner,
        uint16 enclaveReportIsvprodid,
        uint16 enclaveReportIsvSvn
    ) private view returns (bool, EnclaveIdTcbStatus status) {
        bytes32 key = enclaveIdDao.ENCLAVE_ID_KEY(uint256(2), uint256(4));
        bytes memory data = enclaveIdDao.getAttestedData(key);
        if (data.length == 0) {
            return (false, status);
        }

        (IdentityObj memory identity, EnclaveIdentityJsonObj memory enclaveIdentityObj) =
            abi.decode(data, (IdentityObj, EnclaveIdentityJsonObj));
        enclaveIdentityObj;

        bool miscselectMatched = enclaveReportMiscselect & identity.miscselectMask == identity.miscselect;
        bool attributesMatched = enclaveReportAttributes & identity.attributesMask == identity.attributes;
        bool mrsignerMatched = enclaveReportMrsigner == identity.mrsigner;
        bool isvprodidMatched = enclaveReportIsvprodid == identity.isvprodid;

        bool tcbFound;
        for (uint256 i = 0; i < identity.tcb.length; i++) {
            if (identity.tcb[i].isvsvn <= enclaveReportIsvSvn) {
                tcbFound = true;
                status = identity.tcb[i].status;
                break;
            }
        }

        return (miscselectMatched && attributesMatched && mrsignerMatched && isvprodidMatched && tcbFound, status);
    }

    function _getTdxTcbInfo(bytes6 fmspc)
        private
        view
        returns (bool success, TCBLevelsObj[] memory tcbLevels, TDXModuleIdentity[] memory moduleIdentities)
    {
        bytes32 key = tcbDao.FMSPC_TCB_KEY(uint8(1), fmspc, uint32(3));
        bytes memory data = tcbDao.getAttestedData(key);
        if (data.length == 0) {
            return (false, tcbLevels, moduleIdentities);
        }

        (, , bytes memory encodedModuleIdentities, bytes memory encodedTcbLevels,) =
            abi.decode(data, (TcbInfoBasic, TDXModule, bytes, bytes, TcbInfoJsonObj));

        FmspcTcbHelper helper = tcbDao.FmspcTcbLib();

        bytes[] memory moduleIdentityBlobs = abi.decode(encodedModuleIdentities, (bytes[]));
        moduleIdentities = new TDXModuleIdentity[](moduleIdentityBlobs.length);
        for (uint256 i = 0; i < moduleIdentityBlobs.length; i++) {
            moduleIdentities[i] = helper.tdxModuleIdentityFromBytes(moduleIdentityBlobs[i]);
        }

        bytes[] memory tcbLevelBlobs = abi.decode(encodedTcbLevels, (bytes[]));
        tcbLevels = new TCBLevelsObj[](tcbLevelBlobs.length);
        for (uint256 i = 0; i < tcbLevelBlobs.length; i++) {
            tcbLevels[i] = helper.tcbLevelsObjFromBytes(tcbLevelBlobs[i]);
        }

        success = true;
    }

    function _checkTdxTcbLevels(
        EnclaveIdTcbStatus qeTcbStatus,
        PCKCertTCB memory pckTcb,
        bytes16 teeTcbSvn,
        TCBLevelsObj[] memory tcbLevels,
        TDXModuleIdentity[] memory moduleIdentities
    ) private pure returns (bool, TCBStatus status) {
        bool matched;
        uint256 tdxStartIndex = uint8(teeTcbSvn[1]) >= 1 ? 2 : 0;

        for (uint256 i = 0; i < tcbLevels.length; i++) {
            TCBLevelsObj memory current = tcbLevels[i];
            bool pceSvnIsHigherOrGreater = pckTcb.pcesvn >= current.pcesvn;
            bool cpuSvnsAreHigherOrGreater = _isSvnArrayHigherOrGreater(pckTcb.cpusvns, current.sgxComponentCpuSvns, 0);
            bool tdxSvnsAreHigherOrGreater =
                _isBytes16HigherOrGreater(teeTcbSvn, current.tdxComponentCpuSvns, tdxStartIndex);
            if (pceSvnIsHigherOrGreater && cpuSvnsAreHigherOrGreater && tdxSvnsAreHigherOrGreater) {
                matched = true;
                status = _adjustStatusForQe(current.status, qeTcbStatus);
                break;
            }
        }

        if (!matched || status == TCBStatus.TCB_REVOKED || status == TCBStatus.TCB_UNRECOGNIZED) {
            return (false, status);
        }

        uint8 tdxModuleVersion = uint8(teeTcbSvn[1]);
        if (tdxModuleVersion >= 1) {
            (bool moduleMatched, TCBStatus moduleStatus) =
                _checkTdxModuleIdentity(teeTcbSvn, tdxModuleVersion, moduleIdentities);
            if (!moduleMatched) {
                return (false, moduleStatus);
            }
            status = _combineTcbStatuses(status, moduleStatus);
            if (status == TCBStatus.TCB_REVOKED || status == TCBStatus.TCB_UNRECOGNIZED) {
                return (false, status);
            }
        }

        return (true, status);
    }

    function _checkTdxModuleIdentity(
        bytes16 teeTcbSvn,
        uint8 tdxModuleVersion,
        TDXModuleIdentity[] memory moduleIdentities
    ) private pure returns (bool, TCBStatus status) {
        string memory expectedId = tdxModuleVersion < 10
            ? string.concat("TDX_0", LibString.toString(uint256(tdxModuleVersion)))
            : string.concat("TDX_", LibString.toString(uint256(tdxModuleVersion)));

        for (uint256 i = 0; i < moduleIdentities.length; i++) {
            if (moduleIdentities[i].id.eq(expectedId)) {
                for (uint256 j = 0; j < moduleIdentities[i].tcbLevels.length; j++) {
                    if (uint8(teeTcbSvn[0]) >= moduleIdentities[i].tcbLevels[j].isvsvn) {
                        return (true, moduleIdentities[i].tcbLevels[j].status);
                    }
                }
                return (false, TCBStatus.TCB_UNRECOGNIZED);
            }
        }

        return (false, TCBStatus.TCB_UNRECOGNIZED);
    }

    function _adjustStatusForQe(TCBStatus currentStatus, EnclaveIdTcbStatus qeTcbStatus)
        private
        pure
        returns (TCBStatus status)
    {
        status = currentStatus;
        if (qeTcbStatus == EnclaveIdTcbStatus.SGX_ENCLAVE_REPORT_ISVSVN_OUT_OF_DATE) {
            if (currentStatus == TCBStatus.OK || currentStatus == TCBStatus.TCB_SW_HARDENING_NEEDED) {
                status = TCBStatus.TCB_OUT_OF_DATE;
            }
            if (
                currentStatus == TCBStatus.TCB_CONFIGURATION_NEEDED
                    || currentStatus == TCBStatus.TCB_CONFIGURATION_AND_SW_HARDENING_NEEDED
            ) {
                status = TCBStatus.TCB_OUT_OF_DATE_CONFIGURATION_NEEDED;
            }
        }
    }

    function _combineTcbStatuses(TCBStatus a, TCBStatus b) private pure returns (TCBStatus) {
        if (a == TCBStatus.TCB_UNRECOGNIZED || b == TCBStatus.TCB_UNRECOGNIZED) {
            return TCBStatus.TCB_UNRECOGNIZED;
        }
        if (a == TCBStatus.TCB_REVOKED || b == TCBStatus.TCB_REVOKED) {
            return TCBStatus.TCB_REVOKED;
        }

        return _severity(a) >= _severity(b) ? a : b;
    }

    function _severity(TCBStatus status) private pure returns (uint256) {
        if (status == TCBStatus.TCB_OUT_OF_DATE_CONFIGURATION_NEEDED) return 6;
        if (status == TCBStatus.TCB_OUT_OF_DATE) return 5;
        if (status == TCBStatus.TCB_CONFIGURATION_AND_SW_HARDENING_NEEDED) return 4;
        if (status == TCBStatus.TCB_CONFIGURATION_NEEDED) return 3;
        if (status == TCBStatus.TCB_SW_HARDENING_NEEDED) return 2;
        if (status == TCBStatus.OK) return 1;
        return 0;
    }

    function _isSvnArrayHigherOrGreater(
        uint8[] memory lhs,
        uint8[] memory rhs,
        uint256 startIndex
    ) private pure returns (bool) {
        if (lhs.length != rhs.length) {
            return false;
        }
        for (uint256 i = startIndex; i < lhs.length; i++) {
            if (lhs[i] < rhs[i]) {
                return false;
            }
        }
        return true;
    }

    function _isBytes16HigherOrGreater(
        bytes16 lhs,
        uint8[] memory rhs,
        uint256 startIndex
    ) private pure returns (bool) {
        if (rhs.length != 16) {
            return false;
        }
        for (uint256 i = startIndex; i < 16; i++) {
            if (uint8(lhs[i]) < rhs[i]) {
                return false;
            }
        }
        return true;
    }
}
