import crypto from "crypto";
import fs from "fs/promises";
import path from "path";
import { DstackClient } from "@phala/dstack-sdk";
const PHALA_SOCKET_PATH = "/var/run/dstack.sock";
const PHALA_STATE_PATH = "/var/lib/vita-fl/participant-rsa.v1.sealed.json";
export const PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH = "/run/vita-fl/participant-private.pem";
export const PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH = "/run/vita-fl/participant-public.pem";
const KEY_PATH = "vita-fl/participant-rsa-wrap/v1";
const KEY_PURPOSE = "aes-256-gcm-sealing";
const STATE_DOMAIN = "VITAFL_PARTICIPANT_RSA_KEYSTORE_V1";
const RSA_BITS = 3072;
const normalizeAddress = (value) => {
    const address = String(value || "").trim().toLowerCase();
    if (!/^0x[0-9a-f]{40}$/.test(address)) {
        throw new Error("ACCOUNT_ADDRESS must be a 20-byte Ethereum address before participant-key initialization.");
    }
    return address;
};
const keyDetails = (privateKey, minimumBits) => {
    if (privateKey.type !== "private" || privateKey.asymmetricKeyType !== "rsa") {
        throw new Error("Participant private key must be an RSA private key.");
    }
    const modulusLength = privateKey.asymmetricKeyDetails?.modulusLength;
    if (!Number.isInteger(modulusLength) || Number(modulusLength) < minimumBits) {
        throw new Error(`Participant RSA key must use a modulus of at least ${minimumBits} bits.`);
    }
};
const participantKey = (privateKey, source, minimumBits) => {
    keyDetails(privateKey, minimumBits);
    const publicKey = crypto.createPublicKey(privateKey);
    const publicKeyDer = Buffer.from(publicKey.export({ format: "der", type: "spki" }));
    return Object.freeze({
        privateKey,
        publicKey,
        publicKeyDer,
        publicKeyDerHex: `0x${publicKeyDer.toString("hex")}`,
        source,
    });
};
const aadForAddress = (address) => Buffer.from(`${STATE_DOMAIN}\0${address}`, "utf8");
const deriveWrappingKey = (rawKey) => {
    if (rawKey.byteLength < 32) {
        throw new Error("dstack GetKey returned fewer than 32 bytes.");
    }
    const input = Buffer.from(rawKey);
    try {
        const salt = crypto.createHash("sha256")
            .update(`${STATE_DOMAIN}:salt`, "utf8")
            .digest();
        return Buffer.from(crypto.hkdfSync("sha256", input, salt, Buffer.from(`${STATE_DOMAIN}:wrap`, "utf8"), 32));
    }
    finally {
        input.fill(0);
        if (rawKey instanceof Uint8Array)
            rawKey.fill(0);
    }
};
const canonicalBase64 = (value, label) => {
    if (typeof value !== "string" || value.length === 0) {
        throw new Error(`Sealed participant key has an invalid ${label}.`);
    }
    const decoded = Buffer.from(value, "base64");
    if (decoded.length === 0 || decoded.toString("base64") !== value) {
        throw new Error(`Sealed participant key has non-canonical base64 in ${label}.`);
    }
    return decoded;
};
const parseSealedDocument = (raw) => {
    let value;
    try {
        value = JSON.parse(raw);
    }
    catch {
        throw new Error("Sealed participant key is not valid JSON.");
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("Sealed participant key must be a JSON object.");
    }
    const document = value;
    if (document.version !== 1 ||
        document.rsa_bits !== RSA_BITS ||
        document.cipher !== "aes-256-gcm" ||
        typeof document.public_key_sha256 !== "string" ||
        !/^[0-9a-f]{64}$/.test(document.public_key_sha256)) {
        throw new Error("Sealed participant key has unsupported metadata.");
    }
    return document;
};
const atomicWrite = async (filePath, contents, mode) => {
    const directory = path.dirname(filePath);
    await fs.mkdir(directory, { recursive: true, mode: 0o700 });
    await fs.chmod(directory, 0o700);
    const temporaryPath = path.join(directory, `.${path.basename(filePath)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`);
    let handle;
    try {
        handle = await fs.open(temporaryPath, "wx", mode);
        await handle.writeFile(contents);
        await handle.sync();
        await handle.close();
        handle = undefined;
        await fs.rename(temporaryPath, filePath);
        await fs.chmod(filePath, mode);
    }
    finally {
        await handle?.close().catch(() => { });
        await fs.rm(temporaryPath, { force: true }).catch(() => { });
    }
};
const generateRsaPrivateKey = () => new Promise((resolve, reject) => {
    crypto.generateKeyPair("rsa", {
        modulusLength: RSA_BITS,
        publicExponent: 0x10001,
    }, (error, _publicKey, privateKey) => {
        if (error)
            reject(error);
        else
            resolve(privateKey);
    });
});
const sealPrivateKey = (privateKey, wrappingKey, address) => {
    const plaintext = Buffer.from(privateKey.export({ format: "der", type: "pkcs8" }));
    const iv = crypto.randomBytes(12);
    try {
        const cipher = crypto.createCipheriv("aes-256-gcm", wrappingKey, iv);
        cipher.setAAD(aadForAddress(address));
        const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
        const authTag = cipher.getAuthTag();
        const publicKeyDer = Buffer.from(crypto.createPublicKey(privateKey).export({ format: "der", type: "spki" }));
        return {
            version: 1,
            rsa_bits: RSA_BITS,
            cipher: "aes-256-gcm",
            iv_b64: iv.toString("base64"),
            auth_tag_b64: authTag.toString("base64"),
            ciphertext_b64: ciphertext.toString("base64"),
            public_key_sha256: crypto.createHash("sha256").update(publicKeyDer).digest("hex"),
        };
    }
    finally {
        plaintext.fill(0);
    }
};
const unsealPrivateKey = (document, wrappingKey, address) => {
    const iv = canonicalBase64(document.iv_b64, "iv_b64");
    const authTag = canonicalBase64(document.auth_tag_b64, "auth_tag_b64");
    const ciphertext = canonicalBase64(document.ciphertext_b64, "ciphertext_b64");
    if (iv.length !== 12 || authTag.length !== 16) {
        throw new Error("Sealed participant key has invalid AES-GCM parameters.");
    }
    let plaintext;
    try {
        const decipher = crypto.createDecipheriv("aes-256-gcm", wrappingKey, iv);
        decipher.setAAD(aadForAddress(address));
        decipher.setAuthTag(authTag);
        plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
        const privateKey = crypto.createPrivateKey({
            key: plaintext,
            format: "der",
            type: "pkcs8",
        });
        const key = participantKey(privateKey, "dstack-sealed", RSA_BITS);
        const publicKeyHash = crypto.createHash("sha256").update(key.publicKeyDer).digest("hex");
        if (!crypto.timingSafeEqual(Buffer.from(publicKeyHash, "hex"), Buffer.from(document.public_key_sha256, "hex"))) {
            throw new Error("Sealed participant key public-key fingerprint does not match.");
        }
        return key;
    }
    catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        throw new Error(`Could not authenticate or load the sealed participant key: ${message}`);
    }
    finally {
        plaintext?.fill(0);
    }
};
const defaultDstackDerivation = async () => {
    const client = new DstackClient(PHALA_SOCKET_PATH);
    const response = await client.getKey(KEY_PATH, KEY_PURPOSE);
    return response.key;
};
const loadDstackSealedKey = async ({ env, statePath, deriveKey, }) => {
    const address = normalizeAddress(env.ACCOUNT_ADDRESS);
    let derived;
    try {
        derived = await deriveKey();
    }
    catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        throw new Error(`dstack participant-key derivation failed: ${message}`);
    }
    const wrappingKey = deriveWrappingKey(derived);
    try {
        let sealed;
        try {
            sealed = await fs.readFile(statePath, "utf8");
        }
        catch (error) {
            const code = error?.code;
            if (code !== "ENOENT")
                throw error;
        }
        if (sealed !== undefined) {
            return unsealPrivateKey(parseSealedDocument(sealed), wrappingKey, address);
        }
        const privateKey = await generateRsaPrivateKey();
        const key = participantKey(privateKey, "dstack-sealed", RSA_BITS);
        const document = sealPrivateKey(privateKey, wrappingKey, address);
        await atomicWrite(statePath, `${JSON.stringify(document)}\n`, 0o600);
        return key;
    }
    finally {
        wrappingKey.fill(0);
    }
};
const loadLocalFileKey = async (env) => {
    const filePath = String(env.PARTICIPANT_RSA_PRIVATE_KEY_FILE || "").trim();
    if (!filePath) {
        throw new Error("PARTICIPANT_KEY_PROVIDER=file requires PARTICIPANT_RSA_PRIVATE_KEY_FILE.");
    }
    const privateKey = crypto.createPrivateKey(await fs.readFile(filePath));
    return participantKey(privateKey, "local-file", 2048);
};
export const loadParticipantKey = async (options = {}) => {
    const env = options.env || process.env;
    const phala = env.DOCKER === "phala";
    const configuredProvider = String(env.PARTICIPANT_KEY_PROVIDER || "").trim();
    const provider = configuredProvider || (phala ? "dstack" : "");
    if (phala && provider !== "dstack") {
        throw new Error("Phala participant keys must use the dstack key provider.");
    }
    if (provider === "dstack") {
        return loadDstackSealedKey({
            env,
            statePath: options.statePath ||
                String(env.PARTICIPANT_KEY_STATE_PATH || PHALA_STATE_PATH),
            deriveKey: options.deriveKey || defaultDstackDerivation,
        });
    }
    if (provider === "file" && !phala) {
        return loadLocalFileKey(env);
    }
    throw new Error("Set PARTICIPANT_KEY_PROVIDER=file for local development; Phala selects dstack automatically.");
};
export const materializeParticipantPrivateKey = async (key, paths = {}) => {
    const privateKeyPath = paths.privateKeyPath ||
        process.env.PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH ||
        PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH;
    const publicKeyPath = paths.publicKeyPath ||
        process.env.PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH ||
        PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH;
    const privatePem = key.privateKey.export({ format: "pem", type: "pkcs8" });
    const publicPem = key.publicKey.export({ format: "pem", type: "spki" });
    await atomicWrite(privateKeyPath, privatePem, 0o600);
    await atomicWrite(publicKeyPath, publicPem, 0o644);
    return Object.freeze({
        privateKeyPath,
        publicKeyPath,
    });
};
