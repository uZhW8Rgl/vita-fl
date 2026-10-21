/** Dedicated cloud experiment: fingerprints and freshly quoted observations only. */
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {createRequire} from 'node:module';
import {initializeAttestedRuntimeKeys, obtainApplicationKey, BROKER_STATE_PATH} from '../../dfl/node_server/dist/attested_keys.js';
import {RUNTIME_KEY_DIRECTORY, requireVolatileDirectory} from '../../dfl/node_server/dist/runtime_keys.js';
import {ed25519PublicKeyFromSeed} from '../../dfl/node_server/dist/sello_key.js';
import {initialiseOnchainKeys} from '../../dfl/node_server/dist/key_admission.js';
import {loadParticipantActionSigner} from '../../dfl/node_server/dist/action_key.js';
const require=createRequire(new URL('../../dfl/node_server/package.json',import.meta.url));
const {DstackClient}=await import(require.resolve('@phala/dstack-sdk'));
const {Web3}=require('web3');
const client=new DstackClient('/var/run/dstack.sock');
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const names=['action-root.seed','sello-signing.seed','air-signing.seed','participant-private.pem'];
const token=process.env.PRIVATE_KEY;
if(process.env.EVALUATION_GATES_ENABLED!=='true'||!token||token.length<32) throw new Error('Dedicated authenticated test required');
await requireVolatileDirectory();
for(const name of names) await fs.rm(path.join(RUNTIME_KEY_DIRECTORY,name),{force:true});
const processId=crypto.randomBytes(16).toString('hex');
const bootId=(await fs.readFile('/proc/sys/kernel/random/boot_id','utf8')).trim();
let initialized=false,busy=false;

async function inspect(challenge,claimedComposeHash) {
    const info=await client.info();
    const context={appId:'0x'+info.app_id.replace(/^(0x|app_)/,'').toLowerCase(),
        composeHash:hash(info.tcb_info.app_compose),participant:process.env.ACCOUNT_ADDRESS.toLowerCase(),
        registry:process.env.REGISTRY_ADDRESS.toLowerCase()};
    let denied=false,status=null,fingerprints=null,possession=false,decryptable=false;
    try {
        if(claimedComposeHash) {
            if(process.env.KEY_SERVICE_PROTOCOL==='online-v1'){
                const root=await obtainApplicationKey({...context,composeHash:claimedComposeHash});root.fill(0);
            }else await initialiseOnchainKeys(process.env,{claimedComposeHash});
            throw new Error('Unexpected release to forged deployment context');
        }
        if(!initialized){await initializeAttestedRuntimeKeys();initialized=true;}
        const privateRsa=await fs.readFile(path.join(RUNTIME_KEY_DIRECTORY,'participant-private.pem'));
        const rsa=crypto.createPrivateKey(privateRsa);privateRsa.fill(0);
        const pub=crypto.createPublicKey(rsa).export({format:'pem',type:'spki'});
        const actionSeed=await fs.readFile(path.join(RUNTIME_KEY_DIRECTORY,'action-root.seed'));
        const action=await loadParticipantActionSigner({deriveKey:async()=>Buffer.from(actionSeed)});actionSeed.fill(0);
        const eds={},signatures=[];
        for(const name of ['sello','air']) {
            const seed=await fs.readFile(path.join(RUNTIME_KEY_DIRECTORY, name+'-signing.seed'));
            try {
                const key=crypto.createPrivateKey({key:Buffer.concat([Buffer.from('302e020100300506032b657004220420','hex'),seed]),format:'der',type:'pkcs8'});
                eds[name]=hash(ed25519PublicKeyFromSeed(seed));
                signatures.push(crypto.verify(null,Buffer.from(challenge),crypto.createPublicKey(key),crypto.sign(null,Buffer.from(challenge),key)));
            } finally {seed.fill(0);}
        }
        const actionDigest=new Web3().utils.keccak256(challenge),signature=await action.signDigest(actionDigest);
        possession=signatures.every(Boolean)&&new Web3().eth.accounts.recover(actionDigest,signature,true).toLowerCase()===action.address.toLowerCase()
            &&crypto.verify('sha256',Buffer.from(challenge),pub,crypto.sign('sha256',Buffer.from(challenge),rsa));
        fingerprints={rsa:hash(pub),action:hash(action.address.toLowerCase()),...eds};
        action.destroy();
        const filename='/var/lib/vita-fl/prior-rsa-envelope.bin';
        const surrogate=crypto.randomBytes(32);
        try {await fs.writeFile(filename,crypto.publicEncrypt({key:pub,oaepHash:'sha256'},surrogate),{flag:'wx',mode:0o600}).catch(e=>{if(e.code!=='EEXIST')throw e;});}
        finally {surrogate.fill(0);}
        const plain=crypto.privateDecrypt({key:rsa,oaepHash:'sha256'},await fs.readFile(filename));
        decryptable=plain.length===32;plain.fill(0);
    } catch(error) {
        // Only an explicit service denial is a successful negative observation.
        if(error.statusCode!==403) throw error;
        denied=true;status=error.statusCode;
    }
    const sealed=await fs.readFile(BROKER_STATE_PATH).catch(e=>{if(e.code==='ENOENT')return null;throw e;});
    const present=await Promise.all(names.map(async name=>fs.access(path.join(RUNTIME_KEY_DIRECTORY,name)).then(()=>true,e=>{if(e.code==='ENOENT')return false;throw e;})));
    const claims={schema_version:1,challenge,context,claimed_compose_hash:claimedComposeHash||null,
        process_instance_id:processId,system_boot_id:bootId,variant:(await fs.readFile('/evaluation/phala-broker-probe/variant','utf8')).trim(),
        key_release_denied:denied,denial_http_status:status,fingerprints,key_possession_verified:possession,
        prior_ciphertext_decryptable:decryptable,sealed_state_sha256:sealed?hash(sealed):null,
        runtime_keys_present:present.every(Boolean),runtime_keys_absent:present.every(v=>!v)};
    const claims_json=JSON.stringify(claims);
    const evidence=await client.getQuote(crypto.createHash('sha512').update('VITA-FL:broker-evaluation:v1\0').update(claims_json).digest());
    return {claims,claims_json,quote:evidence.quote,event_log:evidence.event_log,app_compose:info.tcb_info.app_compose};
}
const server=http.createServer(async(req,res)=>{
    const send=(s,v)=>res.writeHead(s,{'Content-Type':'application/json','Cache-Control':'no-store'}).end(JSON.stringify(v));
    if(req.method==='GET'&&req.url==='/health'){send(200,{ready:true,process_instance_id:processId});return;}
    if(!crypto.timingSafeEqual(Buffer.from(hash(req.headers.authorization||''),'hex'),Buffer.from(hash('Bearer '+token),'hex'))){send(401,{});return;}
    if(req.method!=='POST'){send(404,{});return;}
    if(busy){send(409,{});return;}
    busy=true;
    try {
        if(req.url==='/restart'){send(200,{restarting:true});setTimeout(()=>process.exit(0),100);return;}
        if(req.url!=='/observe'){send(404,{});return;}
        const chunks=[];let size=0;
        for await(const chunk of req){size+=chunk.length;if(size>512)throw new Error();chunks.push(chunk);}
        const value=JSON.parse(Buffer.concat(chunks).toString());
        if(!/^[a-f0-9]{64}$/.test(value.challenge)||value.claimedComposeHash&&!/^[a-f0-9]{64}$/.test(value.claimedComposeHash))throw new Error();
        send(200,await inspect(value.challenge,value.claimedComposeHash));
    }catch{send(503,{error:'PROBE_INCONCLUSIVE'});}finally{busy=false;}
});
server.requestTimeout=60000;
server.listen(8001,'0.0.0.0');
