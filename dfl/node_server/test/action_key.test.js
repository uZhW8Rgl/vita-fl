import test from "node:test";
import assert from "node:assert/strict";
import { secp256k1 } from "@noble/curves/secp256k1";
import Web3 from "web3";

import {
    loadParticipantActionSigner,
    PARTICIPANT_ACTION_KEY_CONTEXT,
} from "../dist/action_key.js";

const KEY = Buffer.from(
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "hex",
);
const web3 = new Web3();
const PHALA_ENV = {
    DOCKER: "phala",
    ACCOUNT_ADDRESS: "0x1111111111111111111111111111111111111111",
    EXPECTED_CHAIN_ID: "31337",
    REGISTRY_ADDRESS: "0x2222222222222222222222222222222222222222",
};
const LOCAL_ENV = {
    LOCAL_TDX_MOCK: "1",
    PRIVATE_KEY: `0x${KEY.toString("hex")}`,
    ACCOUNT_ADDRESS: "0x1111111111111111111111111111111111111111",
    EXPECTED_CHAIN_ID: "31337",
    REGISTRY_ADDRESS: "0x2222222222222222222222222222222222222222",
};

const derive = async () => Uint8Array.from(KEY);

test("injected test entropy produces a stable signer that signs raw digests", async () => {
    const signer = await loadParticipantActionSigner({
        env: PHALA_ENV,
        deriveKey: derive,
    });
    const second = await loadParticipantActionSigner({
        env: PHALA_ENV,
        deriveKey: derive,
    });
    assert.equal(signer.address, second.address);
    assert.match(signer.address, /^0x[0-9a-f]{40}$/);

    const digest = `0x${"42".repeat(32)}`;
    const signature = await signer.signDigest(digest);
    assert.match(signature, /^0x[0-9a-f]{130}$/);
    assert.ok(signature.endsWith("1b") || signature.endsWith("1c"));
    const recovery = Number.parseInt(signature.slice(-2), 16) - 27;
    const publicKey = secp256k1.Signature
        .fromCompact(signature.slice(2, 130))
        .addRecoveryBit(recovery)
        .recoverPublicKey(digest.slice(2))
        .toRawBytes(false);
    const recoveredAddress =
        `0x${web3.utils.keccak256(publicKey.slice(1)).slice(-40)}`.toLowerCase();
    assert.equal(recoveredAddress, signer.address);

    const rawTransaction = await signer.signTransaction({
        from: signer.address,
        to: "0x0000000000000000000000000000000000000001",
        value: "0",
        gas: "50000",
        gasPrice: "1",
        nonce: 0,
        chainId: 31337,
        data: "0x12345678",
    });
    assert.match(rawTransaction, /^0x[0-9a-f]+$/);
    assert.equal(
        web3.eth.accounts.recoverTransaction(rawTransaction).toLowerCase(),
        signer.address,
    );
});

test("different injected entropy creates different action addresses", async () => {
    const first = await loadParticipantActionSigner({
        env: PHALA_ENV,
        deriveKey: derive,
    });
    const restarted = await loadParticipantActionSigner({
        env: PHALA_ENV,
        deriveKey: async () => Uint8Array.from(Buffer.from("ab".repeat(32), "hex")),
    });
    assert.notEqual(first.address, restarted.address);
});

test("participant, chain, and registry are part of the action-key derivation context", async () => {
    const base = await loadParticipantActionSigner({
        env: PHALA_ENV,
        deriveKey: derive,
    });
    for (const env of [
        { ...PHALA_ENV, ACCOUNT_ADDRESS: "0x3333333333333333333333333333333333333333" },
        { ...PHALA_ENV, EXPECTED_CHAIN_ID: "1" },
        { ...PHALA_ENV, REGISTRY_ADDRESS: "0x4444444444444444444444444444444444444444" },
    ]) {
        const scoped = await loadParticipantActionSigner({ env, deriveKey: derive });
        assert.notEqual(base.address, scoped.address);
    }
});

test("action signer accepts only fully local contract calls and no value transfer", async () => {
    const signer = await loadParticipantActionSigner({
        env: PHALA_ENV,
        deriveKey: derive,
    });
    const base = {
        from: signer.address,
        to: "0x0000000000000000000000000000000000000001",
        gas: "50000",
        gasPrice: "1",
        nonce: 0,
        chainId: 31337,
        data: "0x12345678",
    };
    await assert.rejects(
        signer.signTransaction({ ...base, value: "1" }),
        /cannot authorize value transfers/,
    );
    await assert.rejects(
        signer.signTransaction({ ...base, from: "0x0000000000000000000000000000000000000002" }),
        /unexpected sender/,
    );
    await assert.rejects(
        signer.signTransaction({ ...base, data: "0x" }),
        /requires contract calldata/,
    );
});

test("entropy acquisition failures do not fall back to environment action keys", async () => {
    await assert.rejects(
        loadParticipantActionSigner({
            env: {
                ...PHALA_ENV,
                LOCAL_TDX_MOCK: "1",
                LOCAL_ACTION_PRIVATE_KEY: `0x${KEY.toString("hex")}`,
            },
            deriveKey: async () => {
                throw new Error("entropy unavailable");
            },
        }),
        /entropy unavailable/,
    );
});

test("local action key is restricted to mock mode", async () => {
    await assert.rejects(
        loadParticipantActionSigner({
            env: {
                ACCOUNT_ADDRESS: LOCAL_ENV.ACCOUNT_ADDRESS,
                EXPECTED_CHAIN_ID: LOCAL_ENV.EXPECTED_CHAIN_ID,
                REGISTRY_ADDRESS: LOCAL_ENV.REGISTRY_ADDRESS,
            },
        }),
        /only allowed with LOCAL_TDX_MOCK=1/,
    );

    const signer = await loadParticipantActionSigner({
        env: LOCAL_ENV,
    });
    assert.match(signer.address, /^0x[0-9a-f]{40}$/);
});

test("destroy disables the in-memory authority and prevents further signing", async () => {
    const signer = await loadParticipantActionSigner({
        env: PHALA_ENV,
        deriveKey: derive,
    });
    signer.destroy();
    await assert.rejects(
        signer.signDigest(`0x${"42".repeat(32)}`),
        /has been destroyed/,
    );
    await assert.rejects(
        signer.signTransaction({
            to: "0x0000000000000000000000000000000000000001",
            gas: "21000",
            gasPrice: "1",
            nonce: 0,
            chainId: 31337,
        }),
        /has been destroyed/,
    );
});

test("action signing uses its own ephemeral purpose and context", () => {
    assert.equal(PARTICIPANT_ACTION_KEY_CONTEXT.purpose, "ethereum-secp256k1-action");
    assert.equal(PARTICIPANT_ACTION_KEY_CONTEXT.derivationDomain,
        "MasterThesis.participant-ethereum-action.ephemeral.v1");
});

test("real random action keys differ per initialization and remain stable during signing", async () => {
    const first = await loadParticipantActionSigner({env: PHALA_ENV});
    const second = await loadParticipantActionSigner({env: PHALA_ENV});
    assert.equal(first.source, "ephemeral-random");
    assert.notEqual(first.address, second.address);
    const digest = `0x${"42".repeat(32)}`;
    assert.equal(await first.signDigest(digest), await first.signDigest(digest));
    first.destroy(); second.destroy();
});

test("action entropy is acquired once per lifetime, never for individual operations", async () => {
    let calls = 0;
    const signer = await loadParticipantActionSigner({env:PHALA_ENV, deriveKey:async () => {calls++; return Uint8Array.from(KEY);}});
    await signer.signDigest(`0x${"42".repeat(32)}`);
    await signer.signDigest(`0x${"43".repeat(32)}`);
    assert.equal(calls, 1);
    signer.destroy();
});
