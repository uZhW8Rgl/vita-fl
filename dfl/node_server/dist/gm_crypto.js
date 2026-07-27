import crypto from "crypto";
import fs from "fs/promises";
export const OUTPUT_BUNDLE_HASH_DOMAIN = "VITA-FL:global-model-output-bundle:v1";
const uint64FrameLength = (value) => {
    if (!Number.isSafeInteger(value) || value < 0) {
        throw new Error("Output-bundle frame length must be a safe unsigned integer.");
    }
    const encoded = Buffer.alloc(8);
    encoded.writeBigUInt64BE(BigInt(value));
    return encoded;
};
const requireBytes = (value, label) => {
    if (!(value instanceof Uint8Array)) {
        throw new Error(`${label} must be bytes.`);
    }
    return Buffer.from(value);
};
const framedHashField = (label, value) => {
    const labelBytes = Buffer.from(label, "utf8");
    const valueBytes = requireBytes(value, label);
    return Buffer.concat([
        uint64FrameLength(labelBytes.length),
        labelBytes,
        uint64FrameLength(valueBytes.length),
        valueBytes,
    ]);
};
export const deriveOutputBundleHash = ({ encryptedBundleBytes, encryptedSignatureBytes, keyBundleBytes, }) => {
    const digest = crypto.createHash("sha256");
    digest.update(framedHashField("domain", Buffer.from(OUTPUT_BUNDLE_HASH_DOMAIN, "utf8")));
    digest.update(framedHashField("bundle", encryptedBundleBytes));
    digest.update(framedHashField("signature", encryptedSignatureBytes));
    digest.update(framedHashField("key-bundle", keyBundleBytes));
    return `0x${digest.digest("hex")}`;
};
const normalizeHex = (value) => {
    const trimmed = String(value || "").trim();
    return trimmed.startsWith("0x") || trimmed.startsWith("0X") ? trimmed.slice(2) : trimmed;
};
const toBufferFromBase64 = (value, label) => {
    try {
        return Buffer.from(value, "base64");
    }
    catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        throw new Error(`Invalid base64 for ${label}: ${message}`);
    }
};
const loadPublicKeyFromDerHex = (publicKeyDerHex) => {
    const der = Buffer.from(normalizeHex(publicKeyDerHex), "hex");
    return crypto.createPublicKey({ key: der, format: "der", type: "spki" });
};
export const buildEncryptedGlobalModelArtifacts = async ({ modelPath, signaturePath, encryptedBundlePath, encryptedSignaturePath, keyBundlePath, recipients, round, signingPrivateKey, }) => {
    if (!Array.isArray(recipients)) {
        throw new Error("Recipients for GM encryption must be provided as an array.");
    }
    const modelBytes = await fs.readFile(modelPath);
    const signatureBytes = await fs.readFile(signaturePath);
    const payload = Buffer.from(JSON.stringify({
        version: 1,
        round,
        generated_at: new Date().toISOString(),
        model_b64: modelBytes.toString("base64"),
        signature_b64: signatureBytes.toString("base64"),
    }), "utf8");
    const aesKey = crypto.randomBytes(32);
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv("aes-256-gcm", aesKey, iv);
    const ciphertext = Buffer.concat([cipher.update(payload), cipher.final()]);
    const authTag = cipher.getAuthTag();
    const wrappedKeys = {};
    for (const recipient of recipients) {
        const normalizedAddress = String(recipient.address || "").toLowerCase();
        if (!/^0x[0-9a-f]{40}$/.test(normalizedAddress)) {
            throw new Error(`Invalid recipient address for GM encryption: ${recipient.address}`);
        }
        const wrapped = crypto.publicEncrypt({
            key: loadPublicKeyFromDerHex(recipient.publicKeyDerHex),
            padding: crypto.constants.RSA_PKCS1_OAEP_PADDING,
            oaepHash: "sha256",
        }, Buffer.concat([aesKey, iv]));
        wrappedKeys[normalizedAddress] = wrapped.toString("base64");
    }
    const bundleDocument = {
        version: 1,
        type: "global-model-bundle",
        cipher: "aes-256-gcm",
        iv_b64: iv.toString("base64"),
        auth_tag_b64: authTag.toString("base64"),
        ciphertext_b64: ciphertext.toString("base64"),
    };
    await fs.writeFile(encryptedBundlePath, JSON.stringify(bundleDocument));
    const bundleBytes = await fs.readFile(encryptedBundlePath);
    const signature = crypto.sign("RSA-SHA256", bundleBytes, {
        key: signingPrivateKey,
        padding: crypto.constants.RSA_PKCS1_PADDING,
    });
    await fs.writeFile(encryptedSignaturePath, signature);
    const keyBundleDocument = {
        version: 1,
        type: "global-model-key-bundle",
        round,
        wrapped_keys_b64: wrappedKeys,
    };
    const keyBundleBytes = Buffer.from(JSON.stringify(keyBundleDocument), "utf8");
    await fs.writeFile(keyBundlePath, keyBundleBytes);
    return {
        recipients: Object.keys(wrappedKeys).length,
        encryptedBundlePath,
        encryptedSignaturePath,
        keyBundlePath,
        outputBundleHash: deriveOutputBundleHash({
            encryptedBundleBytes: bundleBytes,
            encryptedSignatureBytes: signature,
            keyBundleBytes,
        }),
    };
};
export const decryptEncryptedGlobalModelArtifacts = async ({ encryptedBundlePath, keyBundlePath, ownAddress, outModelPath, outSignaturePath, decryptionPrivateKey, }) => {
    const bundleDocument = JSON.parse(await fs.readFile(encryptedBundlePath, "utf8"));
    const keyBundleDocument = JSON.parse(await fs.readFile(keyBundlePath, "utf8"));
    const normalizedAddress = String(ownAddress || "").toLowerCase();
    const wrapped = keyBundleDocument?.wrapped_keys_b64?.[normalizedAddress];
    if (!wrapped) {
        throw new Error(`No wrapped GM round key found for participant ${ownAddress}.`);
    }
    const keyIv = crypto.privateDecrypt({
        key: decryptionPrivateKey,
        padding: crypto.constants.RSA_PKCS1_OAEP_PADDING,
        oaepHash: "sha256",
    }, toBufferFromBase64(wrapped, "wrapped GM round key"));
    if (keyIv.length !== 44) {
        throw new Error(`Invalid wrapped GM round key length: expected 44 bytes, got ${keyIv.length}.`);
    }
    const aesKey = keyIv.subarray(0, 32);
    const iv = keyIv.subarray(32);
    const decipher = crypto.createDecipheriv("aes-256-gcm", aesKey, iv);
    decipher.setAuthTag(toBufferFromBase64(bundleDocument.auth_tag_b64, "GM auth tag"));
    const plaintext = Buffer.concat([
        decipher.update(toBufferFromBase64(bundleDocument.ciphertext_b64, "GM ciphertext")),
        decipher.final(),
    ]);
    const payload = JSON.parse(plaintext.toString("utf8"));
    const modelBytes = toBufferFromBase64(payload.model_b64, "plaintext model");
    const signatureBytes = toBufferFromBase64(payload.signature_b64, "plaintext signature");
    await fs.writeFile(outModelPath, modelBytes);
    await fs.writeFile(outSignaturePath, signatureBytes);
    return {
        outModelPath,
        outSignaturePath,
        round: Number(keyBundleDocument?.round ?? 0),
    };
};
