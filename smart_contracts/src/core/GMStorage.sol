// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import {StrictECDSA} from "../crypto/StrictECDSA.sol";

interface IDeviceRegistry {
    function isAuthorized(address _address) external view returns (bool);
    function getAuthorizedDevices() external view returns (address[] memory);
    function getDevice(address _address) external view returns (bool, string memory, string memory, bytes memory);
    function actionKeys(address participant) external view returns (address);
    function resolveAuthorizedParticipant(address actionKey) external view returns (address participant);
}

interface IAggregatorSelection {
    function isAggregator(address _address) external view returns (bool);
    function lastSelectionRound() external view returns (uint256);
}

interface IAggregationPolicy {
    function gmStorage() external view returns (address);
    function openRound(uint256 round) external;
    function recordSubmission(uint256 round, address worker, bytes32 submissionCommitment) external;
    function closeRound(uint256 round, uint256 reportedSubmissionCount) external;
    function verifyAndRecordPublication(
        uint256 round,
        address aggregator,
        address actionKey,
        bytes32 outputModelHash,
        bytes32 outputBundleHash,
        bytes32 publicationHash,
        bytes calldata signature
    ) external returns (bytes32 statementDigest);
}

contract GMStorage {
    using StrictECDSA for bytes32;

    bytes32 public constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 public constant MODEL_SUBMISSION_TYPEHASH = keccak256(
        "ModelSubmission(uint256 round,address aggregator,address worker,bytes32 parentModelHash,bytes32 modelHash,bytes32 packageHash,uint256 nonce)"
    );
    bytes32 private constant EIP712_NAME_HASH = keccak256("VITA-FL GMStorage");
    bytes32 private constant EIP712_VERSION_HASH = keccak256("1");

    /// @notice Content-addressed, public model from which bootstrap round 0 starts.
    /// @dev This CID is fixed at deployment. It is the integrity anchor for the
    ///      unsigned plaintext bootstrap model and is never overwritten.
    string public initialModelCid;
    string public globalModel;
    string public backupGlobalModel;
    string public globalModelSignature;
    string public backupGlobalModelSignature;
    string public globalModelKeyBundle;
    string public backupGlobalModelKeyBundle;
    uint256 public round;
    uint256 public completedRoundCount;
    address public lastRoundAggregator;
    address public owner;
    address public device_registry_address;
    address public aggregator_selection_address;
    address public aggregation_policy_address;
    mapping(address => uint256) public contributions;
    mapping(uint256 => mapping(address => bool)) public modelSubmitted;
    mapping(uint256 => mapping(address => bytes32)) public modelSubmissionHash;
    mapping(uint256 => mapping(address => bytes32)) public modelSubmissionPackageHash;
    mapping(uint256 => mapping(address => bytes32)) public modelSubmissionParentModelHash;
    mapping(uint256 => mapping(address => uint256)) public modelSubmissionNonce;
    mapping(uint256 => mapping(address => bytes32)) public modelSubmissionCommitment;
    mapping(address => uint256) public workerSubmissionNonces;
    mapping(uint256 => uint256) public modelSubmissionCount;
    mapping(uint256 => bool) public modelSubmissionsClosed;
    mapping(uint256 => bool) public globalModelPublished;
    mapping(uint256 => address) public globalModelPublisher;
    mapping(uint256 => bytes) public globalModelPublisherPublicKey;
    mapping(uint256 => bytes32) public globalModelPlaintextHash;
    mapping(uint256 => bytes32) public globalModelBundleHash;
    mapping(uint256 => bytes32) public globalModelPublicationHash;
    mapping(uint256 => bytes32) public aggregationStatementDigest;
    mapping(uint256 => bool) public roundCompleted;
    mapping(uint256 => bool) public roundAborted;
    bool public hasFinalizedModel;
    uint256 public lastFinalizedModelRound;
    bytes public activeModelPublisherPublicKey;
    bytes public lastFinalizedPublisherPublicKey;
    mapping(uint256 => mapping(address => mapping(bytes32 => bool))) public penaltyApplied;
    address[] private contributors;

    event ModelSubmissionRecorded(
        uint256 indexed round,
        address indexed aggregator,
        address indexed worker,
        bytes32 modelHash,
        bytes32 packageHash,
        bytes32 parentModelHash,
        uint256 workerNonce
    );
    event ModelSubmissionsClosed(uint256 indexed round, address indexed aggregator, uint256 acceptedModels);
    event ContributionIncremented(uint256 indexed round, address indexed device, uint256 score);
    event ContributionDecremented(uint256 indexed round, address indexed device, uint256 score, string reason);
    event GlobalModelPublished(
        uint256 indexed round,
        address indexed aggregator,
        string model,
        string signature,
        string keyBundle,
        bytes32 outputModelHash,
        bytes32 outputBundleHash,
        bytes32 publicationHash,
        bytes32 statementDigest
    );
    event AggregationPolicyAddressSet(address indexed aggregationPolicy);
    event RoundAborted(uint256 indexed round, address indexed failedAggregator);

    constructor(
        address _device_registry_address,
        address _aggregator_selection_address,
        string memory _initial_GM_CID
    ) {
        require(bytes(_initial_GM_CID).length > 0, "Initial model CID is empty");
        initialModelCid = _initial_GM_CID;
        globalModel = _initial_GM_CID;
        backupGlobalModel = _initial_GM_CID;
        // The public bootstrap model deliberately has neither an origin
        // signature nor an encryption-key bundle. W0 creates both artifacts
        // when it finalizes round 0 through the normal aggregation path.
        globalModelSignature = "";
        backupGlobalModelSignature = "";
        globalModelKeyBundle = "";
        backupGlobalModelKeyBundle = "";
        device_registry_address = _device_registry_address;
        aggregator_selection_address = _aggregator_selection_address;
        round = 0;
        owner = msg.sender;
        lastRoundAggregator = address(0);
    }

    function setAggregationPolicyAddress(address aggregationPolicy) external {
        require(msg.sender == owner, "not GMStorage owner");
        require(aggregation_policy_address == address(0), "aggregation policy already configured");
        require(aggregationPolicy != address(0), "aggregation policy is zero");
        require(aggregationPolicy.code.length > 0, "aggregation policy has no code");
        require(
            IAggregationPolicy(aggregationPolicy).gmStorage() == address(this),
            "aggregation policy is bound to another GMStorage"
        );
        aggregation_policy_address = aggregationPolicy;
        emit AggregationPolicyAddressSet(aggregationPolicy);
    }

    function setGlobalModel(string memory _newGlobalModel) external {
        _newGlobalModel;
        revert("Encrypted GM flow requires setGlobalModelAndSignatureAndKeyBundle");
    }

    function setGlobalModelSignature(string memory _newGlobalModelSignature) external {
        _newGlobalModelSignature;
        revert("Encrypted GM flow requires setGlobalModelAndSignatureAndKeyBundle");
    }

    function setGlobalModelAndSignature(string memory _newGlobalModel, string memory _newGlobalModelSignature)
        external
    {
        _newGlobalModel;
        _newGlobalModelSignature;
        revert("Encrypted GM flow requires setGlobalModelAndSignatureAndKeyBundle");
    }

    function setGlobalModelAndSignatureAndKeyBundle(
        string memory _newGlobalModel,
        string memory _newGlobalModelSignature,
        string memory _newGlobalModelKeyBundle
    ) external {
        _newGlobalModel;
        _newGlobalModelSignature;
        _newGlobalModelKeyBundle;
        revert("Aggregation statement and atomic finalization required");
    }

    function finalizeRoundWithAggregation(
        string calldata newGlobalModel,
        string calldata newGlobalModelSignature,
        string calldata newGlobalModelKeyBundle,
        bytes32 outputModelHash,
        bytes32 outputBundleHash,
        bytes calldata statementSignature
    ) external {
        address aggregator = _requireActiveAuthorizedAggregator();
        uint256 sourceRound = round;
        require(aggregation_policy_address != address(0), "aggregation policy is not configured");
        require(modelSubmissionsClosed[sourceRound], "Model submissions are still open");
        require(!globalModelPublished[sourceRound], "Global model already published");
        require(bytes(newGlobalModel).length > 0, "Model CID is empty");
        require(bytes(newGlobalModelSignature).length > 0, "Model signature CID is empty");
        require(bytes(newGlobalModelKeyBundle).length > 0, "Model key bundle CID is empty");
        require(outputModelHash != bytes32(0), "Output model hash is zero");
        require(outputBundleHash != bytes32(0), "Output bundle hash is zero");

        IDeviceRegistry deviceRegistry = IDeviceRegistry(device_registry_address);
        (bool publisherAuthorized,,, bytes memory publisherPublicKey) = deviceRegistry.getDevice(aggregator);
        require(publisherAuthorized, "Publisher is not authorized");
        require(publisherPublicKey.length > 0, "Publisher public key is empty");
        address actionKey = deviceRegistry.actionKeys(aggregator);
        require(actionKey == msg.sender, "Caller is not current aggregator action key");

        bytes32 publicationHash =
            keccak256(abi.encode(newGlobalModel, newGlobalModelSignature, newGlobalModelKeyBundle));
        bytes32 statementDigest = IAggregationPolicy(aggregation_policy_address).verifyAndRecordPublication(
            sourceRound,
            aggregator,
            actionKey,
            outputModelHash,
            outputBundleHash,
            publicationHash,
            statementSignature
        );

        backupGlobalModel = globalModel;
        backupGlobalModelSignature = globalModelSignature;
        backupGlobalModelKeyBundle = globalModelKeyBundle;
        globalModel = newGlobalModel;
        globalModelSignature = newGlobalModelSignature;
        globalModelKeyBundle = newGlobalModelKeyBundle;
        globalModelPublished[sourceRound] = true;
        globalModelPublisher[sourceRound] = aggregator;
        globalModelPublisherPublicKey[sourceRound] = publisherPublicKey;
        globalModelPlaintextHash[sourceRound] = outputModelHash;
        globalModelBundleHash[sourceRound] = outputBundleHash;
        globalModelPublicationHash[sourceRound] = publicationHash;
        aggregationStatementDigest[sourceRound] = statementDigest;

        roundCompleted[sourceRound] = true;
        completedRoundCount++;
        activeModelPublisherPublicKey = publisherPublicKey;
        lastFinalizedPublisherPublicKey = publisherPublicKey;
        hasFinalizedModel = true;
        lastFinalizedModelRound = sourceRound + 1;
        lastRoundAggregator = aggregator;
        addContributor(aggregator);
        contributions[aggregator]++;
        round = sourceRound + 1;

        emit GlobalModelPublished(
            sourceRound,
            aggregator,
            newGlobalModel,
            newGlobalModelSignature,
            newGlobalModelKeyBundle,
            outputModelHash,
            outputBundleHash,
            publicationHash,
            statementDigest
        );
        emit ContributionIncremented(sourceRound, aggregator, contributions[aggregator]);
    }

    function penalizeContribution(
        uint256 expectedRound,
        address expectedAggregator,
        address[] memory _addresses,
        string memory reason
    ) external {
        IAggregatorSelection aggregatorSelection = IAggregatorSelection(aggregator_selection_address);
        require(expectedRound == round, "Penalty round mismatch");
        require(aggregatorSelection.isAggregator(expectedAggregator), "Penalty aggregator mismatch");
        bytes32 reasonHash = keccak256(bytes(reason));

        if (msg.sender == aggregator_selection_address) {
            require(reasonHash == keccak256(bytes("aggregator_timeout_consensus")), "Invalid timeout penalty reason");
            require(_addresses.length == 1, "Timeout penalty requires one target");
            require(_addresses[0] == expectedAggregator, "Timeout target is not expected aggregator");
            _applyPenalty(_addresses[0], reason, reasonHash);
        } else {
            address aggregator = _requireActiveAuthorizedAggregator();
            require(aggregator == expectedAggregator, "Caller cannot penalize");
            require(reasonHash == keccak256(bytes("missed_model_deadline")), "Invalid worker penalty reason");
            require(_addresses.length > 0, "Worker penalty requires targets");
            require(modelSubmissionsClosed[round], "Model submissions are still open");
            require(!globalModelPublished[round], "Worker penalties closed after publication");

            IDeviceRegistry deviceRegistry = IDeviceRegistry(device_registry_address);
            for (uint256 i = 0; i < _addresses.length; i++) {
                address device = _addresses[i];
                require(device != expectedAggregator, "Cannot apply worker penalty to aggregator");
                require(!modelSubmitted[round][device], "Cannot penalize submitted model");
                if (!deviceRegistry.isAuthorized(device) && contributions[device] == 0) {
                    continue;
                }
                _applyPenalty(device, reason, reasonHash);
            }
        }
    }

    function recordModelSubmission(
        uint256 expectedRound,
        address worker,
        bytes32 modelHash,
        bytes32 packageHash,
        bytes32 parentModelHash,
        uint256 workerNonce,
        bytes calldata workerSignature
    ) external {
        address aggregator = _requireActiveAuthorizedAggregator();
        require(expectedRound == round, "Submission round mismatch");
        IDeviceRegistry deviceRegistry = IDeviceRegistry(device_registry_address);
        require(deviceRegistry.isAuthorized(worker), "Worker is not authorized");
        require(
            !IAggregatorSelection(aggregator_selection_address).isAggregator(worker),
            "Current aggregator cannot receive worker score"
        );
        require(modelHash != bytes32(0), "Model hash is zero");
        require(packageHash != bytes32(0), "Package hash is zero");
        require(parentModelHash != bytes32(0), "Parent model hash is zero");

        if (modelSubmitted[round][worker]) {
            require(modelSubmissionHash[round][worker] == modelHash, "Conflicting model submission hash");
            require(
                modelSubmissionPackageHash[round][worker] == packageHash, "Conflicting model submission package hash"
            );
            require(
                modelSubmissionParentModelHash[round][worker] == parentModelHash,
                "Conflicting model submission parent hash"
            );
            require(modelSubmissionNonce[round][worker] == workerNonce, "Conflicting model submission nonce");
            return;
        }

        require(parentModelHash == currentParentModelHash(), "Parent model hash mismatch");

        bytes32 digest = modelSubmissionDigest(
            expectedRound, worker, aggregator, parentModelHash, modelHash, packageHash, workerNonce
        );
        require(
            digest.recover(workerSignature) == deviceRegistry.actionKeys(worker), "Invalid worker submission signature"
        );
        bytes32 commitment = keccak256(
            abi.encode(expectedRound, aggregator, worker, parentModelHash, modelHash, packageHash, workerNonce)
        );

        require(!modelSubmissionsClosed[round], "Model submissions are closed");
        require(!globalModelPublished[round], "Submissions closed after publication");
        require(workerNonce == workerSubmissionNonces[worker], "Worker submission nonce mismatch");
        addContributor(worker);
        modelSubmitted[round][worker] = true;
        modelSubmissionHash[round][worker] = modelHash;
        modelSubmissionPackageHash[round][worker] = packageHash;
        modelSubmissionParentModelHash[round][worker] = parentModelHash;
        modelSubmissionNonce[round][worker] = workerNonce;
        modelSubmissionCommitment[round][worker] = commitment;
        workerSubmissionNonces[worker] = workerNonce + 1;
        modelSubmissionCount[round]++;
        require(aggregation_policy_address != address(0), "aggregation policy is not configured");
        IAggregationPolicy(aggregation_policy_address).recordSubmission(round, worker, commitment);
        contributions[worker]++;
        emit ModelSubmissionRecorded(round, aggregator, worker, modelHash, packageHash, parentModelHash, workerNonce);
        emit ContributionIncremented(round, worker, contributions[worker]);
    }

    function closeModelSubmissions(uint256 expectedRound) external {
        address aggregator = _requireActiveAuthorizedAggregator();
        require(expectedRound == round, "Submission round mismatch");
        require(!globalModelPublished[round], "Global model already published");
        if (modelSubmissionsClosed[round]) {
            return;
        }
        require(aggregation_policy_address != address(0), "aggregation policy is not configured");
        IAggregationPolicy(aggregation_policy_address).closeRound(round, modelSubmissionCount[round]);
        modelSubmissionsClosed[round] = true;
        emit ModelSubmissionsClosed(round, aggregator, modelSubmissionCount[round]);
    }

    function openModelSubmissions(uint256 expectedRound) external {
        _requireActiveAuthorizedAggregator();
        require(expectedRound == round, "Submission round mismatch");
        require(!modelSubmissionsClosed[round], "Model submissions are closed");
        require(!globalModelPublished[round], "Global model already published");
        require(aggregation_policy_address != address(0), "aggregation policy is not configured");
        IAggregationPolicy(aggregation_policy_address).openRound(round);
    }

    function domainSeparator() public view returns (bytes32) {
        return keccak256(
            abi.encode(EIP712_DOMAIN_TYPEHASH, EIP712_NAME_HASH, EIP712_VERSION_HASH, block.chainid, address(this))
        );
    }

    function modelSubmissionDigest(
        uint256 expectedRound,
        address worker,
        address aggregator,
        bytes32 parentModelHash,
        bytes32 modelHash,
        bytes32 packageHash,
        uint256 workerNonce
    ) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                MODEL_SUBMISSION_TYPEHASH,
                expectedRound,
                aggregator,
                worker,
                parentModelHash,
                modelHash,
                packageHash,
                workerNonce
            )
        );
        return keccak256(abi.encodePacked(hex"1901", domainSeparator(), structHash));
    }

    function currentParentModelHash() public view returns (bytes32) {
        if (globalModelPublished[round]) {
            return keccak256(abi.encode(backupGlobalModel, backupGlobalModelSignature, backupGlobalModelKeyBundle));
        }
        return keccak256(abi.encode(globalModel, globalModelSignature, globalModelKeyBundle));
    }

    function hasSubmittedModel(uint256 _round, address _address) external view returns (bool) {
        return modelSubmitted[_round][_address];
    }

    function _applyPenalty(address device, string memory reason, bytes32 reasonHash) internal {
        if (penaltyApplied[round][device][reasonHash]) {
            return;
        }
        penaltyApplied[round][device][reasonHash] = true;
        addContributor(device);
        if (contributions[device] > 0) {
            contributions[device]--;
        }
        emit ContributionDecremented(round, device, contributions[device], reason);
    }

    function addContributor(address _address) internal {
        if (!isContributor(_address)) {
            contributors.push(_address);
        }
    }

    function isContributor(address _address) internal view returns (bool) {
        for (uint256 i = 0; i < contributors.length; i++) {
            if (contributors[i] == _address) {
                return true;
            }
        }
        return false;
    }

    function incrementRound() external {
        revert("Round advancement requires atomic aggregation finalization");
    }

    function abortRound(address failedAggregator) external {
        require(msg.sender == aggregator_selection_address, "Caller is not aggregator selection");
        require(round > 0, "Bootstrap round cannot be aborted");
        require(
            IAggregatorSelection(aggregator_selection_address).isAggregator(failedAggregator),
            "Failed aggregator is not current aggregator"
        );
        // Publication metadata is retained as audit history. roundAborted and
        // roundCompleted distinguish the attempted publication from a finalized one.
        roundAborted[round] = true;
        if (globalModelPublished[round]) {
            globalModel = backupGlobalModel;
            globalModelSignature = backupGlobalModelSignature;
            globalModelKeyBundle = backupGlobalModelKeyBundle;
        }
        emit RoundAborted(round, failedAggregator);
        round++;
    }

    function setLastRoundAggregator() external {
        address aggregator = IDeviceRegistry(device_registry_address).resolveAuthorizedParticipant(msg.sender);
        require(
            IAggregatorSelection(aggregator_selection_address).isAggregator(aggregator), "Caller is not an aggregator"
        );
        require(
            round > 0 && roundCompleted[round - 1] && globalModelPublisher[round - 1] == aggregator,
            "Caller did not complete previous round"
        );
        lastRoundAggregator = aggregator;
    }

    function _requireActiveAuthorizedAggregator() internal view returns (address aggregator) {
        aggregator = IDeviceRegistry(device_registry_address).resolveAuthorizedParticipant(msg.sender);
        IAggregatorSelection aggregatorSelection = IAggregatorSelection(aggregator_selection_address);
        require(aggregatorSelection.isAggregator(aggregator), "Caller is not the current aggregator");
        require(aggregatorSelection.lastSelectionRound() == round, "Aggregator not selected for current round");
    }

    function getRound() external view returns (uint256) {
        return round;
    }

    function getCompletedRoundCount() external view returns (uint256) {
        return completedRoundCount;
    }

    function getGlobalModel() external view returns (string memory) {
        return globalModel;
    }

    function getGlobalModelSignature() external view returns (string memory) {
        return globalModelSignature;
    }

    function getBackupGlobalModel() external view returns (string memory) {
        return backupGlobalModel;
    }

    function getBackupGlobalModelSignature() external view returns (string memory) {
        return backupGlobalModelSignature;
    }

    function getGlobalModelKeyBundle() external view returns (string memory) {
        return globalModelKeyBundle;
    }

    function getBackupGlobalModelKeyBundle() external view returns (string memory) {
        return backupGlobalModelKeyBundle;
    }

    function getLastRoundsAggregator() external view returns (address) {
        return lastRoundAggregator;
    }

    function getFinalizedModelBundle()
        external
        view
        returns (
            string memory model,
            string memory signature,
            string memory keyBundle,
            address aggregator,
            uint256 modelRound,
            bytes memory publisherPublicKey
        )
    {
        require(hasFinalizedModel, "No finalized model");
        require(!globalModelPublished[round], "Current round publication is not finalized");
        require(round > 0 && roundCompleted[lastFinalizedModelRound - 1], "Finalized model invariant failed");
        require(bytes(globalModel).length > 0, "Finalized model CID is empty");
        require(bytes(globalModelSignature).length > 0, "Finalized signature CID is empty");
        require(bytes(globalModelKeyBundle).length > 0, "Finalized key bundle CID is empty");
        require(lastRoundAggregator != address(0), "Finalized aggregator is empty");
        require(lastFinalizedPublisherPublicKey.length > 0, "Finalized publisher key is empty");
        return (
            globalModel,
            globalModelSignature,
            globalModelKeyBundle,
            lastRoundAggregator,
            lastFinalizedModelRound,
            lastFinalizedPublisherPublicKey
        );
    }

    function getActiveModelBundle()
        external
        view
        returns (
            string memory model,
            string memory signature,
            string memory keyBundle,
            address publisher,
            bytes memory publisherPublicKey
        )
    {
        bool currentPublicationPending = globalModelPublished[round];
        model = currentPublicationPending ? backupGlobalModel : globalModel;
        signature = currentPublicationPending ? backupGlobalModelSignature : globalModelSignature;
        keyBundle = currentPublicationPending ? backupGlobalModelKeyBundle : globalModelKeyBundle;
        publisher = lastRoundAggregator;
        publisherPublicKey = activeModelPublisherPublicKey;

        require(bytes(model).length > 0, "Active model CID is empty");
        require(bytes(signature).length > 0, "Active signature CID is empty");
        require(bytes(keyBundle).length > 0, "Active key bundle CID is empty");
        require(publisher != address(0), "Active publisher is empty");
        require(publisherPublicKey.length > 0, "Active publisher key is empty");
    }

    function getContribution(address _address) external view returns (uint256) {
        return contributions[_address];
    }

    function getTopContributor() external view returns (address) {
        address topContributor;
        uint256 topScore = 0;
        for (uint256 i = 0; i < contributors.length; i++) {
            if (contributions[contributors[i]] > topScore) {
                topScore = contributions[contributors[i]];
                topContributor = contributors[i];
            }
        }
        return topContributor;
    }

    function getWeightedRandomContributor(uint256 randomness) external view returns (address) {
        return selectWeightedRandomContributor(randomness, address(0), false);
    }

    function getWeightedRandomContributorExcluding(uint256 randomness, address excluded)
        external
        view
        returns (address)
    {
        return selectWeightedRandomContributor(randomness, excluded, true);
    }

    function selectWeightedRandomContributor(uint256 randomness, address excluded, bool useExclusion)
        internal
        view
        returns (address)
    {
        address[] memory authorizedDevices = IDeviceRegistry(device_registry_address).getAuthorizedDevices();
        uint256 totalWeight = 0;

        for (uint256 i = 0; i < authorizedDevices.length; i++) {
            if (useExclusion && authorizedDevices[i] == excluded) {
                continue;
            }
            totalWeight += contributions[authorizedDevices[i]] + 1;
        }

        if (totalWeight == 0) {
            return address(0);
        }

        uint256 selectedWeight = randomness % totalWeight;
        uint256 cursor = 0;

        for (uint256 i = 0; i < authorizedDevices.length; i++) {
            if (useExclusion && authorizedDevices[i] == excluded) {
                continue;
            }
            cursor += contributions[authorizedDevices[i]] + 1;
            if (selectedWeight < cursor) {
                return authorizedDevices[i];
            }
        }

        return address(0);
    }
}
