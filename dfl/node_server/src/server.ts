// @ts-nocheck
// code adapted from pinata docs https://docs.pinata.cloud/quickstart/node-js
import 'dotenv/config';
import path from 'path';
import { fileURLToPath } from 'url';
import { getCurrentGM, getCurrentGMSignature, getCurrentGMKeyBundle, setGlobalModel, getCurrentState, getAggregatorEndpoint, setAggregatorEndpoint, setCurrentState, setContribution, getTopContributor, getContribution, triggerAggregatorSelection, reportAggregatorTimeout, getRound, incrementRound, isAuthorized, getAuthorizedDevices, getDevicePublicKey, getPreviousAggregatorFromGMStorage, getLastRoundsAggregator, registerDeviceWithTeeQuote, registerDeviceWithTeeQuoteAndRtmr3Events, submitModel, hasSubmittedModel, penalizeContribution } from "./bc_client.js";
import { getCurrentModel, pinFile, getFileFromIPFS, publishPendingGmUpdate, readPendingGmUpdateState, restorePendingGmUpdateStateFromLocalBundle, updateGM } from "./ipfs.js";
import { deriveTimingConfig, validateTimingConfig } from "./state_timing.js";
import fs from 'fs/promises';
import { readFileSync } from 'fs';
import { DstackClient } from '@phala/dstack-sdk';
import crypto from 'crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const deviceID = process.env.DEVICE_ID;
let currentState = "";
let aggregatorServerRunning = false;
const pythonServiceUrl = process.env.PYTHON_SERVICE_URL || 'http://127.0.0.1:8000';

function loadPemFromEnvOrFile(envName, fileEnvName, fallbackFileName) {
    const inline = process.env[envName];
    if (inline && inline.trim()) {
        return inline.replace(/\\n/g, '\n');
    }

    const candidates = [
        process.env[fileEnvName],
        path.resolve(__dirname, "..", fallbackFileName),
    ].filter(Boolean);
    for (const candidate of candidates) {
        try {
            return readFileSync(candidate, 'utf8');
        }
        catch {
            // Try the next configured location.
        }
    }
    return "";
}

const rsaPublicKey = loadPemFromEnvOrFile('RSA_PUBLIC_KEY', 'RSA_PUBLIC_KEY_FILE', 'public_key.pem');
const rsaPrivateKey = loadPemFromEnvOrFile('RSA_PRIVATE_KEY', 'RSA_PRIVATE_KEY_FILE', 'private_key.pem');
let localTdxRegistrationDone = false;
const timingConfig = deriveTimingConfig(process.env);
const modelSubmissionDeadlineMs = timingConfig.modelSubmissionDeadlineMs;
const gmUpdateTimeoutMs = timingConfig.gmUpdateTimeoutMs;
const gmUpdateTimeoutLoops = timingConfig.gmUpdateTimeoutLoops;
const gmUpdatePollMs = timingConfig.gmUpdatePollMs;
const modelTransferTimeoutMs = timingConfig.modelTransferTimeoutMs;
const modelTransferRetryDelayMs = timingConfig.modelTransferRetryDelayMs;
for (const warning of validateTimingConfig(timingConfig)) {
    console.warn("Timing config warning:", warning);
}
let missedGMUpdateLoops = 0;

function isTransientSepoliaProviderError(error) {
    const code = error?.code;
    const haystacks = [
        error?.message,
        error?.cause?.message,
        error?.cause,
        error?.data?.message,
        error?.data,
        error?.request,
        error?.stack,
    ]
        .filter(Boolean)
        .map((value) => String(value).toLowerCase());
    return code === 100 && haystacks.some((value) => value.includes("returned error"));
}

function isRecoverablePendingTxError(error) {
    const haystacks = [
        error?.message,
        error?.cause?.message,
        error?.cause,
        error?.data?.message,
        error?.data,
        error?.request,
        error?.stack,
    ]
        .filter(Boolean)
        .map((value) => String(value).toLowerCase());
    return haystacks.some((value) =>
        value.includes("replacement transaction underpriced")
        || value.includes("already known")
        || value.includes("nonce too low")
        || value.includes("transactionblocktimeouterror")
        || value.includes("was not mined within")
        || value.includes("not mined within")
    );
}

process.on("uncaughtException", (error) => {
    if (!isTransientSepoliaProviderError(error) && !isRecoverablePendingTxError(error)) {
        throw error;
    }
    console.warn("Ignoring recoverable Sepolia/provider exception:", error?.message || String(error));
});

process.on("unhandledRejection", (reason) => {
    if (!isTransientSepoliaProviderError(reason) && !isRecoverablePendingTxError(reason)) {
        throw reason;
    }
    console.warn("Ignoring recoverable Sepolia/provider rejection:", reason?.message || String(reason));
});

function sameAddress(left, right) {
    return String(left || '').toLowerCase() === String(right || '').toLowerCase();
}

function targetRound() {
    return Number(process.env.ROUND || 0);
}

async function assertFetchedGlobalModelIsStillCurrent(fetchedGlobalModel) {
    const current = {
        modelCid: String(await getCurrentGM() || ""),
        sigCid: String(await getCurrentGMSignature() || ""),
        keyBundleCid: String(await getCurrentGMKeyBundle() || ""),
    };
    const fetched = {
        modelCid: String(fetchedGlobalModel?.modelCid || ""),
        sigCid: String(fetchedGlobalModel?.sigCid || ""),
        keyBundleCid: String(fetchedGlobalModel?.keyBundleCid || ""),
    };
    if (
        !fetched.modelCid ||
        fetched.modelCid !== current.modelCid ||
        fetched.sigCid !== current.sigCid ||
        fetched.keyBundleCid !== current.keyBundleCid
    ) {
        throw new Error(
            "Fetched global model is no longer the current on-chain model; skipping local training."
        );
    }
    console.log("Fetched global model matches current on-chain CIDs.");
    return current;
}

async function runtimeEvent(name, attributes = {}) {
    return undefined;
}

async function runOperation(name, attributes, operation) {
    return operation();
}

async function waitForSubmittedRoundToAdvance(currentRound) {
    console.log(`Model already submitted for round ${currentRound}. Waiting for round advance instead of retraining.`);
    await runtimeEvent("worker.round_already_submitted.waiting", {
        role: "worker",
        round: currentRound,
    });
    try {
        const nextRound = await waitForRoundAdvance(currentRound, {
            pollMs: gmUpdatePollMs,
            timeoutMs: gmUpdateTimeoutMs,
        });
        missedGMUpdateLoops = 0;
        console.log(`Round advanced from ${currentRound} to ${nextRound}.`);
    } catch (e) {
        missedGMUpdateLoops++;
        console.warn(`Round ${currentRound} did not advance within timeout. Missed update loop ${missedGMUpdateLoops}/${gmUpdateTimeoutLoops}.`);
        if (missedGMUpdateLoops >= gmUpdateTimeoutLoops) {
            console.warn("Reporting aggregator timeout onchain.");
            await runtimeEvent("worker.aggregator_timeout.reported", {
                role: "worker",
                missed_loops: missedGMUpdateLoops,
            });
            try {
                await reportAggregatorTimeout();
            } catch (reportError) {
                console.error("Error reporting aggregator timeout:", reportError);
            }
            missedGMUpdateLoops = 0;
        }
    }
}

async function callPythonService(endpoint, payload = {}, { timeoutMs = 0 } = {}) {
    const controller = timeoutMs > 0 ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
        const res = await fetch(`${pythonServiceUrl}${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            signal: controller?.signal,
        });
        const text = await res.text();
        let data = {};
        if (text) {
            try { data = JSON.parse(text); } catch { data = { raw: text }; }
        }
        if (!res.ok || data.ok === false) {
            throw new Error(`Python service ${endpoint} failed (${res.status}): ${text}`);
        }
        return data;
    } finally {
        if (timeout) clearTimeout(timeout);
    }
}

async function stopAggregatorServer() {
    if (!aggregatorServerRunning) return;
    await callPythonService('/server/stop');
    aggregatorServerRunning = false;
}

function normalizeHexBytes(input) {
    let hex = Buffer.isBuffer(input) ? input.toString('utf8') : String(input || '');
    hex = hex.trim();
    if (hex.startsWith('0x') || hex.startsWith('0X')) hex = hex.slice(2);
    hex = hex.replace(/\s+/g, '');
    if (!/^[0-9a-fA-F]*$/.test(hex) || hex.length % 2 !== 0) {
        throw new Error('TDX quote file must contain hex-encoded bytes');
    }
    return `0x${hex}`;
}

function rsaPublicKeyDerHex() {
    if (!rsaPublicKey.trim()) {
        throw new Error('RSA_PUBLIC_KEY or RSA_PUBLIC_KEY_FILE is required for device registration.');
    }
    const key = crypto.createPublicKey(rsaPublicKey);
    const der = key.export({ format: 'der', type: 'spki' });
    return `0x${Buffer.from(der).toString('hex')}`;
}

async function localModelPackageHash() {
    const data = await fs.readFile('./data/lm.bin.enc');
    return `0x${crypto.createHash('sha256').update(data).digest('hex')}`;
}

async function hasLocalModelPackage() {
    try {
        await fs.access('./data/lm.bin.enc');
        return true;
    } catch {
        return false;
    }
}

async function expectedWorkerAddresses() {
    const own = String(process.env.ACCOUNT_ADDRESS || '').toLowerCase();
    const authorizedDevices = await getAuthorizedDevices();
    return authorizedDevices
        .filter(address => address.toLowerCase() !== own);
}

async function receivedWorkerModelFiles() {
    const expected = new Set((await expectedWorkerAddresses()).map(address => address.toLowerCase()));
    const entries = await fs.readdir(srcModelsDir, { withFileTypes: true }).catch(err => {
        if (err && err.code === 'ENOENT') return [];
        throw err;
    });
    const files = [];
    for (const entry of entries) {
        if (!entry.isFile() || !entry.name.endsWith('.bin')) continue;
        const filePath = path.join(srcModelsDir, entry.name);
        const match = entry.name.match(/^wb_client_(.+)\.bin$/);
        if (!match) {
            files.push({ name: entry.name, path: filePath, authorized: false, reason: "unknown filename" });
            continue;
        }
        const address = match[1];
        if (!/^0x[0-9a-fA-F]{40}$/.test(address)) {
            files.push({ name: entry.name, path: filePath, authorized: false, reason: "filename does not contain a device address" });
            continue;
        }
        if (!expected.has(address.toLowerCase())) {
            files.push({ name: entry.name, path: filePath, address, authorized: false, reason: "not expected this round" });
            continue;
        }
        const authorized = await isAuthorized(address);
        files.push({
            name: entry.name,
            path: filePath,
            address,
            authorized,
            reason: authorized ? "" : "not TEE authorized",
        });
    }
    return files.sort((a, b) => a.name.localeCompare(b.name));
}

async function receivedWorkerAddresses() {
    const files = await receivedWorkerModelFiles();
    return new Set(
        files
            .filter(file => file.authorized && file.address)
            .map(file => file.address.toLowerCase())
    );
}

async function getMissingAuthorizedWorkers() {
    const expected = await expectedWorkerAddresses();
    if (expected.length === 0) {
        console.warn("No onchain authorized workers found; skipping missed-deadline penalties.");
        return [];
    }
    const received = await receivedWorkerAddresses();
    const missing = [];
    for (const address of expected) {
        if (received.has(address.toLowerCase())) continue;
        if (await isAuthorized(address)) {
            missing.push(address);
        }
    }
    return missing;
}

async function registerWithLocalTdxQuote() {
    if (localTdxRegistrationDone || process.env.DOCKER === "phala") return;
    localTdxRegistrationDone = true;

    const quotePath = process.env.TDX_QUOTE_PATH || './attestation/phala_tdx_quote';
    console.log(`Registering with local TDX quote from ${quotePath} ...`);
    const quoteHex = normalizeHexBytes(await fs.readFile(quotePath));

    await registerDeviceWithTeeQuote(
        quoteHex,
        process.env.ACCOUNT_ADDRESS,
        process.env.PUBLIC_IP || "",
        process.env.MSG_BROKER_IP || "",
        rsaPublicKeyDerHex(),
    );
    console.log("Device registered with onchain TDX quote verification.");
}

function normalizeRtmr3EventDigests(eventLog) {
    let events = eventLog;
    if (typeof events === 'string') {
        events = JSON.parse(events);
    }
    if (!Array.isArray(events)) {
        throw new Error('Phala quote response did not include an RTMR event log array');
    }
    const digests = events
        .filter(event => Number(event?.imr) === 3)
        .map(event => {
            let digest = String(event?.digest || '').trim();
            if (digest.startsWith('0x') || digest.startsWith('0X')) digest = digest.slice(2);
            if (!/^[0-9a-fA-F]{96}$/.test(digest)) {
                throw new Error(`Invalid RTMR3 event digest for event ${event?.event || '<unknown>'}`);
            }
            return `0x${digest}`;
        });
    if (digests.length === 0) {
        throw new Error('Phala quote response did not include RTMR3 event digests');
    }
    return digests;
}

function derHexToBuffer(derHex) {
    if (typeof derHex !== 'string') return Buffer.alloc(0);
    let hex = derHex.trim();
    if (hex.startsWith('0x') || hex.startsWith('0X')) hex = hex.slice(2);
    hex = hex.replace(/\s+/g, '');
    if (hex.length === 0 || (hex.length % 2) !== 0) return Buffer.alloc(0);
    return Buffer.from(hex, 'hex');
}

async function verifyDownloadedGlobalModelSignature({ publicKeyDerHex, modelPath = './data/gm.bin', sigPath = './data/gm.bin.sig' } = {}) {
    const pubDer = derHexToBuffer(publicKeyDerHex);
    if (!pubDer.length) {
        console.error('Signature verification: invalid public key DER-hex');
        return false;
    }

    let modelBytes;
    let sigBytes;
    try {
        modelBytes = await fs.readFile(modelPath);
    } catch (e) {
        console.error(`Signature verification: cannot read model file ${modelPath}`, e);
        return false;
    }
    try {
        sigBytes = await fs.readFile(sigPath);
    } catch (e) {
        console.error(`Signature verification: cannot read signature file ${sigPath}`, e);
        return false;
    }
    if (!sigBytes || sigBytes.length === 0) {
        console.error('Signature verification: signature file empty');
        return false;
    }

    let keyObject;
    try {
        keyObject = crypto.createPublicKey({ key: pubDer, format: 'der', type: 'spki' });
    } catch (e) {
        console.error('Signature verification: failed to parse public key (DER/SPKI)', e);
        return false;
    }

    try {
        const ok = crypto.verify(
            'RSA-SHA256',
            modelBytes,
            { key: keyObject, padding: crypto.constants.RSA_PKCS1_PADDING },
            sigBytes
        );
        return !!ok;
    } catch (e) {
        console.error('Signature verification: crypto.verify failed', e);
        return false;
    }
}

function isMissingRoundKeyError(error) {
    const message = error?.message || String(error || "");
    return message.includes("No wrapped GM round key found");
}

function bootstrapModelPath() {
    return "./data/random_start.bin";
}

async function signFileWithLocalRsaKey(inputPath, outputSignaturePath) {
    if (!rsaPrivateKey.trim()) {
        throw new Error("RSA_PRIVATE_KEY is required to sign the round-0 bootstrap rollover model.");
    }
    const inputBytes = await fs.readFile(inputPath);
    const signatureBytes = crypto.sign(
        'RSA-SHA256',
        inputBytes,
        {
            key: rsaPrivateKey,
            padding: crypto.constants.RSA_PKCS1_PADDING,
        }
    );
    await fs.writeFile(outputSignaturePath, signatureBytes);
}

async function resolveBootstrapSignerPublicKeyDerHex() {
    if (rsaPublicKey.trim()) {
        return rsaPublicKeyDerHex();
    }

    const onchainCandidates = [
        await getLastRoundsAggregator().catch(() => ""),
        process.env.INITIAL_GM_SIGNER_ADDRESS || "",
    ]
        .map((value) => String(value || "").trim())
        .filter(Boolean);

    for (const candidate of onchainCandidates) {
        const publicKey = String(await getDevicePublicKey(candidate).catch(() => "") || "").trim();
        if (publicKey && publicKey !== "0x") {
            return publicKey;
        }
    }

    return "";
}

async function prepareRoundZeroBootstrapRollover() {
    console.log("Round 0 bootstrap rollover: fetching current encrypted GM bundle.");
    try {
        await getCurrentModel();

        const lastSignersPubKey = await resolveBootstrapSignerPublicKeyDerHex();
        const sigOk = await verifyDownloadedGlobalModelSignature({
            publicKeyDerHex: lastSignersPubKey,
            modelPath: "./data/gm.bin",
            sigPath: "./data/gm.bin.sig",
        });
        if (!sigOk) {
            throw new Error("Round-0 bootstrap rollover signature verification failed.");
        }
    } catch (error) {
        if (!isMissingRoundKeyError(error)) {
            throw error;
        }
        console.warn("Round 0 bootstrap rollover detected a legacy GM bundle without the mandatory primary-aggregator key. Rebuilding the update artifacts from the local bootstrap model snapshot.");
    }

    await fs.mkdir(resultsIIDDir, { recursive: true });
    const sourceModelPath = await fs.access("./data/gm.bin").then(() => "./data/gm.bin").catch(() => bootstrapModelPath());
    await fs.copyFile(sourceModelPath, path.join(resultsIIDDir, "aggregated.bin"));
    await signFileWithLocalRsaKey(
        path.join(resultsIIDDir, "aggregated.bin"),
        path.join(resultsIIDDir, "aggregated.bin.sig"),
    );
    console.log("Round 0 bootstrap rollover prepared aggregated.bin + aggregated.bin.sig for encrypted republish.");
}

async function ensureUpdateArtifactsForCurrentRound() {
    const currentRound = Number(await getRound());
    const aggregatedPath = path.join(resultsIIDDir, "aggregated.bin");
    const aggregatedSigPath = path.join(resultsIIDDir, "aggregated.bin.sig");
    const encryptedBundlePath = path.join(resultsIIDDir, "aggregated.bundle.enc");
    const encryptedBundleSigPath = path.join(resultsIIDDir, "aggregated.bundle.enc.sig");
    const encryptedBundleKeyPath = path.join(resultsIIDDir, "aggregated.bundle.keys.json");
    const hasArtifacts = await fs.access(aggregatedPath).then(() => fs.access(aggregatedSigPath)).then(() => true).catch(() => false);
    const hasEncryptedBundleArtifacts = await fs.access(encryptedBundlePath)
        .then(() => fs.access(encryptedBundleSigPath))
        .then(() => fs.access(encryptedBundleKeyPath))
        .then(() => true)
        .catch(() => false);
    const pendingUpdateState = readPendingGmUpdateState();
    if (pendingUpdateState?.modelCid && pendingUpdateState?.sigCid && pendingUpdateState?.keyBundleCid) {
        const [latestModelCid, latestSigCid, latestKeyBundleCid] = await Promise.all([
            getCurrentGM().catch(() => ""),
            getCurrentGMSignature().catch(() => ""),
            getCurrentGMKeyBundle().catch(() => ""),
        ]);
        if (
            String(latestModelCid || "") === String(pendingUpdateState.modelCid)
            && String(latestSigCid || "") === String(pendingUpdateState.sigCid)
            && String(latestKeyBundleCid || "") === String(pendingUpdateState.keyBundleCid)
        ) {
            console.log(`Round ${currentRound} resumed in UPDATING after the GM publish was already finalized on-chain. Continuing with round finalization only.`);
            return "published_resume";
        }
        console.log(`Round ${currentRound} resumed in UPDATING with a pending GM publish marker. Reusing the existing encrypted bundle CIDs.`);
        return "pending_publish_resume";
    }
    if (hasArtifacts) {
        return "ready";
    }
    if (currentRound > 0 && hasEncryptedBundleArtifacts) {
        const [localModelCid, localSigCid, localKeyBundleCid, latestModelCid, latestSigCid, latestKeyBundleCid] = await Promise.all([
            pinFile(encryptedBundlePath),
            pinFile(encryptedBundleSigPath),
            pinFile(encryptedBundleKeyPath),
            getCurrentGM().catch(() => ""),
            getCurrentGMSignature().catch(() => ""),
            getCurrentGMKeyBundle().catch(() => ""),
        ]);
        if (
            String(latestModelCid || "") === String(localModelCid || "")
            && String(latestSigCid || "") === String(localSigCid || "")
            && String(latestKeyBundleCid || "") === String(localKeyBundleCid || "")
        ) {
            console.log(`Round ${currentRound} resumed in UPDATING with bundle artifacts that already match the on-chain CIDs. Continuing with round finalization only.`);
            return "published_resume";
        }
        await restorePendingGmUpdateStateFromLocalBundle();
        console.log(`Round ${currentRound} resumed in UPDATING with local encrypted bundle artifacts. Reusing their pinned CIDs for publish retry.`);
        return "pending_publish_resume";
    }
    if (currentRound > 0) {
        throw new Error(`Round ${currentRound} update artifacts are missing after restart and no finalized pending GM update marker was found.`);
    }
    console.log("Round 0 update artifacts missing after restart. Rebuilding them from the encrypted bootstrap model.");
    await prepareRoundZeroBootstrapRollover();
    return "ready";
}

async function ensureAggregatingStateWithReceivedModels(expectedModels) {
    const currentState = await getCurrentState();
    const onchainState = String(currentState?.[0] || "");
    const presentModels = await countAuthorizedModelFiles();
    if (onchainState === "AGGREGATING" && presentModels > 0) {
        console.log(`On-chain state is already AGGREGATING and ${presentModels}/${expectedModels} authorized model file(s) are present. Resuming aggregation directly.`);
        return true;
    }
    return false;
}

async function aggregateCurrentRoundAsAggregator() {
    currentState = "AGGREGATING";
    console.log("I am the aggregator");
    console.log("Starting the aggregation process ...");
    await runtimeEvent("aggregator.aggregation.started", { role: "aggregator" });
    try {
        const currentRound = Number(await getRound());
        const expected = Number(process.env.CLIENT_LIMIT || 1);
        const present = await waitForModels(expected, {
            dir: srcModelsDir,
            pollMs: 2000,
            timeoutMs: 1,
        });
        console.log(`Models present before aggregation: ${present}/${expected}`);

        if (aggregatorServerRunning) {
            console.log("Stopping aggregator server before aggregation...");
            await stopAggregatorServer();
        }

        const count = await stageAggregation();
        console.log(`Staged ${count} model file(s) for aggregation.`);
        await runtimeEvent("aggregator.models.staged", {
            role: "aggregator",
            model_count: count,
            expected_models: expected,
        });
        if (count <= 0) {
            if (currentRound === 0) {
                console.log("Round 0 has no worker submissions. Re-publishing the verified bootstrap model for the next encrypted round.");
                await runOperation("aggregator.round0.bootstrap_rollover", {
                    role: "aggregator",
                    round: currentRound,
                }, () => prepareRoundZeroBootstrapRollover());
                await setCurrentState("UPDATING");
                return;
            }
            console.log("No models to aggregate (count=0). Keeping round open and waiting for workers.");
            await sleep(5000);
            return;
        }
        const missingWorkers = currentRound === 0 ? [] : await getMissingAuthorizedWorkers();
        if (missingWorkers.length > 0) {
            console.log("Penalizing missing model submissions:", missingWorkers);
            await penalizeContribution(missingWorkers, "missed_model_deadline");
            await runtimeEvent("aggregator.penalty.applied", {
                role: "aggregator",
                reason: "missed_model_deadline",
                count: missingWorkers.length,
            });
        } else if (currentRound === 0) {
            console.log("Skipping missed-deadline penalties in round 0.");
        }
        if (count < expected) {
            console.log(`Aggregating with ${count}/${expected} models.`);
        }

        const globalModelRound = currentRound + 1;
        const participantCount = Number(process.env.WORKER_COUNT || expected + 1);
        const aggregateResult = await runOperation("aggregator.aggregation", {
            role: "aggregator",
            model_count: count,
            expected_models: expected,
        }, () => callPythonService('/aggregate', {
            num_files: Number(count),
            round_id: globalModelRound,
            source_round: currentRound,
            expected_models: expected,
            participant_count: participantCount,
        }, { timeoutMs: 3 * 60 * 1000 }));
        const metrics = aggregateResult?.metrics;
        if (metrics && typeof metrics === "object") {
            console.log("Global model evaluation metrics:", metrics);
            await runtimeEvent("aggregator.global_model_evaluation", {
                role: "aggregator",
                global_model_round: Number(metrics.round ?? globalModelRound),
                source_round: currentRound,
                participant_count: Number(metrics.participant_count ?? participantCount),
                aggregated_model_count: Number(metrics.aggregated_model_count ?? count),
                expected_models: expected,
                accuracy_percent: Number(metrics.accuracy_percent ?? 0),
                loss: Number(metrics.loss ?? 0),
                macro_f1: Number(metrics.macro_f1 ?? 0),
                macro_auroc: Number(metrics.macro_auroc ?? 0),
            });
        }
    } catch (e) {
        console.error("Error during aggregation:", e);
        await sleep(2000);
        return;
    }
    console.log("Aggregation complete.");
    await runtimeEvent("aggregator.aggregation.finished", { role: "aggregator" });
    try {
        await setCurrentState("UPDATING");
    } catch (error) {
        const latestState = await getCurrentState().catch(() => null);
        if (!sameAddress(latestState?.[1], process.env.ACCOUNT_ADDRESS)) {
            throw error;
        }
        console.warn("Proceeding with the update phase despite delayed UPDATING state because aggregation has already completed locally.");
        await updateCurrentRoundAsAggregator();
    }
}

async function updateCurrentRoundAsAggregator() {
    currentState = "UPDATING";
    console.log("I am the aggregator");
    console.log("Starting the updating process ...");
    await runtimeEvent("aggregator.update.started", { role: "aggregator" });
    let updatePreparation = "ready";
    try {
        updatePreparation = await runOperation(
            "aggregator.update.prepare_artifacts",
            { role: "aggregator" },
            () => ensureUpdateArtifactsForCurrentRound(),
        );
        if (updatePreparation === "ready") {
            await runOperation("aggregator.update_global_model", { role: "aggregator" }, () => updateGM());
        } else if (updatePreparation === "pending_publish_resume") {
            await runOperation("aggregator.update_global_model.resume_publish", { role: "aggregator" }, () => publishPendingGmUpdate());
        } else {
            console.log("Skipping duplicate GM update because the publish step is already finalized on-chain.");
        }
    } catch (e) {
        console.error("Error during updating the global model:", e);
        return;
    }
    console.log("Updating complete.");
    await runtimeEvent("aggregator.update.finished", { role: "aggregator" });
    console.log("Current Global Model:", await getCurrentGM());
    await stageCleaning();
    console.log("Passed cleaning");
    await incrementRound();
    const nextRound = Number(await getRound());
    console.log("Rounds left: ", (targetRound() - nextRound));
    if (nextRound >= targetRound()) {
        console.log(`Reached configured final round ${targetRound()}. Skipping next aggregator selection and stopping the worker loop.`);
        await runtimeEvent("training.completed", {
            role: "aggregator",
            final_round: nextRound,
        });
        return;
    }
    try {
        await runOperation("aggregator.selection", { role: "aggregator" }, () => triggerAggregatorSelection());
        console.log("Triggered new aggregator selection");
        await runtimeEvent("aggregator.selection.triggered", { role: "aggregator" });
    } catch (e) {
        console.error("Aggregator selection failed; falling back to current aggregator for next round:", e);
        await runtimeEvent("aggregator.selection.failed", {
            role: "aggregator",
            error: e?.message || String(e),
        });
        await setCurrentState("TRAINING");
        console.log("Set state to TRAINING for next round with the current aggregator");
        await sleep(2000);
    }
}

async function finalizeWorkerSubmission(currentRound, aggregatorAddress) {
    const ownAddress = String(process.env.ACCOUNT_ADDRESS || "");
    if (await hasSubmittedModel(currentRound, ownAddress)) {
        console.log(`Model submission already recorded for round ${currentRound}; skipping duplicate submit transaction.`);
    } else {
        await runOperation("worker.submit_model", {
            role: "worker",
            aggregator: String(aggregatorAddress),
        }, async () => submitModel(await localModelPackageHash()));
    }

    const contribution = Number(await getContribution(ownAddress).catch(() => 0));
    if (contribution > 0) {
        console.log(`Contribution already recorded for ${ownAddress}; skipping duplicate contribution transaction.`);
        return;
    }

    await runOperation("worker.set_contribution", {
        role: "worker",
        aggregator: String(aggregatorAddress),
    }, () => setContribution([ownAddress]));
}

async function resendSubmittedModelToAggregator(currentRound, aggregatorAddress) {
    if (!await hasLocalModelPackage()) {
        console.warn(`Model submission is already on-chain for round ${currentRound}, but ./data/lm.bin.enc is missing. Cannot re-send the local model package to the aggregator.`);
        return false;
    }

    console.log(`Model submission is already on-chain for round ${currentRound}. Re-sending the existing local model package to aggregator ${aggregatorAddress} while the round is still in TRAINING.`);
    await runtimeEvent("worker.model_transfer.resend_started", {
        role: "worker",
        aggregator: String(aggregatorAddress),
        round: currentRound,
    });
    await runOperation("worker.model_transfer.resend", {
        role: "worker",
        aggregator: String(aggregatorAddress),
        round: currentRound,
    }, () => callPythonService('/client', {
        server_ip: String(aggregatorAddress),
        device_id: String(process.env.ACCOUNT_ADDRESS),
        timeout_ms: modelTransferTimeoutMs,
    }, { timeoutMs: modelTransferTimeoutMs + 5000 }));
    await runtimeEvent("worker.model_transfer.resend_finished", {
        role: "worker",
        aggregator: String(aggregatorAddress),
        round: currentRound,
    });
    return true;
}

const stateMachine = async () => {
    if (process.env.DOCKER === "phala") {
        console.log("Fetching TDX Quote ...");
        const client = new DstackClient();

        // Get TEE instance information
        const info = await client.info();
        console.log('App ID:', info.app_id);
        console.log('Instance ID:', info.instance_id);
        console.log('App Name:', info.app_name);
        console.log('TCB Info:', info.tcb_info);

        // Generate remote attestation quote
        const applicationData = JSON.stringify({
        version: '1.0.0',
        timestamp: Date.now(),
        user_id: process.env.ACCOUNT_ADDRESS,
        });

        const reportData = crypto.createHash('sha256').update(applicationData).digest();
        const quote = await client.getQuote(reportData);
        const quoteHex = normalizeHexBytes(quote.quote);
        const rtmr3EventDigests = normalizeRtmr3EventDigests(quote.event_log);
        console.log('TDX Quote:', quote.quote);
        console.log(`Registering with live Phala TDX quote and ${rtmr3EventDigests.length} RTMR3 event digests ...`);

        await registerDeviceWithTeeQuoteAndRtmr3Events(
            quoteHex,
            rtmr3EventDigests,
            process.env.ACCOUNT_ADDRESS,
            process.env.PUBLIC_IP || "",
            process.env.MSG_BROKER_IP || "",
            rsaPublicKeyDerHex(),
        );
        console.log("Device registered with onchain TDX quote and RTMR3 event replay verification.");
    }
    else {
        await registerWithLocalTdxQuote();
    }
    while (Number(await getRound()) < targetRound()) {
        let state = await getCurrentState();

        switch (state["0"]) {
            case "TRAINING":
                if (sameAddress(state[1], process.env.ACCOUNT_ADDRESS)) {
                    console.log("I am the aggregator");
                    await runtimeEvent("state.training.aggregator", { role: "aggregator" });
                    const currentRound = Number(await getRound());
                    console.log("Round %d.", currentRound);
                    if (currentRound === 0) {
                        console.log("Round 0 bootstrap phase: skipping worker submission wait and proceeding directly to aggregation.");
                        try {
                            await setCurrentState("AGGREGATING");
                        } catch (error) {
                            const resumedAggregation = await ensureAggregatingStateWithReceivedModels(0);
                            if (!resumedAggregation) {
                                throw error;
                            }
                        }
                        continue;
                    }
                    console.log("Starting the zerompq server ...");
                    try {
                        if (!aggregatorServerRunning) {
                            await callPythonService('/server/start', {
                                client_limit: Number(process.env.CLIENT_LIMIT || 1),
                            });
                            aggregatorServerRunning = true;
                        }
                        const expected = Number(process.env.CLIENT_LIMIT || 1);
                        console.log(`Waiting for local model submissions before aggregation (${expected} expected).`);
                        await runtimeEvent("aggregator.wait_for_models.started", {
                            role: "aggregator",
                            expected_models: expected,
                            deadline_ms: modelSubmissionDeadlineMs,
                        });
                        const present = await runOperation("aggregator.wait_for_models", {
                            role: "aggregator",
                            expected_models: expected,
                            deadline_ms: modelSubmissionDeadlineMs,
                        }, () => waitForModels(expected, {
                            dir: srcModelsDir,
                            pollMs: 2000,
                            timeoutMs: modelSubmissionDeadlineMs,
                        }));
                        await runtimeEvent("aggregator.wait_for_models.finished", {
                            role: "aggregator",
                            expected_models: expected,
                            present_models: present,
                        });
                        if (present <= 0) {
                            console.log("No model submissions yet. Keeping aggregator server open and waiting before retry.");
                            await sleep(5000);
                            continue;
                        }
                        try {
                            await setCurrentState("AGGREGATING");
                        } catch (error) {
                            const resumedAggregation = await ensureAggregatingStateWithReceivedModels(expected);
                            if (!resumedAggregation && present < expected) {
                                throw error;
                            }
                            console.warn("Proceeding with aggregation despite delayed AGGREGATING state because the expected model files are already present locally.");
                            await aggregateCurrentRoundAsAggregator();
                            continue;
                        }
                        continue;
                    } catch (e) {
                        console.error("Error during starting the aggregator server:", e);
                        return;
                    }
                } else {
                    console.log("I am not the aggregator");
                    await runtimeEvent("state.training.worker", { role: "worker", aggregator: String(state["1"]) });
                    const currentRound = Number(await getRound());
                    console.log("Round %d.", currentRound);
                    if (await hasSubmittedModel(currentRound, process.env.ACCOUNT_ADDRESS)) {
                        try {
                            await resendSubmittedModelToAggregator(currentRound, state["1"]);
                        } catch (resendError) {
                            console.warn(`Re-send of already submitted model for round ${currentRound} failed:`, resendError);
                        }
                        await waitForSubmittedRoundToAdvance(currentRound);
                        await sleep(2000);
                        continue;
                    }
                    console.log("Fetching the global model from IPFS ...");
                    await runtimeEvent("worker.fetch_global_model.started", { role: "worker" });
                    let fetchedGlobalModel;
                    try {
                        fetchedGlobalModel = await runOperation("worker.fetch_global_model", { role: "worker" }, () => getCurrentModel());
                    } catch (error) {
                        if (!isMissingRoundKeyError(error)) {
                            throw error;
                        }
                        console.warn(`No round key for ${process.env.ACCOUNT_ADDRESS} in round ${currentRound}. Waiting for the next round after registration.`);
                        await runtimeEvent("worker.fetch_global_model.deferred", {
                            role: "worker",
                            round: currentRound,
                            reason: "missing_round_key",
                        });
                        await waitForRoundAdvance(currentRound, { pollMs: gmUpdatePollMs });
                        continue;
                    }
                    await runtimeEvent("worker.fetch_global_model.finished", { role: "worker" });

                    let currentGlobalModel;
                    try {
                        currentGlobalModel = await assertFetchedGlobalModelIsStillCurrent(fetchedGlobalModel);
                    } catch (error) {
                        console.warn(error?.message || String(error));
                        await runtimeEvent("worker.fetch_global_model.stale", {
                            role: "worker",
                            round: currentRound,
                            error: error?.message || String(error),
                        });
                        await sleep(2000);
                        continue;
                    }
                    const prevGM = currentGlobalModel.modelCid;

                    const lastSignerAddress = await getLastRoundsAggregator();
                    const lastSignersPubKey = await getDevicePublicKey(lastSignerAddress);
                    const sigOk = await verifyDownloadedGlobalModelSignature({
                        publicKeyDerHex: lastSignersPubKey,
                        modelPath: "./data/gm.bin",
                        sigPath: "./data/gm.bin.sig",
                    });
                    if (!sigOk) {
                        console.error("Global model signature verification FAILED. Aborting training.");
                        return;
                    }
                    console.log("Global model signature verification successful.");

                    console.log("Starting local training ...");
                    await runtimeEvent("worker.training.started", { role: "worker" });
                    try {
                        await runOperation("worker.training", { role: "worker" }, async () => callPythonService('/train', {
                            epochs: Number(process.env.EPOCH),
                            aggregator_public_key_der_hex: await getDevicePublicKey(state[1]),
                        }));
                    } catch (e) {
                        console.error("Error during local training:", e);
                        return;
                    }
                    console.log("Local training complete.");
                    await runtimeEvent("worker.training.finished", { role: "worker" });

                    const latestState = await getCurrentState();
                    if (sameAddress(latestState[1], process.env.ACCOUNT_ADDRESS)) {
                        console.log("I became the aggregator before model transfer. Returning to the state loop to start the aggregator server.");
                        await runtimeEvent("worker.promoted_to_aggregator_before_transfer", {
                            role: "aggregator",
                            previous_aggregator: String(state["1"]),
                        });
                        continue;
                    }
                    state = latestState;

                    console.log("Is the device authorized? ", await isAuthorized(process.env.ACCOUNT_ADDRESS));
                    console.log("Starting the zerompq client ...");
                    await runtimeEvent("worker.model_transfer.started", { role: "worker", aggregator: String(state["1"]) });
                    try {
                        await runOperation("worker.model_transfer", {
                            role: "worker",
                            aggregator: String(state["1"]),
                        }, () => callPythonService('/client', {
                            server_ip: String(state["1"]),
                            device_id: String(process.env.ACCOUNT_ADDRESS),
                            timeout_ms: modelTransferTimeoutMs,
                        }, { timeoutMs: modelTransferTimeoutMs + 5000 }));
                        await finalizeWorkerSubmission(currentRound, state["1"]);
                        await runtimeEvent("worker.model_transfer.finished", { role: "worker", aggregator: String(state["1"]) });
                    } catch (e) {
                        console.error("Error during model transfer:", e);
                        const latestTransferState = await getCurrentState().catch(() => null);
                        const latestTransferSystemState = String(latestTransferState?.[0] || "");
                        if (latestTransferSystemState === "AGGREGATING") {
                            console.warn("Aggregator has already moved to AGGREGATING after the client transfer attempt. Treating the model delivery as successful and finalizing on-chain submission.");
                            try {
                                await finalizeWorkerSubmission(currentRound, state["1"]);
                                await runtimeEvent("worker.model_transfer.recovered_after_timeout", {
                                    role: "worker",
                                    aggregator: String(state["1"]),
                                    round: currentRound,
                                });
                                await waitForSubmittedRoundToAdvance(currentRound);
                                await sleep(2000);
                                continue;
                            } catch (recoveryError) {
                                console.error("Error during model transfer recovery:", recoveryError);
                            }
                        }
                        if (await hasSubmittedModel(currentRound, process.env.ACCOUNT_ADDRESS).catch(() => false)) {
                            console.warn(`Model submission for round ${currentRound} is already recorded on-chain despite the transfer error. Continuing without retraining.`);
                            await runtimeEvent("worker.model_transfer.recovered", {
                                role: "worker",
                                aggregator: String(state["1"]),
                                round: currentRound,
                            });
                            await waitForSubmittedRoundToAdvance(currentRound);
                            await sleep(2000);
                            continue;
                        }
                        await runtimeEvent("worker.model_transfer.failed", {
                            role: "worker",
                            aggregator: String(state["1"]),
                            error: e?.message || String(e),
                        });
                        await sleep(modelTransferRetryDelayMs);
                        continue;
                    }
                    console.log("Top contributor:", await getTopContributor());
                    console.log("Transfer complete. Send local model to aggregator.");
                    console.log("Rounds left: ", (targetRound() - Number(await getRound())));
                    console.log("Waiting till next round.");
                    try {
                        const newGM = await waitForGMUpdate(prevGM, { pollMs: gmUpdatePollMs, timeoutMs: gmUpdateTimeoutMs });
                        missedGMUpdateLoops = 0;
                        console.log("New Global Model detected:", newGM);
                    } catch (e) {
                        missedGMUpdateLoops++;
                        console.warn(`No new GM within timeout. Missed update loop ${missedGMUpdateLoops}/${gmUpdateTimeoutLoops}.`);
                        if (missedGMUpdateLoops >= gmUpdateTimeoutLoops) {
                            console.warn("Reporting aggregator timeout onchain.");
                            await runtimeEvent("worker.aggregator_timeout.reported", {
                                role: "worker",
                                missed_loops: missedGMUpdateLoops,
                            });
                            try {
                                await reportAggregatorTimeout();
                            } catch (reportError) {
                                console.error("Error reporting aggregator timeout:", reportError);
                            }
                            missedGMUpdateLoops = 0;
                        }
                    }
                    await sleep(2000);
                    continue;
                }

            case "AGGREGATING":
                if (sameAddress(state[1], process.env.ACCOUNT_ADDRESS)) {
                    await aggregateCurrentRoundAsAggregator();
                    continue;
                }
                break;

            case "UPDATING":
                if (sameAddress(state[1], process.env.ACCOUNT_ADDRESS)) {
                    await updateCurrentRoundAsAggregator();
                    continue;
                }
                //return;
                break;

            default:
                console.log("Unknown state:", state);
                currentState = "IDLE";
                await sleep(2000);
                continue; // nicht beenden
        }
    }
};

const srcModelsDir = path.join(__dirname, '../received_models');
const resultsIIDDir = path.join(__dirname, '../data/results_iid');
async function stageAggregation() {
    await fs.mkdir(resultsIIDDir, { recursive: true });

    try {
        const destEntries = await fs.readdir(resultsIIDDir);
        await Promise.all(
            destEntries
                .filter(n => n.endsWith('.bin'))
                .map(n => fs.unlink(path.join(resultsIIDDir, n)).catch(() => {}))
        );
    } catch {}

    const modelFiles = await receivedWorkerModelFiles();
    const acceptedFiles = modelFiles.filter(file => file.authorized);
    const rejectedFiles = modelFiles.filter(file => !file.authorized);

    for (const file of rejectedFiles) {
        console.warn("Ignoring received model %s (%s)", file.path, file.reason);
        await fs.unlink(file.path).catch(() => {});
    }

    if (acceptedFiles.length === 0) {
        console.log("No .bin files found in %s", srcModelsDir);
        return 0;
    }

    console.log("Received TEE-authorized model files:");
    for (const file of acceptedFiles) {
        console.log(" - %s (%s)", file.path, file.address);
    }

    return acceptedFiles.length;
}

async function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
}

async function countBinFiles(dir) {
    try {
        const entries = await fs.readdir(dir, { withFileTypes: true });
        return entries.filter(e => e.isFile() && e.name.endsWith('.bin')).length;
    } catch (e) {
        if (e && e.code === 'ENOENT') return 0;
        throw e;
    }
}

async function countAuthorizedModelFiles() {
    const files = await receivedWorkerModelFiles();
    return files.filter(file => file.authorized).length;
}

async function waitForModels(expected, { dir, pollMs = 2000, timeoutMs = 10 * 60 * 1000 } = {}) {
    const start = Date.now();
    while (true) {
        const n = dir === srcModelsDir ? await countAuthorizedModelFiles() : await countBinFiles(dir);
        if (n >= expected) {
            console.log(`Received ${n}/${expected} TEE-authorized model files.`);
            return n; // Anzahl zurückgeben
        }
        if (Date.now() - start > timeoutMs) {
            console.warn(`Timeout waiting for ${expected} TEE-authorized models. Proceeding with ${n} present in ${dir}.`);
            return n; // mit aktueller Anzahl fortfahren
        }
        await sleep(pollMs);
    }
}

async function waitForGMUpdate(prevCid, { pollMs = 3000, timeoutMs = 10 * 60 * 1000 } = {}) {
    const start = Date.now();
    while (true) {
        const cid = await getCurrentGM();
        if (cid && cid !== prevCid) return cid;
        if (Date.now() - start > timeoutMs) {
            throw new Error("Timeout waiting for aggregator signal (new Global Model).");
        }
        await sleep(pollMs);
    }
}

async function waitForRoundAdvance(prevRound, { pollMs = 3000, timeoutMs = 0 } = {}) {
    const start = Date.now();
    while (true) {
        const currentRound = Number(await getRound());
        if (currentRound > Number(prevRound)) {
            return currentRound;
        }
        if (timeoutMs > 0 && Date.now() - start > timeoutMs) {
            throw new Error(`Timeout waiting for round to advance beyond ${prevRound}.`);
        }
        await sleep(pollMs);
    }
}

async function runService() {
    try {
        await stateMachine();
    } catch (e) {
        console.error('stateMachine error:', e);
    }
}

runService();


process.on('SIGINT', () => {
    console.log('SIGINT received. Exiting.');
    stopAggregatorServer().catch(() => {});
    process.exit(0);
});
process.on('SIGTERM', () => {
    console.log('SIGTERM received. Exiting.');
    stopAggregatorServer().catch(() => {});
    process.exit(0);
});

async function stageCleaning() {
    await Promise.all([
        cleanFilesInDir(srcModelsDir),
        cleanFilesInDir(resultsIIDDir),
    ]);
    console.log("Cleaned files in %s and %s", srcModelsDir, resultsIIDDir);
}

async function cleanFilesInDir(dir) {
    try {
        const entries = await fs.readdir(dir, { withFileTypes: true });
        await Promise.all(entries.map(async (e) => {
            const full = path.join(dir, e.name);
            if (e.isFile() || e.isSymbolicLink()) {
                await fs.unlink(full).catch(() => {});
            }
        }));
    } catch (err) {
        if (err && err.code === 'ENOENT') return;
        throw err;
    }
}
