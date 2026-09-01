// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import {StrictECDSA} from "../crypto/StrictECDSA.sol";

/// @notice Stores the owner-approved aggregation policy and the TEE-signed
///         input/output statement for every DFL round.
/// @dev GMStorage is the only state-mutating caller. The EIP-712 verifying
///      contract remains GMStorage so the statement belongs to the model ledger.
contract AggregationPolicy {
    using StrictECDSA for bytes32;

    uint32 public constant MAX_REQUIRED_SUBMISSIONS = 500;
    uint64 public constant MAX_SUBMISSION_WINDOW_SECONDS = 7 days;

    bytes32 public constant POLICY_HASH_DOMAIN = keccak256("VITA-FL:aggregation-policy:v2");
    bytes32 public constant INPUT_ROOT_SEED = keccak256("VITA-FL:aggregation-input-root:v1");
    bytes32 public constant FEDERATED_AVERAGING_V1_HASH =
        keccak256("VITA-FL:fedavg:torch-state-dict:float64:equal-weight:v1");
    bytes32 public constant BOOTSTRAP_ROLLOVER_V1_HASH = keccak256("VITA-FL:bootstrap-model-rollover:v1");

    bytes32 public constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 public constant AGGREGATION_STATEMENT_TYPEHASH = keccak256(
        "AggregationStatement(uint256 round,address aggregator,bytes32 inputRoot,uint256 inputCount,bytes32 algorithmHash,bytes32 policyHash,bytes32 outputModelHash,bytes32 outputBundleHash,bytes32 publicationHash,uint256 nonce)"
    );
    bytes32 private constant EIP712_NAME_HASH = keccak256("VITA-FL GMStorage");
    bytes32 private constant EIP712_VERSION_HASH = keccak256("1");

    struct RoundPolicy {
        uint64 openedAt;
        uint64 deadline;
        uint32 requiredSubmissions;
        uint32 acceptedSubmissions;
        uint64 configurationVersion;
        bool opened;
        bool closed;
        bytes32 algorithmHash;
        bytes32 validationDataHash;
        uint16 maxLossIncreaseBps;
        bytes32 policyHash;
        bytes32 inputRoot;
    }

    struct AggregationEvidence {
        bool published;
        address aggregator;
        address actionKey;
        uint256 nonce;
        bytes32 inputRoot;
        uint256 inputCount;
        bytes32 algorithmHash;
        bytes32 policyHash;
        bytes32 outputModelHash;
        bytes32 outputBundleHash;
        bytes32 publicationHash;
        bytes32 statementDigest;
        bytes signature;
    }

    address public immutable gmStorage;
    address public owner;
    uint32 public defaultRequiredSubmissions;
    uint64 public defaultSubmissionWindowSeconds;
    uint64 public configurationVersion;

    mapping(uint256 => RoundPolicy) private roundPolicies;
    mapping(uint256 => AggregationEvidence) private aggregationEvidence;
    mapping(address => uint256) public aggregationNonces;

    event DefaultAggregationPolicyConfigured(
        uint64 indexed configurationVersion,
        uint32 requiredSubmissions,
        uint64 submissionWindowSeconds,
        bytes32 algorithmHash,
        bytes32 validationDataHash,
        uint16 maxLossIncreaseBps
    );
    event RoundAggregationPolicyOpened(
        uint256 indexed round,
        uint64 indexed configurationVersion,
        uint32 requiredSubmissions,
        uint64 openedAt,
        uint64 deadline,
        bytes32 algorithmHash,
        bytes32 validationDataHash,
        uint16 maxLossIncreaseBps,
        bytes32 policyHash,
        bytes32 inputRoot
    );
    event RoundAggregationInputExtended(
        uint256 indexed round,
        address indexed worker,
        uint32 acceptedSubmissions,
        bytes32 submissionCommitment,
        bytes32 inputRoot
    );
    event RoundAggregationInputsClosed(uint256 indexed round, uint32 acceptedSubmissions, bytes32 inputRoot);
    event AggregationStatementAccepted(
        uint256 indexed round,
        address indexed aggregator,
        address indexed actionKey,
        uint256 inputCount,
        bytes32 inputRoot,
        bytes32 algorithmHash,
        bytes32 policyHash,
        bytes32 outputModelHash,
        bytes32 outputBundleHash,
        bytes32 publicationHash,
        bytes32 statementDigest
    );

    modifier onlyOwner() {
        require(msg.sender == owner, "not aggregation policy owner");
        _;
    }

    modifier onlyGMStorage() {
        require(msg.sender == gmStorage, "caller is not GMStorage");
        _;
    }

    constructor(address _gmStorage) {
        require(_gmStorage != address(0), "GMStorage is zero");
        gmStorage = _gmStorage;
        owner = msg.sender;
    }

    function configureDefaultPolicy(uint32 requiredSubmissions, uint64 submissionWindowSeconds) external onlyOwner {
        require(
            requiredSubmissions > 0 && requiredSubmissions <= MAX_REQUIRED_SUBMISSIONS, "invalid required submissions"
        );
        require(
            submissionWindowSeconds > 0 && submissionWindowSeconds <= MAX_SUBMISSION_WINDOW_SECONDS,
            "invalid submission window"
        );
        configurationVersion++;
        defaultRequiredSubmissions = requiredSubmissions;
        defaultSubmissionWindowSeconds = submissionWindowSeconds;
        emit DefaultAggregationPolicyConfigured(
            configurationVersion,
            requiredSubmissions,
            submissionWindowSeconds,
            FEDERATED_AVERAGING_V1_HASH,
            bytes32(0),
            0
        );
    }

    function openRound(uint256 round) external onlyGMStorage {
        RoundPolicy storage policy = roundPolicies[round];
        if (policy.opened) {
            require(!policy.closed, "round inputs already closed");
            return;
        }
        require(configurationVersion > 0, "aggregation policy is not configured");
        require(block.timestamp <= type(uint64).max - defaultSubmissionWindowSeconds, "deadline overflow");

        uint32 requiredSubmissions = round == 0 ? 0 : defaultRequiredSubmissions;
        bytes32 algorithmHash = round == 0 ? BOOTSTRAP_ROLLOVER_V1_HASH : FEDERATED_AVERAGING_V1_HASH;
        bytes32 validationDataHash = bytes32(0);
        uint16 maxLossIncreaseBps = 0;
        uint64 openedAt = uint64(block.timestamp);
        uint64 deadline = openedAt + defaultSubmissionWindowSeconds;
        bytes32 policyHash = keccak256(
            abi.encode(
                POLICY_HASH_DOMAIN,
                configurationVersion,
                requiredSubmissions,
                openedAt,
                deadline,
                algorithmHash,
                validationDataHash,
                maxLossIncreaseBps
            )
        );

        policy.openedAt = openedAt;
        policy.deadline = deadline;
        policy.requiredSubmissions = requiredSubmissions;
        policy.configurationVersion = configurationVersion;
        policy.opened = true;
        policy.algorithmHash = algorithmHash;
        policy.validationDataHash = validationDataHash;
        policy.maxLossIncreaseBps = maxLossIncreaseBps;
        policy.policyHash = policyHash;
        policy.inputRoot = INPUT_ROOT_SEED;

        emit RoundAggregationPolicyOpened(
            round,
            configurationVersion,
            requiredSubmissions,
            openedAt,
            deadline,
            algorithmHash,
            validationDataHash,
            maxLossIncreaseBps,
            policyHash,
            INPUT_ROOT_SEED
        );
    }

    function recordSubmission(uint256 round, address worker, bytes32 submissionCommitment) external onlyGMStorage {
        RoundPolicy storage policy = roundPolicies[round];
        require(policy.opened, "round aggregation policy is not open");
        require(!policy.closed, "round inputs are closed");
        require(block.timestamp <= policy.deadline, "round submission deadline passed");
        require(worker != address(0), "worker is zero");
        require(submissionCommitment != bytes32(0), "submission commitment is zero");
        require(policy.acceptedSubmissions < MAX_REQUIRED_SUBMISSIONS, "too many submissions");

        policy.acceptedSubmissions++;
        policy.inputRoot = keccak256(abi.encode(policy.inputRoot, worker, submissionCommitment));
        emit RoundAggregationInputExtended(
            round, worker, policy.acceptedSubmissions, submissionCommitment, policy.inputRoot
        );
    }

    function closeRound(uint256 round, uint256 reportedSubmissionCount) external onlyGMStorage {
        RoundPolicy storage policy = roundPolicies[round];
        require(policy.opened, "round aggregation policy is not open");
        if (policy.closed) {
            require(reportedSubmissionCount == policy.acceptedSubmissions, "closed input count mismatch");
            return;
        }
        require(reportedSubmissionCount == policy.acceptedSubmissions, "input count mismatch");
        require(policy.acceptedSubmissions >= policy.requiredSubmissions, "required submissions not reached");
        policy.closed = true;
        emit RoundAggregationInputsClosed(round, policy.acceptedSubmissions, policy.inputRoot);
    }

    function verifyAndRecordPublication(
        uint256 round,
        address aggregator,
        address actionKey,
        bytes32 outputModelHash,
        bytes32 outputBundleHash,
        bytes32 publicationHash,
        bytes calldata signature
    ) external onlyGMStorage returns (bytes32 statementDigest) {
        RoundPolicy storage policy = roundPolicies[round];
        require(policy.closed, "round inputs are not closed");
        require(aggregator != address(0), "aggregator is zero");
        require(actionKey != address(0), "action key is zero");
        require(outputModelHash != bytes32(0), "output model hash is zero");
        require(outputBundleHash != bytes32(0), "output bundle hash is zero");
        require(publicationHash != bytes32(0), "publication hash is zero");

        AggregationEvidence storage evidence = aggregationEvidence[round];
        if (evidence.published) {
            require(evidence.aggregator == aggregator, "conflicting publication aggregator");
            require(evidence.actionKey == actionKey, "conflicting publication action key");
            require(evidence.outputModelHash == outputModelHash, "conflicting output model hash");
            require(evidence.outputBundleHash == outputBundleHash, "conflicting output bundle hash");
            require(evidence.publicationHash == publicationHash, "conflicting publication hash");
            require(keccak256(evidence.signature) == keccak256(signature), "conflicting statement signature");
            return evidence.statementDigest;
        }

        uint256 nonce = aggregationNonces[aggregator];
        statementDigest = aggregationStatementDigest(
            round,
            aggregator,
            policy.inputRoot,
            policy.acceptedSubmissions,
            policy.algorithmHash,
            policy.policyHash,
            outputModelHash,
            outputBundleHash,
            publicationHash,
            nonce
        );
        require(statementDigest.recover(signature) == actionKey, "invalid aggregation statement");

        evidence.published = true;
        evidence.aggregator = aggregator;
        evidence.actionKey = actionKey;
        evidence.nonce = nonce;
        evidence.inputRoot = policy.inputRoot;
        evidence.inputCount = policy.acceptedSubmissions;
        evidence.algorithmHash = policy.algorithmHash;
        evidence.policyHash = policy.policyHash;
        evidence.outputModelHash = outputModelHash;
        evidence.outputBundleHash = outputBundleHash;
        evidence.publicationHash = publicationHash;
        evidence.statementDigest = statementDigest;
        evidence.signature = signature;
        aggregationNonces[aggregator] = nonce + 1;

        emit AggregationStatementAccepted(
            round,
            aggregator,
            actionKey,
            policy.acceptedSubmissions,
            policy.inputRoot,
            policy.algorithmHash,
            policy.policyHash,
            outputModelHash,
            outputBundleHash,
            publicationHash,
            statementDigest
        );
    }

    function domainSeparator() public view returns (bytes32) {
        return
            keccak256(
                abi.encode(EIP712_DOMAIN_TYPEHASH, EIP712_NAME_HASH, EIP712_VERSION_HASH, block.chainid, gmStorage)
            );
    }

    function aggregationStatementDigest(
        uint256 round,
        address aggregator,
        bytes32 inputRoot,
        uint256 inputCount,
        bytes32 algorithmHash,
        bytes32 policyHash,
        bytes32 outputModelHash,
        bytes32 outputBundleHash,
        bytes32 publicationHash,
        uint256 nonce
    ) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                AGGREGATION_STATEMENT_TYPEHASH,
                round,
                aggregator,
                inputRoot,
                inputCount,
                algorithmHash,
                policyHash,
                outputModelHash,
                outputBundleHash,
                publicationHash,
                nonce
            )
        );
        return keccak256(abi.encodePacked(hex"1901", domainSeparator(), structHash));
    }

    function getRoundPolicy(uint256 round)
        external
        view
        returns (
            bool opened,
            bool closed,
            uint64 openedAt,
            uint64 deadline,
            uint32 requiredSubmissions,
            uint32 acceptedSubmissions,
            uint64 roundConfigurationVersion,
            bytes32 algorithmHash,
            bytes32 validationDataHash,
            uint16 maxLossIncreaseBps,
            bytes32 policyHash,
            bytes32 inputRoot
        )
    {
        RoundPolicy storage policy = roundPolicies[round];
        return (
            policy.opened,
            policy.closed,
            policy.openedAt,
            policy.deadline,
            policy.requiredSubmissions,
            policy.acceptedSubmissions,
            policy.configurationVersion,
            policy.algorithmHash,
            policy.validationDataHash,
            policy.maxLossIncreaseBps,
            policy.policyHash,
            policy.inputRoot
        );
    }

    function getAggregationEvidence(uint256 round)
        external
        view
        returns (
            bool published,
            address aggregator,
            address actionKey,
            uint256 nonce,
            bytes32 inputRoot,
            uint256 inputCount,
            bytes32 algorithmHash,
            bytes32 policyHash,
            bytes32 outputModelHash,
            bytes32 outputBundleHash,
            bytes32 publicationHash,
            bytes32 statementDigest,
            bytes memory signature
        )
    {
        AggregationEvidence storage evidence = aggregationEvidence[round];
        return (
            evidence.published,
            evidence.aggregator,
            evidence.actionKey,
            evidence.nonce,
            evidence.inputRoot,
            evidence.inputCount,
            evidence.algorithmHash,
            evidence.policyHash,
            evidence.outputModelHash,
            evidence.outputBundleHash,
            evidence.publicationHash,
            evidence.statementDigest,
            evidence.signature
        );
    }
}
