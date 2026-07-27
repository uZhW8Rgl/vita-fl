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
const SESSION_SALT = Buffer.from("11".repeat(32), "hex");
const web3 = new Web3();

const derive = async () => Uint8Array.from(KEY);

test("one dstack-backed session keeps a stable action key and signs raw digests", async () => {
    const signer = await loadParticipantActionSigner({
        env: { DOCKER: "phala" },
        deriveKey: derive,
        sessionSalt: SESSION_SALT,
    });
    const second = await loadParticipantActionSigner({
        env: { DOCKER: "phala" },
        deriveKey: derive,
        sessionSalt: SESSION_SALT,
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

test("a fresh process salt rotates the dstack-backed action address", async () => {
    const first = await loadParticipantActionSigner({
        env: { DOCKER: "phala" },
        deriveKey: derive,
        sessionSalt: Buffer.from("22".repeat(32), "hex"),
    });
    const restarted = await loadParticipantActionSigner({
        env: { DOCKER: "phala" },
        deriveKey: derive,
        sessionSalt: Buffer.from("33".repeat(32), "hex"),
    });
    assert.notEqual(first.address, restarted.address);
});

test("action signer accepts only fully local contract calls and no value transfer", async () => {
    const signer = await loadParticipantActionSigner({
        env: { DOCKER: "phala" },
        deriveKey: derive,
        sessionSalt: SESSION_SALT,
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

test("Phala never falls back to an environment action key", async () => {
    await assert.rejects(
        loadParticipantActionSigner({
            env: {
                DOCKER: "phala",
                LOCAL_TDX_MOCK: "1",
                LOCAL_ACTION_PRIVATE_KEY: `0x${KEY.toString("hex")}`,
            },
            deriveKey: async () => {
                throw new Error("dstack unavailable");
            },
            sessionSalt: SESSION_SALT,
        }),
        /dstack unavailable/,
    );
});

test("local action key is restricted to mock mode", async () => {
    await assert.rejects(
        loadParticipantActionSigner({ env: {} }),
        /only allowed with LOCAL_TDX_MOCK=1/,
    );

    const signer = await loadParticipantActionSigner({
        env: {
            LOCAL_TDX_MOCK: "1",
            PRIVATE_KEY: `0x${KEY.toString("hex")}`,
        },
        sessionSalt: SESSION_SALT,
    });
    assert.match(signer.address, /^0x[0-9a-f]{40}$/);
});

test("destroy wipes the session authority and prevents further signing", async () => {
    const signer = await loadParticipantActionSigner({
        env: { DOCKER: "phala" },
        deriveKey: derive,
        sessionSalt: SESSION_SALT,
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

test("RSA and Ethereum action derivation contexts are distinct", () => {
    assert.equal(
        PARTICIPANT_ACTION_KEY_CONTEXT.path,
        "vita-fl/participant-ethereum-action/v1",
    );
    assert.equal(
        PARTICIPANT_ACTION_KEY_CONTEXT.purpose,
        "ethereum-secp256k1-action",
    );
    assert.equal(
        PARTICIPANT_ACTION_KEY_CONTEXT.sessionDomain,
        "MasterThesis.participant-ethereum-action.session.v1",
    );
});
