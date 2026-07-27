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
});
