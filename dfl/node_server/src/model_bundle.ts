type BundleIdentity = {
    modelCid: string;
    sigCid: string;
    keyBundleCid: string;
    publisher: string;
    publisherPublicKeyDerHex: string;
    modelRound: number;
};

export type VerifiedFetchedModelBundle = BundleIdentity & {
    plaintextSignaturePresent: boolean;
    aggregationEvidence: Record<string, unknown> | null;
    aggregationEvidenceHash: `0x${string}` | null;
};

const normalizedIdentity = (value: Record<string, unknown>, label: string): BundleIdentity => {
    const identity = {
        modelCid: String(value?.modelCid || ""),
        sigCid: String(value?.sigCid || ""),
        keyBundleCid: String(value?.keyBundleCid || ""),
        publisher: String(value?.publisher || "").toLowerCase(),
        publisherPublicKeyDerHex: String(
            value?.publisherPublicKeyDerHex || "",
        ).toLowerCase(),
        modelRound: Number(value?.modelRound),
    };
    if (
        !identity.modelCid
        || !identity.sigCid
        || !identity.keyBundleCid
        || !/^0x[0-9a-f]{40}$/.test(identity.publisher)
        || !/^0x[0-9a-f]+$/.test(identity.publisherPublicKeyDerHex)
        || identity.publisherPublicKeyDerHex === "0x"
        || !Number.isSafeInteger(identity.modelRound)
        || identity.modelRound <= 0
    ) {
        throw new Error(`${label} global-model identity is incomplete or invalid.`);
    }
    return identity;
};

export const reconcileFetchedGlobalModel = (
    fetchedValue: Record<string, unknown>,
    currentValue: Record<string, unknown>,
): VerifiedFetchedModelBundle => {
    const fetched = normalizedIdentity(fetchedValue, "Fetched");
    const current = normalizedIdentity(currentValue, "Current on-chain");
    if (
        fetched.modelCid !== current.modelCid
        || fetched.sigCid !== current.sigCid
        || fetched.keyBundleCid !== current.keyBundleCid
        || fetched.publisher !== current.publisher
        || fetched.publisherPublicKeyDerHex !== current.publisherPublicKeyDerHex
        || fetched.modelRound !== current.modelRound
    ) {
        throw new Error(
            "Fetched global model, finalized round, or publisher key is no longer the active on-chain bundle; skipping local training.",
        );
    }
    if (typeof fetchedValue.plaintextSignaturePresent !== "boolean") {
        throw new Error(
            "Fetched global model is missing verified plaintext-signature metadata.",
        );
    }
    const aggregationEvidence = fetchedValue.aggregationEvidence;
    if (
        aggregationEvidence !== null
        && (
            !aggregationEvidence
            || typeof aggregationEvidence !== "object"
            || Array.isArray(aggregationEvidence)
        )
    ) {
        throw new Error("Fetched global model has invalid aggregation-evidence metadata.");
    }
    const aggregationEvidenceHash = fetchedValue.aggregationEvidenceHash;
    if (
        aggregationEvidenceHash !== null
        && !/^0x[0-9a-f]{64}$/.test(
            String(aggregationEvidenceHash || "").toLowerCase(),
        )
    ) {
        throw new Error("Fetched global model has an invalid aggregation-evidence hash.");
    }
    if ((aggregationEvidence === null) !== (aggregationEvidenceHash === null)) {
        throw new Error(
            "Fetched global model aggregation evidence and hash presence do not match.",
        );
    }
    return {
        // All identity fields deliberately come from the second authoritative
        // on-chain read. Only metadata already verified while decrypting the
        // same CID tuple is carried over from the fetched result.
        ...current,
        plaintextSignaturePresent: fetchedValue.plaintextSignaturePresent,
        aggregationEvidence: aggregationEvidence as Record<string, unknown> | null,
        aggregationEvidenceHash:
            aggregationEvidenceHash === null
                ? null
                : String(aggregationEvidenceHash).toLowerCase() as `0x${string}`,
    };
};
