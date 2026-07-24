export const normalizePublisherPublicKeyDerHex = (value) => {
    if (typeof value !== "string") {
        throw new Error("Active model publisher public key must be returned as hexadecimal bytes.");
    }
    const normalized = value.trim().replace(/^0x/i, "").toLowerCase();
    if (!normalized || normalized.length % 2 !== 0 || !/^[0-9a-f]+$/.test(normalized)) {
        throw new Error("Active model publisher public key is empty or malformed.");
    }
    return `0x${normalized}`;
};
