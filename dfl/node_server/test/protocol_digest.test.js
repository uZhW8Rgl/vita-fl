import test from "node:test";
import assert from "node:assert/strict";

import {
    BOOTSTRAP_ROLLOVER_V1_HASH,
    AGGREGATION_POLICY_HASH_DOMAIN,
    FEDERATED_AVERAGING_V1_HASH,
    deriveAggregationStatementDigest,
    deriveModelSubmissionDigest,
    derivePublicationHash,
    deriveRoundAggregationPolicyHash,
} from "../dist/protocol_digest.js";

const commitment = {
    chainId: 31337,
    verifyingContract: "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0",
    round: 7,
    aggregator: "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    worker: "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
    parentModelHash: `0x${"11".repeat(32)}`,
    modelHash: `0x${"22".repeat(32)}`,
    packageHash: `0x${"33".repeat(32)}`,
    nonce: 4,
};

test("model-submission EIP-712 digest matches the independent Foundry vector", () => {
    assert.equal(
        deriveModelSubmissionDigest(commitment),
        "0x7661b7f15cbe0b4ce9f1a44ea7b755e6e2cd58d69098b799345f06dfac4df3f4",
    );
});

test("model-submission digest rejects malformed domain and commitment fields", () => {
    assert.throws(
        () => deriveModelSubmissionDigest({ ...commitment, chainId: -1 }),
        /chainId must be an unsigned integer/,
    );
    assert.throws(
        () => deriveModelSubmissionDigest({
            ...commitment,
            verifyingContract: "0x1234",
        }),
        /verifyingContract must be a 20-byte Ethereum address/,
    );
    assert.throws(
        () => deriveModelSubmissionDigest({
            ...commitment,
            packageHash: "0x1234",
        }),
        /packageHash must be exactly 32 bytes/,
    );
});

const aggregationStatement = {
    chainId: 31337,
    verifyingContract: "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0",
    round: 7,
    aggregator: "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    inputRoot: `0x${"44".repeat(32)}`,
    inputCount: 3,
    algorithmHash: FEDERATED_AVERAGING_V1_HASH,
    policyHash: `0x${"99".repeat(32)}`,
    outputModelHash: `0x${"55".repeat(32)}`,
    outputBundleHash: `0x${"66".repeat(32)}`,
    publicationHash: `0x${"77".repeat(32)}`,
    nonce: 2,
};

test("aggregation policy constants match the Solidity protocol constants", () => {
    assert.equal(
        AGGREGATION_POLICY_HASH_DOMAIN,
        "0x03545c9295b6307cadb078599150abf360d5ce9b1d67152a83df4777add8ace9",
    );
    assert.equal(
        FEDERATED_AVERAGING_V1_HASH,
        "0x6c955b19c102b6c3438fb28e1190cda5a6fc0e0f435a62c14f2de62476784cd9",
    );
    assert.equal(
        BOOTSTRAP_ROLLOVER_V1_HASH,
        "0xba9d99ae43ad6eea0955e2921695b55e933a192ef6de67cbc6368e41dfe83082",
    );
});

test("FedAvg round policy hash binds its zeroed extension fields", () => {
    const policy = {
        configurationVersion: 2,
        requiredSubmissions: 3,
        openedAt: 1000,
        deadline: 1600,
        algorithmHash: FEDERATED_AVERAGING_V1_HASH,
        validationDataHash: `0x${"00".repeat(32)}`,
        maxLossIncreaseBps: 0,
    };
    assert.equal(
        deriveRoundAggregationPolicyHash(policy),
        "0x19a4d837501c069375bd19aca79380bd837a44ad74c7c3b467754a2acd014576",
    );
    for (const [field, value] of [
        ["configurationVersion", 3],
        ["requiredSubmissions", 4],
        ["openedAt", 1001],
        ["deadline", 1601],
        ["algorithmHash", BOOTSTRAP_ROLLOVER_V1_HASH],
        ["validationDataHash", `0x${"01".repeat(32)}`],
        ["maxLossIncreaseBps", 1],
    ]) {
        assert.notEqual(
            deriveRoundAggregationPolicyHash({ ...policy, [field]: value }),
            deriveRoundAggregationPolicyHash(policy),
            `${field} was not bound by the round policy hash`,
        );
    }
});

test("aggregation-statement EIP-712 digest matches the independent ABI vector", () => {
    assert.equal(
        deriveAggregationStatementDigest(aggregationStatement),
        "0x16a0072deac9beefba98c90de17ae3c16a672651891afb8c9da33c7b62fdb668",
    );
});

test("every aggregation-statement and EIP-712 domain field changes the digest", () => {
    const expected = deriveAggregationStatementDigest(aggregationStatement);
    const mutations = [
        ["chainId", 31338],
        ["verifyingContract", "0x0000000000000000000000000000000000000001"],
        ["round", 8],
        ["aggregator", "0x0000000000000000000000000000000000000002"],
        ["inputRoot", `0x${"45".repeat(32)}`],
        ["inputCount", 4],
        ["algorithmHash", BOOTSTRAP_ROLLOVER_V1_HASH],
        ["policyHash", `0x${"98".repeat(32)}`],
        ["outputModelHash", `0x${"56".repeat(32)}`],
        ["outputBundleHash", `0x${"67".repeat(32)}`],
        ["publicationHash", `0x${"78".repeat(32)}`],
        ["nonce", 3],
    ];

    for (const [field, value] of mutations) {
        assert.notEqual(
            deriveAggregationStatementDigest({
                ...aggregationStatement,
                [field]: value,
            }),
            expected,
            `${field} was not bound by the aggregation statement`,
        );
    }
});

test("aggregation-statement digest rejects malformed fields", () => {
    assert.throws(
        () => deriveAggregationStatementDigest({
            ...aggregationStatement,
            inputCount: -1,
        }),
        /inputCount must be an unsigned integer/,
    );
    assert.throws(
        () => deriveAggregationStatementDigest({
            ...aggregationStatement,
            inputRoot: "0x1234",
        }),
        /inputRoot must be exactly 32 bytes/,
    );
    assert.throws(
        () => deriveAggregationStatementDigest({
            ...aggregationStatement,
            policyHash: "0x1234",
        }),
        /policyHash must be exactly 32 bytes/,
    );
    assert.throws(
        () => deriveAggregationStatementDigest({
            ...aggregationStatement,
            aggregator: "0x1234",
        }),
        /aggregator must be a 20-byte Ethereum address/,
    );
});

test("publication hash matches Solidity abi.encode for the three exact CID strings", () => {
    const publication = {
        modelCid: "bafy-model-v1",
        signatureCid: "bafy-signature-v1",
        keyBundleCid: "bafy-key-bundle-v1",
    };
    assert.equal(
        derivePublicationHash(publication),
        "0x5c50b8111ad90aeaaa3709b1ef99f34e4d2e3560ee1e3bafd09a79d95246a66e",
    );

    const expected = derivePublicationHash(publication);
    for (const [field, value] of [
        ["modelCid", "bafy-model-v2"],
        ["signatureCid", "bafy-signature-v2"],
        ["keyBundleCid", "bafy-key-bundle-v2"],
    ]) {
        assert.notEqual(
            derivePublicationHash({ ...publication, [field]: value }),
            expected,
            `${field} was not bound by the publication hash`,
        );
    }
});

test("publication hash preserves ABI string boundaries and rejects empty CIDs", () => {
    assert.notEqual(
        derivePublicationHash({
            modelCid: "ab",
            signatureCid: "c",
            keyBundleCid: "d",
        }),
        derivePublicationHash({
            modelCid: "a",
            signatureCid: "bc",
            keyBundleCid: "d",
        }),
    );
    assert.throws(
        () => derivePublicationHash({
            modelCid: "",
            signatureCid: "signature",
            keyBundleCid: "keys",
        }),
        /modelCid must be a non-empty string/,
    );
});
