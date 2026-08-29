import crypto from "crypto";
import fs from "fs/promises";

type RecipientKey = {
    address: string;
    publicKeyDerHex: string;
};

export const OUTPUT_BUNDLE_HASH_DOMAIN =
    "VITA-FL:global-model-output-bundle:v2";

const uint64FrameLength = (value: number) => {
    if (!Number.isSafeInteger(value) || value < 0) {
        throw new Error("Output-bundle frame length must be a safe unsigned integer.");
    }
    const encoded = Buffer.alloc(8);
    encoded.writeBigUInt64BE(BigInt(value));
    return encoded;
};

const requireBytes = (value: Uint8Array, label: string) => {
    if (!(value instanceof Uint8Array)) {
        throw new Error(`${label} must be bytes.`);
    }
    return Buffer.from(value);
};

const framedHashField = (label: string, value: Uint8Array) => {
    const labelBytes = Buffer.from(label, "utf8");
    const valueBytes = requireBytes(value, label);
    return Buffer.concat([
        uint64FrameLength(labelBytes.length),
        labelBytes,
        uint64FrameLength(valueBytes.length),
        valueBytes,
    ]);
};

export const deriveOutputBundleHash = ({
    encryptedBundleBytes,
    encryptedSignatureBytes,
    keyBundleBytes,
}: {
    encryptedBundleBytes: Uint8Array;
    encryptedSignatureBytes: Uint8Array;
    keyBundleBytes: Uint8Array;
}): `0x${string}` => {
    const digest = crypto.createHash("sha256");
    digest.update(framedHashField("domain", Buffer.from(OUTPUT_BUNDLE_HASH_DOMAIN, "utf8")));
    digest.update(framedHashField("bundle", encryptedBundleBytes));
    digest.update(framedHashField("signature", encryptedSignatureBytes));
    digest.update(framedHashField("key-bundle", keyBundleBytes));
    return `0x${digest.digest("hex")}`;
};

const normalizeHex = (value: string) => {
    const trimmed = String(value || "").trim();
    return trimmed.startsWith("0x") || trimmed.startsWith("0X") ? trimmed.slice(2) : trimmed;
};

const toBufferFromBase64 = (value: string, label: string) => {
    try {
        return Buffer.from(value, "base64");
    } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        throw new Error(`Invalid base64 for ${label}: ${message}`);
    }
};

const loadPublicKeyFromDerHex = (publicKeyDerHex: string) => {
    const der = Buffer.from(normalizeHex(publicKeyDerHex), "hex");
    return crypto.createPublicKey({ key: der, format: "der", type: "spki" });
};

const requirePositiveModelRound = (value: unknown, label: string) => {
    const round = Number(value);
    if (!Number.isSafeInteger(round) || round <= 0) {
        throw new Error(`${label} is invalid: ${value}.`);
    }
    return round;
};

export const verifyEncryptedGlobalModelBundleSignature = ({
    encryptedBundleBytes,
    encryptedSignatureBytes,
    publisherPublicKeyDerHex,
}: {
    encryptedBundleBytes: Uint8Array;
    encryptedSignatureBytes: Uint8Array;
    publisherPublicKeyDerHex: string;
}) => crypto.verify(
    "RSA-SHA256",
    requireBytes(encryptedBundleBytes, "encrypted global-model bundle"),
    {
        key: loadPublicKeyFromDerHex(publisherPublicKeyDerHex),
        padding: crypto.constants.RSA_PKCS1_PADDING,
    },
    requireBytes(encryptedSignatureBytes, "encrypted global-model bundle signature"),
);

export const buildEncryptedGlobalModelArtifacts = async ({
    modelPath,
    signaturePath,
    aggregationEvidencePath,
    encryptedBundlePath,
    encryptedSignaturePath,
    keyBundlePath,
    recipients,
    round,
    signingPrivateKey,
}: {
    modelPath: string;
    signaturePath: string;
    aggregationEvidencePath?: string;
    encryptedBundlePath: string;
    encryptedSignaturePath: string;
    keyBundlePath: string;
    recipients: RecipientKey[];
    round: number;
    signingPrivateKey: crypto.KeyObject;
}) => {
    if (!Array.isArray(recipients)) {
        throw new Error("Recipients for GM encryption must be provided as an array.");
    }

    const modelBytes = await fs.readFile(modelPath);
    const signatureBytes = await fs.readFile(signaturePath);
    let aggregationEvidenceBytes: Buffer | null = null;
    let aggregationEvidenceHash: string | null = null;
    if (aggregationEvidencePath) {
        aggregationEvidenceBytes = await fs.readFile(aggregationEvidencePath);
        if (aggregationEvidenceBytes.length === 0) {
            throw new Error("Hybrid-R aggregation evidence must not be empty.");
        }
        try {
            const parsed = JSON.parse(aggregationEvidenceBytes.toString("utf8"));
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
                throw new Error("the JSON root must be an object");
            }
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            throw new Error(`Invalid Hybrid-R aggregation evidence: ${message}`);
        }
        aggregationEvidenceHash = crypto
            .createHash("sha256")
            .update(aggregationEvidenceBytes)
            .digest("hex");
    }
    if (recipients.length === 0) {
        throw new Error(
            "GM encryption requires at least one recipient with a registered key.",
        );
    }
    if (!Number.isSafeInteger(round) || round <= 0) {
        throw new Error(`Encrypted global-model round is invalid: ${round}.`);
    }
    if (signatureBytes.length === 0 && round !== 1) {
        throw new Error(
            "Only the public bootstrap promoted to model round 1 may omit the plaintext model signature.",
        );
    }
    const payload = Buffer.from(JSON.stringify({
        version: 2,
        round,
        generated_at: new Date().toISOString(),
        model_b64: modelBytes.toString("base64"),
        // Only the public bootstrap model promoted to round 1 has no origin signature.
        // Learned global models continue to carry the aggregator's plaintext
        // signature, while every encrypted bundle (including bootstrap) is
        // authenticated separately below.
        signature_b64:
            signatureBytes.length > 0 ? signatureBytes.toString("base64") : null,
        aggregation_evidence_b64:
            aggregationEvidenceBytes?.toString("base64") ?? null,
        aggregation_evidence_sha256: aggregationEvidenceHash,
    }), "utf8");

    const aesKey = crypto.randomBytes(32);
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv("aes-256-gcm", aesKey, iv);
    const ciphertext = Buffer.concat([cipher.update(payload), cipher.final()]);
    const authTag = cipher.getAuthTag();

    const wrappedKeys: Record<string, string> = {};
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
        version: 2,
        type: "global-model-key-bundle",
        round,
        wrapped_keys_b64: wrappedKeys,
        aggregation_evidence_b64:
            aggregationEvidenceBytes?.toString("base64") ?? null,
        aggregation_evidence_sha256: aggregationEvidenceHash,
    };
    const keyBundleBytes = Buffer.from(JSON.stringify(keyBundleDocument), "utf8");
    await fs.writeFile(keyBundlePath, keyBundleBytes);

    return {
        recipients: Object.keys(wrappedKeys).length,
        encryptedBundlePath,
        encryptedSignaturePath,
        keyBundlePath,
        aggregationEvidenceHash: aggregationEvidenceHash
            ? `0x${aggregationEvidenceHash}`
            : null,
        outputBundleHash: deriveOutputBundleHash({
            encryptedBundleBytes: bundleBytes,
            encryptedSignatureBytes: signature,
            keyBundleBytes,
        }),
    };
};

export const decryptEncryptedGlobalModelArtifacts = async ({
    encryptedBundlePath,
    keyBundlePath,
    ownAddress,
    outModelPath,
    outSignaturePath,
    outAggregationEvidencePath,
    decryptionPrivateKey,
    expectedModelRound,
}: {
    encryptedBundlePath: string;
    keyBundlePath: string;
    ownAddress: string;
    outModelPath: string;
    outSignaturePath: string;
    outAggregationEvidencePath?: string;
    decryptionPrivateKey: crypto.KeyObject;
    expectedModelRound: number;
}) => {
    const bundleDocument = JSON.parse(await fs.readFile(encryptedBundlePath, "utf8"));
    const keyBundleDocument = JSON.parse(await fs.readFile(keyBundlePath, "utf8"));
    const finalizedModelRound = requirePositiveModelRound(
        expectedModelRound,
        "Finalized on-chain model round",
    );
    const keyBundleRound = requirePositiveModelRound(
        keyBundleDocument?.round,
        "Encrypted global-model key-bundle round",
    );
    if (keyBundleRound !== finalizedModelRound) {
        throw new Error(
            `Encrypted global-model key-bundle round ${keyBundleRound} does not match `
            + `finalized on-chain model round ${finalizedModelRound}.`,
        );
    }
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
    const decipher = crypto.createDecipheriv(
        "aes-256-gcm",
        aesKey,
        iv,
    );
    decipher.setAuthTag(toBufferFromBase64(bundleDocument.auth_tag_b64, "GM auth tag"));
    const plaintext = Buffer.concat([
        decipher.update(toBufferFromBase64(bundleDocument.ciphertext_b64, "GM ciphertext")),
        decipher.final(),
    ]);
    const payload = JSON.parse(plaintext.toString("utf8"));
    const payloadRound = requirePositiveModelRound(
        payload?.round,
        "Encrypted global-model payload round",
    );
    if (payloadRound !== keyBundleRound) {
        throw new Error(
            `Encrypted global-model payload round ${payloadRound} does not match `
            + `key-bundle round ${keyBundleRound}.`,
        );
    }
    const modelBytes = toBufferFromBase64(payload.model_b64, "plaintext model");
    let signatureBytes: Buffer;
    let plaintextSignaturePresent: boolean;
    if (payload.signature_b64 === null || payload.signature_b64 === undefined) {
        signatureBytes = Buffer.alloc(0);
        plaintextSignaturePresent = false;
    } else if (typeof payload.signature_b64 === "string") {
        signatureBytes = toBufferFromBase64(
            payload.signature_b64,
            "plaintext signature",
        );
        plaintextSignaturePresent = signatureBytes.length > 0;
    } else {
        throw new Error("Encrypted GM plaintext signature field is invalid.");
    }
    if (!plaintextSignaturePresent && payloadRound !== 1) {
        throw new Error(
            `Encrypted global model round ${payloadRound} lacks its required `
            + "plaintext model signature.",
        );
    }
    await fs.writeFile(outModelPath, modelBytes);
    await fs.writeFile(outSignaturePath, signatureBytes);

    let aggregationEvidence: Record<string, unknown> | null = null;
    let aggregationEvidenceHash: `0x${string}` | null = null;
    if (outAggregationEvidencePath) {
        await fs.rm(outAggregationEvidencePath, { force: true });
    }
    if (payload.aggregation_evidence_b64 !== null && payload.aggregation_evidence_b64 !== undefined) {
        const evidenceBytes = toBufferFromBase64(
            payload.aggregation_evidence_b64,
            "Hybrid-R aggregation evidence",
        );
        const actualHash = crypto.createHash("sha256").update(evidenceBytes).digest("hex");
        if (
            typeof payload.aggregation_evidence_sha256 !== "string"
            || payload.aggregation_evidence_sha256.toLowerCase() !== actualHash
        ) {
            throw new Error("Hybrid-R aggregation evidence hash mismatch.");
        }
        if (
            keyBundleDocument.aggregation_evidence_b64
                !== payload.aggregation_evidence_b64
            || String(
                keyBundleDocument.aggregation_evidence_sha256 || "",
            ).toLowerCase() !== actualHash
        ) {
            throw new Error(
                "Public key-bundle Hybrid-R evidence does not match the encrypted payload.",
            );
        }
        const parsed = JSON.parse(evidenceBytes.toString("utf8"));
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error("Hybrid-R aggregation evidence JSON root must be an object.");
        }
        aggregationEvidence = parsed as Record<string, unknown>;
        aggregationEvidenceHash = `0x${actualHash}`;
        if (outAggregationEvidencePath) {
            await fs.writeFile(outAggregationEvidencePath, evidenceBytes);
        }
    } else if (
        keyBundleDocument.aggregation_evidence_b64 !== null
        && keyBundleDocument.aggregation_evidence_b64 !== undefined
    ) {
        throw new Error(
            "Public key-bundle Hybrid-R evidence is missing from the encrypted payload.",
        );
    }

    return {
        outModelPath,
        outSignaturePath,
        outAggregationEvidencePath:
            aggregationEvidence && outAggregationEvidencePath
                ? outAggregationEvidencePath
                : null,
        aggregationEvidence,
        aggregationEvidenceHash,
        plaintextSignaturePresent,
        round: payloadRound,
    };
};
