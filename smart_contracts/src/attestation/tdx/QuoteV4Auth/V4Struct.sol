// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {V3Struct} from "automata-dcap-v3-attestation/v3/QuoteV3Auth/V3Struct.sol";

library V4Struct {
    struct Header {
        bytes2 version;
        bytes2 attestationKeyType;
        bytes4 teeType;
        bytes2 reserved1;
        bytes2 reserved2;
        bytes16 qeVendorId;
        bytes20 userData;
    }

    struct Body {
        bytes16 teeTcbSvn;
        bytes mrtd; // 48 bytes
        bytes rtmr3; // 48 bytes
        bytes reportData; // 64 bytes
    }

    struct CertificationData {
        uint16 certType;
        uint32 certDataSize;
        bytes[] decodedCertDataArray;
    }

    struct QEReportCertificationData {
        V3Struct.EnclaveReport qeReport;
        bytes qeReportSignature; // 64 bytes
        V3Struct.QEAuthData qeAuthData;
        CertificationData certification;
    }

    struct ParsedV4Quote {
        Header header;
        Body body;
        bytes signedData;
        bytes quoteSignature; // 64 bytes
        bytes attestationKey; // 64 bytes
        bytes rawQeReport; // 384 bytes
        QEReportCertificationData qeReportCertificationData;
    }
}
