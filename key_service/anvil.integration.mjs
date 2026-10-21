/** Real Anvil transactions and HTTP/crypto. Quote appraisal is explicitly synthetic. */
import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import net from "node:net";
import {once} from "node:events";
import {spawn,spawnSync} from "node:child_process";
import {readFile} from "node:fs/promises";
import {setTimeout as delay} from "node:timers/promises";
import {createRequire} from "node:module";
import {createPolicy,createKeyServer} from "./service.mjs";
import {canonicalClaims,rawPublicKey,decryptApplicationKey} from "../dfl/node_server/dist/attested_key_protocol.js";
const require=createRequire(new URL("../dfl/node_server/package.json",import.meta.url));
const {Web3}=require("web3");
const anvil=process.env.ANVIL_BINARY || "anvil";
const available=spawnSync(anvil,["--version"],{stdio:"ignore"}).status===0;
const required=process.env.REQUIRE_ANVIL_TESTS === "1" || process.env.CI === "true";

test("Anvil policy governs actual HTTP key release, substitution rejection and revocation",{
    skip:available||required?false:"Anvil required",timeout:60000,
},async t=>{
    assert.ok(available,"Anvil required");
    const reservation=net.createServer().listen(0,"127.0.0.1");await once(reservation,"listening");
    const port=reservation.address().port;await new Promise(resolve=>reservation.close(resolve));
    const child=spawn(anvil,["--quiet","--host","127.0.0.1","--port",String(port),"--chain-id","31337"],{stdio:"ignore"});
    t.after(async()=>{if(child.exitCode===null){const done=once(child,"exit");child.kill("SIGTERM");await done;}});
    const rpcUrl="http://127.0.0.1:"+port,web3=new Web3(rpcUrl);
    for(let i=0;;i++){try{await web3.eth.getChainId();break;}catch(error){if(i>100)throw error;await delay(25);}}
    const [administrator]=await web3.eth.getAccounts();
    const artifacts=process.env.KEY_ARTIFACT_DIR || new URL("../smart_contracts/out/",import.meta.url).pathname;
    const deploy=async(name,args)=>{
        const artifact=JSON.parse(await readFile(`${artifacts}/${name}.sol/${name}.json`,"utf8"));
        return new web3.eth.Contract(artifact.abi).deploy({data:artifact.bytecode.object,arguments:args}).send({from:administrator,gas:"25000000"});
    };
    const registry=await deploy("DeviceRegistry",[web3.utils.keccak256("independent key service test")]);
    const contract=await deploy("AttestedKeyPolicy",[registry.options.address]);
    const policy=contract.options.address.toLowerCase(),registryAddress=registry.options.address.toLowerCase();
    const codeHash=web3.utils.keccak256(await web3.eth.getCode(policy));
    const account=web3.eth.accounts.privateKeyToAccount("0x"+"12".repeat(32));
    const appId="0x"+"11".repeat(20),composeHash="22".repeat(32);
    await contract.methods.authorise(account.address,appId,"0x"+composeHash).send({from:administrator,gas:"300000"});
    const backend=await createPolicy({rpcUrl,policy,registry:registryAddress,codeHash});
    await assert.rejects(createPolicy({rpcUrl,policy,registry:registryAddress,codeHash:"0x"+"00".repeat(32)}),/code hash/);
    let appraisals=0;
    const root=crypto.randomBytes(32),server=createKeyServer({root,backend,serviceId:"33".repeat(32),
        appraise:async(_evidence,claims)=>{appraisals++;return {verified:true,appId:claims.appId,composeHash:claims.composeHash};}});
    await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
    t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));root.fill(0);});
    const post=async(endpoint,body)=>{
        const r=await fetch("http://127.0.0.1:"+server.address().port+endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
        return {status:r.status,body:await r.json()};
    };
    const release=async(hash)=>{
        const pair=crypto.generateKeyPairSync("x25519");
        const challenge=await post("/v1/challenge",{participant:account.address.toLowerCase(),appId,composeHash:hash,
            registry:registryAddress,recipientPublicKey:rawPublicKey(pair.publicKey)});
        assert.equal(challenge.status,200);
        const claims=challenge.body.claims;
        const response=await post("/v1/release",{challenge:claims.challenge,signature:account.sign(canonicalClaims(claims).toString()).signature,evidence:{}});
        return {...response,claims,pair};
    };
    const first=await release(composeHash),second=await release(composeHash);
    assert.equal(first.status,200);assert.equal(second.status,200);
    const key=decryptApplicationKey(first.body,first.claims,first.pair.privateKey);
    const next=decryptApplicationKey(second.body,second.claims,second.pair.privateKey);
    try{assert.deepEqual(key,next);}finally{key.fill(0);next.fill(0);}
    assert.equal((await release("99".repeat(32))).status,403);
    assert.equal(appraisals,2,"policy rejection must precede secret derivation/appraisal");
    await contract.methods.revoke(account.address,appId).send({from:administrator,gas:"300000"});
    assert.equal((await release(composeHash)).status,403);
    assert.equal(appraisals,2);
});
