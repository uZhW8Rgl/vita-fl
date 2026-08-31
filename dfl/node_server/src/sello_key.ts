import { DstackClient } from "@phala/dstack-sdk";
import crypto from "crypto";

const PHALA_SOCKET_PATH = "/var/run/dstack.sock";
export const SELLO_TEE_KEY_PATH = "vita-fl/sello/tee-inference/v1";
export const SELLO_TEE_KEY_PURPOSE = "ed25519-cose-sign1";
export const SELLO_TEE_KEY_DERIVATION_DOMAIN =
    "MasterThesis.sello.tee-inference.app-bound.v1";

const ED25519_PKCS8_SEED_PREFIX = Buffer.from(
    "302e020100300506032b657004220420",
    "hex",
);
const ED25519_SPKI_PREFIX = Buffer.from(
    "302a300506032b6570032100",
    "hex",
);

type KeyProvider = () => Promise<Uint8Array>;

export type SelloReceiptKeyOptions = {
    env?: NodeJS.ProcessEnv;
    deriveKey?: KeyProvider;
};

const decodeLocalRoot = (value: string) => {
    const text = value.trim();
    let decoded: Buffer;
    if (/^(?:0x)?[0-9a-fA-F]{64}$/.test(text)) {
        decoded = Buffer.from(text.replace(/^0x/i, ""), "hex");
    } else {
        decoded = Buffer.from(text, "base64url");
    }
    if (decoded.length !== 32) {
        decoded.fill(0);
        throw new Error(
            "SELLO_SERVICE_SIGNING_SEED must contain exactly 32 bytes in local mock mode.",
        );
    }
    return Uint8Array.from(decoded);
};

const localProvider = (env: NodeJS.ProcessEnv): KeyProvider => async () => {
    if (env.LOCAL_TDX_MOCK !== "1") {
        throw new Error(
            "An environment-provided Sello key is only allowed with LOCAL_TDX_MOCK=1.",
        );
    }
    return decodeLocalRoot(String(env.SELLO_SERVICE_SIGNING_SEED || ""));
};

const dstackProvider = (): KeyProvider => async () => {
    const client = new DstackClient(PHALA_SOCKET_PATH);
    const response = await client.getKey(
        SELLO_TEE_KEY_PATH,
        SELLO_TEE_KEY_PURPOSE,
    );
    return response.key;
};

const derivationContext = (env: NodeJS.ProcessEnv) => {
    const participant = String(env.ACCOUNT_ADDRESS || "").trim().toLowerCase();
    const registry = String(
        env.REGISTRY_ADDRESS || env.EXPECTED_DEVICE_REGISTRY_ADDRESS || "",
    ).trim().toLowerCase();
    if (!/^0x[0-9a-f]{40}$/.test(participant)) {
        throw new Error("ACCOUNT_ADDRESS is required for Sello key derivation.");
    }
    if (!/^0x[0-9a-f]{40}$/.test(registry)) {
        throw new Error("REGISTRY_ADDRESS is required for Sello key derivation.");
    }

    let chainId: bigint;
    try {
        chainId = BigInt(String(env.EXPECTED_CHAIN_ID || ""));
    } catch {
        throw new Error("EXPECTED_CHAIN_ID is required for Sello key derivation.");
    }
    if (chainId <= 0n || chainId >= (1n << 256n)) {
        throw new Error("EXPECTED_CHAIN_ID is outside the Sello key derivation domain.");
    }

    return Buffer.concat([
        Buffer.from(SELLO_TEE_KEY_DERIVATION_DOMAIN, "utf8"),
        Buffer.from(participant.slice(2), "hex"),
        Buffer.from(chainId.toString(16).padStart(64, "0"), "hex"),
        Buffer.from(registry.slice(2), "hex"),
        Buffer.from("tee-inference", "utf8"),
    ]);
};

export const deriveSelloSigningSeed = (
    root: Uint8Array,
    env: NodeJS.ProcessEnv,
) => {
    if (!(root instanceof Uint8Array) || root.byteLength !== 32) {
        root?.fill?.(0);
        throw new Error("Sello key provider must return exactly 32 bytes.");
    }
    const rootBytes = Buffer.from(root);
    try {
        return Uint8Array.from(
            crypto
                .createHmac("sha256", rootBytes)
                .update(derivationContext(env))
                .digest(),
        );
    } finally {
        rootBytes.fill(0);
        root.fill(0);
    }
};

export const ed25519PublicKeyFromSeed = (seed: Uint8Array) => {
    if (!(seed instanceof Uint8Array) || seed.byteLength !== 32) {
        throw new Error("Sello signing seed must contain exactly 32 bytes.");
    }
    const privateDer = Buffer.concat([
        ED25519_PKCS8_SEED_PREFIX,
        Buffer.from(seed),
    ]);
    try {
        const privateKey = crypto.createPrivateKey({
            key: privateDer,
            format: "der",
            type: "pkcs8",
        });
        const publicDer = crypto.createPublicKey(privateKey).export({
            format: "der",
            type: "spki",
        }) as Buffer;
        if (
            publicDer.length !== ED25519_SPKI_PREFIX.length + 32
            || !publicDer.subarray(0, ED25519_SPKI_PREFIX.length).equals(ED25519_SPKI_PREFIX)
        ) {
            throw new Error("OpenSSL returned an unexpected Ed25519 public-key encoding.");
        }
        return Buffer.from(publicDer.subarray(ED25519_SPKI_PREFIX.length));
    } finally {
        privateDer.fill(0);
    }
};

export const loadSelloReceiptPublicKey = async (
    options: SelloReceiptKeyOptions = {},
) => {
    const env = options.env || process.env;
    const phala = env.DOCKER === "phala";
    const providerName = String(
        env.SELLO_SERVICE_KEY_PROVIDER || (phala ? "dstack" : "env"),
    ).trim().toLowerCase();
    if (phala && providerName !== "dstack") {
        throw new Error("Phala Sello receivers require SELLO_SERVICE_KEY_PROVIDER=dstack.");
    }
    if (!phala && providerName !== "env") {
        throw new Error("Local Sello receivers only support the env test provider.");
    }

    const provider = options.deriveKey
        || (phala ? dstackProvider() : localProvider(env));
    const seed = deriveSelloSigningSeed(await provider(), env);
    try {
        return ed25519PublicKeyFromSeed(seed);
    } finally {
        seed.fill(0);
    }
};
