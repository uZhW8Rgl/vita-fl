// @ts-nocheck
// code adapted from pinata docs https://docs.pinata.cloud/quickstart/node-js
import 'dotenv/config';
import path from 'path';
import { fileURLToPath } from 'url';
import { getActiveModelBundle, getCurrentGM, getCurrentGMSignature, getCurrentGMKeyBundle, getCurrentState, getAggregatorEndpoint, setAggregatorEndpoint, setCurrentState, getTopContributor, triggerAggregatorSelection, reportAggregatorTimeout, getRound, getCompletedRoundCount, getLastSelectionRound, isAuthorized, isDeviceRegistrationCurrent, getCommittedRunRosterState, getDevicePublicKey, getDeviceActionKey, getDeviceRegistrationReportData, getBlockchainChainId, getMedicalSignerSnapshot, registerDeviceWithTeeQuoteAndRtmr3Events, createModelSubmissionCommitment, recordModelSubmission, openModelSubmissions, closeModelSubmissions, getRoundAggregationPolicy, hasSubmittedModel, getModelSubmissionHash, isGlobalModelPublished, isRoundCompleted, isRoundAborted, configureParticipantActionSigner, fundParticipantActionKey } from "./bc_client.js";
import { getCurrentModel, pinFile, getFileFromIPFS, updateGM } from "./ipfs.js";
import { deriveTimingConfig, nextAggregatorTimeoutTracker, selectionGapRecoveryNeeded, validateTimingConfig } from "./state_timing.js";
import {
    loadParticipantKey,
    materializeParticipantPrivateKey,
    PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH,
} from "./participant_key.js";
import {
    loadParticipantActionSigner,
} from "./action_key.js";
import { loadSelloReceiptPublicKey } from "./sello_key.js";
import { FEDERATED_AVERAGING_V1_HASH } from "./protocol_digest.js";
import { dstackHttpsEndpoint } from "./runtime_endpoints.js";
import fs from 'fs/promises';
import { existsSync } from 'fs';
import { DstackClient, TappdClient, getComposeHash } from '@phala/dstack-sdk';
import crypto from 'crypto';
import http from 'http';
import { emitTelemetryEvent } from "./telemetry.js";
import {
    buildRoundZeroBootstrapSnapshot,
    createCommittedRunRosterBinding,
    frozenRecipientsForCommittedRoster,
    normalizeBootstrapPublicKey,
    normalizeRunRosterDigest,
    requireFrozenRecipientKeysMatchRegistry,
} from "./bootstrap_snapshot.js";
import { reconcileFetchedGlobalModel } from "./model_bundle.js";
import { isExplicitlyFinalizedSourceRound } from "./finalization_recovery.js";
import {
    requireAbortedAggregatorAttemptGap,
    skippedAggregatorAttemptRounds,
} from "./parent_model_round.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const deviceID = process.env.DEVICE_ID;
let currentState = "";
let aggregatorServerRunning = false;
let modelUploadServer;
let modelUploadQueue = Promise.resolve();
const activeModelUploadHandlers = new Set();
// A round marked aborted cannot later become a successful finalized round.
// Cache only positive confirmations so repeated parent preparation (TRAINING
// and AGGREGATING) does not re-read the full immutable abort gap each time.
const verifiedAbortedAttemptRounds = new Set<number>();
const pythonServiceUrl = process.env.PYTHON_SERVICE_URL || 'http://127.0.0.1:8000';
const modelUploadPort = Number(process.env.MODEL_UPLOAD_PORT || 8001);
const maxModelUploadBytes = Number(process.env.MODEL_UPLOAD_MAX_BYTES || 25 * 1024 * 1024);
const participantPrivateKeyRuntimePath =
    process.env.PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH ||
    PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH;
let activeParticipantKey;
let activeParticipantActionSigner;
let activeSelloReceiptPublicKey;
let localTdxRegistrationDone = false;
const timingConfig = deriveTimingConfig(process.env);
const gmUpdateTimeoutMs = timingConfig.gmUpdateTimeoutMs;
const gmUpdateTimeoutLoops = timingConfig.gmUpdateTimeoutLoops;
const gmUpdatePollMs = timingConfig.gmUpdatePollMs;
const modelTransferTimeoutMs = timingConfig.modelTransferTimeoutMs;
const modelTransferRetryDelayMs = timingConfig.modelTransferRetryDelayMs;
for (const warning of validateTimingConfig(timingConfig)) {
    console.warn("Timing config warning:", warning);
}
let aggregatorTimeoutTracker = { contextKey: "", missedLoops: 0 };

function sameAddress(left, right) {
    return String(left || '').toLowerCase() === String(right || '').toLowerCase();
}

const ZERO_BYTES32 = `0x${"00".repeat(32)}`;

function sameHex(left, right) {
    return String(left || '').toLowerCase() === String(right || '').toLowerCase();
}

function targetRound() {
    return Number(process.env.ROUND || 0);
}

async function assertFetchedGlobalModelIsStillCurrent(fetchedGlobalModel) {
    const current = await getActiveModelBundle();
    const reconciled = reconcileFetchedGlobalModel(fetchedGlobalModel, current);
    console.log(
        "Fetched global model, finalized round, and publisher key match the active on-chain bundle.",
    );
    return reconciled;
}

async function runtimeEvent(name, attributes = {}) {
    await emitTelemetryEvent(name, attributes);
}

async function runOperation(name, attributes, operation) {
    return operation();
}

function resetAggregatorTimeoutTracker() {
    aggregatorTimeoutTracker = { contextKey: "", missedLoops: 0 };
}

async function recordMissedAggregatorProgress(expectedRound, expectedAggregator, reason, error = null) {
    const next = nextAggregatorTimeoutTracker({
        tracker: aggregatorTimeoutTracker,
        expectedRound,
        expectedAggregator,
        maxLoops: gmUpdateTimeoutLoops,
    });
    aggregatorTimeoutTracker = {
        contextKey: next.contextKey,
        missedLoops: next.missedLoops,
    };
    console.warn(
        `Aggregator ${next.expectedAggregator} made no observable progress for round ${next.expectedRound} ` +
        `(${reason}); failure ${next.failureCount}/${gmUpdateTimeoutLoops}.`
    );
    await runtimeEvent("worker.aggregator_progress.missed", {
        role: "worker",
        round: next.expectedRound,
        aggregator: next.expectedAggregator,
        reason,
        missed_loops: next.failureCount,
        error: error ? (error?.message || String(error)) : undefined,
    });
    if (!next.shouldReportTimeout) {
        return;
    }

    try {
        const policy = await getRoundAggregationPolicy(next.expectedRound);
        if (
            policy.opened
            && !policy.closed
            && policy.chainTimestamp <= policy.deadline
        ) {
            console.warn(
                `Deferring timeout report: round ${next.expectedRound} remains inside ` +
                `its immutable on-chain submission window ending at ${policy.deadline}.`,
            );
            return;
        }
    } catch (policyError) {
        console.warn(
            "Could not inspect the aggregation policy before timeout reporting; " +
            "deferring the report until the on-chain policy is observable:",
            policyError?.message || String(policyError),
        );
        return;
    }

    console.warn(
        `Reporting timeout for observed aggregator ${next.expectedAggregator} in round ${next.expectedRound}.`
    );
    try {
        await reportAggregatorTimeout(next.expectedRound, next.expectedAggregator);
        await runtimeEvent("worker.aggregator_timeout.reported", {
            role: "worker",
            round: next.expectedRound,
            aggregator: next.expectedAggregator,
            reason,
            missed_loops: next.failureCount,
        });
    } catch (reportError) {
        console.error("Error reporting aggregator timeout:", reportError);
        await runtimeEvent("worker.aggregator_timeout.report_failed", {
            role: "worker",
            round: next.expectedRound,
            aggregator: next.expectedAggregator,
            reason,
            missed_loops: next.failureCount,
            error: reportError?.message || String(reportError),
        });
    }
}

async function waitForSubmittedRoundToAdvance(currentRound, expectedAggregator) {
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
        resetAggregatorTimeoutTracker();
        console.log(`Round advanced from ${currentRound} to ${nextRound}.`);
    } catch (e) {
        await recordMissedAggregatorProgress(currentRound, expectedAggregator, "round_not_advanced", e);
    }
}

async function monitorNonAggregatorStateProgress(observedRound, expectedAggregator, observedState) {
    console.log(
        `Waiting for aggregator ${expectedAggregator} to advance round ${observedRound} ` +
        `from state ${observedState}.`
    );
    try {
        const nextRound = await waitForRoundAdvance(observedRound, {
            pollMs: gmUpdatePollMs,
            timeoutMs: gmUpdateTimeoutMs,
        });
        resetAggregatorTimeoutTracker();
        console.log(`Round advanced from ${observedRound} to ${nextRound}.`);
    } catch (error) {
        await recordMissedAggregatorProgress(
            observedRound,
            expectedAggregator,
            `aggregator_state_stalled_${String(observedState || "unknown").toLowerCase()}`,
            error,
        );
    }
    await sleep(2000);
}

async function waitForRoundZeroBootstrap(expectedAggregator) {
    console.log(
        `Round 0 is a bootstrap-only phase. Waiting for W0 ${expectedAggregator} `
        + 'to publish the encrypted initial model.',
    );
    await runtimeEvent('worker.round0_bootstrap.waiting', {
        role: 'worker',
        round: 0,
        aggregator: expectedAggregator,
    });
    const nextRound = await waitForRoundAdvance(0, {
        pollMs: gmUpdatePollMs,
        timeoutMs: 0,
    });
    console.log(`Round-0 bootstrap completed; on-chain round is now ${nextRound}.`);
}

async function recoverSelectionGap(observedRound, expectedAggregator, observedState) {
    const [lastSelectionRound, completedRounds] = await Promise.all([
        getLastSelectionRound(),
        getCompletedRoundCount(),
    ]);
    if (Number(completedRounds) >= targetRound()) {
        resetAggregatorTimeoutTracker();
        console.log(
            `Configured target of ${targetRound()} successful rounds is already complete; ` +
            "no further aggregator selection is required."
        );
        return "training-complete";
    }
    if (!selectionGapRecoveryNeeded({
        state: observedState,
        observedRound,
        lastSelectionRound: Number(lastSelectionRound),
        completedRounds: Number(completedRounds),
        targetRounds: targetRound(),
    })) {
        return null;
    }

    const [latestState, latestRound] = await Promise.all([
        getCurrentState(),
        getRound(),
    ]);
    if (
        Number(latestRound) !== Number(observedRound)
        || String(latestState[0]) !== "UPDATING"
        || !sameAddress(latestState[1], expectedAggregator)
    ) {
        if (
            Number(latestRound) === Number(observedRound)
            && String(latestState[0]) === "TRAINING"
            && Number(await getLastSelectionRound()) === Number(observedRound)
        ) {
            resetAggregatorTimeoutTracker();
            return true;
        }
        return null;
    }

    console.warn(
        `Detected an unfinished aggregator-selection transition for round ${observedRound}; ` +
        "attempting authorized recovery."
    );
    try {
        await triggerAggregatorSelection();
    } catch (error) {
        const reconciledState = await getCurrentState().catch(() => null);
        const reconciledRound = Number(await getRound().catch(() => -1));
        const reconciledSelectionRound = Number(await getLastSelectionRound().catch(() => -1));
        if (
            reconciledState
            && reconciledRound === Number(observedRound)
            && String(reconciledState[0]) === "TRAINING"
            && reconciledSelectionRound === Number(observedRound)
        ) {
            console.log("Another authorized worker completed the aggregator-selection recovery.");
        } else {
            console.error("Aggregator-selection gap recovery failed:", error);
            return false;
        }
    }

    resetAggregatorTimeoutTracker();
    await runtimeEvent("worker.aggregator_selection_gap.recovered", {
        role: "worker",
        round: Number(observedRound),
        previous_aggregator: String(expectedAggregator),
    });
    return true;
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
    const server = modelUploadServer;
    modelUploadServer = undefined;
    if (server) {
        await new Promise((resolve, reject) => {
            server.close((error) => error ? reject(error) : resolve());
        });
        while (activeModelUploadHandlers.size > 0) {
            await Promise.allSettled(Array.from(activeModelUploadHandlers));
        }
        console.log("Authenticated model upload server stopped.");
    }
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
    if (!activeParticipantKey) {
        throw new Error('Participant key has not been initialized.');
    }
    return activeParticipantKey.publicKeyDerHex;
}

function modelBytesHash(data) {
    return `0x${crypto.createHash('sha256').update(data).digest('hex')}`;
}

async function modelFileHash(filePath) {
    return modelBytesHash(await fs.readFile(filePath));
}

function uint256Bytes(value, label) {
    const integer = BigInt(value);
    if (integer < 0n || integer >= (1n << 256n)) {
        throw new Error(`${label} is outside the uint256 range`);
    }
    return Buffer.from(integer.toString(16).padStart(64, '0'), 'hex');
}

function addressBytes(value, label) {
    const address = String(value || '');
    if (!/^0x[0-9a-fA-F]{40}$/.test(address)) {
        throw new Error(`${label} must be a 20-byte Ethereum address`);
    }
    return Buffer.from(address.slice(2), 'hex');
}

function sha256Bytes(value) {
    return crypto.createHash('sha256').update(value).digest();
}

function modelUploadSigningPayload({
    packageBytes,
    round,
    chainId,
    gmStorageAddress,
    aggregatorAddress,
    workerAddress,
    parentModelCid,
    parentSignatureCid,
    parentKeyBundleCid,
}) {
    return Buffer.concat([
        sha256Bytes(Buffer.from('VITAFL_MODEL_UPLOAD_V1', 'utf8')),
        uint256Bytes(round, 'model upload round'),
        uint256Bytes(chainId, 'model upload chain id'),
        addressBytes(gmStorageAddress, 'GMStorage address'),
        addressBytes(aggregatorAddress, 'expected aggregator'),
        addressBytes(workerAddress, 'worker address'),
        sha256Bytes(Buffer.from(String(parentModelCid), 'utf8')),
        sha256Bytes(Buffer.from(String(parentSignatureCid), 'utf8')),
        sha256Bytes(Buffer.from(String(parentKeyBundleCid), 'utf8')),
        sha256Bytes(packageBytes),
    ]);
}

function gatewayDomainFromEnvironment() {
    const explicit = String(process.env.DSTACK_GATEWAY_DOMAIN || '').trim().replace(/^\./, '');
    if (explicit) return explicit;
    const kuboHost = new URL(String(process.env.KUBO_API || '')).hostname;
    const firstDot = kuboHost.indexOf('.');
    if (firstDot < 0) throw new Error('Cannot derive the Phala gateway domain from KUBO_API');
    return kuboHost.slice(firstDot + 1);
}

function ownModelUploadEndpoint(appId) {
    return dstackHttpsEndpoint({
        appId,
        port: modelUploadPort,
        gatewayDomain: gatewayDomainFromEnvironment(),
    });
}

function teeInferenceEnabled() {
    return /^(?:1|true|yes)$/i.test(String(process.env.TEE_INFERENCE_ENABLED || '').trim());
}

function currentSelloReceiptKey() {
    if (!teeInferenceEnabled()) return ZERO_BYTES32;
    if (!Buffer.isBuffer(activeSelloReceiptPublicKey) || activeSelloReceiptPublicKey.length !== 32) {
        throw new Error("TEE inference is enabled but its app-bound Sello receipt key is unavailable.");
    }
    return `0x${activeSelloReceiptPublicKey.toString("hex")}`;
}

function ownTeeInferenceEndpoint(appId) {
    return dstackHttpsEndpoint({
        appId,
        port: 8080,
        gatewayDomain: gatewayDomainFromEnvironment(),
    });
}

async function uploadLocalModel(
    endpoint,
    deviceId,
    expectedRound,
    expectedAggregator,
    parentModel,
) {
    const packageBytes = await fs.readFile('./data/lm.bin.enc');
    const modelHash = normalizeHashHex(
        await modelFileHash('./data/lm.bin'),
        32,
        'local plaintext model SHA-256',
    );
    const packageHash = normalizeHashHex(
        modelBytesHash(packageBytes),
        32,
        'encrypted model package SHA-256',
    );
    const commitment = await createModelSubmissionCommitment(
        expectedRound,
        deviceId,
        modelHash,
        packageHash,
    );
    if (!sameAddress(commitment.aggregatorAddress, expectedAggregator)) {
        throw new Error(
            `Submission commitment targets aggregator ${commitment.aggregatorAddress}, not ${expectedAggregator}.`,
        );
    }
    const chainId = await getBlockchainChainId();
    const signingPayload = modelUploadSigningPayload({
        packageBytes,
        round: expectedRound,
        chainId,
        gmStorageAddress: process.env.GM_STORAGE_ADDRESS,
        aggregatorAddress: expectedAggregator,
        workerAddress: deviceId,
        parentModelCid: parentModel.modelCid,
        parentSignatureCid: parentModel.sigCid,
        parentKeyBundleCid: parentModel.keyBundleCid,
    });
    if (!activeParticipantKey) {
        throw new Error('Participant key has not been initialized.');
    }
    const signature = crypto.sign(
        'sha256',
        signingPayload,
        activeParticipantKey.privateKey,
    ).toString('base64');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), modelTransferTimeoutMs);
    try {
        const response = await fetch(`${String(endpoint).replace(/\/+$/, '')}/model`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                device_id: deviceId,
                expected_round: expectedRound,
                expected_aggregator: expectedAggregator,
                parent_model_cid: parentModel.modelCid,
                parent_signature_cid: parentModel.sigCid,
                parent_key_bundle_cid: parentModel.keyBundleCid,
                package_base64: packageBytes.toString('base64'),
                signature_base64: signature,
                model_sha256: commitment.modelHash,
                package_sha256: commitment.packageHash,
                parent_model_hash: commitment.parentModelHash,
                submission_nonce: commitment.workerNonce,
                action_signature: commitment.workerSignature,
            }),
            signal: controller.signal,
        });
        const body = await response.text();
        if (!response.ok) throw new Error(`aggregator model endpoint failed (${response.status}): ${body}`);
    } finally {
        clearTimeout(timeout);
    }
}

async function waitForAggregatorModelEndpoint(expectedAggregator) {
    const deadline = Date.now() + modelTransferTimeoutMs;
    while (Date.now() < deadline) {
        const latestState = await getCurrentState();
        if (!sameAddress(latestState[1], expectedAggregator)) {
            throw new Error(`Aggregator changed from ${expectedAggregator} to ${latestState[1]} before model transfer`);
        }
        const endpoint = String(await getAggregatorEndpoint());
        if (endpoint.startsWith('https://')) return endpoint;
        await sleep(500);
    }
    throw new Error(`Aggregator ${expectedAggregator} did not publish an HTTPS model endpoint within ${modelTransferTimeoutMs}ms`);
}

async function handleModelUpload(request, response) {
    const reply = (status, payload) => {
        const body = Buffer.from(JSON.stringify(payload));
        response.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': body.length });
        response.end(body);
    };
    if (request.method === 'GET' && request.url === '/health') return reply(200, { ok: true });
    if (request.method !== 'POST' || request.url !== '/model') return reply(404, { ok: false, error: 'not found' });
    let releaseUpload = () => {};
    try {
        const chunks = [];
        let length = 0;
        for await (const chunk of request) {
            length += chunk.length;
            if (length > maxModelUploadBytes) throw new Error('model upload exceeds size limit');
            chunks.push(chunk);
        }
        const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        const previousUpload = modelUploadQueue;
        modelUploadQueue = new Promise((resolve) => {
            releaseUpload = resolve;
        });
        await previousUpload;
        const deviceId = String(payload.device_id || '').toLowerCase();
        const expectedAggregator = String(payload.expected_aggregator || '');
        const expectedRound = Number(payload.expected_round);
        const parentModel = {
            modelCid: String(payload.parent_model_cid || ''),
            sigCid: String(payload.parent_signature_cid || ''),
            keyBundleCid: String(payload.parent_key_bundle_cid || ''),
        };
        const [chainState, currentRound, currentModelCid, currentSignatureCid, currentKeyBundleCid, chainId] =
            await Promise.all([
                getCurrentState(),
                getRound(),
                getCurrentGM(),
                getCurrentGMSignature(),
                getCurrentGMKeyBundle(),
                getBlockchainChainId(),
            ]);
        if (!sameAddress(chainState[1], process.env.ACCOUNT_ADDRESS)) {
            return reply(409, { ok: false, error: 'this worker is not the current aggregator' });
        }
        if (!['TRAINING', 'AGGREGATING'].includes(String(chainState[0]))) {
            return reply(409, {
                ok: false,
                error: `model uploads are closed while the system is ${String(chainState[0])}`,
            });
        }
        if (!sameAddress(expectedAggregator, chainState[1])) {
            return reply(409, { ok: false, error: 'signed upload targets a different aggregator' });
        }
        if (!Number.isSafeInteger(expectedRound) || expectedRound < 0 || expectedRound !== Number(currentRound)) {
            return reply(409, { ok: false, error: 'signed upload targets a different round' });
        }
        if (
            !parentModel.modelCid ||
            parentModel.modelCid !== String(currentModelCid) ||
            parentModel.sigCid !== String(currentSignatureCid) ||
            parentModel.keyBundleCid !== String(currentKeyBundleCid)
        ) {
            return reply(409, { ok: false, error: 'signed upload targets a stale global-model bundle' });
        }
        if (!/^0x[0-9a-fA-F]{40}$/.test(deviceId) || !(await isAuthorized(deviceId))) {
            return reply(403, { ok: false, error: 'device is not authorized' });
        }
        const alreadySubmitted = await hasSubmittedModel(expectedRound, deviceId);
        const recordedModelHash = alreadySubmitted
            ? normalizeHashHex(
                await getModelSubmissionHash(expectedRound, deviceId),
                32,
                "recorded decrypted model SHA-256",
            )
            : null;
        const packageBytes = Buffer.from(String(payload.package_base64 || ''), 'base64');
        const packageHash = normalizeHashHex(
            payload.package_sha256,
            32,
            'worker-committed encrypted package SHA-256',
        );
        if (packageHash !== normalizeHashHex(
            modelBytesHash(packageBytes),
            32,
            'received encrypted package SHA-256',
        )) {
            return reply(403, { ok: false, error: 'worker commitment package hash mismatch' });
        }
        const committedModelHash = normalizeHashHex(
            payload.model_sha256,
            32,
            'worker-committed plaintext model SHA-256',
        );
        const parentModelHash = normalizeHashHex(
            payload.parent_model_hash,
            32,
            'worker-committed parent bundle hash',
        );
        const submissionNonce = String(payload.submission_nonce ?? '');
        if (!/^(?:0|[1-9][0-9]*)$/.test(submissionNonce)) {
            return reply(400, { ok: false, error: 'invalid worker submission nonce' });
        }
        const actionSignature = String(payload.action_signature || '');
        if (!/^0x[0-9a-fA-F]{130}$/.test(actionSignature)) {
            return reply(400, { ok: false, error: 'invalid worker action signature encoding' });
        }
        const signature = Buffer.from(String(payload.signature_base64 || ''), 'base64');
        const publicKeyDer = Buffer.from(String(await getDevicePublicKey(deviceId)).replace(/^0x/, ''), 'hex');
        const publicKey = crypto.createPublicKey({ key: publicKeyDer, format: 'der', type: 'spki' });
        const signingPayload = modelUploadSigningPayload({
            packageBytes,
            round: expectedRound,
            chainId,
            gmStorageAddress: process.env.GM_STORAGE_ADDRESS,
            aggregatorAddress: expectedAggregator,
            workerAddress: deviceId,
            parentModelCid: parentModel.modelCid,
            parentSignatureCid: parentModel.sigCid,
            parentKeyBundleCid: parentModel.keyBundleCid,
        });
        if (!crypto.verify('sha256', signingPayload, publicKey, signature)) {
            return reply(403, { ok: false, error: 'invalid model package signature' });
        }
        const acceptedModel = await callPythonService('/model/receive', {
            device_id: deviceId,
            package_base64: packageBytes.toString('base64'),
            expected_model_sha256: recordedModelHash,
            private_key: participantPrivateKeyRuntimePath,
        }, { timeoutMs: modelTransferTimeoutMs });
        const modelHash = normalizeHashHex(
            acceptedModel.model_sha256,
            32,
            "decrypted model SHA-256",
        );
        if (modelHash !== committedModelHash) {
            return reply(403, { ok: false, error: 'worker commitment plaintext model hash mismatch' });
        }
        await recordModelSubmission({
            expectedRound,
            workerAddress: deviceId,
            aggregatorAddress: expectedAggregator,
            parentModelHash,
            modelHash,
            packageHash,
            workerNonce: submissionNonce,
            workerSignature: actionSignature,
        });
        return reply(200, {
            ok: true,
            round: expectedRound,
            model_hash: modelHash,
        });
    } catch (error) {
        console.error('Model upload rejected:', error);
        return reply(400, { ok: false, error: error?.message || String(error) });
    } finally {
        releaseUpload();
    }
}

async function startModelUploadServer() {
    if (modelUploadServer) return;
    const server = http.createServer((request, response) => {
        const task = handleModelUpload(request, response).catch((error) => {
            console.error("Unhandled authenticated model-upload error:", error);
            if (!response.headersSent && !response.destroyed) {
                const body = Buffer.from(JSON.stringify({ ok: false, error: error?.message || String(error) }));
                response.writeHead(500, {
                    'Content-Type': 'application/json',
                    'Content-Length': body.length,
                });
                response.end(body);
            }
        });
        activeModelUploadHandlers.add(task);
        void task.finally(() => activeModelUploadHandlers.delete(task));
    });
    modelUploadServer = server;
    try {
        await new Promise((resolve, reject) => {
            server.once('error', reject);
            server.listen(modelUploadPort, '0.0.0.0', resolve);
        });
    } catch (error) {
        if (modelUploadServer === server) modelUploadServer = undefined;
        throw error;
    }
    console.log(`Authenticated model upload server listening on port ${modelUploadPort}.`);
}

function serializePhalaEventLog(eventLog) {
    if (typeof eventLog === 'string') {
        return eventLog.endsWith('\n') ? eventLog : `${eventLog}\n`;
    }
    if (Array.isArray(eventLog)) {
        return `${eventLog.map(event => JSON.stringify(event)).join('\n')}\n`;
    }
    return `${JSON.stringify(eventLog, null, 2)}\n`;
}

function workerArtifactName() {
    const raw = process.env.DEVICE_ID !== undefined
        ? `worker-${process.env.DEVICE_ID}`
        : String(process.env.ACCOUNT_ADDRESS || 'worker').toLowerCase();
    return raw.replace(/[^a-zA-Z0-9._-]/g, '-');
}

async function readFirstExistingText(paths) {
    for (const candidate of paths.filter(Boolean)) {
        try {
            return {
                path: candidate,
                text: await fs.readFile(candidate, 'utf8'),
            };
        } catch (error) {
            if (error?.code !== 'ENOENT') {
                console.warn(`Could not read Phala app-code candidate ${candidate}:`, error?.message || error);
            }
        }
    }
    return null;
}

function appCodeFromDstackInfo(info) {
    if (!info || typeof info !== 'object') return null;
    if (info.tcb_info?.app_compose !== undefined) {
        const appCompose = info.tcb_info.app_compose;
        if (typeof appCompose === 'string') return appCompose;
        return `${JSON.stringify(appCompose, null, 2)}\n`;
    }
    for (const key of ['app_code', 'appCode', 'app_compose', 'appCompose', 'compose', 'docker_compose_file']) {
        if (info[key] === undefined) continue;
        if (typeof info[key] === 'string') return info[key];
        return `${JSON.stringify(info[key], null, 2)}\n`;
    }
    return null;
}

async function discoverLivePhalaAppCode(info) {
    const fromInfo = appCodeFromDstackInfo(info);
    if (fromInfo) {
        return { source: 'dstack.info', text: fromInfo.endsWith('\n') ? fromInfo : `${fromInfo}\n` };
    }
    const discovered = await readFirstExistingText([
        process.env.PHALA_LIVE_APP_CODE_PATH,
        '/dstack/app_code.txt',
        '/dstack/app-code.txt',
        '/dstack/app_compose.json',
        '/dstack/app-compose.json',
        '/etc/dstack/app_code.txt',
        '/etc/dstack/app-compose.json',
        './app_code.txt',
        './app-compose.json',
    ]);
    return discovered ? { source: discovered.path, text: discovered.text } : null;
}

async function kuboMfsWriteText(apiBase, mfsPath, text) {
    const form = new FormData();
    form.append('file', new Blob([text], { type: 'text/plain' }), 'file');
    const url = `${apiBase}/api/v0/files/write?arg=${encodeURIComponent(mfsPath)}&create=true&truncate=true&parents=true`;
    const response = await fetch(url, { method: 'POST', body: form });
    if (!response.ok) {
        throw new Error(`Kubo MFS write failed for ${mfsPath} (${response.status}): ${await response.text()}`);
    }
}

async function publishLivePhalaArtifacts(quote, info = null) {
    if (process.env.PHALA_PUBLISH_ATTESTATION_ARTIFACTS === '0') return;
    const kuboApi = String(process.env.KUBO_API || '').replace(/\/+$/, '');
    if (!kuboApi) {
        console.warn('KUBO_API is unset; skipping live Phala attestation artifact export.');
        return;
    }

    const workerName = workerArtifactName();
    const eventLogText = serializePhalaEventLog(quote.event_log);
    const appCode = await discoverLivePhalaAppCode(info);
    const writes = [
        [`/phala-artifacts/${workerName}/rtmr3_event_log.txt`, eventLogText],
        ['/phala-artifacts/latest/rtmr3_event_log.txt', eventLogText],
    ];
    if (appCode) {
        writes.push([`/phala-artifacts/${workerName}/app_code.txt`, appCode.text]);
        writes.push(['/phala-artifacts/latest/app_code.txt', appCode.text]);
    }
    if (info) {
        writes.push([`/phala-artifacts/${workerName}/dstack_info.json`, `${JSON.stringify(info, null, 2)}\n`]);
    }

    for (const [mfsPath, text] of writes) {
        await kuboMfsWriteText(kuboApi, mfsPath, text);
    }
    console.log('Published live Phala attestation artifacts to Kubo MFS:', {
        worker: workerName,
        appCodeSource: appCode?.source || null,
        basePath: `/phala-artifacts/${workerName}`,
        latestPath: '/phala-artifacts/latest',
    });
}

function normalizeHashHex(value, bytes, label) {
    if (typeof value !== 'string') {
        throw new Error(`${label} must be a hex string`);
    }
    let hex = value.trim();
    if (hex.startsWith('0x') || hex.startsWith('0X')) hex = hex.slice(2);
    if (!new RegExp(`^[0-9a-fA-F]{${bytes * 2}}$`).test(hex)) {
        throw new Error(`Invalid ${label}; expected ${bytes}-byte hex value`);
    }
    return `0x${hex.toLowerCase()}`;
}

function parseAppCompose(appComposeRaw) {
    if (!appComposeRaw) {
        throw new Error('dstack info did not include app_compose');
    }
    if (typeof appComposeRaw === 'string') {
        return JSON.parse(appComposeRaw);
    }
    if (typeof appComposeRaw === 'object') {
        return appComposeRaw;
    }
    throw new Error('Unsupported app_compose encoding in dstack info');
}

function sortComposeValue(value) {
    if (value === undefined || value === null) return value;
    if (Array.isArray(value)) return value.map(sortComposeValue);
    if (value && typeof value === 'object' && value.constructor === Object) {
        return Object.keys(value).sort().reduce((result, key) => {
            result[key] = sortComposeValue(value[key]);
            return result;
        }, {});
    }
    return value;
}

function canonicalAppComposeBytes(appCompose) {
    const normalized = { ...appCompose };
    if (normalized.runner === 'bash' && 'docker_compose_file' in normalized) {
        delete normalized.docker_compose_file;
    } else if (normalized.runner === 'docker-compose' && 'bash_script' in normalized) {
        delete normalized.bash_script;
    }
    if ('pre_launch_script' in normalized && !normalized.pre_launch_script) {
        delete normalized.pre_launch_script;
    }
    const deterministicJson = JSON.stringify(sortComposeValue(normalized), (_key, value) => {
        return typeof value === 'number' && !Number.isFinite(value) ? null : value;
    });
    return Buffer.from(deterministicJson, 'utf8');
}

function measuredAppCompose(appComposeRaw) {
    const appCompose = parseAppCompose(appComposeRaw);
    // dstack measures SHA-256 over the exact app-compose file bytes and exposes
    // that same raw string through info(). Preserve it instead of serializing it
    // again. Object-valued responses only exist on legacy/mock SDK paths.
    const bytes = typeof appComposeRaw === 'string'
        ? Buffer.from(appComposeRaw, 'utf8')
        : canonicalAppComposeBytes(appCompose);
    return { appCompose, bytes };
}

function tcbInfoFromDstackInfo(info) {
    return info?.tcb_info || info?.tcbInfo || info?.tcb || null;
}

function measuredAppComposeIdentity(info) {
    const tcbInfo = tcbInfoFromDstackInfo(info);
    const appComposeRaw = tcbInfo?.app_compose || tcbInfo?.appCompose;
    const { appCompose, bytes: canonicalAppCompose } = measuredAppCompose(appComposeRaw);
    const measuredComposeHash = normalizeHashHex(
        crypto.createHash('sha256').update(canonicalAppCompose).digest('hex'),
        32,
        'measured app_compose hash',
    );
    const reportedComposeHash = tcbInfo?.compose_hash || info?.compose_hash;
    if (reportedComposeHash) {
        const normalizedReportedHash = normalizeHashHex(reportedComposeHash, 32, 'dstack info compose hash');
        if (measuredComposeHash !== normalizedReportedHash) {
            throw new Error(`Raw app_compose hash ${measuredComposeHash} does not match dstack info hash ${normalizedReportedHash}`);
        }
    }
    const sdkComposeHash = normalizeHashHex(getComposeHash(appCompose, false), 32, 'SDK app_compose hash');
    if (measuredComposeHash !== sdkComposeHash) {
        console.warn('Raw measured app_compose is not in SDK canonical JSON form; the quote-bound raw hash remains authoritative.', {
            measuredComposeHash,
            sdkComposeHash,
        });
    }
    return {
        composeHash: measuredComposeHash,
        canonicalAppCompose: `0x${canonicalAppCompose.toString('hex')}`,
    };
}

function verifyMeasuredAppCompose(info, liveComposeHash) {
    const identity = measuredAppComposeIdentity(info);
    const quoteComposeHash = normalizeHashHex(liveComposeHash, 32, 'live quote compose hash');
    if (identity.composeHash !== quoteComposeHash) {
        throw new Error(`dstack info compose hash ${identity.composeHash} does not match quote compose hash ${quoteComposeHash}`);
    }

    console.log('Verified exact measured app_compose against the live quote event:', {
        composeHash: identity.composeHash,
    });
    return identity;
}

async function boundReportData(reportDataFactory, identity) {
    const value = await reportDataFactory(identity);
    const reportData = Buffer.isBuffer(value)
        ? value
        : Buffer.from(String(value || '').replace(/^0x/i, ''), 'hex');
    if (reportData.length !== 64) {
        throw new Error(`Registration REPORTDATA must be exactly 64 bytes, got ${reportData.length}`);
    }
    return reportData;
}

async function fetchLivePhalaQuote(reportDataFactory) {
    const dstackSock = '/var/run/dstack.sock';
    const tappdSock = '/var/run/tappd.sock';

    if (existsSync(dstackSock)) {
        const client = new DstackClient(dstackSock);
        const info = await client.info();
        console.log('App ID:', info.app_id);
        console.log('Instance ID:', info.instance_id);
        console.log('App Name:', info.app_name);
        console.log('TCB Info:', info.tcb_info);
        const identity = measuredAppComposeIdentity(info);
        const reportData = await boundReportData(reportDataFactory, identity);
        const quote = await client.getQuote(reportData);
        await publishLivePhalaArtifacts(quote, info).catch(error => {
            console.warn('Could not publish live Phala attestation artifacts:', error?.message || error);
        });
        const rtmr3Policy = normalizeRtmr3EventPolicy(quote.event_log);
        const appComposeIdentity = verifyMeasuredAppCompose(info, rtmr3Policy.composeHash);
        return {
            quoteHex: normalizeHexBytes(quote.quote),
            rtmr3EventLog: rtmr3Policy.events,
            composeHash: rtmr3Policy.composeHash,
            canonicalAppCompose: appComposeIdentity.canonicalAppCompose,
        };
    }

    if (existsSync(tappdSock)) {
        console.log('Using legacy Phala tappd.sock attestation path.');
        const client = new TappdClient(tappdSock);
        const info = typeof client.info === 'function' ? await client.info().catch(error => {
            console.warn('Legacy tappd.sock info() unavailable:', error?.message || error);
            return null;
        }) : null;
        if (!tcbInfoFromDstackInfo(info)?.app_compose && !tcbInfoFromDstackInfo(info)?.appCompose) {
            throw new Error('Legacy tappd.sock path did not expose app_compose; mount /var/run/dstack.sock for measured image verification');
        }
        const identity = measuredAppComposeIdentity(info);
        const reportData = await boundReportData(reportDataFactory, identity);
        const quote = await client.tdxQuote(reportData, 'raw');
        await publishLivePhalaArtifacts(quote, info).catch(error => {
            console.warn('Could not publish live Phala attestation artifacts:', error?.message || error);
        });
        const rtmr3Policy = normalizeRtmr3EventPolicy(quote.event_log);
        const appComposeIdentity = verifyMeasuredAppCompose(info, rtmr3Policy.composeHash);
        return {
            quoteHex: normalizeHexBytes(quote.quote),
            rtmr3EventLog: rtmr3Policy.events,
            composeHash: rtmr3Policy.composeHash,
            canonicalAppCompose: appComposeIdentity.canonicalAppCompose,
        };
    }

    throw new Error('Neither /var/run/dstack.sock nor /var/run/tappd.sock is available for Phala attestation.');
}

async function currentPhalaIdentity() {
    if (existsSync('/var/run/dstack.sock')) {
        const client = new DstackClient('/var/run/dstack.sock');
        const info = await client.info();
        return { ...measuredAppComposeIdentity(info), appId: info?.app_id || info?.appId };
    }
    if (existsSync('/var/run/tappd.sock')) {
        const client = new TappdClient('/var/run/tappd.sock');
        const info = typeof client.info === 'function' ? await client.info() : null;
        if (!tcbInfoFromDstackInfo(info)?.app_compose && !tcbInfoFromDstackInfo(info)?.appCompose) {
            throw new Error('Legacy tappd.sock path did not expose app_compose');
        }
        return { ...measuredAppComposeIdentity(info), appId: info?.app_id || info?.appId };
    }
    throw new Error('No dstack attestation socket is available');
}

function positiveRuntimeDuration(name, fallback, minimum) {
    const parsed = Number(process.env[name]);
    if (!Number.isFinite(parsed) || parsed < minimum) return fallback;
    return Math.floor(parsed);
}

async function readRuntimeMfsJson(markerPath, timeoutMs) {
    const kuboApi = String(process.env.KUBO_API || '').trim().replace(/\/+$/, '');
    if (!kuboApi) throw new Error('KUBO_API is required while reading runtime bootstrap data');
    const url = new URL(`${kuboApi}/api/v0/files/read`);
    url.searchParams.set('arg', markerPath);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, {
            method: 'POST',
            signal: controller.signal,
        });
        if (!response.ok) {
            throw new Error(
                `Kubo MFS marker ${markerPath} returned HTTP ${response.status}`,
            );
        }
        return await response.json();
    } finally {
        clearTimeout(timeout);
    }
}

function normalizeBootstrapRecipientAddresses(declaration) {
    if (!declaration || declaration.status !== 'declared') {
        throw new Error(
            'bootstrap recipient declaration does not contain status=declared',
        );
    }
    if (!Array.isArray(declaration.recipients) || declaration.recipients.length === 0) {
        throw new Error(
            'bootstrap recipient declaration must contain at least one recipient',
        );
    }
    const addresses = declaration.recipients.map((address, index) => {
        const normalized = String(address || '').trim().toLowerCase();
        if (!/^0x[0-9a-f]{40}$/.test(normalized)) {
            throw new Error(`invalid bootstrap recipient ${index}: ${address}`);
        }
        return normalized;
    });
    if (new Set(addresses).size !== addresses.length) {
        throw new Error('bootstrap recipient declaration contains duplicates');
    }
    return addresses;
}

async function currentCommittedRunRosterContext({ requireFrozen = false } = {}) {
    const [liveChainIdRaw, rosterState] = await Promise.all([
        getBlockchainChainId(),
        getCommittedRunRosterState(),
    ]);
    const liveChainId = Number(liveChainIdRaw);
    if (!Number.isSafeInteger(liveChainId) || liveChainId <= 0) {
        throw new Error(`live blockchain returned an invalid chain id: ${liveChainIdRaw}`);
    }
    const registryAddress = String(process.env.REGISTRY_ADDRESS || '')
        .trim()
        .toLowerCase();
    if (!/^0x[0-9a-f]{40}$/.test(registryAddress)) {
        throw new Error('REGISTRY_ADDRESS is invalid during round-0 bootstrap');
    }
    if (!rosterState?.committed) {
        throw new Error('DeviceRegistry run roster is not committed');
    }
    const binding = createCommittedRunRosterBinding({
        chainId: liveChainId,
        registryAddress,
        rosterDigest: rosterState.digest,
        recipientAddresses: rosterState.roster,
    });
    const registeredWorkerCount = Number(rosterState.registeredWorkerCount);
    if (
        !Number.isSafeInteger(registeredWorkerCount)
        || registeredWorkerCount < 0
        || registeredWorkerCount > binding.workerCount
    ) {
        throw new Error(
            `DeviceRegistry returned invalid registered run-member count: `
            + `${rosterState.registeredWorkerCount}`,
        );
    }
    if (
        Number(process.env.WORKER_COUNT || binding.workerCount)
        !== binding.workerCount
    ) {
        throw new Error(
            `committed run-roster count ${binding.workerCount} does not match `
            + 'the selected worker count',
        );
    }
    if (!sameAddress(binding.bootstrapWorker, process.env.ACCOUNT_ADDRESS)) {
        throw new Error(
            'round-0 bootstrap must be executed by committed run-roster worker W0',
        );
    }
    const frozen = rosterState.frozen === true;
    if (frozen && registeredWorkerCount !== binding.workerCount) {
        throw new Error(
            'DeviceRegistry froze an incomplete committed run roster',
        );
    }
    if (requireFrozen && !frozen) {
        throw new Error(
            `DeviceRegistry run roster is not frozen `
            + `(${registeredWorkerCount}/${binding.workerCount} registered)`,
        );
    }
    return { binding, frozen, registeredWorkerCount };
}

async function validateBootstrapDeclarationAgainstCommittedRoster(
    binding,
    timeoutMs = 5_000,
) {
    const declaration = await readRuntimeMfsJson(
        '/runtime/bootstrap-recipients.json',
        timeoutMs,
    );
    const addresses = normalizeBootstrapRecipientAddresses(declaration);
    if (
        Number(declaration.worker_count) !== addresses.length
        || addresses.length !== binding.workerCount
    ) {
        throw new Error(
            `bootstrap recipient count ${addresses.length} does not match `
            + `the committed run-roster count ${binding.workerCount}`,
        );
    }
    if (
        !sameAddress(declaration.bootstrap_worker, binding.bootstrapWorker)
        || !sameAddress(binding.bootstrapWorker, process.env.ACCOUNT_ADDRESS)
    ) {
        throw new Error(
            'bootstrap declaration does not select committed run-roster worker W0',
        );
    }
    if (Number(declaration.chain_id) !== binding.chainId) {
        throw new Error('bootstrap declaration has the wrong chain id');
    }
    if (!sameAddress(declaration.registry_address, binding.registryAddress)) {
        throw new Error(
            'bootstrap declaration has the wrong DeviceRegistry address',
        );
    }
    if (
        normalizeRunRosterDigest(
            declaration.onchain_roster_digest,
            'bootstrap declaration on-chain roster digest',
        ) !== binding.rosterDigest
    ) {
        throw new Error(
            'bootstrap declaration does not match the committed run-roster digest',
        );
    }
    if (JSON.stringify(addresses) !== JSON.stringify(binding.recipientAddresses)) {
        throw new Error(
            'bootstrap declaration does not match the exact ordered committed run roster',
        );
    }
    return declaration;
}

async function readBootstrapRecipientKeys(addresses) {
    const recipients = [];
    for (const address of addresses) {
        const publicKeyDerHex = normalizeBootstrapPublicKey(
            await getDevicePublicKey(address),
            `registered bootstrap recipient ${address} RSA public key`,
        );
        recipients.push({ address, publicKeyDerHex });
    }
    return recipients;
}

async function waitForRegisteredBootstrapRecipients() {
    const timeoutMs = positiveRuntimeDuration(
        'RUNTIME_BOOTSTRAP_TIMEOUT_MS',
        10 * 60 * 1000,
        1_000,
    );
    const pollMs = positiveRuntimeDuration('RUNTIME_BOOTSTRAP_POLL_MS', 2_000, 100);
    const deadline = Date.now() + timeoutMs;
    let attempt = 0;
    let lastError;
    while (Date.now() < deadline) {
        attempt++;
        try {
            const remaining = Math.max(1, deadline - Date.now());
            const firstContext = await currentCommittedRunRosterContext();
            await validateBootstrapDeclarationAgainstCommittedRoster(
                firstContext.binding,
                Math.min(5_000, remaining),
            );
            if (!firstContext.frozen) {
                throw new Error(
                    `DeviceRegistry run roster registration is incomplete `
                    + `(${firstContext.registeredWorkerCount}/`
                    + `${firstContext.binding.workerCount})`,
                );
            }
            const firstRecipients = await readBootstrapRecipientKeys(
                firstContext.binding.recipientAddresses,
            );

            // Re-read the immutable commitment and every registered key before
            // committing the snapshot. A changing or mixed view fails closed.
            const finalContext = await currentCommittedRunRosterContext({
                requireFrozen: true,
            });
            frozenRecipientsForCommittedRoster(
                buildRoundZeroBootstrapSnapshot(
                    firstContext.binding,
                    firstRecipients,
                ),
                finalContext.binding,
            );
            const secondRecipients = await readBootstrapRecipientKeys(
                finalContext.binding.recipientAddresses,
            );
            if (JSON.stringify(firstRecipients) !== JSON.stringify(secondRecipients)) {
                throw new Error(
                    'selected worker RSA keys changed while the round-0 roster was frozen',
                );
            }
            await validateBootstrapDeclarationAgainstCommittedRoster(
                finalContext.binding,
                Math.min(5_000, Math.max(1, deadline - Date.now())),
            );
            console.log(
                `All ${firstContext.binding.workerCount} committed workers are registered `
                + 'and the frozen round-0 RSA-key snapshot is stable.',
            );
            return {
                binding: finalContext.binding,
                recipients: firstRecipients,
            };
        } catch (error) {
            lastError = error;
            if (attempt === 1 || attempt % 15 === 0) {
                console.log(
                    `Waiting for the selected round-0 recipients (attempt ${attempt}):`,
                    error?.message || String(error),
                );
            }
        }
        await sleep(Math.min(pollMs, Math.max(0, deadline - Date.now())));
    }
    throw new Error(
        `Timed out after ${timeoutMs}ms waiting for the selected round-0 recipients: ` +
        `${lastError?.message || String(lastError || 'not ready')}`,
    );
}

async function receivedWorkerModelFiles() {
    const currentRound = Number(await getRound());
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
        const normalizedAddress = address.toLowerCase();
        if (entry.name !== `wb_client_${normalizedAddress}.bin`) {
            files.push({
                name: entry.name,
                path: filePath,
                address: normalizedAddress,
                authorized: false,
                reason: "non-canonical or duplicate device-address filename",
            });
            continue;
        }
        const [submitted, onchainHash, fileHash] = await Promise.all([
            hasSubmittedModel(currentRound, normalizedAddress),
            getModelSubmissionHash(currentRound, normalizedAddress),
            modelFileHash(filePath),
        ]);
        let authorized = submitted;
        let reason = submitted
            ? ""
            : `model package not accepted onchain for round ${currentRound}`;
        let actualHash = "";
        let recordedHash = "";
        actualHash = normalizeHashHex(fileHash, 32, "received model hash");
        recordedHash = normalizeHashHex(onchainHash, 32, "onchain model submission hash");
        if (authorized && actualHash !== recordedHash) {
            authorized = false;
            reason = `model file hash does not match onchain acceptance for round ${currentRound}`;
        }
        files.push({
            name: entry.name,
            path: filePath,
            address: normalizedAddress,
            authorized,
            reason,
            actualHash,
            recordedHash,
        });
    }
    return files.sort((a, b) => a.name.localeCompare(b.name));
}

async function hasCurrentDeviceRegistration(
    publicIp,
    brokerIp,
    publicKey,
    selloReceiptKey,
    canonicalAppCompose,
) {
    const accountAddress = process.env.ACCOUNT_ADDRESS;
    if (!accountAddress) return false;
    const registeredActionKey = await getDeviceActionKey(accountAddress);
    if (/^0x0{40}$/i.test(registeredActionKey)) return false;

    const current = await isDeviceRegistrationCurrent(
        accountAddress,
        activeParticipantActionSigner?.address,
        publicIp,
        brokerIp,
        publicKey,
        selloReceiptKey,
        canonicalAppCompose,
    );
    if (current) return true;

    throw new Error(
        "Participant already has a different on-chain device registration; "
        + "explicit deregistration or a fresh contract runtime is required.",
    );
}

async function registerWithLocalTdxMock() {
    if (localTdxRegistrationDone || process.env.DOCKER === "phala") return;
    localTdxRegistrationDone = true;

    if (process.env.LOCAL_TDX_MOCK !== '1') {
        throw new Error('Local TDX registration requires LOCAL_TDX_MOCK=1 or a live TEE quote path');
    }
    const rpcUrl = String(process.env.RPC_URL || '');
    const localRpc = /^http:\/\/(anvil|127\.0\.0\.1|localhost):8545\/?$/i.test(rpcUrl);
    if (!localRpc || await getBlockchainChainId() !== 31337) {
        throw new Error('LOCAL_TDX_MOCK is restricted to the local Anvil endpoint on chain 31337');
    }

    const workerImageDigest = normalizeHashHex(
        process.env.LOCAL_TDX_IMAGE_DIGEST || '0x7849ee527ff2efc746c58f67cd6336572d5c71743c608bbd3810079289c7c066',
        32,
        'local mock worker image digest',
    );
    const canonicalAppCompose = Buffer.from(JSON.stringify({
        docker_compose_file: `services:\n  dfl-worker:\n    image: local/dfl-worker@sha256:${workerImageDigest.slice(2)}\n`,
        manifest_version: 2,
        name: 'local-dfl-worker',
        runner: 'docker-compose',
    }), 'utf8');
    const canonicalAppComposeHex = `0x${canonicalAppCompose.toString('hex')}`;
    const publicKey = rsaPublicKeyDerHex();
    const selloReceiptKey = currentSelloReceiptKey();
    const publicIp = process.env.PUBLIC_IP || '';
    const brokerIp = process.env.MSG_BROKER_IP || '';
    if (await hasCurrentDeviceRegistration(
        publicIp,
        brokerIp,
        publicKey,
        selloReceiptKey,
        canonicalAppComposeHex,
    )) {
        console.log('Device already has a bound registration for the current RSA key; reusing it.');
        return;
    }
    const reportData = await getDeviceRegistrationReportData(
        process.env.ACCOUNT_ADDRESS,
        activeParticipantActionSigner.address,
        publicIp,
        brokerIp,
        publicKey,
        selloReceiptKey,
        canonicalAppComposeHex,
    );

    console.warn('Registering through the LOCAL-ONLY mock TDX verifier; this is not a hardware attestation.');
    await registerDeviceWithTeeQuoteAndRtmr3Events(
        reportData,
        [{ eventType: 0x08000001, eventName: 'compose-hash', eventPayload: `0x${'00'.repeat(32)}` }],
        canonicalAppComposeHex,
        process.env.ACCOUNT_ADDRESS,
        activeParticipantActionSigner.address,
        publicIp,
        brokerIp,
        publicKey,
        selloReceiptKey,
    );
    console.log('Device registered with bound local mock REPORTDATA.');
}

function decodeEventBytes(rawValue) {
    if (Buffer.isBuffer(rawValue) || Array.isArray(rawValue) || ArrayBuffer.isView(rawValue)) {
        return Buffer.from(rawValue);
    }

    if (typeof rawValue === 'string') {
        let encoded = rawValue.trim();
        if (encoded === '') return Buffer.alloc(0);
        if (encoded.startsWith('0x') || encoded.startsWith('0X')) encoded = encoded.slice(2);
        if (/^[0-9a-fA-F]+$/.test(encoded) && encoded.length % 2 === 0) {
            return Buffer.from(encoded, 'hex');
        }
        if (/^[A-Za-z0-9+/]+={0,2}$/.test(encoded) && encoded.length % 4 === 0) {
            return Buffer.from(encoded, 'base64');
        }
        return null;
    }

    if (rawValue && typeof rawValue === 'object') {
        if (rawValue.type === 'Buffer' && Array.isArray(rawValue.data)) {
            return Buffer.from(rawValue.data);
        }
        for (const key of ['value', 'bytes', 'hex']) {
            if (rawValue[key] !== undefined) {
                const decoded = decodeEventBytes(rawValue[key]);
                if (decoded) return decoded;
            }
        }
        const keys = Object.keys(rawValue);
        if (keys.length > 0 && keys.every(key => /^\d+$/.test(key))) {
            const values = keys
                .sort((a, b) => Number(a) - Number(b))
                .map(key => rawValue[key]);
            if (values.every(value => Number.isInteger(value) && value >= 0 && value <= 255)) {
                return Buffer.from(values);
            }
        }
    }

    return null;
}

function normalizeRtmr3EventPolicy(eventLog) {
    let events = eventLog;
    if (typeof events === 'string') {
        events = JSON.parse(events);
    }
    if (!Array.isArray(events)) {
        throw new Error('Phala quote response did not include an RTMR event log array');
    }
    const rtmr3Events = events.filter(event => Number(event?.imr) === 3);
    const structuredEvents = rtmr3Events.map(event => {
        const payload = decodeEventBytes(event?.event_payload);
        if (!payload) throw new Error(`Invalid RTMR3 event payload for event ${event?.event || '<unknown>'}`);
        return {
            eventType: Number(event?.event_type),
            eventName: String(event?.event || ''),
            eventPayload: `0x${payload.toString('hex')}`,
        };
    });
    if (structuredEvents.length === 0) {
        throw new Error('Phala quote response did not include RTMR3 runtime events');
    }
    const composeEventIndex = rtmr3Events.findIndex(event => event?.event === 'compose-hash');
    let composeHash = '0x0000000000000000000000000000000000000000000000000000000000000000';
    if (composeEventIndex >= 0) {
        const payload = decodeEventBytes(rtmr3Events[composeEventIndex]?.event_payload);
        if (!payload || payload.length !== 32) {
            throw new Error('Invalid RTMR3 compose-hash event payload');
        }
        composeHash = `0x${payload.toString('hex')}`;
        console.log('Live RTMR3 compose event:', {
            composeHash,
        });
    } else {
        console.warn('Live RTMR3 event log does not contain a compose-hash event.');
    }
    return { events: structuredEvents, composeHash };
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

async function prepareAggregatorParentModel(currentRound, expectedState) {
    if (currentRound <= 0) {
        return null;
    }

    console.log(
        `Fetching and authenticating the active global model for aggregation round ${currentRound} ...`,
    );
    await runtimeEvent("aggregator.fetch_parent_global_model.started", {
        role: "aggregator",
        round: currentRound,
    });
    const fetchedGlobalModel = await runOperation(
        "aggregator.fetch_parent_global_model",
        { role: "aggregator", round: currentRound },
        () => getCurrentModel(activeParticipantKey.privateKey),
    );
    const currentGlobalModel = await assertFetchedGlobalModelIsStillCurrent(
        fetchedGlobalModel,
    );

    const modelRound = Number(currentGlobalModel.modelRound);
    const skippedAttemptRounds = skippedAggregatorAttemptRounds({
        modelRound,
        currentRound,
    });
    if (skippedAttemptRounds.length > 0) {
        const abortedAttemptRounds: number[] = [];
        for (const round of skippedAttemptRounds) {
            if (verifiedAbortedAttemptRounds.has(round)) {
                abortedAttemptRounds.push(round);
                continue;
            }
            if (await isRoundAborted(round)) {
                verifiedAbortedAttemptRounds.add(round);
                abortedAttemptRounds.push(round);
            }
        }
        requireAbortedAggregatorAttemptGap({
            modelRound,
            currentRound,
            abortedAttemptRounds,
        });
        console.log(
            `Using finalized global-model round ${modelRound} as the parent for `
            + `aggregation round ${currentRound}; intervening contract attempt `
            + `round(s) ${skippedAttemptRounds.join(", ")} were aborted.`,
        );
    }

    if (!currentGlobalModel.plaintextSignaturePresent) {
        if (modelRound !== 1) {
            throw new Error(
                `Encrypted global model round ${currentGlobalModel.modelRound} `
                + 'has no plaintext aggregator signature.',
            );
        }
        console.log(
            'Bootstrap parent model has no origin signature; its encrypted bundle '
            + 'was authenticated with the registered W0 participant key.',
        );
    } else {
        const sigOk = await verifyDownloadedGlobalModelSignature({
            publicKeyDerHex: currentGlobalModel.publisherPublicKeyDerHex,
            modelPath: "./data/gm.bin",
            sigPath: "./data/gm.bin.sig",
        });
        if (!sigOk) {
            throw new Error("Global parent-model signature verification failed.");
        }
        console.log("Global parent-model signature verification successful.");
    }

    const [latestState, latestRound] = await Promise.all([
        getCurrentState(),
        getRound(),
    ]);
    if (
        latestState["0"] !== expectedState
        || Number(latestRound) !== currentRound
        || !sameAddress(latestState["1"], process.env.ACCOUNT_ADDRESS)
    ) {
        throw new Error(
            `${expectedState} context changed while preparing the aggregator parent model for round ${currentRound}.`,
        );
    }

    await runtimeEvent("aggregator.fetch_parent_global_model.finished", {
        role: "aggregator",
        round: currentRound,
        model_round: modelRound,
        model_cid: currentGlobalModel.modelCid,
        publisher: currentGlobalModel.publisher,
    });
    return currentGlobalModel;
}

function isMissingRoundKeyError(error) {
    const message = error?.message || String(error || "");
    return message.includes("No wrapped GM round key found");
}

async function fixedRoundZeroRecipients() {
    const snapshotDirectory = process.env.DOCKER === 'phala'
        ? path.dirname(
            process.env.PARTICIPANT_KEY_STATE_PATH
            || '/var/lib/vita-fl/participant-rsa.v1.sealed.json',
        )
        : resultsIIDDir;
    const snapshotPath = path.join(
        snapshotDirectory,
        'round-0-bootstrap-recipients.json',
    );
    await fs.mkdir(snapshotDirectory, { recursive: true });

    try {
        const existing = JSON.parse(await fs.readFile(snapshotPath, 'utf8'));
        // Recovery deliberately consults only the immutable on-chain roster.
        // Mutable MFS readiness declarations are transport for the first
        // freeze and cannot wedge or retarget a restart.
        const currentContext = await currentCommittedRunRosterContext({
            requireFrozen: true,
        });
        const recipients = frozenRecipientsForCommittedRoster(
            existing,
            currentContext.binding,
        );
        const registeredRecipients = await readBootstrapRecipientKeys(
            currentContext.binding.recipientAddresses,
        );
        requireFrozenRecipientKeysMatchRegistry(
            recipients,
            registeredRecipients,
        );
        console.log(
            `Reusing ${recipients.length} frozen round-0 recipient keys for the `
            + `immutable on-chain roster ${currentContext.binding.rosterDigest}.`,
        );
        return recipients;
    } catch (error) {
        if (error?.code !== 'ENOENT') throw error;
    }

    const frozen = await waitForRegisteredBootstrapRecipients();
    const snapshot = buildRoundZeroBootstrapSnapshot(
        frozen.binding,
        frozen.recipients,
    );

    // Validate against the immutable on-chain commitment immediately before
    // the atomic commit. MFS was checked while creating the first snapshot,
    // but it is intentionally not part of the persisted recovery binding.
    const currentContext = await currentCommittedRunRosterContext({
        requireFrozen: true,
    });
    frozenRecipientsForCommittedRoster(snapshot, currentContext.binding);

    const temporaryPath = `${snapshotPath}.${process.pid}.${crypto.randomUUID()}.tmp`;
    let temporaryHandle;
    try {
        temporaryHandle = await fs.open(temporaryPath, 'wx', 0o600);
        await temporaryHandle.writeFile(`${JSON.stringify(snapshot)}\n`, 'utf8');
        await temporaryHandle.sync();
        await temporaryHandle.close();
        temporaryHandle = null;
        await fs.rename(temporaryPath, snapshotPath);
        const directoryHandle = await fs.open(snapshotDirectory, 'r');
        try {
            await directoryHandle.sync();
        } finally {
            await directoryHandle.close();
        }
    } finally {
        if (temporaryHandle) await temporaryHandle.close().catch(() => {});
        await fs.unlink(temporaryPath).catch(() => {});
    }

    const persisted = JSON.parse(await fs.readFile(snapshotPath, 'utf8'));
    const persistedContext = await currentCommittedRunRosterContext({
        requireFrozen: true,
    });
    const persistedRecipients = frozenRecipientsForCommittedRoster(
        persisted,
        persistedContext.binding,
    );
    const registeredRecipients = await readBootstrapRecipientKeys(
        persistedContext.binding.recipientAddresses,
    );
    return requireFrozenRecipientKeysMatchRegistry(
        persistedRecipients,
        registeredRecipients,
    );
}

async function prepareRoundZeroBootstrap() {
    if (Number(await getRound()) !== 0) {
        throw new Error('The public initial model may only be prepared in round 0.');
    }
    const initialModelCid = String(await getCurrentGM()).trim();
    if (!initialModelCid) {
        throw new Error('GMStorage contains no public initial-model CID.');
    }
    const frozenRecipients = await fixedRoundZeroRecipients();

    await fs.mkdir(resultsIIDDir, { recursive: true });
    await getFileFromIPFS(
        initialModelCid,
        path.join(resultsIIDDir, 'aggregated.bin'),
    );
    // The public initial model intentionally has no origin signature. W0 will
    // authenticate the encrypted bundle it publishes through the normal
    // participant-key and aggregation-statement path.
    await fs.writeFile(
        path.join(resultsIIDDir, 'aggregated.bin.sig'),
        Buffer.alloc(0),
    );
    console.log(
        `Round 0 bootstrap prepared the public initial model for `
        + `${frozenRecipients.length} fixed recipients; no origin signature was required.`,
    );
}

const stateMachine = async () => {
    const completedRoundsAtStartup = Number(await getCompletedRoundCount());
    const trainingWasCompleteAtStartup = completedRoundsAtStartup >= targetRound();
    if (process.env.DOCKER === "phala") {
        const publicKey = rsaPublicKeyDerHex();
        const selloReceiptKey = currentSelloReceiptKey();
        const liveIdentity = await currentPhalaIdentity();
        const publicIp = teeInferenceEnabled()
            ? ownTeeInferenceEndpoint(liveIdentity.appId)
            : process.env.PUBLIC_IP || "";
        const brokerIp = process.env.MSG_BROKER_IP || "";
        if (await hasCurrentDeviceRegistration(
            publicIp,
            brokerIp,
            publicKey,
            selloReceiptKey,
            liveIdentity.canonicalAppCompose,
        )) {
            console.log('Device already has a bound registration for the current RSA key; reusing it.');
        } else {
            console.log("Fetching TDX Quote ...");
            const { quoteHex, rtmr3EventLog, canonicalAppCompose } = await fetchLivePhalaQuote(
                async (identity) => getDeviceRegistrationReportData(
                    process.env.ACCOUNT_ADDRESS,
                    activeParticipantActionSigner.address,
                    publicIp,
                    brokerIp,
                    publicKey,
                    selloReceiptKey,
                    identity.canonicalAppCompose,
                ),
            );
            console.log(`Registering with live Phala TDX quote, canonical app_compose and ${rtmr3EventLog.length} RTMR3 events ...`);

            await registerDeviceWithTeeQuoteAndRtmr3Events(
                quoteHex,
                rtmr3EventLog,
                canonicalAppCompose,
                process.env.ACCOUNT_ADDRESS,
                activeParticipantActionSigner.address,
                publicIp,
                brokerIp,
                publicKey,
                selloReceiptKey,
            );
            console.log("Device registered with onchain TDX quote and RTMR3 event replay verification.");
        }
    }
    else {
        await registerWithLocalTdxMock();
    }
    if (trainingWasCompleteAtStartup) {
        console.log(
            `Training was already complete at worker startup `
            + `(${completedRoundsAtStartup}/${targetRound()} rounds); `
            + "keeping the rebooted worker idle without submitting transactions.",
        );
        await runtimeEvent("training.rebooted_idle", {
            role: "worker",
            completed_rounds: completedRoundsAtStartup,
            target_rounds: targetRound(),
        });
        await new Promise<void>(() => {
            setInterval(() => {}, 60 * 60 * 1000);
        });
    }
    while (Number(await getCompletedRoundCount()) < targetRound()) {
        let state = await getCurrentState();
        const observedRound = Number(await getRound());
        if (
            observedRound === 0
            && !sameAddress(state[1], process.env.ACCOUNT_ADDRESS)
        ) {
            await waitForRoundZeroBootstrap(String(state[1]));
            continue;
        }

        switch (state["0"]) {
            case "TRAINING":
                if (sameAddress(state[1], process.env.ACCOUNT_ADDRESS)) {
                    console.log("I am the aggregator");
                    await runtimeEvent("state.training.aggregator", { role: "aggregator" });
                    const currentRound = Number(await getRound());
                    console.log("Round %d.", currentRound);
                    if (currentRound > 0) {
                        try {
                            await prepareAggregatorParentModel(currentRound, "TRAINING");
                        } catch (error) {
                            console.error(
                                "Aggregator parent-model preparation failed; keeping submissions closed:",
                                error,
                            );
                            await runtimeEvent("aggregator.fetch_parent_global_model.failed", {
                                role: "aggregator",
                                round: currentRound,
                                error: error?.message || String(error),
                            });
                            await sleep(2000);
                            continue;
                        }
                    }
                    const openedPolicy = await openModelSubmissions(currentRound);
                    console.log("Round aggregation policy opened.", {
                        round: currentRound,
                        requiredSubmissions: openedPolicy.requiredSubmissions,
                        deadline: openedPolicy.deadline,
                        configurationVersion: openedPolicy.configurationVersion,
                        algorithmHash: openedPolicy.algorithmHash,
                        policyHash: openedPolicy.policyHash,
                    });
                    if (openedPolicy.closed) {
                        console.log(
                            `Round ${currentRound} input policy is already closed; resuming aggregation.`,
                        );
                        await setCurrentState("AGGREGATING");
                        continue;
                    }
                    if (currentRound === 0) {
                        console.log(
                            "Round 0 bootstrap phase: closing the empty input set and proceeding directly to aggregation.",
                        );
                        await closeModelSubmissions(currentRound);
                        await setCurrentState("AGGREGATING");
                        continue;
                    }
                    console.log("Starting the authenticated HTTPS model receiver ...");
                    try {
                        if (!aggregatorServerRunning) {
                            const identity = await currentPhalaIdentity();
                            await startModelUploadServer();
                            const endpoint = ownModelUploadEndpoint(identity.appId);
                            if (String(await getAggregatorEndpoint()) !== endpoint) {
                                await setAggregatorEndpoint(endpoint);
                            }
                            console.log(`Published aggregator model endpoint: ${endpoint}`);
                            aggregatorServerRunning = true;
                        }
                        const expected = openedPolicy.requiredSubmissions;
                        const remainingMs = Math.max(
                            0,
                            (openedPolicy.deadline * 1000) - Date.now(),
                        );
                        console.log(
                            `Waiting for ${expected} policy-required model submissions ` +
                            `until on-chain deadline ${openedPolicy.deadline}.`,
                        );
                        await runtimeEvent("aggregator.wait_for_models.started", {
                            role: "aggregator",
                            expected_models: expected,
                            deadline_unix_seconds: openedPolicy.deadline,
                            remaining_ms: remainingMs,
                            policy_hash: openedPolicy.policyHash,
                        });
                        const present = await runOperation("aggregator.wait_for_models", {
                            role: "aggregator",
                            expected_models: expected,
                            deadline_unix_seconds: openedPolicy.deadline,
                            remaining_ms: remainingMs,
                        }, () => waitForModels(expected, {
                            dir: srcModelsDir,
                            pollMs: 2000,
                            timeoutMs: remainingMs,
                        }));
                        const latestPolicy = await getRoundAggregationPolicy(currentRound);
                        await runtimeEvent("aggregator.wait_for_models.finished", {
                            role: "aggregator",
                            expected_models: expected,
                            present_models: present,
                            accepted_models: latestPolicy.acceptedSubmissions,
                        });
                        if (latestPolicy.acceptedSubmissions < expected || present < expected) {
                            console.log(
                                `Aggregation threshold not reached: local=${present}, ` +
                                `on-chain=${latestPolicy.acceptedSubmissions}, required=${expected}. ` +
                                "The aggregator cannot close or publish this round.",
                            );
                            await sleep(5000);
                            continue;
                        }
                        await closeModelSubmissions(currentRound);
                        await setCurrentState("AGGREGATING");
                        continue;
                    } catch (e) {
                        console.error(
                            "Aggregator receive/close step failed; keeping TRAINING for reconciliation:",
                            e,
                        );
                        await sleep(2000);
                        continue;
                    }
                } else {
                    console.log("I am not the aggregator");
                    await runtimeEvent("state.training.worker", { role: "worker", aggregator: String(state["1"]) });
                    const currentRound = Number(await getRound());
                    console.log("Round %d.", currentRound);
                    if (await hasSubmittedModel(currentRound, process.env.ACCOUNT_ADDRESS)) {
                        await waitForSubmittedRoundToAdvance(currentRound, String(state[1]));
                        await sleep(2000);
                        continue;
                    }
                    let medicalSignerSnapshot = null;
                    if ((process.env.DATASET_NAME || "mnist").toLowerCase() === "chestmnist") {
                        medicalSignerSnapshot = await runOperation(
                            "worker.fetch_medical_signers",
                            { role: "worker", round: currentRound },
                            () => getMedicalSignerSnapshot(),
                        );
                        console.log(
                            `Fetched ${medicalSignerSnapshot.signers.length} approved medical signer keys ` +
                            `from block ${medicalSignerSnapshot.block_number} (key set ${medicalSignerSnapshot.key_set_version}).`
                        );
                        await runtimeEvent("worker.medical_signers.fetched", {
                            role: "worker",
                            round: currentRound,
                            block_number: medicalSignerSnapshot.block_number,
                            block_hash: medicalSignerSnapshot.block_hash,
                            key_set_version: medicalSignerSnapshot.key_set_version,
                            signer_count: medicalSignerSnapshot.signers.length,
                        });
                    }
                    console.log("Fetching the global model from IPFS ...");
                    await runtimeEvent("worker.fetch_global_model.started", { role: "worker" });
                    let fetchedGlobalModel;
                    try {
                        fetchedGlobalModel = await runOperation(
                            "worker.fetch_global_model",
                            { role: "worker" },
                            () => getCurrentModel(activeParticipantKey.privateKey),
                        );
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
                        await monitorNonAggregatorStateProgress(
                            currentRound,
                            String(state[1]),
                            "TRAINING_MISSING_ROUND_KEY",
                        );
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

                    if (!currentGlobalModel.plaintextSignaturePresent) {
                        if (Number(currentGlobalModel.modelRound) !== 1) {
                            throw new Error(
                                `Encrypted global model round ${currentGlobalModel.modelRound} `
                                + 'has no plaintext aggregator signature.',
                            );
                        }
                        console.log(
                            'Bootstrap model has no origin signature; its encrypted bundle '
                            + 'was authenticated with the registered W0 participant key.',
                        );
                    } else {
                        const sigOk = await verifyDownloadedGlobalModelSignature({
                            publicKeyDerHex: currentGlobalModel.publisherPublicKeyDerHex,
                            modelPath: "./data/gm.bin",
                            sigPath: "./data/gm.bin.sig",
                        });
                        if (!sigOk) {
                            console.error("Global model signature verification FAILED. Aborting training.");
                            return;
                        }
                        console.log("Global model signature verification successful.");
                    }

                    const trainingAggregator = String(state[1]);
                    console.log("Starting local training ...");
                    await runtimeEvent("worker.training.started", { role: "worker" });
                    try {
                        await runOperation("worker.training", { role: "worker" }, async () => callPythonService('/train', {
                            epochs: Number(process.env.EPOCH),
                            aggregator_public_key_der_hex: await getDevicePublicKey(trainingAggregator),
                            medical_signer_snapshot: medicalSignerSnapshot,
                            private_key: participantPrivateKeyRuntimePath,
                            round_id: currentRound,
                            device_id: process.env.DEVICE_ID,
                        }));
                    } catch (e) {
                        console.error("Error during local training:", e);
                        return;
                    }
                    console.log("Local training complete.");
                    await runtimeEvent("worker.training.finished", { role: "worker" });

                    const latestState = await getCurrentState();
                    const latestRound = Number(await getRound());
                    if (
                        latestRound !== currentRound ||
                        !sameAddress(latestState[1], trainingAggregator)
                    ) {
                        console.log(
                            "Round or aggregator changed during training. Discarding the stale encrypted update and returning to the state loop."
                        );
                        await runtimeEvent("worker.training_context_changed_before_transfer", {
                            role: sameAddress(latestState[1], process.env.ACCOUNT_ADDRESS) ? "aggregator" : "worker",
                            trained_round: currentRound,
                            current_round: latestRound,
                            trained_for_aggregator: trainingAggregator,
                            current_aggregator: String(latestState[1]),
                        });
                        continue;
                    }
                    try {
                        await assertFetchedGlobalModelIsStillCurrent(currentGlobalModel);
                    } catch (error) {
                        console.warn(error?.message || String(error));
                        await runtimeEvent("worker.training_parent_changed_before_transfer", {
                            role: "worker",
                            round: currentRound,
                            error: error?.message || String(error),
                        });
                        continue;
                    }
                    state = latestState;

                    console.log("Is the device authorized? ", await isAuthorized(process.env.ACCOUNT_ADDRESS));
                    console.log("Uploading encrypted local model to the aggregator ...");
                    await runtimeEvent("worker.model_transfer.started", { role: "worker", aggregator: String(state["1"]) });
                    try {
                        await runOperation("worker.model_transfer", {
                            role: "worker",
                            aggregator: String(state["1"]),
                        }, async () => {
                            const endpoint = await waitForAggregatorModelEndpoint(trainingAggregator);
                            await uploadLocalModel(
                                endpoint,
                                String(process.env.ACCOUNT_ADDRESS),
                                currentRound,
                                trainingAggregator,
                                currentGlobalModel,
                            );
                        });
                        await runtimeEvent("worker.model_transfer.finished", { role: "worker", aggregator: String(state["1"]) });
                    } catch (e) {
                        console.error("Error during model transfer:", e);
                        await runtimeEvent("worker.model_transfer.failed", {
                            role: "worker",
                            aggregator: String(state["1"]),
                            error: e?.message || String(e),
                        });
                        await recordMissedAggregatorProgress(
                            currentRound,
                            trainingAggregator,
                            "model_transfer_failed",
                            e,
                        );
                        await sleep(modelTransferRetryDelayMs);
                        continue;
                    }
                    console.log("Top contributor:", await getTopContributor());
                    console.log("Transfer complete. Send local model to aggregator.");
                    console.log("Completed training rounds left: ", (targetRound() - Number(await getCompletedRoundCount())));
                    console.log("Waiting till next round.");
                    try {
                        const newGM = await waitForGMUpdate(prevGM, { pollMs: gmUpdatePollMs, timeoutMs: gmUpdateTimeoutMs });
                        resetAggregatorTimeoutTracker();
                        console.log("New Global Model detected:", newGM);
                    } catch (e) {
                        await recordMissedAggregatorProgress(
                            currentRound,
                            trainingAggregator,
                            "global_model_not_updated",
                            e,
                        );
                    }
                    await sleep(2000);
                    continue;
                }

            case "AGGREGATING":
                if (sameAddress(state[1], process.env.ACCOUNT_ADDRESS)) {
                    currentState = "AGGREGATING";
                    console.log("I am the aggregator");
                    console.log("Starting the aggregation process ...");
                    await runtimeEvent("aggregator.aggregation.started", { role: "aggregator" });
                    try {
                        const currentRound = Number(await getRound());
                        if (currentRound > 0) {
                            await prepareAggregatorParentModel(
                                currentRound,
                                "AGGREGATING",
                            );
                        }
                        const aggregationPolicy = await getRoundAggregationPolicy(currentRound);
                        if (!aggregationPolicy.opened || !aggregationPolicy.closed) {
                            throw new Error(
                                `Round ${currentRound} aggregation inputs are not immutably closed.`,
                            );
                        }
                        if (
                            aggregationPolicy.acceptedSubmissions
                            < aggregationPolicy.requiredSubmissions
                        ) {
                            throw new Error(
                                `Round ${currentRound} has ${aggregationPolicy.acceptedSubmissions} ` +
                                `accepted submissions but requires ${aggregationPolicy.requiredSubmissions}.`,
                            );
                        }
                        const expected = aggregationPolicy.acceptedSubmissions;
                        console.log(
                            `Closed round ${currentRound} commits to ${expected} accepted model(s).`,
                            {
                                inputRoot: aggregationPolicy.inputRoot,
                                policyHash: aggregationPolicy.policyHash,
                            },
                        );

                        if (aggregatorServerRunning || modelUploadServer) {
                            console.log("Stopping aggregator server before aggregation...");
                            await stopAggregatorServer();
                        }

                        const count = await stageAggregation();
                        console.log(`Staged ${count} model file(s) for aggregation.`);
                        await runtimeEvent("aggregator.models.staged", {
                            role: "aggregator",
                            model_count: count,
                            expected_models: expected,
                            input_root: aggregationPolicy.inputRoot,
                            policy_hash: aggregationPolicy.policyHash,
                        });
                        if (currentRound === 0 && count === 0) {
                            console.log("Round 0 has no worker submissions. Encrypting the public initial model for the fixed worker set.");
                            await runOperation("aggregator.round0.bootstrap", {
                                role: "aggregator",
                                round: currentRound,
                            }, () => prepareRoundZeroBootstrap());
                            await setCurrentState("UPDATING");
                            continue;
                        }
                        if (count !== expected) {
                            console.log(
                                `The local verified input set (${count}) does not exactly match ` +
                                `the immutable on-chain input count (${expected}). ` +
                                "Keeping AGGREGATING so timeout recovery can replace this aggregator.",
                            );
                            await sleep(5000);
                            continue;
                        }

                        if (
                            !sameHex(
                                aggregationPolicy.algorithmHash,
                                FEDERATED_AVERAGING_V1_HASH,
                            )
                        ) {
                            throw new Error(
                                `Unsupported aggregation policy ${aggregationPolicy.algorithmHash}; ` +
                                `this worker implements ${FEDERATED_AVERAGING_V1_HASH}.`,
                            );
                        }
                        if (
                            !sameHex(
                                aggregationPolicy.validationDataHash,
                                ZERO_BYTES32,
                            )
                            || Number(aggregationPolicy.maxLossIncreaseBps) !== 0
                        ) {
                            throw new Error(
                                "The active FedAvg baseline requires a zero validation-data hash " +
                                "and a zero loss-gate value.",
                            );
                        }

                        const globalModelRound = currentRound + 1;
                        const participantCount = Number(
                            process.env.WORKER_COUNT || expected + 1,
                        );
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
                            private_key: participantPrivateKeyRuntimePath,
                        }, { timeoutMs: 3 * 60 * 1000 }));
                        await runtimeEvent("aggregator.fedavg.completed", {
                            role: "aggregator",
                            source_round: currentRound,
                            model_count: expected,
                            algorithm_hash: aggregationPolicy.algorithmHash,
                        });
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
                        console.log(
                            "Keeping the immutable submission window closed and retrying AGGREGATING; " +
                            "a quorum timeout can replace this aggregator if recovery fails."
                        );
                        await sleep(2000);
                        continue;
                    }
                    console.log("Aggregation complete.");
                    await runtimeEvent("aggregator.aggregation.finished", { role: "aggregator" });
                    await setCurrentState("UPDATING");
                    continue;
                }
                await monitorNonAggregatorStateProgress(
                    Number(await getRound()),
                    String(state[1]),
                    "AGGREGATING",
                );
                continue;

            case "UPDATING":
                if (sameAddress(state[1], process.env.ACCOUNT_ADDRESS)) {
                    currentState = "UPDATING";
                    console.log("I am the aggregator");
                    console.log("Starting the updating process ...");
                    const recoveryRound = Number(await getRound());
                    const selectionGapRecovery = await recoverSelectionGap(
                        recoveryRound,
                        String(state[1]),
                        "UPDATING",
                    );
                    if (selectionGapRecovery !== null) {
                        if (selectionGapRecovery === "training-complete") {
                            await stageCleaning();
                            await runtimeEvent("training.completed", {
                                role: "aggregator",
                                final_round: recoveryRound,
                                completed_rounds: Number(await getCompletedRoundCount()),
                            });
                            return;
                        }
                        if (selectionGapRecovery) {
                            await stageCleaning();
                            console.log(
                                "Recovered aggregator selection from on-chain state; " +
                                "local finalization artifacts are no longer required.",
                            );
                        }
                        await sleep(2000);
                        continue;
                    }
                    const journalPath = path.join(resultsIIDDir, ".updating-finalization.json");
                    const journalTempPath = `${journalPath}.${process.pid}.tmp`;
                    const aggregatedModelPath = path.join(resultsIIDDir, "aggregated.bin");
                    const aggregatedSignaturePath = path.join(
                        resultsIIDDir,
                        "aggregated.bin.sig",
                    );
                    if (
                        recoveryRound === 0
                        && (
                            !existsSync(aggregatedModelPath)
                            || !existsSync(aggregatedSignaturePath)
                        )
                    ) {
                        console.log(
                            "Recovering the round-0 bootstrap artifacts from the immutable initial-model CID.",
                        );
                        try {
                            await runOperation(
                                "aggregator.round0.bootstrap_recovery",
                                { role: "aggregator", round: recoveryRound },
                                () => prepareRoundZeroBootstrap(),
                            );
                        } catch (error) {
                            console.error(
                                "Round-0 bootstrap artifact recovery failed; keeping UPDATING for retry:",
                                error,
                            );
                            await sleep(2000);
                            continue;
                        }
                    }
                    let artifactFingerprint;
                    try {
                        const [artifactStat, artifactHash] = await Promise.all([
                            fs.stat(aggregatedModelPath, { bigint: true }),
                            modelFileHash(aggregatedModelPath),
                        ]);
                        artifactFingerprint =
                            `${artifactStat.size}:${artifactStat.mtimeNs}:${artifactHash}`;
                    } catch (e) {
                        console.error("Cannot finalize UPDATING without the aggregated model artifact:", e);
                        await sleep(2000);
                        continue;
                    }

                    let finalization = null;
                    try {
                        const parsed = JSON.parse(await fs.readFile(journalPath, "utf8"));
                        if (parsed.artifact_fingerprint === artifactFingerprint) {
                            const sourceRound = Number(parsed.source_round);
                            const expectedNextRound = Number(parsed.expected_next_round);
                            if (
                                parsed.version !== 1 ||
                                !Number.isSafeInteger(sourceRound) ||
                                sourceRound < 0 ||
                                sourceRound >= Number.MAX_SAFE_INTEGER ||
                                expectedNextRound !== sourceRound + 1
                            ) {
                                throw new Error("invalid UPDATING finalization journal");
                            }
                            finalization = {
                                sourceRound,
                                expectedNextRound,
                                artifactFingerprint,
                            };
                        } else {
                            console.warn("Ignoring a stale UPDATING journal for an older aggregate artifact.");
                        }
                    } catch (e) {
                        if (e?.code !== "ENOENT") {
                            console.error("Cannot read the UPDATING finalization journal:", e);
                            await sleep(2000);
                            continue;
                        }
                    }

                    if (!finalization) {
                        const sourceRound = Number(await getRound());
                        if (
                            !Number.isSafeInteger(sourceRound) ||
                            sourceRound < 0 ||
                            sourceRound >= Number.MAX_SAFE_INTEGER
                        ) {
                            console.error("Cannot finalize UPDATING with an invalid on-chain round:", sourceRound);
                            await sleep(2000);
                            continue;
                        }
                        finalization = {
                            sourceRound,
                            expectedNextRound: sourceRound + 1,
                            artifactFingerprint,
                        };
                        await fs.mkdir(resultsIIDDir, { recursive: true });
                        try {
                            await fs.writeFile(journalTempPath, JSON.stringify({
                                version: 1,
                                source_round: finalization.sourceRound,
                                expected_next_round: finalization.expectedNextRound,
                                artifact_fingerprint: finalization.artifactFingerprint,
                            }));
                            await fs.rename(journalTempPath, journalPath);
                        } catch (e) {
                            console.error("Cannot persist the UPDATING finalization journal:", e);
                            await sleep(2000);
                            continue;
                        } finally {
                            await fs.unlink(journalTempPath).catch(() => {});
                        }
                    }

                    let observedRound;
                    try {
                        observedRound = Number(await getRound());
                    } catch (e) {
                        console.error("Cannot read the round before UPDATING reconciliation:", e);
                        await sleep(2000);
                        continue;
                    }
                    if (observedRound < finalization.sourceRound) {
                        console.error(
                            `On-chain round ${observedRound} is behind the persisted UPDATING round ${finalization.sourceRound}.`
                        );
                        await sleep(2000);
                        continue;
                    }

                    if (observedRound === finalization.sourceRound) {
                        await runtimeEvent("aggregator.update.started", {
                            role: "aggregator",
                            round: finalization.sourceRound,
                        });
                        try {
                            const frozenBootstrapRecipients =
                                finalization.sourceRound === 0
                                    ? await fixedRoundZeroRecipients()
                                    : undefined;
                            await runOperation(
                                "aggregator.update_global_model",
                                { role: "aggregator", round: finalization.sourceRound },
                                () => updateGM(
                                    finalization.expectedNextRound,
                                    activeParticipantKey.privateKey,
                                    { frozenBootstrapRecipients },
                                ),
                            );
                        } catch (e) {
                            console.error("Error during updating the global model; keeping UPDATING for retry:", e);
                            await runtimeEvent("aggregator.update.failed", {
                                role: "aggregator",
                                round: finalization.sourceRound,
                                error: e?.message || String(e),
                            });
                            await sleep(2000);
                            continue;
                        }
                        console.log("Updating complete.");
                        await runtimeEvent("aggregator.update.finished", {
                            role: "aggregator",
                            round: finalization.sourceRound,
                        });
                        console.log("Current Global Model:", await getCurrentGM());
                        observedRound = Number(await getRound());
                    } else {
                        console.log(
                            `Recovered UPDATING finalization after round advancement ` +
                            `${finalization.sourceRound} -> ${observedRound}; reconciling its explicit publication status.`
                        );
                    }

                    try {
                        observedRound = Number(await getRound());
                    } catch (e) {
                        console.error("Cannot confirm the on-chain round after atomic finalization:", e);
                        await sleep(2000);
                        continue;
                    }
                    if (observedRound < finalization.expectedNextRound) {
                        console.error(
                            `Round ${finalization.sourceRound} is not yet finalized on-chain; keeping UPDATING for retry.`
                        );
                        await sleep(2000);
                        continue;
                    }

                    let sourceRoundPublished;
                    let sourceRoundCompleted;
                    try {
                        [sourceRoundPublished, sourceRoundCompleted] = await Promise.all([
                            isGlobalModelPublished(finalization.sourceRound),
                            isRoundCompleted(finalization.sourceRound),
                        ]);
                    } catch (e) {
                        console.error(
                            'Cannot reconcile explicit source-round finalization flags:',
                            e,
                        );
                        await sleep(2000);
                        continue;
                    }
                    if (!isExplicitlyFinalizedSourceRound({
                        published: sourceRoundPublished,
                        completed: sourceRoundCompleted,
                    })) {
                        console.error(
                            `On-chain round advanced beyond ${finalization.sourceRound}, but that `
                            + `source round is not finalized (published=${sourceRoundPublished}, `
                            + `completed=${sourceRoundCompleted}). Preserving the UPDATING journal `
                            + 'and artifacts instead of treating an aborted round as successful.',
                        );
                        await runtimeEvent('aggregator.update.not_finalized', {
                            role: 'aggregator',
                            source_round: finalization.sourceRound,
                            observed_round: observedRound,
                            published: sourceRoundPublished,
                            completed: sourceRoundCompleted,
                        });
                        await sleep(2000);
                        continue;
                    }
                    if (observedRound > finalization.sourceRound) {
                        console.log(
                            `Round ${finalization.sourceRound} is explicitly published and completed; `
                            + 'duplicate atomic publication is unnecessary.',
                        );
                    }

                    const completedRounds = Number(await getCompletedRoundCount());
                    console.log("Completed training rounds left: ", (targetRound() - completedRounds));
                    if (completedRounds >= targetRound()) {
                        await stageCleaning();
                        console.log("Cleaned finalized round artifacts.");
                        console.log(`Reached configured final round ${targetRound()}. Skipping next aggregator selection and stopping the worker loop.`);
                        await runtimeEvent("training.completed", {
                            role: "aggregator",
                            final_round: observedRound,
                            completed_rounds: completedRounds,
                        });
                        return;
                    }

                    const readSelectionOutcome = async () => {
                        const [selectionState, selectionRoundValue] = await Promise.all([
                            getCurrentState(),
                            getRound(),
                        ]);
                        return {
                            state: String(selectionState[0]),
                            aggregator: String(selectionState[1]),
                            round: Number(selectionRoundValue),
                        };
                    };
                    const selectionIsConfirmed = (outcome) =>
                        outcome.round >= finalization.expectedNextRound &&
                        outcome.state === "TRAINING";

                    let selectionOutcome;
                    try {
                        selectionOutcome = await readSelectionOutcome();
                    } catch (e) {
                        console.error("Cannot read on-chain selection state; keeping UPDATING for retry:", e);
                        await sleep(2000);
                        continue;
                    }

                    let selectionReconciled = selectionIsConfirmed(selectionOutcome);
                    let selectionRecovered = selectionReconciled;
                    if (!selectionReconciled) {
                        if (
                            selectionOutcome.round !== finalization.expectedNextRound ||
                            selectionOutcome.state !== "UPDATING" ||
                            !sameAddress(selectionOutcome.aggregator, process.env.ACCOUNT_ADDRESS)
                        ) {
                            console.error(
                                "On-chain state is not safe for normal aggregator selection; keeping UPDATING for retry:",
                                selectionOutcome,
                            );
                            await sleep(2000);
                            continue;
                        }

                        try {
                            await runOperation(
                                "aggregator.selection",
                                { role: "aggregator", round: finalization.expectedNextRound },
                                () => triggerAggregatorSelection(),
                            );
                            selectionOutcome = await readSelectionOutcome();
                            selectionReconciled = selectionIsConfirmed(selectionOutcome);
                            selectionRecovered = false;
                            if (!selectionReconciled) {
                                throw new Error("selection transaction returned without an on-chain TRAINING state");
                            }
                        } catch (selectionError) {
                            try {
                                selectionOutcome = await readSelectionOutcome();
                                selectionReconciled = selectionIsConfirmed(selectionOutcome);
                                selectionRecovered = selectionReconciled;
                            } catch (reconciliationError) {
                                console.error(
                                    "Aggregator selection failed and its on-chain outcome could not be reconciled:",
                                    selectionError,
                                    reconciliationError,
                                );
                                await sleep(2000);
                                continue;
                            }
                            if (!selectionReconciled) {
                                console.error(
                                    "Aggregator selection failed without an on-chain state change; keeping UPDATING for retry:",
                                    selectionError,
                                );
                                await runtimeEvent("aggregator.selection.failed", {
                                    role: "aggregator",
                                    round: finalization.expectedNextRound,
                                    error: selectionError?.message || String(selectionError),
                                });
                                await sleep(2000);
                                continue;
                            }
                            console.warn(
                                "Aggregator selection receipt was unavailable, but on-chain state confirms selection.",
                                selectionOutcome,
                            );
                        }
                    } else {
                        console.log("Recovered an already completed aggregator selection from on-chain state.");
                    }

                    console.log("Aggregator selection confirmed:", selectionOutcome);
                    await runtimeEvent("aggregator.selection.triggered", {
                        role: "aggregator",
                        round: finalization.expectedNextRound,
                        selected_aggregator: selectionOutcome.aggregator,
                        reconciled: selectionRecovered,
                    });
                    await stageCleaning();
                    console.log("Cleaned finalized round artifacts.");
                    continue;
                }
                const selectionGapRecovery = await recoverSelectionGap(
                    Number(await getRound()),
                    String(state[1]),
                    "UPDATING",
                );
                if (selectionGapRecovery !== null) {
                    if (selectionGapRecovery === "training-complete") {
                        await runtimeEvent("training.completed", {
                            role: "worker",
                            final_round: Number(await getRound()),
                            completed_rounds: Number(await getCompletedRoundCount()),
                        });
                        return;
                    }
                    await sleep(2000);
                    continue;
                }
                await monitorNonAggregatorStateProgress(
                    Number(await getRound()),
                    String(state[1]),
                    "UPDATING",
                );
                continue;

            default:
                console.log("Unknown state:", state);
                currentState = "IDLE";
                if (!sameAddress(state[1], process.env.ACCOUNT_ADDRESS)) {
                    await monitorNonAggregatorStateProgress(
                        Number(await getRound()),
                        String(state[1]),
                        String(state[0] || "UNKNOWN"),
                    );
                    continue;
                }
                await sleep(2000);
                continue;
        }
    }
};

const srcModelsDir = path.join(__dirname, '../received_models');
const resultsIIDDir = path.join(__dirname, '../data/results_iid');
const aggregationInputsDir = path.join(resultsIIDDir, 'aggregation_inputs');
async function stageAggregation() {
    await fs.mkdir(resultsIIDDir, { recursive: true });
    await fs.rm(aggregationInputsDir, { recursive: true, force: true });
    await fs.mkdir(aggregationInputsDir, { recursive: true });

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
        const stagedPath = path.join(aggregationInputsDir, file.name);
        await fs.copyFile(file.path, stagedPath);
        const stagedHash = normalizeHashHex(await modelFileHash(stagedPath), 32, "staged model hash");
        if (stagedHash !== file.recordedHash) {
            await fs.unlink(stagedPath).catch(() => {});
            throw new Error(`Staged model hash changed for ${file.address}`);
        }
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
    const retainParticipantPrivateKeyForInference = teeInferenceEnabled();
    try {
        activeParticipantActionSigner = await loadParticipantActionSigner();
        configureParticipantActionSigner(activeParticipantActionSigner);
        console.log("TEE participant action key initialized.", {
            source: activeParticipantActionSigner.source,
            actionAddress: activeParticipantActionSigner.address,
            logicalParticipant: process.env.ACCOUNT_ADDRESS,
        });
        await fundParticipantActionKey();

        if (teeInferenceEnabled()) {
            activeSelloReceiptPublicKey = await loadSelloReceiptPublicKey();
            console.log("TEE Sello receipt key initialized and will be bound by DCAP admission.", {
                publicKeySha256: crypto
                    .createHash("sha256")
                    .update(activeSelloReceiptPublicKey)
                    .digest("hex"),
            });
        } else {
            activeSelloReceiptPublicKey = undefined;
        }

        activeParticipantKey = await loadParticipantKey();
        await materializeParticipantPrivateKey(activeParticipantKey);
        console.log("Participant RSA key initialized inside the application TEE.", {
            source: activeParticipantKey.source,
            publicKeySha256: crypto
                .createHash("sha256")
                .update(activeParticipantKey.publicKeyDer)
                .digest("hex"),
        });
        await stateMachine();
    } catch (e) {
        console.error('stateMachine error:', e);
        process.exitCode = 1;
    } finally {
        try {
            await stopAggregatorServer();
        } catch (error) {
            console.error("Error while closing the model receiver after state-machine exit:", error);
            process.exitCode = 1;
        }
        activeParticipantActionSigner?.destroy();
        activeParticipantActionSigner = undefined;
        activeSelloReceiptPublicKey = undefined;
        activeParticipantKey = undefined;
        if (retainParticipantPrivateKeyForInference) {
            console.log(
                "Retaining the runtime participant private key for the co-located TEE inference receiver.",
            );
        } else {
            await fs.rm(participantPrivateKeyRuntimePath, { force: true }).catch((error) => {
                console.error("Could not remove the runtime participant private key:", error);
                process.exitCode = 1;
            });
        }
    }
}

runService();

let shuttingDown = false;
async function shutdown(signal) {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`${signal} received. Closing the model receiver before exiting.`);
    try {
        await stopAggregatorServer();
    } catch (error) {
        console.error("Error while closing the model receiver:", error);
    } finally {
        activeParticipantActionSigner?.destroy();
        activeParticipantActionSigner = undefined;
        activeSelloReceiptPublicKey = undefined;
        activeParticipantKey = undefined;
        if (teeInferenceEnabled()) {
            console.log(
                "Leaving runtime participant-key cleanup to the combined-service supervisor.",
            );
        } else {
            await fs.rm(participantPrivateKeyRuntimePath, { force: true }).catch((error) => {
                console.error("Could not remove the runtime participant private key:", error);
            });
        }
        process.exit(0);
    }
}

process.on('SIGINT', () => void shutdown('SIGINT'));
process.on('SIGTERM', () => void shutdown('SIGTERM'));

async function stageCleaning() {
    await Promise.all([
        cleanFilesInDir(srcModelsDir),
        cleanFilesInDir(resultsIIDDir),
        fs.rm(aggregationInputsDir, { recursive: true, force: true }),
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
