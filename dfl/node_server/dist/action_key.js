import { secp256k1 } from "@noble/curves/secp256k1";
import { DstackClient } from "@phala/dstack-sdk";
import crypto from "crypto";
import { AccessListEIP2930Transaction, privateKeyToAccount, signTransaction as signTypedTransaction, } from "web3-eth-accounts";
const PHALA_SOCKET_PATH = "/var/run/dstack.sock";
const ACTION_KEY_PATH = "vita-fl/participant-ethereum-action/v1";
const ACTION_KEY_PURPOSE = "ethereum-secp256k1-action";
const ACTION_KEY_SESSION_DOMAIN = "MasterThesis.participant-ethereum-action.session.v1";
const normalizePrivateKey = (raw) => {
    if (!(raw instanceof Uint8Array) || raw.byteLength !== 32) {
        throw new Error("Participant action-key provider must return exactly 32 bytes.");
    }
    const bytes = Buffer.from(raw);
    try {
        const privateKey = `0x${bytes.toString("hex")}`;
        // This also rejects zero and values outside the secp256k1 scalar field.
        const account = privateKeyToAccount(privateKey);
        return {
            address: account.address.toLowerCase(),
            privateKey,
        };
    }
    finally {
        bytes.fill(0);
        raw.fill(0);
    }
};
const localTestProvider = (env) => async () => {
    if (env.LOCAL_TDX_MOCK !== "1") {
        throw new Error("A local participant action key is only allowed with LOCAL_TDX_MOCK=1.");
    }
    const configured = String(env.LOCAL_ACTION_PRIVATE_KEY || env.PRIVATE_KEY || "").trim();
    if (!/^0x[0-9a-fA-F]{64}$/.test(configured)) {
        throw new Error("LOCAL_ACTION_PRIVATE_KEY (or PRIVATE_KEY in local mock mode) must be a 32-byte key.");
    }
    return Uint8Array.from(Buffer.from(configured.slice(2), "hex"));
};
const dstackProvider = () => async () => {
    const client = new DstackClient(PHALA_SOCKET_PATH);
    const response = await client.getKey(ACTION_KEY_PATH, ACTION_KEY_PURPOSE);
    return response.key;
};
const deriveSessionPrivateKey = async (provider, sessionSalt) => {
    const root = await provider();
    if (!(root instanceof Uint8Array) || root.byteLength !== 32) {
        root?.fill?.(0);
        throw new Error("Participant action-key provider must return exactly 32 bytes.");
    }
    const rootBytes = Buffer.from(root);
    try {
        for (let counter = 0; counter < 256; counter += 1) {
            const candidate = crypto
                .createHmac("sha256", rootBytes)
                .update(ACTION_KEY_SESSION_DOMAIN, "utf8")
                .update(sessionSalt)
                .update(Buffer.from([counter]))
                .digest();
            try {
                if (secp256k1.utils.isValidPrivateKey(candidate)) {
                    return Uint8Array.from(candidate);
                }
            }
            finally {
                candidate.fill(0);
            }
        }
        throw new Error("Could not derive a valid secp256k1 participant action key.");
    }
    finally {
        rootBytes.fill(0);
        root.fill(0);
    }
};
const signatureHex = (digest, privateKey) => {
    if (!/^0x[0-9a-fA-F]{64}$/.test(digest)) {
        throw new Error("Action-key signing digest must be exactly 32 bytes.");
    }
    const keyBytes = Buffer.from(privateKey.slice(2), "hex");
    try {
        const signature = secp256k1.sign(Buffer.from(digest.slice(2), "hex"), keyBytes, { lowS: true });
        const recovery = signature.recovery;
        if (recovery !== 0 && recovery !== 1) {
            throw new Error("secp256k1 signing returned an invalid recovery id.");
        }
        return `0x${Buffer.from(signature.toCompactRawBytes()).toString("hex")}${(recovery + 27).toString(16).padStart(2, "0")}`;
    }
    finally {
        keyBytes.fill(0);
    }
};
const validateActionTransaction = (transaction, expectedAddress) => {
    if (String(transaction.from || "").toLowerCase()
        !== expectedAddress.toLowerCase()) {
        throw new Error("Participant action transaction has an unexpected sender.");
    }
    if (!/^0x[0-9a-fA-F]{40}$/.test(String(transaction.to || ""))) {
        throw new Error("Participant action transaction requires a contract address.");
    }
    if (!/^0x[0-9a-fA-F]{8,}$/.test(String(transaction.data || ""))) {
        throw new Error("Participant action transaction requires contract calldata.");
    }
    for (const field of ["chainId", "nonce", "gas", "gasPrice"]) {
        try {
            if (BigInt(transaction[field]) < 0n) {
                throw new Error();
            }
        }
        catch {
            throw new Error(`Participant action transaction has an invalid ${field}.`);
        }
    }
    if (BigInt(transaction.chainId) === 0n) {
        throw new Error("Participant action transaction requires a positive chainId.");
    }
    if (transaction.value !== undefined
        && BigInt(transaction.value) !== 0n) {
        throw new Error("Participant action key cannot authorize value transfers.");
    }
};
export const signRawDigest = (digest, privateKey) => signatureHex(digest, privateKey);
export const loadParticipantActionSigner = async (options = {}) => {
    const env = options.env || process.env;
    const phala = env.DOCKER === "phala";
    const provider = options.deriveKey || (phala ? dstackProvider() : localTestProvider(env));
    const source = phala ? "dstack-ephemeral" : "local-test-ephemeral";
    const sessionSalt = options.sessionSalt
        ? Uint8Array.from(options.sessionSalt)
        : Uint8Array.from(crypto.randomBytes(32));
    if (sessionSalt.byteLength !== 32) {
        sessionSalt.fill(0);
        throw new Error("Participant action-key session salt must be exactly 32 bytes.");
    }
    let destroyed = false;
    const derive = async () => {
        if (destroyed) {
            throw new Error("Participant action signer has been destroyed.");
        }
        return deriveSessionPrivateKey(provider, sessionSalt);
    };
    const first = normalizePrivateKey(await derive());
    const expectedAddress = first.address;
    const withPrivateKey = async (operation) => {
        const current = normalizePrivateKey(await derive());
        if (current.address !== expectedAddress) {
            throw new Error(`Participant action-key address changed from ${expectedAddress} to ${current.address}.`);
        }
        try {
            return await operation(current.privateKey);
        }
        finally {
            // Strings cannot be wiped in JavaScript. Keep their lifetime scoped to this callback.
            current.privateKey = "0x";
        }
    };
    // Drop the only startup-scoped hexadecimal representation immediately.
    first.privateKey = "0x";
    return Object.freeze({
        address: expectedAddress,
        source,
        signTransaction: async (transaction) => withPrivateKey(async (privateKey) => {
            validateActionTransaction(transaction, expectedAddress);
            const typedTransaction = AccessListEIP2930Transaction.fromTxData({
                chainId: BigInt(transaction.chainId),
                nonce: BigInt(transaction.nonce),
                gasPrice: BigInt(transaction.gasPrice),
                gasLimit: BigInt(transaction.gas),
                to: String(transaction.to),
                value: 0,
                data: String(transaction.data),
                accessList: [],
            });
            const signed = await signTypedTransaction(typedTransaction, privateKey);
            if (!signed.rawTransaction) {
                throw new Error("Participant action transaction signing returned no raw transaction.");
            }
            return signed.rawTransaction;
        }),
        signDigest: async (digest) => withPrivateKey(async (privateKey) => signatureHex(digest, privateKey)),
        destroy: () => {
            if (destroyed)
                return;
            sessionSalt.fill(0);
            destroyed = true;
        },
    });
};
export const PARTICIPANT_ACTION_KEY_CONTEXT = Object.freeze({
    path: ACTION_KEY_PATH,
    purpose: ACTION_KEY_PURPOSE,
    sessionDomain: ACTION_KEY_SESSION_DOMAIN,
});
