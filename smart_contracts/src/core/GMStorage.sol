// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

interface IDeviceRegistry {
    function isAuthorized(address _address) external view returns (bool);
    function getAuthorizedDevices() external view returns (address[] memory);
    function getDevice(address _address) external view returns (bool, string memory, string memory, bytes memory);
}

interface IAggregatorSelection {
    function isAggregator(address _address) external view returns (bool);
    function lastSelectionRound() external view returns (uint256);
}

contract GMStorage {
    string public globalModel;
    string public backupGlobalModel;
    string public globalModelSignature;
    string public backupGlobalModelSignature;
    string public globalModelKeyBundle;
    string public backupGlobalModelKeyBundle;
    uint256 public round;
    uint256 public completedRoundCount;
    address public lastRoundAggregator;
    address public bootstrapAuthority;
    address public device_registry_address;
    address public aggregator_selection_address;
    mapping(address => uint256) public contributions;
    mapping(uint256 => mapping(address => bool)) public modelSubmitted;
    mapping(uint256 => mapping(address => bytes32)) public modelSubmissionHash;
    mapping(uint256 => uint256) public modelSubmissionCount;
    mapping(uint256 => bool) public modelSubmissionsClosed;
    mapping(uint256 => bool) public globalModelPublished;
    mapping(uint256 => address) public globalModelPublisher;
    mapping(uint256 => bytes) public globalModelPublisherPublicKey;
    mapping(uint256 => bool) public roundCompleted;
    mapping(uint256 => bool) public roundAborted;
    bool public hasFinalizedModel;
    uint256 public lastFinalizedModelRound;
    bytes public activeModelPublisherPublicKey;
    bytes public lastFinalizedPublisherPublicKey;
    mapping(uint256 => mapping(address => mapping(bytes32 => bool))) public penaltyApplied;
    address[] private contributors;

    event ModelSubmissionRecorded(
        uint256 indexed round, address indexed aggregator, address indexed worker, bytes32 modelHash
    );
    event ModelSubmissionsClosed(uint256 indexed round, address indexed aggregator, uint256 acceptedModels);
    event ContributionIncremented(uint256 indexed round, address indexed device, uint256 score);
    event ContributionDecremented(uint256 indexed round, address indexed device, uint256 score, string reason);
    event GlobalModelPublished(
        uint256 indexed round, address indexed aggregator, string model, string signature, string keyBundle
    );
    event EncryptedBootstrapInitialized(
        address indexed authority, string model, string signature, string keyBundle, bytes32 publisherPublicKeyHash
    );
    event RoundAborted(uint256 indexed round, address indexed failedAggregator);

    constructor(
        address _device_registry_address,
        address _aggregator_selection_address,
        string memory _initial_GM_CID,
        string memory _initial_GM_SIG_CID,
        address _initial_GM_SIGNER_ADDRESS
    ) {
        globalModel = _initial_GM_CID;
        backupGlobalModel = _initial_GM_CID;
        globalModelSignature = _initial_GM_SIG_CID;
        backupGlobalModelSignature = _initial_GM_SIG_CID;
        globalModelKeyBundle = "";
        backupGlobalModelKeyBundle = "";
        device_registry_address = _device_registry_address;
        aggregator_selection_address = _aggregator_selection_address;
        round = 0;
        bootstrapAuthority = msg.sender;
        lastRoundAggregator = _initial_GM_SIGNER_ADDRESS;
    }

    function initializeEncryptedBootstrap(
        string memory encryptedModel,
        string memory encryptedSignature,
        string memory encryptedKeyBundle,
        bytes memory publisherPublicKey
    ) external {
        require(msg.sender == bootstrapAuthority, "Caller is not bootstrap authority");
        require(round == 0 && !globalModelPublished[0], "Bootstrap phase is closed");
        require(bytes(encryptedModel).length > 0, "Model CID is empty");
        require(bytes(encryptedSignature).length > 0, "Model signature CID is empty");
        require(bytes(encryptedKeyBundle).length > 0, "Model key bundle CID is empty");
        require(publisherPublicKey.length > 0, "Bootstrap publisher public key is empty");

        globalModel = encryptedModel;
        backupGlobalModel = encryptedModel;
        globalModelSignature = encryptedSignature;
        backupGlobalModelSignature = encryptedSignature;
        globalModelKeyBundle = encryptedKeyBundle;
        backupGlobalModelKeyBundle = encryptedKeyBundle;
        activeModelPublisherPublicKey = publisherPublicKey;
        address authority = bootstrapAuthority;
        bootstrapAuthority = address(0);
        emit EncryptedBootstrapInitialized(
            authority, encryptedModel, encryptedSignature, encryptedKeyBundle, keccak256(publisherPublicKey)
        );
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
        _requireActiveAuthorizedAggregator(msg.sender);
        require(modelSubmissionsClosed[round], "Model submissions are still open");
        require(round == 0 || modelSubmissionCount[round] > 0, "No confirmed worker submissions");
        require(bytes(_newGlobalModel).length > 0, "Model CID is empty");
        require(bytes(_newGlobalModelSignature).length > 0, "Model signature CID is empty");
        require(bytes(_newGlobalModelKeyBundle).length > 0, "Model key bundle CID is empty");
        (bool publisherAuthorized,,, bytes memory publisherPublicKey) =
            IDeviceRegistry(device_registry_address).getDevice(msg.sender);
        require(publisherAuthorized, "Publisher is not authorized");
        require(publisherPublicKey.length > 0, "Publisher public key is empty");
        if (!globalModelPublished[round]) {
            backupGlobalModel = globalModel;
            backupGlobalModelSignature = globalModelSignature;
            backupGlobalModelKeyBundle = globalModelKeyBundle;
        }
        globalModel = _newGlobalModel;
        globalModelSignature = _newGlobalModelSignature;
        globalModelKeyBundle = _newGlobalModelKeyBundle;
        globalModelPublished[round] = true;
        globalModelPublisher[round] = msg.sender;
        globalModelPublisherPublicKey[round] = publisherPublicKey;
        emit GlobalModelPublished(
            round, msg.sender, _newGlobalModel, _newGlobalModelSignature, _newGlobalModelKeyBundle
        );
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
            _requireActiveAuthorizedAggregator(msg.sender);
            require(msg.sender == expectedAggregator, "Caller cannot penalize");
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

    function recordModelSubmission(uint256 expectedRound, address worker, bytes32 modelHash) external {
        _requireActiveAuthorizedAggregator(msg.sender);
        require(expectedRound == round, "Submission round mismatch");
        require(IDeviceRegistry(device_registry_address).isAuthorized(worker), "Worker is not authorized");
        require(
            !IAggregatorSelection(aggregator_selection_address).isAggregator(worker),
            "Current aggregator cannot receive worker score"
        );
        require(modelHash != bytes32(0), "Model hash is zero");

        if (modelSubmitted[round][worker]) {
            require(modelSubmissionHash[round][worker] == modelHash, "Conflicting model submission hash");
            return;
        }

        require(!modelSubmissionsClosed[round], "Model submissions are closed");
        require(!globalModelPublished[round], "Submissions closed after publication");
        addContributor(worker);
        modelSubmitted[round][worker] = true;
        modelSubmissionHash[round][worker] = modelHash;
        modelSubmissionCount[round]++;
        contributions[worker]++;
        emit ModelSubmissionRecorded(round, msg.sender, worker, modelHash);
        emit ContributionIncremented(round, worker, contributions[worker]);
    }

    function closeModelSubmissions(uint256 expectedRound) external {
        _requireActiveAuthorizedAggregator(msg.sender);
        require(expectedRound == round, "Submission round mismatch");
        require(!globalModelPublished[round], "Global model already published");
        if (modelSubmissionsClosed[round]) {
            return;
        }
        modelSubmissionsClosed[round] = true;
        emit ModelSubmissionsClosed(round, msg.sender, modelSubmissionCount[round]);
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
        _requireActiveAuthorizedAggregator(msg.sender);
        require(globalModelPublished[round], "Global model not published for round");
        require(globalModelPublisher[round] == msg.sender, "Caller did not publish current round model");
        require(!roundCompleted[round], "Round already completed");
        roundCompleted[round] = true;
        completedRoundCount++;
        bytes memory publisherPublicKey = globalModelPublisherPublicKey[round];
        require(publisherPublicKey.length > 0, "Publisher public key is empty");
        activeModelPublisherPublicKey = publisherPublicKey;
        lastFinalizedPublisherPublicKey = publisherPublicKey;
        hasFinalizedModel = true;
        lastFinalizedModelRound = round + 1;
        addContributor(msg.sender);
        contributions[msg.sender]++;
        emit ContributionIncremented(round, msg.sender, contributions[msg.sender]);
        round++;
        lastRoundAggregator = msg.sender;
    }

    function abortRound(address failedAggregator) external {
        require(msg.sender == aggregator_selection_address, "Caller is not aggregator selection");
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
        require(
            IAggregatorSelection(aggregator_selection_address).isAggregator(msg.sender), "Caller is not an aggregator"
        );
        require(IDeviceRegistry(device_registry_address).isAuthorized(msg.sender), "Aggregator is not authorized");
        require(
            round > 0 && roundCompleted[round - 1] && globalModelPublisher[round - 1] == msg.sender,
            "Caller did not complete previous round"
        );
        lastRoundAggregator = msg.sender;
    }

    function _requireActiveAuthorizedAggregator(address aggregator) internal view {
        IAggregatorSelection aggregatorSelection = IAggregatorSelection(aggregator_selection_address);
        require(aggregatorSelection.isAggregator(aggregator), "Caller is not the current aggregator");
        require(IDeviceRegistry(device_registry_address).isAuthorized(aggregator), "Aggregator is not authorized");
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
