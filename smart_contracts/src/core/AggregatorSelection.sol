// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

interface IDeviceRegistry {
    function isAuthorized(address _address) external view returns (bool);
    function getAuthorizedDevices() external view returns (address[] memory);
    function getDevice(
        address _address
    ) external view returns (bool, string memory, string memory, bytes memory);
}

interface IGMStorage {
    function getTopContributor() external view returns (address);
    function getRound() external view returns (uint256);
    function getWeightedRandomContributor(uint256 randomness) external view returns (address);
    function getWeightedRandomContributorExcluding(
        uint256 randomness,
        address excluded
    ) external view returns (address);
    function device_registry_address() external view returns (address);
    function penalizeContribution(address[] memory _addresses, string memory reason) external;
    function abortRound(address failedAggregator) external;
}

contract AggregatorSelection {
    enum SystemState {
        TRAINING,
        AGGREGATING,
        SELECTING
    }
    string public system_state;
    address public current_aggregator;
    string public broker_endpoint;
    uint256 public time_to_aggregate;
    uint public time_to_select;
    address public gm_storage_address;
    uint256 public timeoutReportThresholdPercent;
    mapping(uint256 => mapping(address => bool)) public timeoutReported;
    mapping(uint256 => uint256) public timeoutReportCount;
    mapping(uint256 => bool) public roundAborted;

    event GMStorageAddressUpdated(address indexed oldAddress, address indexed newAddress);
    event TimeoutReportThresholdUpdated(uint256 oldPercent, uint256 newPercent);
    event AggregatorSelected(
        address indexed previousAggregator,
        address indexed newAggregator,
        uint256 indexed round,
        uint256 randomness
    );
    event AggregatorTimeoutReported(
        uint256 indexed round,
        address indexed reporter,
        address indexed aggregator,
        uint256 reportCount,
        uint256 requiredReports
    );
    event RoundAborted(
        uint256 indexed round,
        address indexed failedAggregator,
        address indexed newAggregator
    );

    constructor() {
        system_state = "TRAINING";
        current_aggregator = msg.sender;
        broker_endpoint = "test_endpoint";
        time_to_aggregate = 0;
        time_to_select = 0;
        timeoutReportThresholdPercent = 50;
    }

    // view functions
    function getSystemState()
        external
        view
        returns (string memory, address, string memory, uint256, uint)
    {
        return (
            system_state,
            current_aggregator,
            broker_endpoint,
            time_to_aggregate,
            time_to_select
        );
    }

    function getCurrentAggregator() external view returns (address) {
        return current_aggregator;
    }

    function isAggregator(address _address) external view returns (bool) {
        return _address == current_aggregator;
    }

    function getBrokerEndpoint() external view returns (string memory) {
        return broker_endpoint;
    }

    function getTimeToAggregate() external view returns (uint256) {
        return time_to_aggregate;
    }

    function getTimeToSelect() external view returns (uint) {
        return time_to_select;
    }

    function setSystemState(string memory _state) external {
        // check if requester is current aggregator
        require(
            msg.sender == current_aggregator,
            "Caller is not the current aggregator"
        );
        system_state = _state;
    }

    function setCurrentAggregator(address _aggregator) external {
        current_aggregator = _aggregator;
    }

    function setBrokerEndpoint(string memory _endpoint) external {
        broker_endpoint = _endpoint;
    }

    function setGMStorageAddress(address _gm_storage_address) external {
        // Restrict access to the current aggregator
        require(
            msg.sender == current_aggregator,
            "Caller is not the current aggregator"
        );
        emit GMStorageAddressUpdated(gm_storage_address, _gm_storage_address);
        gm_storage_address = _gm_storage_address;
    }

    function setTimeoutReportThresholdPercent(uint256 _thresholdPercent) external {
        require(
            msg.sender == current_aggregator,
            "Caller is not the current aggregator"
        );
        require(_thresholdPercent > 0 && _thresholdPercent <= 100, "invalid threshold percent");
        emit TimeoutReportThresholdUpdated(
            timeoutReportThresholdPercent,
            _thresholdPercent
        );
        timeoutReportThresholdPercent = _thresholdPercent;
    }

    function triggerAggregatorSelection() external {
        uint256 current_timestamp = block.timestamp;
        // check if requester is current aggregator
        require(
            msg.sender == current_aggregator ||
                current_timestamp >= time_to_aggregate,
            "Caller is not the current aggregator or the time to aggregate has not been reached"
        );
        // set system state to SELECTING
        system_state = "SELECTING";

        IGMStorage gm_storage = IGMStorage(gm_storage_address);
        uint256 round = gm_storage.getRound();
        uint256 randomness = uint256(
            keccak256(
                abi.encodePacked(
                    block.prevrandao,
                    block.timestamp,
                    block.number,
                    round,
                    current_aggregator
                )
            )
        );
        address selectedAggregator = gm_storage.getWeightedRandomContributor(
            randomness
        );
        require(selectedAggregator != address(0), "No authorized aggregator candidates");

        address previousAggregator = current_aggregator;
        current_aggregator = selectedAggregator;
        broker_endpoint = "new_endpoint";
        system_state = "TRAINING";
        emit AggregatorSelected(
            previousAggregator,
            selectedAggregator,
            round,
            randomness
        );
    }

    function reportAggregatorTimeout() external {
        require(gm_storage_address != address(0), "GMStorage not configured");

        IGMStorage gm_storage = IGMStorage(gm_storage_address);
        uint256 round = gm_storage.getRound();
        require(!roundAborted[round], "round already aborted");

        IDeviceRegistry deviceRegistry = IDeviceRegistry(
            gm_storage.device_registry_address()
        );
        require(deviceRegistry.isAuthorized(msg.sender), "reporter not authorized");
        require(msg.sender != current_aggregator, "aggregator cannot report itself");
        require(!timeoutReported[round][msg.sender], "timeout already reported");

        uint256 eligibleReporters = getEligibleTimeoutReporterCount(
            deviceRegistry
        );
        require(eligibleReporters > 0, "no eligible timeout reporters");
        uint256 requiredReports = getRequiredTimeoutReports(eligibleReporters);

        timeoutReported[round][msg.sender] = true;
        timeoutReportCount[round]++;

        emit AggregatorTimeoutReported(
            round,
            msg.sender,
            current_aggregator,
            timeoutReportCount[round],
            requiredReports
        );

        if (timeoutReportCount[round] >= requiredReports) {
            abortRoundAndSelectNewAggregator(gm_storage, round);
        }
    }

    function getRequiredTimeoutReports(
        uint256 eligibleReporters
    ) public view returns (uint256) {
        uint256 requiredReports = (eligibleReporters *
            timeoutReportThresholdPercent +
            99) / 100;
        if (requiredReports == 0) {
            return 1;
        }
        if (requiredReports > eligibleReporters) {
            return eligibleReporters;
        }
        return requiredReports;
    }

    function getEligibleTimeoutReporterCount(
        IDeviceRegistry deviceRegistry
    ) internal view returns (uint256) {
        address[] memory authorizedDevices = deviceRegistry
            .getAuthorizedDevices();
        uint256 eligibleReporters = 0;
        for (uint256 i = 0; i < authorizedDevices.length; i++) {
            if (authorizedDevices[i] != current_aggregator) {
                eligibleReporters++;
            }
        }
        return eligibleReporters;
    }

    function abortRoundAndSelectNewAggregator(
        IGMStorage gm_storage,
        uint256 round
    ) internal {
        address failedAggregator = current_aggregator;
        address[] memory penalized = new address[](1);
        penalized[0] = failedAggregator;
        gm_storage.penalizeContribution(
            penalized,
            "aggregator_timeout_consensus"
        );
        gm_storage.abortRound(failedAggregator);

        uint256 randomness = uint256(
            keccak256(
                abi.encodePacked(
                    block.prevrandao,
                    block.timestamp,
                    block.number,
                    round,
                    failedAggregator,
                    timeoutReportCount[round],
                    "aggregator-timeout"
                )
            )
        );
        address selectedAggregator = gm_storage
            .getWeightedRandomContributorExcluding(randomness, failedAggregator);
        require(selectedAggregator != address(0), "No replacement aggregator candidates");

        roundAborted[round] = true;
        current_aggregator = selectedAggregator;
        broker_endpoint = "new_endpoint";
        system_state = "TRAINING";

        emit RoundAborted(round, failedAggregator, selectedAggregator);
        emit AggregatorSelected(
            failedAggregator,
            selectedAggregator,
            round,
            randomness
        );
    }
}
