/** Dedicated observations using the published worker's production key loaders.
 * Only public fingerprints and bounded custody facts leave this workload.
 */
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {createRequire} from 'node:module';
import {loadParticipantKey, materializeParticipantPrivateKey} from '../../dfl/node_server/dist/participant_key.js';
import {loadParticipantActionSigner} from '../../dfl/node_server/dist/action_key.js';
import {loadSelloReceiptPublicKey, ed25519PublicKeyFromSeed} from '../../dfl/node_server/dist/sello_key.js';
import {initializeRuntimeSigningKeys, readRuntimeSigningSeed, requireVolatileDirectory,
    RUNTIME_KEY_DIRECTORY, AIR_RUNTIME_KEY, SELLO_RUNTIME_KEY} from '../../dfl/node_server/dist/runtime_keys.js';

const execute = promisify(execFile);
const {recover} = createRequire(new URL('../../dfl/node_server/package.json',import.meta.url))('web3-eth-accounts');
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
let keys;

async function initialize() {
    await requireVolatileDirectory();
    // Match production supervisor initialization after a workload lifetime ends.
    for (const name of [AIR_RUNTIME_KEY, SELLO_RUNTIME_KEY, 'participant-private.pem', 'participant-public.pem']) {
        await fs.rm(path.join(RUNTIME_KEY_DIRECTORY, name), {force:true});
    }
    await initializeRuntimeSigningKeys();
    const rsa = await loadParticipantKey();
    await materializeParticipantPrivateKey(rsa);
    const action = await loadParticipantActionSigner();
    const sello = await loadSelloReceiptPublicKey();
    const airSeed = await readRuntimeSigningSeed(AIR_RUNTIME_KEY);
    let air;
    try {air = ed25519PublicKeyFromSeed(airSeed);} finally {airSeed.fill(0);}
    const directory = '/var/lib/vita-fl-fixtures';
    await fs.mkdir(directory, {recursive:true,mode:0o700});
    const filename = path.join(directory, 'rsa-envelope.bin');
    const surrogate = crypto.randomBytes(32);
    try {
        const ciphertext = crypto.publicEncrypt({key:rsa.publicKey,oaepHash:'sha256'},surrogate);
        await fs.writeFile(filename,ciphertext,{flag:'wx',mode:0o600}).catch(error=>{
            if(error.code!=='EEXIST') throw error;
        });
    } finally {surrogate.fill(0);}
    return {rsa,action,sello,air,filename};
}

export async function allKeyObservation(challenge) {
    keys ||= initialize();
    const key = await keys;
    const python = JSON.parse((await execute('python3', ['/evaluation/phala-key-probe/security.py','keys',challenge],
        {maxBuffer:262144,timeout:90000})).stdout);
    const ciphertext = await fs.readFile(key.filename);
    let decryptable = false;
    try {
        const plain = crypto.privateDecrypt({key:key.rsa.privateKey,oaepHash:'sha256'},ciphertext);
        decryptable = plain.length===32;plain.fill(0);
    } catch(error) {
        if (!String(error.code||'').startsWith('ERR_OSSL_') && error.code!=='ERR_CRYPTO_INVALID_KEY_OBJECT_TYPE') throw error;
    }
    const privateNames=['participant-private.pem',AIR_RUNTIME_KEY,SELLO_RUNTIME_KEY];
    const plaintextPath='/var/lib/vita-fl-fixtures/model-io/received_models/wb_client_0x'+'12'.repeat(20)+'.bin';
    const retainedPlaintext=await fs.readFile(plaintextPath).catch(error=>{
        if(error.code==='ENOENT') return null;
        throw error;
    });
    const modes = await Promise.all(privateNames.map(async name=>{
        const stat=await fs.lstat(path.join(RUNTIME_KEY_DIRECTORY,name));
        return stat.isFile() && !stat.isSymbolicLink() && (stat.mode&0o077)===0 && stat.uid===process.getuid();
    }));
    const core=(await fs.readFile('/proc/self/limits','utf8')).split('\n').find(line=>line.startsWith('Max core file size'));
    const digest=Buffer.from(challenge,'hex');
    const signature=crypto.sign('sha256',digest,key.rsa.privateKey);
    // Signing the challenge exercises the retained action signer as well.
    const actionSignature=await key.action.signDigest('0x'+challenge);
    return {
        action_public_key_sha256:sha(key.action.address.toLowerCase()),
        sello_public_key_sha256:sha(key.sello),air_public_key_sha256:sha(key.air),
        python_keys_match:python.sello_public_key_sha256===sha(key.sello) && python.air_public_key_sha256===sha(key.air),
        key_possession_verified:crypto.verify('sha256',digest,key.rsa.publicKey,signature) &&
            recover('0x'+challenge,actionSignature,true).toLowerCase()===key.action.address.toLowerCase() &&
            python.signatures_verified===true,
        private_files_on_tmpfs:(await fs.statfs(RUNTIME_KEY_DIRECTORY)).type===0x01021994,
        private_file_modes:modes.every(Boolean),
        no_swap:(await fs.readFile('/proc/swaps','utf8')).trim().split('\n').length===1,
        core_dump_disabled:Boolean(core && /^Max core file size\s+0\s+0\s+bytes/.test(core)),
        retained_ciphertext_sha256:sha(ciphertext),retained_ciphertext_decryptable:decryptable,
        retained_plaintext_model_sha256:retainedPlaintext ? sha(retainedPlaintext) : null,
    };
}

export async function probeParticipantKey() {
    keys ||= initialize();
    return (await keys).rsa;
}

export async function securityReport(challenge) {
    await allKeyObservation(challenge);
    return JSON.parse((await execute('python3',['/evaluation/phala-key-probe/security.py','security',challenge],
        {maxBuffer:1048576,timeout:120000})).stdout);
}
