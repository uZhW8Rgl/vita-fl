import axios from "axios";
import crypto from "crypto";
import fs from "fs";
import FormData from "form-data";
import { getActiveModelBundle, getCommittedRunRosterState, createAggregationStatement, finalizeRoundWithAggregation, getDevicePublicKey, getRound, isGlobalModelPublished, isRoundCompleted, } from "./bc_client.js";
import { buildEncryptedGlobalModelArtifacts, decryptEncryptedGlobalModelArtifacts, verifyEncryptedGlobalModelBundleSignature, } from "./gm_crypto.js";
import { HYBRID_R_V1_HASH, HYBRID_R_VALIDATION_DATA_V1_HASH, } from "./protocol_digest.js";
import { normalizeBootstrapPublicKey, } from "./bootstrap_snapshot.js";
import { isExplicitlyFinalizedSourceRound } from "./finalization_recovery.js";
export const pinFile = async (filePath) => {
    try {
        const formData = new FormData();
        const file = fs.createReadStream(filePath);
        formData.append("file", file);
        if (process.env.IPFS_PROVIDER === "kubo") {
            const api = kuboApiBaseUrl();
            if (!api) {
                throw new Error("IPFS_PROVIDER=kubo requires KUBO_API");
            }
            const res = await axios.post(`${api}/api/v0/add?pin=true&cid-version=1&wrap-with-directory=false`, formData, {
                headers: formData.getHeaders(),
                timeout: ipfsTimeoutMs(),
            });
            return res.data.Hash;
        }
        const pinataMetadata = JSON.stringify({
            name: "File name",
        });
        formData.append("pinataMetadata", pinataMetadata);
        const pinataOptions = JSON.stringify({
            cidVersion: 1,
        });
        formData.append("pinataOptions", pinataOptions);
        const res = await axios.post("https://api.pinata.cloud/pinning/pinFileToIPFS", formData, {
            headers: {
                Authorization: `Bearer ${process.env.PINATA_JWT}`,
            },
        });
        return res.data.IpfsHash;
    }
    catch (error) {
        console.error(`Failed to pin ${filePath} to IPFS`, error);
        throw error;
    }
};
const ipfsTimeoutMs = () => Number(process.env.IPFS_FETCH_TIMEOUT_MS || 30000);
const kuboApiBaseUrl = () => (process.env.KUBO_API || "").replace(/\/+$/, "");
const ipfsArchiveDir = () => {
    const raw = (process.env.IPFS_ARCHIVE_DIR || "/").trim();
    if (raw === "/" || raw === "") {
        return "/";
    }
    return raw.replace(/\/+$/, "");
};
const timestampedArtifactName = (baseName, round) => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    return `round-${round}-${stamp}-${baseName}`;
};
const ensureKuboMfsDir = async (dir) => {
    const api = kuboApiBaseUrl();
    if (!api) {
        throw new Error("IPFS_PROVIDER=kubo requires KUBO_API");
    }
    await axios.post(`${api}/api/v0/files/mkdir`, null, {
        params: {
            arg: dir,
            parents: true,
        },
        timeout: ipfsTimeoutMs(),
    });
};
const copyCidToKuboMfs = async (cid, destPath) => {
    const api = kuboApiBaseUrl();
    if (!api) {
        throw new Error("IPFS_PROVIDER=kubo requires KUBO_API");
    }
    await axios.post(`${api}/api/v0/files/cp`, null, {
        params: {
            arg: [`/ipfs/${cid}`, destPath],
        },
        timeout: ipfsTimeoutMs(),
        paramsSerializer: {
            serialize: (params) => {
                const values = Array.isArray(params.arg) ? params.arg : [params.arg];
                return values.map((value) => `arg=${encodeURIComponent(String(value))}`).join("&");
            },
        },
    });
};
const archivePinnedFileToKubo = async (cid, baseName, round) => {
    if (process.env.IPFS_PROVIDER !== "kubo") {
        return;
    }
    const dir = ipfsArchiveDir();
    const filename = timestampedArtifactName(baseName, round);
    const destPath = dir === "/" ? `/${filename}` : `${dir}/${filename}`;
    if (dir !== "/") {
        await ensureKuboMfsDir(dir);
    }
    await copyCidToKuboMfs(cid, destPath);
    console.log(`Archived ${baseName} in Kubo MFS: ${destPath}`);
};
const ipfsGatewayUrl = (hash) => {
    const configuredGateway = process.env.IPFS_PROVIDER === "kubo"
        ? process.env.KUBO_GATEWAY
        : process.env.IPFS_GATEWAY;
    const gateway = (configuredGateway || process.env.IPFS_GATEWAY || process.env.KUBO_GATEWAY || "").replace(/\/+$/, "");
    if (!gateway) {
        throw new Error("Missing IPFS gateway configuration");
    }
    return gateway.endsWith("/ipfs") ? `${gateway}/${hash}` : `${gateway}/ipfs/${hash}`;
};
const kuboApiCatUrl = (hash) => {
    const api = kuboApiBaseUrl();
    return api ? `${api}/api/v0/cat?arg=${encodeURIComponent(hash)}` : "";
};
const fetchIPFSBytes = async (hash) => {
    const catUrl = process.env.IPFS_PROVIDER === "kubo" ? kuboApiCatUrl(hash) : "";
    const url = catUrl || ipfsGatewayUrl(hash);
    try {
        const res = catUrl
            ? await axios.post(url, null, { responseType: "arraybuffer", timeout: ipfsTimeoutMs() })
            : await axios.get(url, { responseType: "arraybuffer", timeout: ipfsTimeoutMs() });
        return Buffer.from(res.data);
    }
    catch (error) {
        if (catUrl) {
            const gatewayUrl = ipfsGatewayUrl(hash);
            console.warn(`Kubo API cat failed for ${hash}; falling back to gateway ${gatewayUrl}`);
            const res = await axios.get(gatewayUrl, { responseType: "arraybuffer", timeout: ipfsTimeoutMs() });
            return Buffer.from(res.data);
        }
        console.error(`Failed to fetch IPFS file ${hash} from ${url}`, error);
        throw error;
    }
};
export const getFileFromIPFS = async (hash, outPath = "./data/gm.bin") => {
    const bytes = await fetchIPFSBytes(hash);
    fs.writeFileSync(outPath, bytes);
    console.log(`File written to ${outPath} from IPFS hash ${hash}`);
};
const encryptedBundlePaths = () => ({
    bundlePath: "./data/results_iid/aggregated.bundle.enc",
    bundleSignaturePath: "./data/results_iid/aggregated.bundle.enc.sig",
    keyBundlePath: "./data/results_iid/aggregated.bundle.keys.json",
    aggregationEvidencePath: "./data/results_iid/aggregated.hybrid-r.json",
    receivedAggregationEvidencePath: "./data/gm.hybrid-r.json",
});
export const updateGM = async (expectedModelRound, participantPrivateKey, { frozenBootstrapRecipients, } = {}) => {
    const modelPath = "./data/results_iid/aggregated.bin";
    const sigPath = "./data/results_iid/aggregated.bin.sig";
    if (!Number.isSafeInteger(expectedModelRound) || expectedModelRound <= 0) {
        throw new Error(`Invalid expected global-model round: ${expectedModelRound}`);
    }
    const intendedSourceRound = expectedModelRound - 1;
    const sourceRound = Number(await getRound());
    if (sourceRound >= expectedModelRound) {
        const [published, completed] = await Promise.all([
            isGlobalModelPublished(intendedSourceRound),
            isRoundCompleted(intendedSourceRound),
        ]);
        if (isExplicitlyFinalizedSourceRound({ published, completed })) {
            console.log(`Aggregation round ${intendedSourceRound} is already atomically finalized; ` +
                "skipping duplicate artifact publication.");
            return {
                sourceRound: intendedSourceRound,
                expectedModelRound,
                reconciled: true,
            };
        }
    }
    if (!Number.isSafeInteger(sourceRound) || sourceRound < 0 || sourceRound + 1 !== expectedModelRound) {
        throw new Error(`Global-model round changed before publication: expected source round ${expectedModelRound - 1}, got ${sourceRound}`);
    }
    const round = expectedModelRound;
    let recipients;
    if (frozenBootstrapRecipients !== undefined) {
        if (intendedSourceRound !== 0) {
            throw new Error("Frozen bootstrap recipients may only be used to publish model round 1.");
        }
        if (!Array.isArray(frozenBootstrapRecipients)
            || frozenBootstrapRecipients.length === 0) {
            throw new Error("Round-0 GM encryption requires frozen recipients.");
        }
        recipients = frozenBootstrapRecipients.map((entry, index) => {
            const address = String(entry?.address || "").trim().toLowerCase();
            if (!/^0x[0-9a-f]{40}$/.test(address)) {
                throw new Error(`Invalid frozen bootstrap recipient address at index ${index}: ${entry?.address}`);
            }
            return {
                address,
                publicKeyDerHex: normalizeBootstrapPublicKey(entry?.publicKeyDerHex, `frozen bootstrap recipient ${address} RSA public key`),
            };
        });
        if (new Set(recipients.map(({ address }) => address)).size !== recipients.length) {
            throw new Error("Frozen bootstrap recipient addresses must be unique.");
        }
    }
    else {
        const rosterState = await getCommittedRunRosterState();
        if (!rosterState.committed || !rosterState.frozen) {
            throw new Error("GM encryption requires the immutable committed run roster to be frozen.");
        }
        const committedAddresses = Array.from(rosterState.roster || []).map((address) => String(address || "").trim().toLowerCase());
        if (committedAddresses.length === 0
            || Number(rosterState.registeredWorkerCount) !== committedAddresses.length) {
            throw new Error("GM encryption requires every committed run-roster member to be registered.");
        }
        if (new Set(committedAddresses).size !== committedAddresses.length) {
            throw new Error("Committed GM encryption recipients must be unique.");
        }
        recipients = [];
        for (const address of committedAddresses) {
            if (!/^0x[0-9a-f]{40}$/.test(address)) {
                throw new Error(`Invalid committed GM encryption recipient address: ${address}`);
            }
            const publicKeyDerHex = normalizeBootstrapPublicKey(await getDevicePublicKey(address), `committed GM encryption recipient ${address} RSA public key`);
            recipients.push({ address, publicKeyDerHex });
        }
    }
    const { bundlePath, bundleSignaturePath, keyBundlePath, aggregationEvidencePath, } = encryptedBundlePaths();
    if (sourceRound > 0 && !fs.existsSync(aggregationEvidencePath)) {
        throw new Error(`Missing Hybrid-R aggregation evidence for source round ${sourceRound}: ` +
            aggregationEvidencePath);
    }
    const { outputBundleHash, aggregationEvidenceHash } = await buildEncryptedGlobalModelArtifacts({
        modelPath,
        signaturePath: sigPath,
        aggregationEvidencePath: sourceRound > 0 ? aggregationEvidencePath : undefined,
        encryptedBundlePath: bundlePath,
        encryptedSignaturePath: bundleSignaturePath,
        keyBundlePath,
        recipients,
        round,
        signingPrivateKey: participantPrivateKey,
    });
    const outputModelHash = `0x${crypto.createHash("sha256").update(fs.readFileSync(modelPath)).digest("hex")}`;
    const modelCid = await pinFile(bundlePath);
    if (!modelCid)
        throw new Error("Pinning failed for encrypted model bundle, no CID returned");
    await archivePinnedFileToKubo(modelCid, "aggregated.bundle.enc", round);
    const sigCid = await pinFile(bundleSignaturePath);
    if (!sigCid)
        throw new Error("Pinning failed for encrypted bundle signature, no CID returned");
    await archivePinnedFileToKubo(sigCid, "aggregated.bundle.enc.sig", round);
    const keyBundleCid = await pinFile(keyBundlePath);
    if (!keyBundleCid)
        throw new Error("Pinning failed for encrypted key bundle, no CID returned");
    await archivePinnedFileToKubo(keyBundleCid, "aggregated.bundle.keys.json", round);
    console.log("New encrypted GM bundle CID:", modelCid);
    console.log("New encrypted GM bundle signature CID:", sigCid);
    console.log("New encrypted GM key bundle CID:", keyBundleCid);
    const aggregationStatement = await createAggregationStatement({
        sourceRound,
        modelCid,
        signatureCid: sigCid,
        keyBundleCid,
        outputModelHash,
        outputBundleHash,
    });
    await finalizeRoundWithAggregation({
        modelCid,
        signatureCid: sigCid,
        keyBundleCid,
        outputModelHash,
        outputBundleHash,
        statementSignature: aggregationStatement.statementSignature,
    });
    const observedRound = Number(await getRound());
    const [published, completed] = await Promise.all([
        isGlobalModelPublished(sourceRound),
        isRoundCompleted(sourceRound),
    ]);
    if (observedRound !== expectedModelRound
        || !isExplicitlyFinalizedSourceRound({ published, completed })) {
        throw new Error(`Atomic aggregation finalization was not observable: expected round ` +
            `${expectedModelRound}, got ${observedRound}; ` +
            `published=${published}, completed=${completed}.`);
    }
    console.log("Encrypted global model and TEE-signed aggregation evidence finalized atomically on-chain.", {
        sourceRound,
        inputRoot: aggregationStatement.policy.inputRoot,
        inputCount: aggregationStatement.policy.acceptedSubmissions,
        policyHash: aggregationStatement.policy.policyHash,
        outputModelHash,
        outputBundleHash,
        aggregationEvidenceHash,
        publicationHash: aggregationStatement.publicationHash,
        statementDigest: aggregationStatement.statementDigest,
    });
    return {
        sourceRound,
        expectedModelRound,
        reconciled: false,
        aggregationStatement,
    };
};
export const getCurrentModel = async (participantPrivateKey) => {
    const activeModel = await getActiveModelBundle();
    const modelCid = String(activeModel.modelCid || "");
    const sigCid = String(activeModel.sigCid || "");
    const keyBundleCid = String(activeModel.keyBundleCid || "");
    const finalizedModelRound = Number(activeModel.modelRound);
    if (!modelCid) {
        throw new Error("Missing encrypted global model bundle CID on-chain.");
    }
    if (!sigCid) {
        throw new Error("Missing encrypted global model bundle signature CID on-chain.");
    }
    if (!keyBundleCid) {
        throw new Error("Missing encrypted global model key bundle CID on-chain.");
    }
    if (!Number.isSafeInteger(finalizedModelRound) || finalizedModelRound <= 0) {
        throw new Error(`Invalid finalized global-model round on-chain: ${activeModel.modelRound}`);
    }
    console.log("Model CID:", modelCid);
    console.log("Sig CID:", sigCid);
    console.log("Key bundle CID:", keyBundleCid);
    const { bundlePath, bundleSignaturePath, keyBundlePath, receivedAggregationEvidencePath, } = encryptedBundlePaths();
    const encryptedBundleBytes = await fetchIPFSBytes(modelCid);
    fs.writeFileSync(bundlePath, encryptedBundleBytes);
    console.log(`Encrypted GM bundle written to ${bundlePath}`);
    const encryptedSignatureBytes = await fetchIPFSBytes(sigCid);
    fs.writeFileSync(bundleSignaturePath, encryptedSignatureBytes);
    console.log(`Encrypted GM bundle signature written to ${bundleSignaturePath}`);
    if (!verifyEncryptedGlobalModelBundleSignature({
        encryptedBundleBytes,
        encryptedSignatureBytes,
        publisherPublicKeyDerHex: activeModel.publisherPublicKeyDerHex,
    })) {
        throw new Error("Encrypted global-model bundle signature verification failed.");
    }
    console.log("Encrypted global-model bundle signature verified.");
    fs.writeFileSync(keyBundlePath, await fetchIPFSBytes(keyBundleCid));
    console.log(`Encrypted GM key bundle written to ${keyBundlePath}`);
    const decrypted = await decryptEncryptedGlobalModelArtifacts({
        encryptedBundlePath: bundlePath,
        keyBundlePath,
        ownAddress: String(process.env.ACCOUNT_ADDRESS || ""),
        outModelPath: "./data/gm.bin",
        outSignaturePath: "./data/gm.bin.sig",
        outAggregationEvidencePath: receivedAggregationEvidencePath,
        decryptionPrivateKey: participantPrivateKey,
        expectedModelRound: finalizedModelRound,
    });
    if (decrypted.round > 1 && !decrypted.aggregationEvidence) {
        throw new Error(`Encrypted global model round ${decrypted.round} is missing bound Hybrid-R evidence.`);
    }
    if (decrypted.aggregationEvidence) {
        const evidence = decrypted.aggregationEvidence;
        const decryptedModelHash = `0x${crypto.createHash("sha256").update(fs.readFileSync("./data/gm.bin")).digest("hex")}`;
        if (String(evidence.algorithm_hash || "").toLowerCase()
            !== HYBRID_R_V1_HASH.toLowerCase()
            || String(evidence.validation_data_hash || "").toLowerCase()
                !== HYBRID_R_VALIDATION_DATA_V1_HASH.toLowerCase()
            || String(evidence.output_model_sha256 || "").toLowerCase()
                !== decryptedModelHash.toLowerCase()
            || Number(evidence.source_round) !== decrypted.round - 1
            || Number(evidence.max_loss_increase_bps) !== 500
            || typeof evidence.gate_passed !== "boolean"
            || evidence.output_kind
                !== (evidence.gate_passed ? "candidate" : "parent_fallback")) {
            throw new Error("Hybrid-R aggregation evidence does not match the implemented policy, " +
                "decrypted model, or key-bundle round.");
        }
    }
    console.log("Encrypted global model bundle fetched + decrypted");
    return {
        modelCid,
        sigCid,
        keyBundleCid,
        publisher: activeModel.publisher,
        publisherPublicKeyDerHex: activeModel.publisherPublicKeyDerHex,
        modelRound: finalizedModelRound,
        plaintextSignaturePresent: decrypted.plaintextSignaturePresent,
        aggregationEvidence: decrypted.aggregationEvidence,
        aggregationEvidenceHash: decrypted.aggregationEvidenceHash,
    };
};
