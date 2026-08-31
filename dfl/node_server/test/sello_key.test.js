import assert from "node:assert/strict";
import test from "node:test";

import {
    deriveSelloSigningSeed,
    ed25519PublicKeyFromSeed,
    loadSelloReceiptPublicKey,
} from "../dist/sello_key.js";

const BASE_ENV = {
    ACCOUNT_ADDRESS: `0x${"11".repeat(20)}`,
    REGISTRY_ADDRESS: `0x${"22".repeat(20)}`,
    EXPECTED_CHAIN_ID: "31337",
};

test("Sello derivation matches the Python cross-language vector", () => {
    const seed = deriveSelloSigningSeed(Uint8Array.from({ length: 32 }, (_, i) => i), BASE_ENV);
    assert.equal(
        Buffer.from(seed).toString("hex"),
        "33cd5c216b863348bfc74d81841283e27c568452bee31162d22e24eab6af4190",
    );
    assert.equal(ed25519PublicKeyFromSeed(seed).length, 32);
});

test("Worker 0 exposes only the public receipt key for registration", async () => {
    const publicKey = await loadSelloReceiptPublicKey({
        env: { ...BASE_ENV, LOCAL_TDX_MOCK: "1" },
        deriveKey: async () => Uint8Array.from({ length: 32 }, (_, i) => i),
    });
    assert.equal(publicKey.length, 32);
});

test("Phala cannot fall back to an environment-provisioned Sello key", async () => {
    await assert.rejects(
        loadSelloReceiptPublicKey({
            env: {
                ...BASE_ENV,
                DOCKER: "phala",
                SELLO_SERVICE_KEY_PROVIDER: "env",
                LOCAL_TDX_MOCK: "1",
                SELLO_SERVICE_SIGNING_SEED: "11".repeat(32),
            },
        }),
        /require SELLO_SERVICE_KEY_PROVIDER=dstack/,
    );
});
