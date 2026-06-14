// @ts-nocheck
// code adapted from pinata docs https://docs.pinata.cloud/quickstart/node-js
import 'dotenv/config';
import path from 'path';
import { fileURLToPath } from 'url';
import { getCurrentGM, setGlobalModel, getCurrentState, getAggregatorEndpoint, setAggregatorEndpoint, setCurrentState, setContribution, getTopContributor, triggerAggregatorSelection, reportAggregatorTimeout, getRound, incrementRound, isAuthorized, getAuthorizedDevices, getDevicePublicKey, getPreviousAggregatorFromGMStorage, getLastRoundsAggregator, registerDeviceWithTeeQuote, registerDeviceWithTeeQuoteAndRtmr3Events, submitModel, penalizeContribution } from "./bc_client.js";
import { getCurrentModel, pinFile, getFileFromIPFS, updateGM } from "./ipfs.js";
import { deriveTimingConfig, validateTimingConfig } from "./state_timing.js";
import { recordRoundEvent, recordRoundSpan } from "./telemetry.js";
import fs from 'fs/promises';
import { DstackClient } from '@phala/dstack-sdk';
import crypto from 'crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const deviceID = process.env.DEVICE_ID;
let currentState = "";
let aggregatorServerRunning = false;
const pythonServiceUrl = process.env.PYTHON_SERVICE_URL || 'http://127.0.0.1:8000';
const rsaPublicKey = process.env.RSA_PUBLIC_KEY.replace(/\\n/g, '\n') || "";
const rsaPrivateKey = process.env.RSA_PRIVATE_KEY.replace(/\\n/g, '\n') || "";
let localTdxRegistrationDone = false;
const timingConfig = deriveTimingConfig(process.env);
const modelSubmissionDeadlineMs = timingConfig.modelSubmissionDeadlineMs;
const gmUpdateTimeoutMs = timingConfig.gmUpdateTimeoutMs;
const gmUpdateTimeoutLoops = timingConfig.gmUpdateTimeoutLoops;
for (const warning of validateTimingConfig(timingConfig)) {
    console.warn("Timing config warning:", warning);
}
let missedGMUpdateLoops = 0;

async function traceEvent(name, attributes = {}) {
    const round = Number(await getRound().catch(() => 0));
    await recordRoundEvent(name, {
        round,
        role: attributes.role || "",
        attributes,
    });
}

async function traceOperation(name, attributes, operation) {
    const round = Number(await getRound().catch(() => 0));
    const startTimeMs = Date.now();
    try {
        const result = await operation();
        await recordRoundSpan(name, {
            round,
            role: attributes.role || "",
            startTimeMs,
            endTimeMs: Date.now(),
            status: "OK",
            attributes,
        });
        return result;
    } catch (error) {
        await recordRoundSpan(name, {
            round,
            role: attributes.role || "",
            startTimeMs,
            endTimeMs: Date.now(),
            status: "ERROR",
            attributes: {
                ...attributes,
                error: error?.message || String(error),
            },
        });
        throw error;
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
    const key = crypto.createPublicKey(rsaPublicKey);
    const der = key.export({ format: 'der', type: 'spki' });
    return `0x${Buffer.from(der).toString('hex')}`;
}

async function localModelPackageHash() {
    const data = await fs.readFile('./data/lm.bin.enc');
    return `0x${crypto.createHash('sha256').update(data).digest('hex')}`;
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

async function prepareRoundZeroBootstrapRollover() {
    console.log("Round 0 bootstrap rollover: fetching current encrypted GM bundle.");
    await getCurrentModel();

    const lastSignerAddress = await getLastRoundsAggregator();
    const lastSignersPubKey = await getDevicePublicKey(lastSignerAddress);
    const sigOk = await verifyDownloadedGlobalModelSignature({
        publicKeyDerHex: lastSignersPubKey,
        modelPath: "./data/gm.bin",
        sigPath: "./data/gm.bin.sig",
    });
    if (!sigOk) {
        throw new Error("Round-0 bootstrap rollover signature verification failed.");
    }

    await fs.mkdir(resultsIIDDir, { recursive: true });
    await fs.copyFile("./data/gm.bin", path.join(resultsIIDDir, "aggregated.bin"));
    await signFileWithLocalRsaKey(
        path.join(resultsIIDDir, "aggregated.bin"),
        path.join(resultsIIDDir, "aggregated.bin.sig"),
    );
    console.log("Round 0 bootstrap rollover prepared aggregated.bin + aggregated.bin.sig for encrypted republish.");
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
    while (Number(await getRound()) < Number(process.env.ROUND)) {
        let state = await getCurrentState();

        switch (state["0"]) {
            case "TRAINING":
                if (state[1] === process.env.ACCOUNT_ADDRESS) {
                    console.log("I am the aggregator");
                    await traceEvent("state.training.aggregator", { role: "aggregator" });
                    const currentRound = Number(await getRound());
                    console.log("Round %d.", currentRound);
                    if (currentRound === 0) {
                        console.log("Round 0 bootstrap phase: skipping worker submission wait and proceeding directly to aggregation.");
                        await setCurrentState("AGGREGATING");
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
                        await traceEvent("aggregator.wait_for_models.started", {
                            role: "aggregator",
                            expected_models: expected,
                            deadline_ms: modelSubmissionDeadlineMs,
                        });
                        const present = await traceOperation("aggregator.wait_for_models", {
                            role: "aggregator",
                            expected_models: expected,
                            deadline_ms: modelSubmissionDeadlineMs,
                        }, () => waitForModels(expected, {
                            dir: srcModelsDir,
                            pollMs: 2000,
                            timeoutMs: modelSubmissionDeadlineMs,
                        }));
                        await traceEvent("aggregator.wait_for_models.finished", {
                            role: "aggregator",
                            expected_models: expected,
                            present_models: present,
                        });
                        if (present <= 0) {
                            console.log("No model submissions yet. Keeping aggregator server open and waiting before retry.");
                            await sleep(5000);
                            continue;
                        }
                        await setCurrentState("AGGREGATING");
                        continue;
                    } catch (e) {
                        console.error("Error during starting the aggregator server:", e);
                        return;
                    }
                } else {
                    console.log("I am not the aggregator");
                    await traceEvent("state.training.worker", { role: "worker", aggregator: String(state["1"]) });
                    const currentRound = Number(await getRound());
                    console.log("Round %d.", currentRound);
                    console.log("Fetching the global model from IPFS ...");
                    await traceEvent("worker.fetch_global_model.started", { role: "worker" });
                    try {
                        await traceOperation("worker.fetch_global_model", { role: "worker" }, () => getCurrentModel());
                    } catch (error) {
                        if (!isMissingRoundKeyError(error)) {
                            throw error;
                        }
                        console.warn(`No round key for ${process.env.ACCOUNT_ADDRESS} in round ${currentRound}. Waiting for the next round after registration.`);
                        await traceEvent("worker.fetch_global_model.deferred", {
                            role: "worker",
                            round: currentRound,
                            reason: "missing_round_key",
                        });
                        await waitForRoundAdvance(currentRound, { pollMs: 5000 });
                        continue;
                    }
                    await traceEvent("worker.fetch_global_model.finished", { role: "worker" });

                    const prevGM = await getCurrentGM();

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
                    await traceEvent("worker.training.started", { role: "worker" });
                    try {
                        await traceOperation("worker.training", { role: "worker" }, async () => callPythonService('/train', {
                            epochs: Number(process.env.EPOCH),
                            aggregator_public_key_der_hex: await getDevicePublicKey(state[1]),
                        }));
                    } catch (e) {
                        console.error("Error during local training:", e);
                        return;
                    }
                    console.log("Local training complete.");
                    await traceEvent("worker.training.finished", { role: "worker" });

                    console.log("Is the device authorized? ", await isAuthorized(process.env.ACCOUNT_ADDRESS));
                    console.log("Starting the zerompq client ...");
                    await traceEvent("worker.model_transfer.started", { role: "worker", aggregator: String(state["1"]) });
                    try {
                        await traceOperation("worker.model_transfer", {
                            role: "worker",
                            aggregator: String(state["1"]),
                        }, () => callPythonService('/client', {
                            server_ip: String(state["1"]),
                            device_id: String(process.env.ACCOUNT_ADDRESS),
                        }));
                        await traceOperation("worker.submit_model", {
                            role: "worker",
                            aggregator: String(state["1"]),
                        }, async () => submitModel(await localModelPackageHash()));
                        await traceOperation("worker.set_contribution", {
                            role: "worker",
                            aggregator: String(state["1"]),
                        }, () => setContribution([process.env.ACCOUNT_ADDRESS]));
                        await traceEvent("worker.model_transfer.finished", { role: "worker", aggregator: String(state["1"]) });
                    } catch (e) {
                        console.error("Error during model transfer:", e);
                        await traceEvent("worker.model_transfer.failed", {
                            role: "worker",
                            aggregator: String(state["1"]),
                            error: e?.message || String(e),
                        });
                        return;
                    }
                    console.log("Top contributor:", await getTopContributor());
                    console.log("Transfer complete. Send local model to aggregator.");
                    console.log("Rounds left: ", (Number(process.env.ROUND) - Number(await getRound())));
                    console.log("Waiting till next round.");
                    try {
                        const newGM = await waitForGMUpdate(prevGM, { pollMs: 5000, timeoutMs: gmUpdateTimeoutMs });
                        missedGMUpdateLoops = 0;
                        console.log("New Global Model detected:", newGM);
                    } catch (e) {
                        missedGMUpdateLoops++;
                        console.warn(`No new GM within timeout. Missed update loop ${missedGMUpdateLoops}/${gmUpdateTimeoutLoops}.`);
                        if (missedGMUpdateLoops >= gmUpdateTimeoutLoops) {
                            console.warn("Reporting aggregator timeout onchain.");
                            await traceEvent("worker.aggregator_timeout.reported", {
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
                if (state[1] === process.env.ACCOUNT_ADDRESS) {
                    currentState = "AGGREGATING";
                    console.log("I am the aggregator");
                    console.log("Starting the aggregation process ...");
                    await traceEvent("aggregator.aggregation.started", { role: "aggregator" });
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
                        await traceEvent("aggregator.models.staged", {
                            role: "aggregator",
                            model_count: count,
                            expected_models: expected,
                        });
                        if (count <= 0) {
                            if (currentRound === 0) {
                                console.log("Round 0 has no worker submissions. Re-publishing the verified bootstrap model for the next encrypted round.");
                                await traceOperation("aggregator.round0.bootstrap_rollover", {
                                    role: "aggregator",
                                    round: currentRound,
                                }, () => prepareRoundZeroBootstrapRollover());
                                await setCurrentState("UPDATING");
                                continue;
                            }
                            console.log("No models to aggregate (count=0). Keeping round open and waiting for workers.");
                            await sleep(5000);
                            continue;
                        }
                        const missingWorkers = currentRound === 0 ? [] : await getMissingAuthorizedWorkers();
                        if (missingWorkers.length > 0) {
                            console.log("Penalizing missing model submissions:", missingWorkers);
                            await penalizeContribution(missingWorkers, "missed_model_deadline");
                            await traceEvent("aggregator.penalty.applied", {
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

                        await traceOperation("aggregator.aggregation", {
                            role: "aggregator",
                            model_count: count,
                            expected_models: expected,
                        }, () => callPythonService('/aggregate', {
                            num_files: Number(count),
                        }, { timeoutMs: 3 * 60 * 1000 }));
                    } catch (e) {
                        console.error("Error during aggregation:", e);
                        await sleep(2000);
                        continue;
                    }
                    console.log("Aggregation complete.");
                    await traceEvent("aggregator.aggregation.finished", { role: "aggregator" });
                    await setCurrentState("UPDATING");
                    continue;
                }
                break;

            case "UPDATING":
                if (state[1] === process.env.ACCOUNT_ADDRESS) {
                    currentState = "UPDATING";
                    console.log("I am the aggregator");
                    console.log("Starting the updating process ...");
                    await traceEvent("aggregator.update.started", { role: "aggregator" });
                    try {
                        await traceOperation("aggregator.update_global_model", { role: "aggregator" }, () => updateGM());
                    } catch (e) {
                        console.error("Error during updating the global model:", e);
                        return;
                    }
                    console.log("Updating complete.");
                    await traceEvent("aggregator.update.finished", { role: "aggregator" });
                    console.log("Current Global Model:", await getCurrentGM());
                    await stageCleaning();
                    console.log("Passed cleaning");
                    await incrementRound();
                    console.log("Rounds left: ", (Number(process.env.ROUND) - Number(await getRound())));
                    await setCurrentState("TRAINING");
                    console.log("Set state to TRAINING for next round");
                    try {
                        await traceOperation("aggregator.selection", { role: "aggregator" }, () => triggerAggregatorSelection());
                        console.log("Triggered new aggregator selection");
                        await traceEvent("aggregator.selection.triggered", { role: "aggregator" });
                    } catch (e) {
                        console.error("Aggregator selection failed; keeping current aggregator alive for retry:", e);
                        await traceEvent("aggregator.selection.failed", {
                            role: "aggregator",
                            error: e?.message || String(e),
                        });
                        await sleep(2000);
                    }

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

async function waitForRoundAdvance(prevRound, { pollMs = 3000 } = {}) {
    while (true) {
        const currentRound = Number(await getRound());
        if (currentRound > Number(prevRound)) {
            return currentRound;
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
