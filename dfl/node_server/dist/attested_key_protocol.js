/** Shared, versioned key-release protocol. No dstack-derived secret enters it. */
import crypto from "crypto";
export const CLAIM_FIELDS = ["version", "challenge", "participant", "appId", "composeHash", "registry",
    "chainId", "policy", "serviceId", "recipientPublicKey"];
export const canonicalClaims = (claims) => {
    if (!claims || Object.keys(claims).sort().join() !== [...CLAIM_FIELDS].sort().join() ||
        claims.version !== 1 || claims.chainId !== "31337")
        throw new Error("Invalid Anvil release claims");
    for (const field of ["participant", "appId", "registry", "policy"]) {
        if (!/^0x[0-9a-f]{40}$/.test(claims[field]) || /^0x0{40}$/.test(claims[field]))
            throw new Error("Invalid release address");
    }
    for (const field of ["challenge", "composeHash", "serviceId", "recipientPublicKey"]) {
        if (!/^[0-9a-f]{64}$/.test(claims[field]))
            throw new Error("Invalid release digest");
    }
    return Buffer.from(JSON.stringify(Object.fromEntries(Object.entries(claims).sort(([a], [b]) => a.localeCompare(b)))));
};
export const releaseReportData = (claims) => crypto.createHash("sha512")
    .update(Buffer.from("VITA-FL/key-service/attestation/v1\0")).update(canonicalClaims(claims)).digest();
const X25519_SPKI = Buffer.from("302a300506032b656e032100", "hex");
export const importRecipient = (raw) => {
    if (!/^[0-9a-f]{64}$/.test(raw))
        throw new Error("Invalid X25519 recipient");
    return crypto.createPublicKey({ key: Buffer.concat([X25519_SPKI, Buffer.from(raw, "hex")]), type: "spki", format: "der" });
};
export const rawPublicKey = (key) => key.export({ format: "der", type: "spki" }).subarray(-32).toString("hex");
export const deriveApplicationKey = (root, claims) => {
    canonicalClaims(claims);
    if (root.length !== 32)
        throw new Error("A 32-byte root is required");
    // Challenge and ephemeral recipient deliberately excluded: restarts restore one identity.
    const context = Buffer.from(JSON.stringify(["VITA-FL/application-key/v1", claims.chainId, claims.policy,
        claims.serviceId, claims.registry, claims.participant, claims.appId, claims.composeHash]));
    return Buffer.from(crypto.hkdfSync("sha256", root, Buffer.alloc(0), context, 32));
};
const transportKey = (secret, claims) => Buffer.from(crypto.hkdfSync("sha256", secret, Buffer.from("VITA-FL/key-service/transport/v1"), canonicalClaims(claims), 32));
export const encryptApplicationKey = (key, claims) => {
    const pair = crypto.generateKeyPairSync("x25519");
    const shared = crypto.diffieHellman({ privateKey: pair.privateKey, publicKey: importRecipient(claims.recipientPublicKey) });
    const wrapping = transportKey(shared, claims);
    try {
        const iv = crypto.randomBytes(12), cipher = crypto.createCipheriv("aes-256-gcm", wrapping, iv);
        cipher.setAAD(canonicalClaims(claims));
        return { version: 1, senderPublicKey: rawPublicKey(pair.publicKey), iv: iv.toString("hex"),
            ciphertext: Buffer.concat([cipher.update(key), cipher.final()]).toString("hex"), tag: cipher.getAuthTag().toString("hex") };
    }
    finally {
        shared.fill(0);
        wrapping.fill(0);
    }
};
export const decryptApplicationKey = (envelope, claims, privateKey) => {
    if (!envelope || Object.keys(envelope).sort().join() !== ["version", "senderPublicKey", "iv", "tag", "ciphertext"].sort().join() ||
        envelope.version !== 1 || !/^[a-f0-9]{24}$/.test(envelope.iv) || !/^[a-f0-9]{32}$/.test(envelope.tag) ||
        !/^[a-f0-9]{64}$/.test(envelope.ciphertext))
        throw new Error("Invalid application-key envelope");
    const shared = crypto.diffieHellman({ privateKey, publicKey: importRecipient(envelope.senderPublicKey) });
    const wrapping = transportKey(shared, claims);
    try {
        const decipher = crypto.createDecipheriv("aes-256-gcm", wrapping, Buffer.from(envelope.iv, "hex"));
        decipher.setAAD(canonicalClaims(claims));
        decipher.setAuthTag(Buffer.from(envelope.tag, "hex"));
        return Buffer.concat([decipher.update(Buffer.from(envelope.ciphertext, "hex")), decipher.final()]);
    }
    finally {
        shared.fill(0);
        wrapping.fill(0);
    }
};
