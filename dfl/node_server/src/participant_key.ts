import crypto, { type KeyObject } from "crypto";
import fs from "fs/promises";
import path from "path";
import { requireVolatileDirectory, RUNTIME_KEY_DIRECTORY } from "./runtime_keys.js";

const PHALA_STATE_PATH = "/var/lib/vita-fl/participant-rsa.v1.sealed.json";
export const PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH =
    "/run/vita-fl/participant-private.pem";
export const PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH =
    "/run/vita-fl/participant-public.pem";

const RSA_BITS = 3072;

export type ParticipantKey = Readonly<{
    privateKey: KeyObject;
    publicKey: KeyObject;
    publicKeyDer: Buffer;
    publicKeyDerHex: `0x${string}`;
    source: "ephemeral-random" | "local-file" | "kms-sealed" | "broker-sealed";
}>;

export type ParticipantKeyOptions = {
    env?: NodeJS.ProcessEnv;
};

const normalizeAddress = (value: string | undefined) => {
    const address = String(value || "").trim().toLowerCase();
    if (!/^0x[0-9a-f]{40}$/.test(address)) {
        throw new Error("ACCOUNT_ADDRESS must be a 20-byte Ethereum address before participant-key initialization.");
    }
    return address;
};

const keyDetails = (privateKey: KeyObject, minimumBits: number) => {
    if (privateKey.type !== "private" || privateKey.asymmetricKeyType !== "rsa") {
        throw new Error("Participant private key must be an RSA private key.");
    }
    const modulusLength = privateKey.asymmetricKeyDetails?.modulusLength;
    if (!Number.isInteger(modulusLength) || Number(modulusLength) < minimumBits) {
        throw new Error(`Participant RSA key must use a modulus of at least ${minimumBits} bits.`);
    }
};

const participantKey = (
    privateKey: KeyObject,
    source: ParticipantKey["source"],
    minimumBits: number,
): ParticipantKey => {
    keyDetails(privateKey, minimumBits);
    const publicKey = crypto.createPublicKey(privateKey);
    const publicKeyDer = Buffer.from(publicKey.export({ format: "der", type: "spki" }));
    return Object.freeze({
        privateKey,
        publicKey,
        publicKeyDer,
        publicKeyDerHex: `0x${publicKeyDer.toString("hex")}` as `0x${string}`,
        source,
    });
};

const atomicWrite = async (
    filePath: string,
    contents: string | Buffer,
    mode: number,
) => {
    const directory = path.dirname(filePath);
    await fs.mkdir(directory, { recursive: true, mode: 0o700 });
    await fs.chmod(directory, 0o700);
    const temporaryPath = path.join(
        directory,
        `.${path.basename(filePath)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
    );
    let handle;
    try {
        handle = await fs.open(temporaryPath, "wx", mode);
        await handle.writeFile(contents);
        await handle.sync();
        await handle.close();
        handle = undefined;
        await fs.rename(temporaryPath, filePath);
        await fs.chmod(filePath, mode);
    } finally {
        await handle?.close().catch(() => {});
        await fs.rm(temporaryPath, { force: true }).catch(() => {});
    }
};

const generateRsaPrivateKey = () => new Promise<KeyObject>((resolve, reject) => {
    crypto.generateKeyPair(
        "rsa",
        {
            modulusLength: RSA_BITS,
            publicExponent: 0x10001,
        },
        (error, _publicKey, privateKey) => {
            if (error) reject(error);
            else resolve(privateKey);
        },
    );
});

const loadLocalFileKey = async (env: NodeJS.ProcessEnv) => {
    const filePath = String(env.PARTICIPANT_RSA_PRIVATE_KEY_FILE || "").trim();
    if (!filePath) {
        throw new Error(
            "PARTICIPANT_KEY_PROVIDER=file requires PARTICIPANT_RSA_PRIVATE_KEY_FILE.",
        );
    }
    const privateKey = crypto.createPrivateKey(await fs.readFile(filePath));
    return participantKey(privateKey, "local-file", 2048);
};

export const loadParticipantKey = async (
    options: ParticipantKeyOptions = {},
): Promise<ParticipantKey> => {
    const env = options.env || process.env;
    const phala = env.DOCKER === "phala";
    const configuredProvider = String(env.PARTICIPANT_KEY_PROVIDER || "").trim();
    const provider = configuredProvider || (phala ? "ephemeral" : "");
    if (phala && !["ephemeral", "kms", "broker"].includes(provider)) {
        throw new Error("Phala participant keys require the ephemeral key provider.");
    }
    if (["kms", "broker"].includes(provider) && phala) {
        await requireVolatileDirectory();
        // Only the guarded supervisor initializes this private tmpfs file.
        return participantKey(crypto.createPrivateKey(await fs.readFile(PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH)), provider === "broker" ? "broker-sealed" : "kms-sealed", RSA_BITS);
    }
    if (provider === "ephemeral") {
        normalizeAddress(env.ACCOUNT_ADDRESS);
        // Neither GetKey nor an old sealed file participates in this generation.
        // Each loader invocation represents a new participant initialization.
        if (phala) {
            const legacy = String(env.PARTICIPANT_KEY_STATE_PATH || PHALA_STATE_PATH);
            try {
                await fs.access(legacy);
                throw new Error("Legacy persistent participant key exists; remove the old deployment state before admission.");
            } catch (error) {
                if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
            }
        }
        return participantKey(await generateRsaPrivateKey(), "ephemeral-random", RSA_BITS);
    }
    if (provider === "file" && !phala) {
        return loadLocalFileKey(env);
    }
    throw new Error(
        "Set PARTICIPANT_KEY_PROVIDER=file for local development; Phala selects ephemeral random keys automatically.",
    );
};

export const materializeParticipantPrivateKey = async (
    key: ParticipantKey,
    paths: {
        privateKeyPath?: string;
        publicKeyPath?: string;
    } = {},
) => {
    const privateKeyPath =
        paths.privateKeyPath ||
        process.env.PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH ||
        PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH;
    const publicKeyPath =
        paths.publicKeyPath ||
        process.env.PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH ||
        PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH;
    if (process.env.DOCKER === "phala") {
        for (const filename of [privateKeyPath, publicKeyPath]) {
            if (path.dirname(filename) !== RUNTIME_KEY_DIRECTORY) {
                throw new Error("Phala participant PEM files must be inside /run/vita-fl.");
            }
        }
        await requireVolatileDirectory();
    }
    const privatePem = key.privateKey.export({ format: "pem", type: "pkcs8" });
    const publicPem = key.publicKey.export({ format: "pem", type: "spki" });
    await atomicWrite(privateKeyPath, privatePem, 0o600);
    await atomicWrite(publicKeyPath, publicPem, 0o644);
    return Object.freeze({
        privateKeyPath,
        publicKeyPath,
    });
};
