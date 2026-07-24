import assert from "node:assert/strict";
import test from "node:test";

import {
    normalizePublisherPublicKeyDerHex,
} from "../dist/model_publisher.js";

test("publisher key normalization accepts a non-empty DER byte string", () => {
    assert.equal(
        normalizePublisherPublicKeyDerHex("0X3082010A"),
        "0x3082010a",
    );
});

test("publisher key normalization rejects missing or malformed on-chain bytes", () => {
    for (const value of [undefined, null, "", "0x", "0x0", "0xzz", new Uint8Array([1])]) {
        assert.throws(
            () => normalizePublisherPublicKeyDerHex(value),
            /publisher public key/i,
        );
    }
});
