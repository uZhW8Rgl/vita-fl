// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

interface IDeviceRegistry {
    function isAuthorized(address _address) external view returns (bool);
    function getAuthorizedDevices() external view returns (address[] memory);
}

interface IAggregatorSelection {
    function isAggregator(address _address) external view returns (bool);
}

contract GMStorage {
    string public globalModel;
    string public backupGlobalModel;
    string public globalModelSignature;
    string public backupGlobalModelSignature;
    string public globalModelKeyBundle;
    string public backupGlobalModelKeyBundle;
    uint256 public round;
    address public lastRoundAggregator;
    address public device_registry_address;
    address public aggregator_selection_address;
    mapping(address => uint256) public contributions;
    mapping(uint256 => mapping(address => bool)) public modelSubmitted;
    mapping(uint256 => mapping(address => bytes32)) public modelSubmissionHash;
    mapping(uint256 => mapping(address => mapping(bytes32 => bool)))
        public penaltyApplied;
    address[] private contributors;

    event ModelSubmitted(
        uint256 indexed round,
        address indexed device,
        bytes32 modelHash
    );
    event ContributionIncremented(
        uint256 indexed round,
        address indexed device,
        uint256 score
    );
    event ContributionDecremented(
        uint256 indexed round,
        address indexed device,
        uint256 score,
        string reason
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
        lastRoundAggregator = _initial_GM_SIGNER_ADDRESS;
    }

    function setGlobalModel(string memory _newGlobalModel) external {
        _newGlobalModel;
        revert(
            "Encrypted GM flow requires setGlobalModelAndSignatureAndKeyBundle"
        );
    }

    function setGlobalModelSignature(string memory _newGlobalModelSignature)
        external
    {
        _newGlobalModelSignature;
        revert(
            "Encrypted GM flow requires setGlobalModelAndSignatureAndKeyBundle"
        );
    }

    function setGlobalModelAndSignature(
        string memory _newGlobalModel,
        string memory _newGlobalModelSignature
    ) external {
        _newGlobalModel;
        _newGlobalModelSignature;
        revert(
            "Encrypted GM flow requires setGlobalModelAndSignatureAndKeyBundle"
        );
    }

    function setGlobalModelAndSignatureAndKeyBundle(
        string memory _newGlobalModel,
        string memory _newGlobalModelSignature,
        string memory _newGlobalModelKeyBundle
    ) external {
        require(
            IAggregatorSelection(aggregator_selection_address).isAggregator(
                msg.sender
            ),
            "Caller is not an aggregator"
        );
        backupGlobalModel = globalModel;
        backupGlobalModelSignature = globalModelSignature;
        backupGlobalModelKeyBundle = globalModelKeyBundle;
        globalModel = _newGlobalModel;
        globalModelSignature = _newGlobalModelSignature;
        globalModelKeyBundle = _newGlobalModelKeyBundle;
        lastRoundAggregator = msg.sender;
    }

    function incrementContribution(address[] memory _addresses) external {
        for (uint256 i = 0; i < _addresses.length; i++) {
            addContributor(_addresses[i]);
            contributions[_addresses[i]]++;
            emit ContributionIncremented(
                round,
                _addresses[i],
                contributions[_addresses[i]]
            );
        }
    }

    function penalizeContribution(
        address[] memory _addresses,
        string memory reason
    ) external {
        require(
            msg.sender == aggregator_selection_address ||
                IAggregatorSelection(aggregator_selection_address).isAggregator(
                    msg.sender
                ),
            "Caller cannot penalize"
        );
        _penalizeContribution(_addresses, reason);
    }

    function submitModel(bytes32 modelHash) external {
        require(
            IDeviceRegistry(device_registry_address).isAuthorized(msg.sender),
            "Device is not authorized"
        );
        addContributor(msg.sender);
        modelSubmitted[round][msg.sender] = true;
        modelSubmissionHash[round][msg.sender] = modelHash;
        emit ModelSubmitted(round, msg.sender, modelHash);
    }

    function hasSubmittedModel(
        uint256 _round,
        address _address
    ) external view returns (bool) {
        return modelSubmitted[_round][_address];
    }

    function _penalizeContribution(
        address[] memory _addresses,
        string memory reason
    ) internal {
        bytes32 reasonHash = keccak256(bytes(reason));
        for (uint256 i = 0; i < _addresses.length; i++) {
            address device = _addresses[i];
            if (penaltyApplied[round][device][reasonHash]) {
                continue;
            }
            penaltyApplied[round][device][reasonHash] = true;
            addContributor(device);
            if (contributions[device] > 0) {
                contributions[device]--;
            }
            emit ContributionDecremented(
                round,
                device,
                contributions[device],
                reason
            );
        }
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
        require(
            IAggregatorSelection(aggregator_selection_address).isAggregator(
                msg.sender
            ),
            "Caller is not an aggregator"
        );
        addContributor(msg.sender);
        contributions[msg.sender]++;
        emit ContributionIncremented(
            round,
            msg.sender,
            contributions[msg.sender]
        );
        round++;
        lastRoundAggregator = msg.sender;
    }

    function abortRound(address failedAggregator) external {
        require(
            msg.sender == aggregator_selection_address,
            "Caller is not aggregator selection"
        );
        emit RoundAborted(round, failedAggregator);
        round++;
        lastRoundAggregator = failedAggregator;
    }

    function setLastRoundAggregator() external {
        require(
            IAggregatorSelection(aggregator_selection_address).isAggregator(
                msg.sender
            ),
            "Caller is not an aggregator"
        );
        lastRoundAggregator = msg.sender;
    }

    function getRound() external view returns (uint256) {
        return round;
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

    function getBackupGlobalModelSignature()
        external
        view
        returns (string memory)
    {
        return backupGlobalModelSignature;
    }

    function getGlobalModelKeyBundle() external view returns (string memory) {
        return globalModelKeyBundle;
    }

    function getBackupGlobalModelKeyBundle()
        external
        view
        returns (string memory)
    {
        return backupGlobalModelKeyBundle;
    }

    function getLastRoundsAggregator() external view returns (address) {
        return lastRoundAggregator;
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

    function getWeightedRandomContributor(
        uint256 randomness
    ) external view returns (address) {
        return selectWeightedRandomContributor(randomness, address(0), false);
    }

    function getWeightedRandomContributorExcluding(
        uint256 randomness,
        address excluded
    ) external view returns (address) {
        return selectWeightedRandomContributor(randomness, excluded, true);
    }

    function selectWeightedRandomContributor(
        uint256 randomness,
        address excluded,
        bool useExclusion
    ) internal view returns (address) {
        address[] memory authorizedDevices = IDeviceRegistry(
            device_registry_address
        ).getAuthorizedDevices();
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
