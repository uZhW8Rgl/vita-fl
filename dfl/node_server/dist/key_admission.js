/** Single on-chain quote appraisal for random worker keys and fresh restoration. */
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { Web3 } from 'web3';
import { DstackClient } from '@phala/dstack-sdk';
import { KEY_ADMISSION_ABI } from './key_admission_abi.js';
import { canonicalClaims, rawPublicKey, decryptApplicationKey } from './attested_key_protocol.js';
import { generateBundle, loadOrCreateKmsBundle } from './kms_keys.js';
import { loadParticipantActionSigner } from './action_key.js';
import { ed25519PublicKeyFromSeed } from './sello_key.js';
import { requireVolatileDirectory, RUNTIME_KEY_DIRECTORY } from './runtime_keys.js';
import { BROKER_STATE_PATH, keyServiceHttpRequest, serviceOrigin } from './attested_keys.js';
import { validateServiceTrustPolicy, validateServiceClaims, verifyServiceResponse, appraiseServiceEvidence } from './service_attestation.js';
let verifiedServiceProof;
/** Public diagnostic evidence only; never includes credentials or private keys. */
export const getVerifiedKeyServiceProof = () => verifiedServiceProof ? structuredClone(verifiedServiceProof) : null;
/** Authenticate the service before disclosing a participant-authorised quote. */
export async function authenticatedService(env = process.env) {
    const trust = validateServiceTrustPolicy(JSON.parse(env.KEY_SERVICE_TRUST_POLICY || 'null'));
    const origin = serviceOrigin(env.KEY_SERVICE_URL || '');
    if (trust.origin !== origin)
        throw new Error('Service origin differs from independent policy');
    const ca = env.KEY_SERVICE_CA_FILE ? await fs.readFile(env.KEY_SERVICE_CA_FILE, 'utf8') : undefined;
    const nonce = crypto.randomBytes(32).toString('hex');
    const result = await keyServiceHttpRequest(origin + '/v3/attestation', { nonce }, ca);
    const binding = { serviceInstanceId: result.body?.serviceInstanceId, servicePublicKey: result.body?.servicePublicKey };
    const proof = verifyServiceResponse(result.body, { ...binding, operation: '/v3/attestation', requestId: nonce }, result.status);
    const scope = { chainId: '31337', registry: String(env.REGISTRY_ADDRESS || env.EXPECTED_DEVICE_REGISTRY_ADDRESS || '').toLowerCase(),
        policy: String(env.KEY_SERVICE_POLICY || '').toLowerCase(), serviceId: env.KEY_SERVICE_ID };
    validateServiceClaims(proof.claims, trust, nonce, scope);
    if (proof.claims.serviceInstanceId !== binding.serviceInstanceId || proof.claims.servicePublicKey !== binding.servicePublicKey)
        throw new Error('Attested service signer mismatch');
    await appraiseServiceEvidence(proof.claims, proof.evidence, trust);
    const rpc = new Web3(env.RPC_URL);
    if (await rpc.eth.getChainId() !== 31337n || rpc.utils.keccak256(await rpc.eth.getCode(scope.policy)) !== proof.claims.codeHash)
        throw new Error('Attested service contract code mismatch');
    verifiedServiceProof = { nonce, envelope: result.body };
    const claims = proof.claims;
    return { binding, async request(operation, payload) {
            if (Math.floor(Date.now() / 1000) >= claims.expiresAt)
                throw new Error('Service attestation expired; start a fresh admission');
            const requestId = crypto.randomBytes(32).toString('hex');
            const response = await keyServiceHttpRequest(origin + operation, { ...binding, requestId, payload }, ca);
            if (Math.floor(Date.now() / 1000) >= claims.expiresAt)
                throw new Error('Service attestation expired during request');
            // A refusal is trusted as such only after checking its signature too.
            return verifyServiceResponse(response.body, { ...binding, operation, requestId }, response.status);
        } };
}
export const publicIdentity = async (bundle, env) => {
    const signer = await loadParticipantActionSigner({ env, deriveKey: async () => Buffer.from(bundle.action, 'hex') });
    try {
        return { actionKey: signer.address.toLowerCase(),
            rsaPublicKey: '0x' + crypto.createPublicKey(bundle.rsa).export({ type: 'spki', format: 'der' }).toString('hex'),
            selloKey: '0x' + ed25519PublicKeyFromSeed(Buffer.from(bundle.sello, 'hex')).toString('hex'),
            airKey: '0x' + ed25519PublicKeyFromSeed(Buffer.from(bundle.air, 'hex')).toString('hex') };
    }
    finally {
        signer.destroy();
    }
};
const identityForBundle = publicIdentity;
export async function initialiseOnchainKeys(env = process.env, testClaims = {}) {
    if (env.DOCKER !== 'phala' || env.PARTICIPANT_KEY_PROVIDER !== 'broker' || env.EXPECTED_CHAIN_ID !== '31337')
        throw new Error('Explicit Anvil/Phala broker profile required');
    await requireVolatileDirectory();
    const client = new DstackClient('/var/run/dstack.sock'), info = await client.info();
    const context = { participant: String(env.ACCOUNT_ADDRESS || '').toLowerCase(), appId: '0x' + info.app_id.replace(/^(0x|app_)/, '').toLowerCase(),
        composeHash: crypto.createHash('sha256').update(info.tcb_info.app_compose).digest('hex'), registry: String(env.REGISTRY_ADDRESS || env.EXPECTED_DEVICE_REGISTRY_ADDRESS || '').toLowerCase() };
    // Dedicated substitution tests may lie in the request; evidence always uses
    // the actual measured manifest below, so this cannot bypass admission.
    if (testClaims.claimedComposeHash) {
        if (env.EVALUATION_GATES_ENABLED !== 'true' || !/^[a-f0-9]{64}$/.test(testClaims.claimedComposeHash))
            throw new Error('Invalid evaluation claim');
        context.composeHash = testClaims.claimedComposeHash;
    }
    let restoring = false;
    try {
        const stat = await fs.lstat(BROKER_STATE_PATH);
        if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077) || stat.uid !== process.getuid?.())
            throw new Error('Unsafe sealed state');
        restoring = true;
    }
    catch (error) {
        if (error.code !== 'ENOENT')
            throw error;
    }
    // Generate before the quote, but never silently replace an already admitted identity.
    const proposed = restoring ? null : await generateBundle();
    const identity = proposed ? await identityForBundle(proposed, env) : null;
    const pair = crypto.generateKeyPairSync('x25519');
    const service = await authenticatedService(env);
    const response = await service.request('/v3/challenge', { ...context, recipientPublicKey: rawPublicKey(pair.publicKey), identity });
    const claims = response.claims;
    canonicalClaims(claims);
    for (const k of ['participant', 'appId', 'composeHash', 'registry'])
        if (claims[k] !== context[k])
            throw new Error('Admission context mismatch');
    if (claims.policy !== String(env.KEY_SERVICE_POLICY).toLowerCase() || claims.serviceId !== env.KEY_SERVICE_ID || claims.recipientPublicKey !== rawPublicKey(pair.publicKey) || response.request.restoring !== restoring)
        throw new Error('Admission service mismatch');
    if (identity && JSON.stringify(identity) !== JSON.stringify(response.identity))
        throw new Error('Proposed public identity changed');
    const web3 = new Web3(env.RPC_URL);
    if (await web3.eth.getChainId() !== 31337n)
        throw new Error('Anvil required');
    const contract = new web3.eth.Contract(KEY_ADMISSION_ABI, claims.policy);
    const report = String(await contract.methods.reportData(response.request, response.identity).call());
    const digest = String(await contract.methods.authorisationDigest(response.request, response.identity).call());
    const req = response.request;
    if (req.participant !== claims.participant || req.appId !== claims.appId || req.composeHash !== '0x' + claims.composeHash || req.challenge !== '0x' + claims.challenge || req.recipientPublicKey !== '0x' + claims.recipientPublicKey ||
        req.serviceInstanceId !== '0x' + service.binding.serviceInstanceId || req.servicePublicKey !== '0x' + service.binding.servicePublicKey || report !== response.reportData || digest !== response.digest)
        throw new Error('On-chain challenge mismatch');
    const account = web3.eth.accounts.privateKeyToAccount(env.PRIVATE_KEY || '');
    if (account.address.toLowerCase() !== context.participant)
        throw new Error('Participant credential mismatch');
    const quote = await client.getQuote(Buffer.from(report.slice(2), 'hex'));
    const envelope = await service.request('/v3/release', { challenge: claims.challenge, signature: account.sign(digest).signature,
        evidence: { quote: quote.quote.replace(/^0x/, ''), eventLog: typeof quote.event_log === 'string' ? quote.event_log : JSON.stringify(quote.event_log), appCompose: info.tcb_info.app_compose } });
    const root = decryptApplicationKey(envelope, claims, pair.privateKey), created = [];
    try {
        const bundle = await loadOrCreateKmsBundle(BROKER_STATE_PATH, root, { ...context, composeHash: '0x' + context.composeHash, chainId: '31337' }, async () => { if (!proposed)
            throw new Error('Restoration state missing'); return proposed; });
        if (JSON.stringify(await identityForBundle(bundle, env)) !== JSON.stringify(response.identity))
            throw new Error('Restored keys differ from admitted identity');
        for (const [name, bytes] of [['action-root.seed', Buffer.from(bundle.action, 'hex')], ['sello-signing.seed', Buffer.from(bundle.sello, 'hex')], ['air-signing.seed', Buffer.from(bundle.air, 'hex')], ['participant-private.pem', Buffer.from(bundle.rsa)]]) {
            const file = path.join(RUNTIME_KEY_DIRECTORY, name);
            try {
                await fs.writeFile(file, bytes, { mode: 0o600, flag: 'wx' });
                created.push(file);
            }
            finally {
                bytes.fill(0);
            }
        }
    }
    catch (error) {
        for (const file of created)
            await fs.rm(file, { force: true });
        throw error;
    }
    finally {
        root.fill(0);
    }
}
