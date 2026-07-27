// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

interface IDeviceRegistry {
    function isAuthorized(address _address) external view returns (bool);
    function getAuthorizedDevices() external view returns (address[] memory);
    function getDevice(address _address) external view returns (bool, string memory, string memory, bytes memory);
    function resolveAuthorizedParticipant(address actionKey) external view returns (address participant);
}

interface IGMStorage {
    function getTopContributor() external view returns (address);
    function getRound() external view returns (uint256);
    function getWeightedRandomContributor(uint256 randomness) external view returns (address);
    function getWeightedRandomContributorExcluding(uint256 randomness, address excluded) external view returns (address);
    function device_registry_address() external view returns (address);
    function penalizeContribution(
        uint256 expectedRound,
        address expectedAggregator,
        address[] memory _addresses,
        string memory reason
    ) external;
    function abortRound(address failedAggregator) external;
    function roundCompleted(uint256 round) external view returns (bool);
}

contract AggregatorSelection {
    enum SystemState {
        TRAINING,
        AGGREGATING,
        SELECTING
    }
    string public system_state;
    address public owner;
    address public current_aggregator;
    string public broker_endpoint;
    uint256 public time_to_aggregate;
    uint256 public time_to_select;
    address public gm_storage_address;
    uint256 public timeoutReportThresholdPercent;
    mapping(uint256 => mapping(address => mapping(address => bool))) public timeoutReported;
    mapping(uint256 => mapping(address => uint256)) public timeoutReportCount;
    mapping(uint256 => mapping(address => uint256)) public timeoutEligibleReporterCount;
    mapping(uint256 => mapping(address => uint256)) public timeoutRequiredReportCount;
    mapping(uint256 => mapping(address => mapping(address => bool))) public timeoutReporterEligible;
    mapping(uint256 => bool) public roundAborted;
    uint256 public lastSelectionRound;

    event GMStorageAddressUpdated(address indexed oldAddress, address indexed newAddress);
    event TimeoutReportThresholdUpdated(uint256 oldPercent, uint256 newPercent);
    event AggregatorSelected(
        address indexed previousAggregator, address indexed newAggregator, uint256 indexed round, uint256 randomness
    );
    event AggregatorTimeoutReported(
        uint256 indexed round,
        address indexed reporter,
        address indexed aggregator,
        uint256 reportCount,
        uint256 requiredReports
    );
    event RoundAborted(uint256 indexed round, address indexed failedAggregator, address indexed newAggregator);

    constructor() {
        owner = msg.sender;
        system_state = "TRAINING";
        current_aggregator = msg.sender;
        broker_endpoint = "test_endpoint";
        time_to_aggregate = 0;
        time_to_select = 0;
        timeoutReportThresholdPercent = 50;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier onlySelectedCurrentAggregator() {
        require(gm_storage_address != address(0), "GMStorage not configured");
        IGMStorage gmStorage = IGMStorage(gm_storage_address);
        address aggregator =
            IDeviceRegistry(gmStorage.device_registry_address()).resolveAuthorizedParticipant(msg.sender);
        require(aggregator == current_aggregator, "Caller is not the current aggregator");
        require(lastSelectionRound == gmStorage.getRound(), "Aggregator not selected for current round");
        _;
    }

    // view functions
    function getSystemState() external view returns (string memory, address, string memory, uint256, uint256) {
        return (system_state, current_aggregator, broker_endpoint, time_to_aggregate, time_to_select);
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

    function getTimeToSelect() external view returns (uint256) {
        return time_to_select;
    }

    function setSystemState(string memory _state) external onlySelectedCurrentAggregator {
        bytes32 stateHash = keccak256(bytes(_state));
        require(
            stateHash == keccak256(bytes("TRAINING")) || stateHash == keccak256(bytes("AGGREGATING"))
                || stateHash == keccak256(bytes("UPDATING")),
            "invalid system state"
        );
        system_state = _state;
    }

    function setCurrentAggregator(address _aggregator) external onlyOwner {
        require(gm_storage_address == address(0), "aggregator bootstrap is closed");
        require(_aggregator != address(0), "invalid aggregator");
        current_aggregator = _aggregator;
    }

    function setBrokerEndpoint(string memory _endpoint) external onlySelectedCurrentAggregator {
        broker_endpoint = _endpoint;
    }

    function setGMStorageAddress(address _gm_storage_address) external onlyOwner {
        require(gm_storage_address == address(0), "GMStorage already configured");
        require(_gm_storage_address != address(0), "invalid GMStorage address");
        emit GMStorageAddressUpdated(gm_storage_address, _gm_storage_address);
        gm_storage_address = _gm_storage_address;
        lastSelectionRound = IGMStorage(_gm_storage_address).getRound();
    }

    function setTimeoutReportThresholdPercent(uint256 _thresholdPercent) external onlyOwner {
        require(_thresholdPercent > 0 && _thresholdPercent <= 100, "invalid threshold percent");
        emit TimeoutReportThresholdUpdated(timeoutReportThresholdPercent, _thresholdPercent);
        timeoutReportThresholdPercent = _thresholdPercent;
    }

    function triggerAggregatorSelection() external {
        require(gm_storage_address != address(0), "GMStorage not configured");

        IGMStorage gm_storage = IGMStorage(gm_storage_address);
        IDeviceRegistry(gm_storage.device_registry_address()).resolveAuthorizedParticipant(msg.sender);
        uint256 round = gm_storage.getRound();
        require(round > lastSelectionRound, "GMStorage round not advanced");
        require(round > 0 && gm_storage.roundCompleted(round - 1), "Previous GMStorage round not completed");

        // set system state to SELECTING
        system_state = "SELECTING";

        uint256 randomness = uint256(
            keccak256(abi.encodePacked(block.prevrandao, block.timestamp, block.number, round, current_aggregator))
        );
        address selectedAggregator = gm_storage.getWeightedRandomContributor(randomness);
        require(selectedAggregator != address(0), "No authorized aggregator candidates");

        address previousAggregator = current_aggregator;
        current_aggregator = selectedAggregator;
        lastSelectionRound = round;
        broker_endpoint = "new_endpoint";
        system_state = "TRAINING";
        emit AggregatorSelected(previousAggregator, selectedAggregator, round, randomness);
    }

    function reportAggregatorTimeout(uint256 expectedRound, address expectedAggregator) external {
        require(gm_storage_address != address(0), "GMStorage not configured");

        IGMStorage gm_storage = IGMStorage(gm_storage_address);
        uint256 round = gm_storage.getRound();
        require(expectedRound == round, "timeout round mismatch");
        require(expectedAggregator == current_aggregator, "timeout aggregator mismatch");
        require(lastSelectionRound == round, "aggregator not selected for current round");
        address reportedAggregator = expectedAggregator;
        require(!roundAborted[round], "round already aborted");

        IDeviceRegistry deviceRegistry = IDeviceRegistry(gm_storage.device_registry_address());
        address reporter = deviceRegistry.resolveAuthorizedParticipant(msg.sender);
        require(reporter != reportedAggregator, "aggregator cannot report itself");
        require(!timeoutReported[round][reportedAggregator][reporter], "timeout already reported");

        uint256 eligibleReporters = timeoutEligibleReporterCount[round][reportedAggregator];
        uint256 requiredReports = timeoutRequiredReportCount[round][reportedAggregator];
        if (requiredReports == 0) {
            eligibleReporters = snapshotEligibleTimeoutReporters(deviceRegistry, round, reportedAggregator);
            require(eligibleReporters > 0, "no eligible timeout reporters");
            requiredReports = getRequiredTimeoutReports(eligibleReporters);
            timeoutEligibleReporterCount[round][reportedAggregator] = eligibleReporters;
            timeoutRequiredReportCount[round][reportedAggregator] = requiredReports;
        }
        require(timeoutReporterEligible[round][reportedAggregator][reporter], "reporter not in timeout snapshot");

        timeoutReported[round][reportedAggregator][reporter] = true;
        timeoutReportCount[round][reportedAggregator]++;

        emit AggregatorTimeoutReported(
            round, reporter, reportedAggregator, timeoutReportCount[round][reportedAggregator], requiredReports
        );

        if (timeoutReportCount[round][reportedAggregator] >= requiredReports) {
            abortRoundAndSelectNewAggregator(gm_storage, round, reportedAggregator);
        }
    }

    function getRequiredTimeoutReports(uint256 eligibleReporters) public view returns (uint256) {
        uint256 requiredReports = (eligibleReporters * timeoutReportThresholdPercent + 99) / 100;
        if (requiredReports == 0) {
            return 1;
        }
        if (requiredReports > eligibleReporters) {
            return eligibleReporters;
        }
        return requiredReports;
    }

    function snapshotEligibleTimeoutReporters(IDeviceRegistry deviceRegistry, uint256 round, address reportedAggregator)
        internal
        returns (uint256)
    {
        address[] memory authorizedDevices = deviceRegistry.getAuthorizedDevices();
        uint256 eligibleReporters = 0;
        for (uint256 i = 0; i < authorizedDevices.length; i++) {
            if (authorizedDevices[i] != reportedAggregator) {
                timeoutReporterEligible[round][reportedAggregator][authorizedDevices[i]] = true;
                eligibleReporters++;
            }
        }
        return eligibleReporters;
    }

    function abortRoundAndSelectNewAggregator(IGMStorage gm_storage, uint256 round, address failedAggregator) internal {
        require(current_aggregator == failedAggregator, "reported aggregator changed");
        roundAborted[round] = true;

        address[] memory penalized = new address[](1);
        penalized[0] = failedAggregator;
        gm_storage.penalizeContribution(round, failedAggregator, penalized, "aggregator_timeout_consensus");
        gm_storage.abortRound(failedAggregator);
        uint256 nextRound = gm_storage.getRound();
        require(nextRound == round + 1, "GMStorage round did not advance");

        uint256 randomness = uint256(
            keccak256(
                abi.encodePacked(
                    block.prevrandao,
                    block.timestamp,
                    block.number,
                    round,
                    failedAggregator,
                    timeoutReportCount[round][failedAggregator],
                    "aggregator-timeout"
                )
            )
        );
        address selectedAggregator = gm_storage.getWeightedRandomContributorExcluding(randomness, failedAggregator);
        require(selectedAggregator != address(0), "No replacement aggregator candidates");

        current_aggregator = selectedAggregator;
        lastSelectionRound = nextRound;
        broker_endpoint = "new_endpoint";
        system_state = "TRAINING";

        emit RoundAborted(round, failedAggregator, selectedAggregator);
        emit AggregatorSelected(failedAggregator, selectedAggregator, nextRound, randomness);
    }
}
