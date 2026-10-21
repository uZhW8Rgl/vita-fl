/** Protocol v3: attested service, signed responses, one on-chain worker appraisal. */
import http from 'node:http';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {createPolicy} from './service.mjs';
import {KEY_ADMISSION_ABI} from '../dfl/node_server/dist/key_admission_abi.js';
import {canonicalClaims,encryptApplicationKey} from '../dfl/node_server/dist/attested_key_protocol.js';
import {hex32} from '../dfl/node_server/dist/service_attestation.js';
const {Web3}=createRequire(new URL('../dfl/node_server/package.json',import.meta.url))('web3');
export const normalIdentity=x=>({actionKey:String(x.actionKey).toLowerCase(),rsaPublicKey:String(x.rsaPublicKey).toLowerCase(),
    selloKey:String(x.selloKey).toLowerCase(),airKey:String(x.airKey).toLowerCase()});
export function validateIdentity(x){
    if(!x||Object.keys(x).sort().join()!==['actionKey','rsaPublicKey','selloKey','airKey'].sort().join())throw new Error('Invalid identity');
    if(!/^0x[a-f0-9]{40}$/.test(x.actionKey)||/^0x0{40}$/.test(x.actionKey)||!/^0x(?:[a-f0-9]{2}){384,1024}$/.test(x.rsaPublicKey)||
        !/^0x[a-f0-9]{64}$/.test(x.selloKey)||!/^0x[a-f0-9]{64}$/.test(x.airKey))throw new Error('Invalid public identity');
}
export function eventsForContract(raw){
    const events=typeof raw==='string'?JSON.parse(raw):raw;
    if(!Array.isArray(events)||events.length>512)throw new Error('Invalid event log');
    return events.filter(x=>x.imr===3).map(x=>{
        if(x.event_type!==0x08000001||typeof x.event!=='string'||typeof x.event_payload!=='string'||!/^(?:[a-fA-F0-9]{2})*$/.test(x.event_payload))throw new Error('Invalid runtime event');
        return {eventType:x.event_type,eventName:x.event,eventPayload:'0x'+x.event_payload};
    });
}
export async function createAdmissionBackend(settings){
    const base=await createPolicy(settings),web3=new Web3(new Web3.providers.HttpProvider(settings.rpcUrl,{providerOptions:{timeout:240000}}));
    const contract=new web3.eth.Contract(KEY_ADMISSION_ABI,settings.policy);
    const registry=new web3.eth.Contract([{type:'function',name:'keyAdmissionPolicy',stateMutability:'view',inputs:[],outputs:[{type:'address'}]}],settings.registry);
    const health=async()=>{
        await base.health();
        if(String(await contract.methods.serviceId().call()).toLowerCase()!=='0x'+settings.serviceId||
            String(await registry.methods.keyAdmissionPolicy().call()).toLowerCase()!==settings.policy.toLowerCase())throw new Error('Admission identity mismatch');
    };
    await health();
    const accounts=await web3.eth.getAccounts(),relayer=settings.relayer||accounts.at(-1);
    if(!relayer)throw new Error('Private Anvil relayer required');
    return {...base,health,codeHash:settings.codeHash,async prepare(claims,proposal){
        await health();
        if(!await base.allowed(claims))throw new Error('Workload policy rejected');
        const stored=normalIdentity(await contract.methods.identityFor(claims.participant,claims.appId).call());
        const restoring=!/^0x0{40}$/.test(stored.actionKey);
        if(restoring?proposal!==null:proposal===null)throw new Error('Sealed-state/chain identity mismatch');
        const identity=restoring?stored:proposal;validateIdentity(identity);
        const request={participant:claims.participant,appId:claims.appId,composeHash:'0x'+claims.composeHash,
            challenge:'0x'+claims.challenge,recipientPublicKey:'0x'+claims.recipientPublicKey,
            expiresAt:String(Math.floor(Date.now()/1000)+180),restoring,
            ...Object.fromEntries(Object.entries(settings.serviceBinding()).map(([k,v])=>[k,'0x'+v]))};
        const reportData=await contract.methods.reportData(request,identity).call();
        const digest=await contract.methods.authorisationDigest(request,identity).call();
        return {request,identity,reportData,digest};
    },async admit(item,signature,evidence){
        await health();
        if(crypto.createHash('sha256').update(evidence.appCompose).digest('hex')!==item.claims.composeHash)throw new Error('Manifest mismatch');
        if(!/^(?:[a-fA-F0-9]{2}){637,65536}$/.test(evidence.quote))throw new Error('Invalid quote');
        // Deliberately no estimateGas/debugVerify call: execute DCAP only in this transaction.
        const receipt=await contract.methods.admit(item.request,item.identity,signature,'0x'+evidence.quote,
            eventsForContract(evidence.eventLog),'0x'+Buffer.from(evidence.appCompose).toString('hex'))
            .send({from:relayer,gas:'500000000'});
        if(!receipt.status)throw new Error('Admission transaction rejected');
        return receipt.transactionHash;
    },async permitted(item){
        await health();return await contract.methods.releaseAuthorised(item.request,item.identity).call()===true;
    }};
}
export function createAdmissionServer({workerRoots,backend,serviceId,serviceIdentity,now=Date.now}){
    if(!serviceIdentity)throw new Error('An attested service identity is required');
    const pending=new Map();let active=0,completed=0;
    const server=http.createServer(async(req,res)=>{
        let requestId;
        const send=(status,value)=>{
            const body=requestId?serviceIdentity.sign(req.url,requestId,status,value):value;
            res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'}).end(JSON.stringify(body));
        };
        if(req.method==='GET'&&(req.url==='/health'||req.url==='/v2/config')){
            try{await serviceIdentity.ensureAuthorised();await backend.health();send(200,{status:'ok',protocol:'onchain-v3',chainId:'31337',policy:backend.policy,
                registry:backend.registry,serviceId,codeHash:backend.codeHash,rootMode:'per-worker-memory-v1',completedAdmissions:completed});}catch{send(503,{error:'Admission chain unavailable'});}return;
        }
        if(req.method!=='POST'||!['/v3/attestation','/v3/challenge','/v3/release'].includes(req.url)){send(404,{});return;}
        try{
            if(!/^application\/json(?:;|$)/i.test(req.headers['content-type']||''))throw new Error();
            let size=0;const chunks=[];for await(const c of req){size+=c.length;if(size>524288)throw new Error();chunks.push(c);}
            const input=JSON.parse(Buffer.concat(chunks).toString());
            if(req.url==='/v3/attestation'){
                if(Object.keys(input).join()!=='nonce'||!hex32(input.nonce))throw new Error();
                requestId=input.nonce;
                send(200,await serviceIdentity.attest(input.nonce));return;
            }
            if(Object.keys(input).sort().join()!==['requestId','serviceInstanceId','servicePublicKey','payload'].sort().join()||!hex32(input.requestId))throw new Error();
            requestId=input.requestId;
            const binding=serviceIdentity.binding();
            if(input.serviceInstanceId!==binding.serviceInstanceId||input.servicePublicKey!==binding.servicePublicKey)throw new Error();
            await serviceIdentity.ensureAuthorised();
            const value=input.payload;
            for(const [k,v] of pending)if(v.expires<=now())pending.delete(k);
            if(req.url==='/v3/challenge'){
                if(Object.keys(value).sort().join()!==['participant','appId','composeHash','registry','recipientPublicKey','identity'].sort().join()||pending.size>=256)throw new Error();
                const {identity,...context}=value;
                const claims={...context,version:1,challenge:crypto.randomBytes(32).toString('hex'),chainId:'31337',policy:backend.policy,serviceId};
                canonicalClaims(claims);if(claims.registry!==backend.registry)throw new Error();
                const admission=await backend.prepare(claims,identity);
                pending.set(claims.challenge,{claims,...admission,expires:now()+180000});
                send(200,{claims,...admission});return;
            }
            if(Object.keys(value).sort().join()!==['challenge','signature','evidence'].sort().join()||!hex32(value.challenge))throw new Error();
            const item=pending.get(value.challenge);pending.delete(value.challenge);
            if(!item||item.expires<=now()||active>=1)throw new Error();
            if(new Web3().eth.accounts.recover(item.digest,value.signature).toLowerCase()!==item.claims.participant)throw new Error();
            if(!value.evidence||Object.keys(value.evidence).sort().join()!==['quote','eventLog','appCompose'].sort().join())throw new Error();
            active++;
            try{
                await backend.admit(item,value.signature,value.evidence);
                if(item.expires<=now()||!await backend.permitted(item))throw new Error();
                await serviceIdentity.ensureAuthorised();
                // Each first admission creates its own random root, only after
                // the fresh quote and policy have been accepted on-chain.
                const key=await workerRoots.applicationKey(item.claims,{restoring:item.request.restoring});
                try{send(200,encryptApplicationKey(key,item.claims));completed++;}finally{key.fill(0);}
            }finally{active--;}
        }catch{send(403,{error:'On-chain admission or release rejected'});}
    });
    server.requestTimeout=10000;server.headersTimeout=10000;return server;
}
