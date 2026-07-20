// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract MedicalSignerRegistry {
    enum SignerRole {
        NONE,
        XRAY_DEVICE,
        RADIOLOGIST
    }

    struct Signer {
        bool known;
        bool active;
        SignerRole role;
        string displayName;
        bytes publicKeyDer;
        bytes certificateDer;
        bytes32 certificateFingerprint;
    }

    address public owner;
    uint256 public keySetVersion;
    mapping(bytes32 => Signer) private signers;
    bytes32[] private signerIds;

    event SignerConfigured(
        bytes32 indexed signerId,
        SignerRole indexed role,
        bool active,
        bytes32 certificateFingerprint,
        uint256 keySetVersion
    );
    event SignerStatusChanged(bytes32 indexed signerId, bool active, uint256 keySetVersion);

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    function configureSigner(
        bytes32 signerId,
        SignerRole role,
        string calldata displayName,
        bytes calldata publicKeyDer,
        bytes calldata certificateDer
    ) external onlyOwner {
        require(signerId != bytes32(0), "invalid signer id");
        require(role != SignerRole.NONE, "invalid signer role");
        require(bytes(displayName).length != 0, "display name required");
        require(publicKeyDer.length != 0, "public key required");
        require(certificateDer.length != 0, "certificate required");

        Signer storage signer = signers[signerId];
        if (!signer.known) {
            signer.known = true;
            signerIds.push(signerId);
        }
        signer.active = true;
        signer.role = role;
        signer.displayName = displayName;
        signer.publicKeyDer = publicKeyDer;
        signer.certificateDer = certificateDer;
        signer.certificateFingerprint = sha256(certificateDer);
        keySetVersion++;

        emit SignerConfigured(signerId, role, true, signer.certificateFingerprint, keySetVersion);
    }

    function setSignerActive(bytes32 signerId, bool active) external onlyOwner {
        Signer storage signer = signers[signerId];
        require(signer.known, "signer not configured");
        require(signer.active != active, "signer status unchanged");
        signer.active = active;
        keySetVersion++;
        emit SignerStatusChanged(signerId, active, keySetVersion);
    }

    function getSigner(bytes32 signerId)
        external
        view
        returns (
            bool active,
            SignerRole role,
            string memory displayName,
            bytes memory publicKeyDer,
            bytes memory certificateDer,
            bytes32 certificateFingerprint
        )
    {
        Signer storage signer = signers[signerId];
        require(signer.known, "signer not configured");
        return (
            signer.active,
            signer.role,
            signer.displayName,
            signer.publicKeyDer,
            signer.certificateDer,
            signer.certificateFingerprint
        );
    }

    function getActiveSignerIds(SignerRole role) external view returns (bytes32[] memory) {
        require(role != SignerRole.NONE, "invalid signer role");
        uint256 count;
        for (uint256 i = 0; i < signerIds.length; i++) {
            Signer storage signer = signers[signerIds[i]];
            if (signer.active && signer.role == role) {
                count++;
            }
        }

        bytes32[] memory activeSignerIds = new bytes32[](count);
        uint256 index;
        for (uint256 i = 0; i < signerIds.length; i++) {
            Signer storage signer = signers[signerIds[i]];
            if (signer.active && signer.role == role) {
                activeSignerIds[index] = signerIds[i];
                index++;
            }
        }
        return activeSignerIds;
    }
}
