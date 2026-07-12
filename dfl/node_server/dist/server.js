// @ts-nocheck
// code adapted from pinata docs https://docs.pinata.cloud/quickstart/node-js
import 'dotenv/config';
import path from 'path';
import { fileURLToPath } from 'url';
import { getCurrentGM, getCurrentGMSignature, getCurrentGMKeyBundle, getCurrentState, setCurrentState, setContribution, getTopContributor, triggerAggregatorSelection, reportAggregatorTimeout, getRound, incrementRound, isAuthorized, getAuthorizedDevices, getDevicePublicKey, getDeviceRegistrationReportData, getBlockchainChainId, getLastRoundsAggregator, registerDeviceWithTeeQuoteAndRtmr3Events, submitModel, hasSubmittedModel, penalizeContribution } from "./bc_client.js";
import { getCurrentModel, updateGM } from "./ipfs.js";
import { deriveTimingConfig, validateTimingConfig } from "./state_timing.js";
import fs from 'fs/promises';
import { existsSync, readFileSync } from 'fs';
import { DstackClient, TappdClient, getComposeHash } from '@phala/dstack-sdk';
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
    if (!fetched.modelCid ||
        fetched.modelCid !== current.modelCid ||
        fetched.sigCid !== current.sigCid ||
        fetched.keyBundleCid !== current.keyBundleCid) {
        throw new Error("Fetched global model is no longer the current on-chain model; skipping local training.");
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
    }
    catch (e) {
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
            }
            catch (reportError) {
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
            try {
                data = JSON.parse(text);
            }
            catch {
                data = { raw: text };
            }
        }
        if (!res.ok || data.ok === false) {
            throw new Error(`Python service ${endpoint} failed (${res.status}): ${text}`);
        }
        return data;
    }
    finally {
        if (timeout)
            clearTimeout(timeout);
    }
}
async function stopAggregatorServer() {
    if (!aggregatorServerRunning)
        return;
    await callPythonService('/server/stop');
    aggregatorServerRunning = false;
}
function normalizeHexBytes(input) {
    let hex = Buffer.isBuffer(input) ? input.toString('utf8') : String(input || '');
    hex = hex.trim();
    if (hex.startsWith('0x') || hex.startsWith('0X'))
        hex = hex.slice(2);
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
        }
        catch (error) {
            if (error?.code !== 'ENOENT') {
                console.warn(`Could not read Phala app-code candidate ${candidate}:`, error?.message || error);
            }
        }
    }
    return null;
}
function appCodeFromDstackInfo(info) {
    if (!info || typeof info !== 'object')
        return null;
    if (info.tcb_info?.app_compose !== undefined) {
        const appCompose = info.tcb_info.app_compose;
        if (typeof appCompose === 'string')
            return appCompose;
        return `${JSON.stringify(appCompose, null, 2)}\n`;
    }
    for (const key of ['app_code', 'appCode', 'app_compose', 'appCompose', 'compose', 'docker_compose_file']) {
        if (info[key] === undefined)
            continue;
        if (typeof info[key] === 'string')
            return info[key];
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
    if (process.env.PHALA_PUBLISH_ATTESTATION_ARTIFACTS === '0')
        return;
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
    if (hex.startsWith('0x') || hex.startsWith('0X'))
        hex = hex.slice(2);
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
function tcbInfoFromDstackInfo(info) {
    return info?.tcb_info || info?.tcbInfo || info?.tcb || null;
}
function extractWorkerImageRefs(appCompose) {
    const dockerComposeText = String(appCompose?.docker_compose_file || '');
    if (!dockerComposeText) {
        throw new Error('app_compose does not include docker_compose_file');
    }
    const matches = [...dockerComposeText.matchAll(/^\s*image:\s*["']?([^"'\s#]+)["']?/gm)]
        .map(match => match[1]);
    if (matches.length === 0) {
        throw new Error('docker_compose_file does not include an image reference');
    }
    return matches;
}
function extractImageDigest(imageRef) {
    const match = String(imageRef || '').match(/@sha256:([0-9a-fA-F]{64})(?:$|[^\w])/);
    return match ? `0x${match[1].toLowerCase()}` : null;
}
function measuredWorkerIdentity(info) {
    const tcbInfo = tcbInfoFromDstackInfo(info);
    const appCompose = parseAppCompose(tcbInfo?.app_compose || tcbInfo?.appCompose);
    const measuredComposeHash = normalizeHashHex(getComposeHash(appCompose, true), 32, 'measured app_compose hash');
    const imageRefs = extractWorkerImageRefs(appCompose);
    const matchedImageRef = imageRefs.find(imageRef => imageRef.includes('master-thesis-dfl-worker@sha256:')) || imageRefs[0];
    if (!matchedImageRef) {
        throw new Error('Measured app_compose does not contain a worker image reference');
    }
    const imageDigest = extractImageDigest(matchedImageRef);
    if (!imageDigest) {
        throw new Error(`Measured worker image is not digest-pinned: ${matchedImageRef}`);
    }
    return { imageRef: matchedImageRef, imageDigest, composeHash: measuredComposeHash };
}
function verifyMeasuredWorkerImage(info, liveComposeHash) {
    const identity = measuredWorkerIdentity(info);
    const quoteComposeHash = normalizeHashHex(liveComposeHash, 32, 'live quote compose hash');
    if (identity.composeHash !== quoteComposeHash) {
        throw new Error(`dstack info compose hash ${identity.composeHash} does not match quote compose hash ${quoteComposeHash}`);
    }
    console.log('Verified measured worker image reference:', {
        image: identity.imageRef,
        imageDigest: identity.imageDigest,
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
        const identity = measuredWorkerIdentity(info);
        const reportData = await boundReportData(reportDataFactory, identity);
        const quote = await client.getQuote(reportData);
        await publishLivePhalaArtifacts(quote, info).catch(error => {
            console.warn('Could not publish live Phala attestation artifacts:', error?.message || error);
        });
        const rtmr3Policy = normalizeRtmr3EventPolicy(quote.event_log);
        const workerImage = verifyMeasuredWorkerImage(info, rtmr3Policy.composeHash);
        return {
            quoteHex: normalizeHexBytes(quote.quote),
            rtmr3EventDigests: rtmr3Policy.digests,
            composeHash: rtmr3Policy.composeHash,
            workerImageDigest: workerImage.imageDigest,
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
        const identity = measuredWorkerIdentity(info);
        const reportData = await boundReportData(reportDataFactory, identity);
        const quote = await client.tdxQuote(reportData, 'raw');
        await publishLivePhalaArtifacts(quote, info).catch(error => {
            console.warn('Could not publish live Phala attestation artifacts:', error?.message || error);
        });
        const rtmr3Policy = normalizeRtmr3EventPolicy(quote.event_log);
        const workerImage = verifyMeasuredWorkerImage(info, rtmr3Policy.composeHash);
        return {
            quoteHex: normalizeHexBytes(quote.quote),
            rtmr3EventDigests: rtmr3Policy.digests,
            composeHash: rtmr3Policy.composeHash,
            workerImageDigest: workerImage.imageDigest,
        };
    }
    throw new Error('Neither /var/run/dstack.sock nor /var/run/tappd.sock is available for Phala attestation.');
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
        if (err && err.code === 'ENOENT')
            return [];
        throw err;
    });
    const files = [];
    for (const entry of entries) {
        if (!entry.isFile() || !entry.name.endsWith('.bin'))
            continue;
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
    return new Set(files
        .filter(file => file.authorized && file.address)
        .map(file => file.address.toLowerCase()));
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
        if (received.has(address.toLowerCase()))
            continue;
        if (await isAuthorized(address)) {
            missing.push(address);
        }
    }
    return missing;
}
async function hasCurrentDeviceRegistration(publicKey) {
    const accountAddress = process.env.ACCOUNT_ADDRESS;
    if (!accountAddress || !(await isAuthorized(accountAddress)))
        return false;
    const storedKey = await getDevicePublicKey(accountAddress);
    return String(storedKey || '').toLowerCase() === String(publicKey || '').toLowerCase();
}
async function registerWithLocalTdxMock() {
    if (localTdxRegistrationDone || process.env.DOCKER === "phala")
        return;
    localTdxRegistrationDone = true;
    if (process.env.LOCAL_TDX_MOCK !== '1') {
        throw new Error('Local TDX registration requires LOCAL_TDX_MOCK=1 or a live TEE quote path');
    }
    const rpcUrl = String(process.env.SEPOLIA_RPC_URL || '');
    const localRpc = /^http:\/\/(anvil|127\.0\.0\.1|localhost):8545\/?$/i.test(rpcUrl);
    if (!localRpc || await getBlockchainChainId() !== 31337) {
        throw new Error('LOCAL_TDX_MOCK is restricted to the local Anvil endpoint on chain 31337');
    }
    const composeHash = normalizeHashHex(process.env.LOCAL_TDX_COMPOSE_HASH || '0x47d7ddfa97906d05b2b7e53ce888440a820598f120079ed887229ab0302982fa', 32, 'local mock compose hash');
    const workerImageDigest = normalizeHashHex(process.env.LOCAL_TDX_IMAGE_DIGEST || '0x7849ee527ff2efc746c58f67cd6336572d5c71743c608bbd3810079289c7c066', 32, 'local mock worker image digest');
    const publicKey = rsaPublicKeyDerHex();
    if (await hasCurrentDeviceRegistration(publicKey)) {
        console.log('Device already has a bound registration for the current RSA key; reusing it.');
        return;
    }
    const publicIp = process.env.PUBLIC_IP || '';
    const brokerIp = process.env.MSG_BROKER_IP || '';
    const reportData = await getDeviceRegistrationReportData(process.env.ACCOUNT_ADDRESS, publicIp, brokerIp, publicKey, composeHash, workerImageDigest);
    console.warn('Registering through the LOCAL-ONLY mock TDX verifier; this is not a hardware attestation.');
    await registerDeviceWithTeeQuoteAndRtmr3Events(reportData, [`0x${'00'.repeat(48)}`], composeHash, workerImageDigest, process.env.ACCOUNT_ADDRESS, publicIp, brokerIp, publicKey);
    console.log('Device registered with bound local mock REPORTDATA.');
}
function decodeEventBytes(rawValue) {
    if (Buffer.isBuffer(rawValue) || Array.isArray(rawValue) || ArrayBuffer.isView(rawValue)) {
        return Buffer.from(rawValue);
    }
    if (typeof rawValue === 'string') {
        let encoded = rawValue.trim();
        if (encoded === '')
            return Buffer.alloc(0);
        if (encoded.startsWith('0x') || encoded.startsWith('0X'))
            encoded = encoded.slice(2);
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
                if (decoded)
                    return decoded;
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
    const digests = rtmr3Events.map(event => {
        let digest = decodeEventBytes(event?.digest);
        if ((!digest || digest.length === 0) && Number(event?.event_type) === 0x08000001) {
            const payload = decodeEventBytes(event?.event_payload);
            if (!payload) {
                throw new Error(`Invalid RTMR3 event payload for event ${event?.event || '<unknown>'}`);
            }
            const eventType = Buffer.alloc(4);
            eventType.writeUInt32LE(0x08000001);
            digest = crypto.createHash('sha384')
                .update(eventType)
                .update(':')
                .update(String(event?.event || ''), 'utf8')
                .update(':')
                .update(payload)
                .digest();
        }
        if (!digest || digest.length === 0 || digest.length > 48) {
            throw new Error(`Invalid RTMR3 event digest for event ${event?.event || '<unknown>'}`);
        }
        // Legacy tappd pads event digests to the 48-byte SHA-384 RTMR input width.
        return `0x${Buffer.concat([digest, Buffer.alloc(48 - digest.length)]).toString('hex')}`;
    });
    if (digests.length === 0) {
        throw new Error('Phala quote response did not include RTMR3 event digests');
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
            digest: digests[composeEventIndex],
            composeHash,
        });
    }
    else {
        console.warn('Live RTMR3 event log does not contain a compose-hash event.');
    }
    return { digests, composeHash };
}
function derHexToBuffer(derHex) {
    if (typeof derHex !== 'string')
        return Buffer.alloc(0);
    let hex = derHex.trim();
    if (hex.startsWith('0x') || hex.startsWith('0X'))
        hex = hex.slice(2);
    hex = hex.replace(/\s+/g, '');
    if (hex.length === 0 || (hex.length % 2) !== 0)
        return Buffer.alloc(0);
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
    }
    catch (e) {
        console.error(`Signature verification: cannot read model file ${modelPath}`, e);
        return false;
    }
    try {
        sigBytes = await fs.readFile(sigPath);
    }
    catch (e) {
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
    }
    catch (e) {
        console.error('Signature verification: failed to parse public key (DER/SPKI)', e);
        return false;
    }
    try {
        const ok = crypto.verify('RSA-SHA256', modelBytes, { key: keyObject, padding: crypto.constants.RSA_PKCS1_PADDING }, sigBytes);
        return !!ok;
    }
    catch (e) {
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
    const signatureBytes = crypto.sign('RSA-SHA256', inputBytes, {
        key: rsaPrivateKey,
        padding: crypto.constants.RSA_PKCS1_PADDING,
    });
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
    await signFileWithLocalRsaKey(path.join(resultsIIDDir, "aggregated.bin"), path.join(resultsIIDDir, "aggregated.bin.sig"));
    console.log("Round 0 bootstrap rollover prepared aggregated.bin + aggregated.bin.sig for encrypted republish.");
}
const stateMachine = async () => {
    if (process.env.DOCKER === "phala") {
        const publicKey = rsaPublicKeyDerHex();
        if (await hasCurrentDeviceRegistration(publicKey)) {
            console.log('Device already has a bound registration for the current RSA key; reusing it.');
        }
        else {
            console.log("Fetching TDX Quote ...");
            const publicIp = process.env.PUBLIC_IP || "";
            const brokerIp = process.env.MSG_BROKER_IP || "";
            const { quoteHex, rtmr3EventDigests, composeHash, workerImageDigest } = await fetchLivePhalaQuote(async (identity) => getDeviceRegistrationReportData(process.env.ACCOUNT_ADDRESS, publicIp, brokerIp, publicKey, identity.composeHash, identity.imageDigest));
            console.log(`Registering with live Phala TDX quote and ${rtmr3EventDigests.length} RTMR3 event digests ...`);
            await registerDeviceWithTeeQuoteAndRtmr3Events(quoteHex, rtmr3EventDigests, composeHash, workerImageDigest, process.env.ACCOUNT_ADDRESS, publicIp, brokerIp, publicKey);
            console.log("Device registered with onchain TDX quote and RTMR3 event replay verification.");
        }
    }
    else {
        await registerWithLocalTdxMock();
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
                        await setCurrentState("AGGREGATING");
                        continue;
                    }
                    catch (e) {
                        console.error("Error during starting the aggregator server:", e);
                        return;
                    }
                }
                else {
                    console.log("I am not the aggregator");
                    await runtimeEvent("state.training.worker", { role: "worker", aggregator: String(state["1"]) });
                    const currentRound = Number(await getRound());
                    console.log("Round %d.", currentRound);
                    if (await hasSubmittedModel(currentRound, process.env.ACCOUNT_ADDRESS)) {
                        await waitForSubmittedRoundToAdvance(currentRound);
                        await sleep(2000);
                        continue;
                    }
                    console.log("Fetching the global model from IPFS ...");
                    await runtimeEvent("worker.fetch_global_model.started", { role: "worker" });
                    let fetchedGlobalModel;
                    try {
                        fetchedGlobalModel = await runOperation("worker.fetch_global_model", { role: "worker" }, () => getCurrentModel());
                    }
                    catch (error) {
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
                    }
                    catch (error) {
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
                    }
                    catch (e) {
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
                        if (await hasSubmittedModel(currentRound, process.env.ACCOUNT_ADDRESS)) {
                            console.log(`Model submission already recorded for round ${currentRound}; skipping duplicate submit/contribution transactions.`);
                        }
                        else {
                            await runOperation("worker.submit_model", {
                                role: "worker",
                                aggregator: String(state["1"]),
                            }, async () => submitModel(await localModelPackageHash()));
                            await runOperation("worker.set_contribution", {
                                role: "worker",
                                aggregator: String(state["1"]),
                            }, () => setContribution([process.env.ACCOUNT_ADDRESS]));
                        }
                        await runtimeEvent("worker.model_transfer.finished", { role: "worker", aggregator: String(state["1"]) });
                    }
                    catch (e) {
                        console.error("Error during model transfer:", e);
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
                    }
                    catch (e) {
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
                            }
                            catch (reportError) {
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
                            await runtimeEvent("aggregator.penalty.applied", {
                                role: "aggregator",
                                reason: "missed_model_deadline",
                                count: missingWorkers.length,
                            });
                        }
                        else if (currentRound === 0) {
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
                    }
                    catch (e) {
                        console.error("Error during aggregation:", e);
                        await sleep(2000);
                        continue;
                    }
                    console.log("Aggregation complete.");
                    await runtimeEvent("aggregator.aggregation.finished", { role: "aggregator" });
                    await setCurrentState("UPDATING");
                    continue;
                }
                break;
            case "UPDATING":
                if (sameAddress(state[1], process.env.ACCOUNT_ADDRESS)) {
                    currentState = "UPDATING";
                    console.log("I am the aggregator");
                    console.log("Starting the updating process ...");
                    await runtimeEvent("aggregator.update.started", { role: "aggregator" });
                    try {
                        await runOperation("aggregator.update_global_model", { role: "aggregator" }, () => updateGM());
                    }
                    catch (e) {
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
                    }
                    catch (e) {
                        console.error("Aggregator selection failed; falling back to current aggregator for next round:", e);
                        await runtimeEvent("aggregator.selection.failed", {
                            role: "aggregator",
                            error: e?.message || String(e),
                        });
                        await setCurrentState("TRAINING");
                        console.log("Set state to TRAINING for next round with the current aggregator");
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
        await Promise.all(destEntries
            .filter(n => n.endsWith('.bin'))
            .map(n => fs.unlink(path.join(resultsIIDDir, n)).catch(() => { })));
    }
    catch { }
    const modelFiles = await receivedWorkerModelFiles();
    const acceptedFiles = modelFiles.filter(file => file.authorized);
    const rejectedFiles = modelFiles.filter(file => !file.authorized);
    for (const file of rejectedFiles) {
        console.warn("Ignoring received model %s (%s)", file.path, file.reason);
        await fs.unlink(file.path).catch(() => { });
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
    }
    catch (e) {
        if (e && e.code === 'ENOENT')
            return 0;
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
        if (cid && cid !== prevCid)
            return cid;
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
    }
    catch (e) {
        console.error('stateMachine error:', e);
    }
}
runService();
process.on('SIGINT', () => {
    console.log('SIGINT received. Exiting.');
    stopAggregatorServer().catch(() => { });
    process.exit(0);
});
process.on('SIGTERM', () => {
    console.log('SIGTERM received. Exiting.');
    stopAggregatorServer().catch(() => { });
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
                await fs.unlink(full).catch(() => { });
            }
        }));
    }
    catch (err) {
        if (err && err.code === 'ENOENT')
            return;
        throw err;
    }
}
