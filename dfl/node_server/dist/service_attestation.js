/** Public service identity, attestation bindings, and authenticated HTTP responses. */
import crypto from 'node:crypto';
import { execFile } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
export const hex32 = (x) => typeof x === 'string' && /^[a-f0-9]{64}$/.test(x);
const keys = (x, names) => x && typeof x === 'object' && !Array.isArray(x) &&
    Object.keys(x).sort().join() === names.sort().join();
export const canonicalServiceJson = (x) => {
    const ordered = (v) => {
        if (v === null || typeof v === 'boolean')
            return v;
        if (typeof v === 'string')
            return v;
        if (typeof v === 'number' && Number.isSafeInteger(v))
            return v;
        if (Array.isArray(v))
            return v.map(ordered);
        if (v && Object.getPrototypeOf(v) === Object.prototype)
            return Object.fromEntries(Object.keys(v).sort().map(k => [k, ordered(v[k])]));
        throw new Error('Invalid canonical service value');
    };
    return Buffer.from(JSON.stringify(ordered(x)));
};
export function validateServiceTrustPolicy(value) {
    if (!keys(value, ['version', 'appId', 'composeHash', 'serviceImage', 'origin', 'platformMeasurements']) ||
        value.version !== 1 || !/^0x[a-f0-9]{40}$/.test(value.appId) || !hex32(value.composeHash) ||
        !/^[^\s@]+@sha256:[a-f0-9]{64}$/.test(value.serviceImage))
        throw new Error('Invalid service trust policy');
    const origin = new URL(value.origin);
    if (origin.protocol !== 'https:' || origin.origin !== value.origin || origin.username || origin.password)
        throw new Error('HTTPS service origin required');
    if (!Array.isArray(value.platformMeasurements) || !value.platformMeasurements.length || value.platformMeasurements.length > 32 ||
        value.platformMeasurements.some((p) => !keys(p, ['mrtd', 'rtmr0', 'rtmr1', 'rtmr2']) ||
            Object.values(p).some(x => typeof x !== 'string' || !/^[a-f0-9]{96}$/.test(x))))
        throw new Error('Explicit service platform references required');
    return value;
}
export const servicePolicyHash = (policy) => crypto.createHash('sha256')
    .update(canonicalServiceJson(validateServiceTrustPolicy(policy))).digest('hex');
export const serviceReportData = (claims) => crypto.createHash('sha512')
    .update('VITA-FL/key-service/identity/v1\0').update(canonicalServiceJson(claims)).digest();
export function validateServiceClaims(claims, policy, nonce, scope, now = Math.floor(Date.now() / 1000)) {
    validateServiceTrustPolicy(policy);
    if (!keys(claims, ['version', 'protocol', 'nonce', 'issuedAt', 'expiresAt', 'appId', 'composeHash', 'trustPolicyHash',
        'chainId', 'registry', 'policy', 'serviceId', 'codeHash', 'serviceInstanceId', 'servicePublicKey']) ||
        claims.version !== 1 || claims.protocol !== 'onchain-v3' || !hex32(nonce) || claims.nonce !== nonce ||
        claims.appId !== policy.appId || claims.composeHash !== policy.composeHash || claims.trustPolicyHash !== servicePolicyHash(policy) ||
        claims.chainId !== '31337' || !/^0x[a-f0-9]{40}$/.test(claims.registry) || !/^0x[a-f0-9]{40}$/.test(claims.policy) ||
        !/^0x[a-f0-9]{64}$/.test(claims.codeHash) || !hex32(claims.serviceId) ||
        !hex32(claims.serviceInstanceId) || !hex32(claims.servicePublicKey) ||
        !Number.isSafeInteger(claims.issuedAt) || !Number.isSafeInteger(claims.expiresAt) ||
        claims.issuedAt > now + 30 || claims.issuedAt < now - 150 || claims.expiresAt <= now ||
        claims.expiresAt <= claims.issuedAt || claims.expiresAt > claims.issuedAt + 120)
        throw new Error('Service attestation context or freshness rejected');
    if (scope && Object.entries(scope).some(([k, v]) => claims[k] !== v))
        throw new Error('Service contract context rejected');
}
const signatureInput = (value) => Buffer.concat([
    Buffer.from('VITA-FL/key-service/response/v1\0'), canonicalServiceJson(value),
]);
const edPublicKey = (raw) => {
    if (!hex32(raw))
        throw new Error('Invalid service public key');
    return crypto.createPublicKey({ key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), Buffer.from(raw, 'hex')]),
        format: 'der', type: 'spki' });
};
export function signServiceResponse(privateKey, binding, operation, requestId, status, payload) {
    if (!hex32(requestId))
        throw new Error('Invalid service request identifier');
    const value = { version: 1, ...binding, operation, requestId, status, payload };
    return { ...value, signature: crypto.sign(null, signatureInput(value), privateKey).toString('hex') };
}
export function verifyServiceResponse(value, expected, status) {
    if (!keys(value, ['version', 'serviceInstanceId', 'servicePublicKey', 'operation', 'requestId', 'status', 'payload', 'signature']) ||
        value.version !== 1 || value.status !== status || Object.entries(expected).some(([k, v]) => value[k] !== v) ||
        !/^[a-f0-9]{128}$/.test(value.signature))
        throw new Error('Service response binding rejected');
    const { signature, ...signed } = value;
    if (!crypto.verify(null, signatureInput(signed), edPublicKey(expected.servicePublicKey), Buffer.from(signature, 'hex')))
        throw new Error('Service response signature rejected');
    if (status !== 200)
        throw Object.assign(new Error('Authenticated key service refusal'), { statusCode: status });
    return value.payload;
}
/** The verifier only receives public evidence; no participant credential or key. */
export function appraiseServiceEvidence(claims, evidence, policy) {
    const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
    return new Promise((resolve, reject) => {
        const child = execFile('python3', ['-m', 'key_service.verify_service'], {
            cwd: root, env: { PATH: process.env.PATH, PYTHONPATH: root, LANG: 'C.UTF-8', PYTHONDONTWRITEBYTECODE: '1' }, timeout: 45000, maxBuffer: 65536,
        }, (error, stdout) => {
            if (error) {
                reject(new Error('Service quote appraisal rejected'));
                return;
            }
            try {
                if (JSON.parse(stdout).verified !== true)
                    throw new Error();
                resolve();
            }
            catch {
                reject(new Error('Service quote appraisal rejected'));
            }
        });
        child.stdin?.on('error', () => { });
        child.stdin?.end(JSON.stringify({ claims, evidence, policy }));
    });
}
