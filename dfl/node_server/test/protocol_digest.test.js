import test from "node:test";
import assert from "node:assert/strict";

import { deriveModelSubmissionDigest } from "../dist/protocol_digest.js";

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
