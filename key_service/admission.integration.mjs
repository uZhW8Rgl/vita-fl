/** Actual Anvil/HTTP/storage/crypto; quote verifier is a labelled synthetic fixture. */
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import {once} from 'node:events';
import {spawn} from 'node:child_process';
import {setTimeout as delay} from 'node:timers/promises';
import {createRequire} from 'node:module';
import {createAdmissionBackend,createAdmissionServer} from './chain_server.mjs';
import {initialiseWorkerRoots,openWorkerRoots} from './worker_roots.mjs';
import {rawPublicKey,decryptApplicationKey} from '../dfl/node_server/dist/attested_key_protocol.js';
import {generateBundle,loadOrCreateKmsBundle} from '../dfl/node_server/dist/kms_keys.js';
import {publicIdentity} from '../dfl/node_server/dist/key_admission.js';
import {signRawDigest} from '../dfl/node_server/dist/action_key.js';
const {Web3}=createRequire(new URL('../dfl/node_server/package.json',import.meta.url))('web3');
test('one on-chain appraisal admits random identities, then restores their sealed bundle without an action private key', {timeout:60000},async t=>{
    const reservation=net.createServer().listen(0,'127.0.0.1');await once(reservation,'listening');
    const port=reservation.address().port;await new Promise(r=>reservation.close(r));
    const child=spawn(process.env.ANVIL_BINARY||'anvil',['--quiet','--host','127.0.0.1','--port',String(port),'--chain-id','31337',
        '--gas-limit','1000000000','--code-size-limit','999999'],{stdio:'ignore'});
    t.after(async()=>{if(child.exitCode===null){const done=once(child,'exit');child.kill('SIGTERM');await done;}});
    const rpcUrl='http://127.0.0.1:'+port,web3=new Web3(rpcUrl);
    for(let i=0;;i++){try{await web3.eth.getChainId();break;}catch(error){if(i>100)throw error;await delay(25);}}
    const [admin]=await web3.eth.getAccounts();
    const deploy=async(name,args=[])=>{
        const artifact=JSON.parse(await fs.readFile(new URL('../smart_contracts/out/'+name+'.sol/'+name+'.json',import.meta.url)));
        return new web3.eth.Contract(artifact.abi).deploy({data:artifact.bytecode.object,arguments:args}).send({from:admin,gas:'25000000'});
    };
    // The fixture verifier is intentionally sourced from the test artifact.
    const registry=await deploy('DeviceRegistry',[web3.utils.keccak256('synthetic admission integration')]);
    const fixtureArtifact=JSON.parse(await fs.readFile(new URL('../smart_contracts/out/AttestedKeyAdmission.t.sol/SingleAdmissionFixtureVerifier.json',import.meta.url)));
    const verifier=await new web3.eth.Contract(fixtureArtifact.abi).deploy({data:fixtureArtifact.bytecode.object}).send({from:admin,gas:'3000000'});
    await registry.methods.setTdxV4Attestation(verifier.options.address).send({from:admin,gas:'300000'});
    const serviceId='ab'.repeat(32),contract=await deploy('AttestedKeyAdmission',[registry.options.address,'0x'+serviceId]);
    await registry.methods.setKeyAdmissionPolicy(contract.options.address).send({from:admin,gas:'300000'});
    const account=web3.eth.accounts.privateKeyToAccount('0x'+'12'.repeat(32)),appId='0x'+'13'.repeat(20);
    const manifest=JSON.stringify({manifest_version:2,runner:'docker-compose',docker_compose_file:'services:\n  dfl-worker:\n    image: example/worker@sha256:'+'11'.repeat(32)+'\n'});
    const composeHash=crypto.createHash('sha256').update(manifest).digest('hex');
    await registry.methods.setExpectedWorkerImageDigest('0x'+'11'.repeat(32)).send({from:admin,gas:'300000'});
    await registry.methods.setWorkerPolicyHashAllowed(await registry.methods.workerPolicyHash('0x'+Buffer.from(manifest).toString('hex')).call(),true).send({from:admin,gas:'300000'});
    await contract.methods.authorise(account.address,appId,'0x'+composeHash).send({from:admin,gas:'300000'});
    const settings={rpcUrl,registry:registry.options.address.toLowerCase(),policy:contract.options.address.toLowerCase(),serviceId,chainId:'31337',
        codeHash:web3.utils.keccak256(await web3.eth.getCode(contract.options.address))};
    const dir=await fs.mkdtemp(path.join(os.tmpdir(),'admission-integration-')),roots=path.join(dir,'workers');
    t.after(()=>fs.rm(dir,{recursive:true,force:true}));await initialiseWorkerRoots(roots,settings);
    let server;
    const start=async()=>{
        server=createAdmissionServer({workerRoots:await openWorkerRoots(roots,settings),backend:await createAdmissionBackend(settings),serviceId});
        await new Promise(r=>server.listen(0,'127.0.0.1',r));
    };
    const stop=async()=>{server.closeAllConnections();await new Promise(r=>server.close(r));};
    await start();t.after(stop);
    t.diagnostic('Contracts deployed, service started with pinned policy.');
    const post=async(endpoint,value)=>{
        const r=await fetch('http://127.0.0.1:'+server.address().port+endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
        return {status:r.status,body:await r.json()};
    };
    const bundle=await generateBundle(),env={ACCOUNT_ADDRESS:account.address,EXPECTED_CHAIN_ID:'31337',REGISTRY_ADDRESS:settings.registry,PARTICIPANT_KEY_PROVIDER:'broker'};
    const identity=await publicIdentity(bundle,env);
    t.diagnostic('Random four-key identity generated.');
    const challenge=async(proposal=identity,hash=composeHash)=>{
        const pair=crypto.generateKeyPairSync('x25519');
        const r=await post('/v2/challenge',{participant:account.address.toLowerCase(),appId,composeHash:hash,registry:settings.registry,
            recipientPublicKey:rawPublicKey(pair.publicKey),identity:proposal});
        return {...r,pair};
    };
    const release=async(q,valid=true)=>{
        const quote=Buffer.alloc(637);quote[0]=valid?0x42:0x43;Buffer.from(q.body.reportData.slice(2),'hex').copy(quote,568);
        return post('/v2/release',{challenge:q.body.claims.challenge,signature:account.sign(q.body.digest).signature,
            evidence:{quote:quote.toString('hex'),appCompose:manifest,eventLog:JSON.stringify([{imr:3,event_type:0x08000001,event:'app-id',event_payload:appId.slice(2)}])}});
    };
    // Invalid evidence must never create a worker root or a public identity.
    assert.equal((await release(await challenge(),false)).status,403);
    assert.deepEqual(await fs.readdir(roots),['scope.json']);
    t.diagnostic('Invalid evidence rejected without creating a root.');
    const first=await challenge();assert.equal(first.status,200);
    const admitted=await release(first);assert.equal(admitted.status,200);
    t.diagnostic('Initial identity admitted, application key released.');
    const key=decryptApplicationKey(admitted.body,first.body.claims,first.pair.privateKey);
    const context={participant:account.address.toLowerCase(),appId,composeHash:'0x'+composeHash,registry:settings.registry,chainId:'31337'};
    const original=await loadOrCreateKmsBundle(path.join(dir,'sealed.json'),key,context,async()=>bundle);key.fill(0);
    assert.equal((await release(first)).status,403,'a challenge is one-use');
    assert.equal((await challenge(identity)).status,403,'an admitted identity cannot be replaced');
    assert.equal((await challenge(null,'ff'.repeat(32))).status,403,'image substitution is rejected');
    await stop();await start();
    const restored=await challenge(null);assert.equal(restored.status,200);assert.deepEqual(restored.body.identity,identity);
    const r=await release(restored);assert.equal(r.status,200);
    const nextKey=decryptApplicationKey(r.body,restored.body.claims,restored.pair.privateKey);
    assert.deepEqual(await loadOrCreateKmsBundle(path.join(dir,'sealed.json'),nextKey,context),original);nextKey.fill(0);
    t.diagnostic('Service restart restored exactly the original four keys.');
    // Registration consumes the recorded admission; a verifier that now always
    // rejects cannot affect it. The random action key is used only after restoration.
    await verifier.methods.disable().send({from:admin,gas:'100000'});
    const participantDigest=await registry.methods.enrollmentDigest(account.address,identity.actionKey,identity.selloKey,'0x'+Buffer.from(manifest).toString('hex')).call();
    await web3.currentProvider.request({jsonrpc:'2.0',id:1,method:'anvil_impersonateAccount',params:[identity.actionKey]});
    await web3.currentProvider.request({jsonrpc:'2.0',id:2,method:'anvil_setBalance',params:[identity.actionKey,'0x56BC75E2D63100000']});
    await registry.methods.registerDeviceWithKeyAdmission(appId,'0x'+Buffer.from(manifest).toString('hex'),account.address,identity.actionKey,'https://worker.example','',identity.rsaPublicKey,identity.selloKey,
        signRawDigest(participantDigest,account.privateKey)).send({from:identity.actionKey,gas:'4000000'});
    assert.equal((await registry.methods.actionKeyForParticipant(account.address).call()).toLowerCase(),identity.actionKey);
    await contract.methods.revoke(account.address,appId).send({from:admin,gas:'100000'});
    assert.equal((await challenge(null)).status,403);
});
