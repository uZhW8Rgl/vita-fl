import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const registryAbi = JSON.parse(
    fs.readFileSync(new URL("../abi/registry.json", import.meta.url), "utf8"),
);
const gmAbi = JSON.parse(
    fs.readFileSync(new URL("../abi/gm.json", import.meta.url), "utf8"),
);
const aggregationPolicyAbi = JSON.parse(
    fs.readFileSync(
        new URL("../abi/aggregation_policy.json", import.meta.url),
        "utf8",
    ),
);

const functionInputs = (abi, name) => {
    const entries = abi.filter(
        (entry) => entry.type === "function" && entry.name === name,
    );
    assert.equal(entries.length, 1);
    return entries[0].inputs.map(({ name: inputName, type }) => ({
        name: inputName,
        type,
    }));
};

const functionOutputs = (abi, name) => {
    const entries = abi.filter(
        (entry) => entry.type === "function" && entry.name === name,
    );
    assert.equal(entries.length, 1);
    return entries[0].outputs.map(({ name: outputName, type }) => ({
        name: outputName,
        type,
    }));
};

const eventInputs = (abi, name) => {
    const entries = abi.filter(
        (entry) => entry.type === "event" && entry.name === name,
    );
    assert.equal(entries.length, 1);
    return entries[0].inputs.map(({ name: inputName, type, indexed }) => ({
        name: inputName,
        type,
        indexed,
    }));
};

test("registration ABIs bind the logical participant to its TEE action key", () => {
    const identityInputs = [
        { name: "_address", type: "address" },
        { name: "_actionKey", type: "address" },
        { name: "_public_ip", type: "string" },
        { name: "_msg_broker_ip", type: "string" },
        { name: "_public_key", type: "bytes" },
        { name: "canonicalAppCompose", type: "bytes" },
    ];
    assert.deepEqual(
        functionInputs(registryAbi, "isDeviceRegistrationCurrent"),
        identityInputs,
    );
    assert.deepEqual(
        functionInputs(registryAbi, "registrationReportData"),
        [
            { name: "_address", type: "address" },
            { name: "actionKey", type: "address" },
            { name: "_public_ip", type: "string" },
            { name: "_msg_broker_ip", type: "string" },
            { name: "_public_key", type: "bytes" },
            { name: "canonicalAppCompose", type: "bytes" },
        ],
    );
    assert.deepEqual(
        functionInputs(registryAbi, "enrollmentDigest"),
        [
            { name: "participant", type: "address" },
            { name: "actionKey", type: "address" },
            { name: "canonicalAppCompose", type: "bytes" },
        ],
    );
    assert.deepEqual(
        functionInputs(registryAbi, "registerDeviceWithAttestedAppCompose"),
        [
            { name: "quote", type: "bytes" },
            { name: "rtmr3EventLog", type: "tuple[]" },
            { name: "canonicalAppCompose", type: "bytes" },
            { name: "_address", type: "address" },
            { name: "actionKey", type: "address" },
            { name: "_public_ip", type: "string" },
            { name: "_msg_broker_ip", type: "string" },
            { name: "_public_key", type: "bytes" },
            { name: "participantAuthorization", type: "bytes" },
        ],
    );
    assert.deepEqual(
        functionInputs(registryAbi, "actionKeys"),
        [{ name: "participant", type: "address" }],
    );
});

test("model-submission ABIs require the worker action-key commitment", () => {
    assert.deepEqual(functionInputs(gmAbi, "currentParentModelHash"), []);
    assert.deepEqual(
        functionInputs(gmAbi, "workerSubmissionNonces"),
        [{ name: "", type: "address" }],
    );
    assert.deepEqual(
        functionInputs(gmAbi, "modelSubmissionDigest"),
        [
            { name: "expectedRound", type: "uint256" },
            { name: "worker", type: "address" },
            { name: "aggregator", type: "address" },
            { name: "parentModelHash", type: "bytes32" },
            { name: "modelHash", type: "bytes32" },
            { name: "packageHash", type: "bytes32" },
            { name: "workerNonce", type: "uint256" },
        ],
    );
    assert.deepEqual(
        functionInputs(gmAbi, "recordModelSubmission"),
        [
            { name: "expectedRound", type: "uint256" },
            { name: "worker", type: "address" },
            { name: "modelHash", type: "bytes32" },
            { name: "packageHash", type: "bytes32" },
            { name: "parentModelHash", type: "bytes32" },
            { name: "workerNonce", type: "uint256" },
            { name: "workerSignature", type: "bytes" },
        ],
    );
});

test("aggregation ABIs require atomic policy-bound finalization", () => {
    assert.deepEqual(
        functionInputs(gmAbi, "openModelSubmissions"),
        [{ name: "expectedRound", type: "uint256" }],
    );
    assert.deepEqual(
        functionInputs(gmAbi, "finalizeRoundWithAggregation"),
        [
            { name: "newGlobalModel", type: "string" },
            { name: "newGlobalModelSignature", type: "string" },
            { name: "newGlobalModelKeyBundle", type: "string" },
            { name: "outputModelHash", type: "bytes32" },
            { name: "outputBundleHash", type: "bytes32" },
            { name: "statementSignature", type: "bytes" },
        ],
    );
    assert.deepEqual(
        functionInputs(aggregationPolicyAbi, "aggregationStatementDigest"),
        [
            { name: "round", type: "uint256" },
            { name: "aggregator", type: "address" },
            { name: "inputRoot", type: "bytes32" },
            { name: "inputCount", type: "uint256" },
            { name: "algorithmHash", type: "bytes32" },
            { name: "policyHash", type: "bytes32" },
            { name: "outputModelHash", type: "bytes32" },
            { name: "outputBundleHash", type: "bytes32" },
            { name: "publicationHash", type: "bytes32" },
            { name: "nonce", type: "uint256" },
        ],
    );
    assert.deepEqual(
        functionOutputs(aggregationPolicyAbi, "getRoundPolicy"),
        [
            { name: "opened", type: "bool" },
            { name: "closed", type: "bool" },
            { name: "openedAt", type: "uint64" },
            { name: "deadline", type: "uint64" },
            { name: "requiredSubmissions", type: "uint32" },
            { name: "acceptedSubmissions", type: "uint32" },
            { name: "roundConfigurationVersion", type: "uint64" },
            { name: "algorithmHash", type: "bytes32" },
            { name: "validationDataHash", type: "bytes32" },
            { name: "maxLossIncreaseBps", type: "uint16" },
            { name: "policyHash", type: "bytes32" },
            { name: "inputRoot", type: "bytes32" },
        ],
    );
    assert.deepEqual(
        functionOutputs(aggregationPolicyAbi, "HYBRID_R_V1_HASH"),
        [{ name: "", type: "bytes32" }],
    );
    assert.deepEqual(
        functionOutputs(
            aggregationPolicyAbi,
            "HYBRID_R_VALIDATION_DATA_V1_HASH",
        ),
        [{ name: "", type: "bytes32" }],
    );
    assert.deepEqual(
        functionOutputs(
            aggregationPolicyAbi,
            "HYBRID_R_MAX_LOSS_INCREASE_BPS",
        ),
        [{ name: "", type: "uint16" }],
    );
    assert.equal(
        aggregationPolicyAbi.some(
            (entry) =>
                entry.type === "function" &&
                entry.name === "FEDERATED_AVERAGING_V1_HASH",
        ),
        false,
    );
    assert.deepEqual(
        eventInputs(aggregationPolicyAbi, "DefaultAggregationPolicyConfigured"),
        [
            { name: "configurationVersion", type: "uint64", indexed: true },
            { name: "requiredSubmissions", type: "uint32", indexed: false },
            {
                name: "submissionWindowSeconds",
                type: "uint64",
                indexed: false,
            },
            { name: "algorithmHash", type: "bytes32", indexed: false },
            { name: "validationDataHash", type: "bytes32", indexed: false },
            { name: "maxLossIncreaseBps", type: "uint16", indexed: false },
        ],
    );
    assert.deepEqual(
        eventInputs(aggregationPolicyAbi, "RoundAggregationPolicyOpened"),
        [
            { name: "round", type: "uint256", indexed: true },
            { name: "configurationVersion", type: "uint64", indexed: true },
            { name: "requiredSubmissions", type: "uint32", indexed: false },
            { name: "openedAt", type: "uint64", indexed: false },
            { name: "deadline", type: "uint64", indexed: false },
            { name: "algorithmHash", type: "bytes32", indexed: false },
            { name: "validationDataHash", type: "bytes32", indexed: false },
            { name: "maxLossIncreaseBps", type: "uint16", indexed: false },
            { name: "policyHash", type: "bytes32", indexed: false },
            { name: "inputRoot", type: "bytes32", indexed: false },
        ],
    );
});
