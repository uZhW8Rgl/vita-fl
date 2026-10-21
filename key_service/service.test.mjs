import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {createRequire} from "node:module";
import {createKeyServer,readRoot} from "./service.mjs";
import {canonicalClaims,rawPublicKey,deriveApplicationKey,decryptApplicationKey} from "../dfl/node_server/dist/attested_key_protocol.js";
import {obtainApplicationKey,serviceOrigin} from "../dfl/node_server/dist/attested_keys.js";
import {loadOrCreateKmsBundle} from "../dfl/node_server/dist/kms_keys.js";
const require=createRequire(new URL("../dfl/node_server/package.json",import.meta.url));
const {Web3}=require("web3");
const account=new Web3().eth.accounts.privateKeyToAccount("0x"+"12".repeat(32));
const context={participant:account.address.toLowerCase(),appId:"0x"+"11".repeat(20),composeHash:"22".repeat(32),registry:"0x"+"33".repeat(20)};
const policy="0x"+"44".repeat(20),serviceId="55".repeat(32);
const accepted=async (_evidence,claims)=>({verified:true,appId:claims.appId,composeHash:claims.composeHash});
const backend=()=>({policy,registry:context.registry,health:async()=>{},allowed:async c=>c.appId===context.appId&&c.composeHash===context.composeHash});
async function fixture(options={}) {
    const root=options.root || crypto.randomBytes(32),server=createKeyServer({root,backend:backend(),serviceId,appraise:accepted,...options});
    await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
    const origin="http://127.0.0.1:"+server.address().port;
    const post=async(endpoint,value)=>{
        const response=await fetch(origin+endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
        return {status:response.status,value:await response.json()};
    };
    const challenge=async (input=context)=>{
        const pair=crypto.generateKeyPairSync("x25519");
        const result=await post("/v1/challenge",{...input,recipientPublicKey:rawPublicKey(pair.publicKey)});
        assert.equal(result.status,200);
        return {pair,claims:result.value.claims};
    };
    const release=async({claims},signature=account.sign(canonicalClaims(claims).toString()).signature)=>post("/v1/release",{challenge:claims.challenge,signature,evidence:{}});
    return {root,server,post,challenge,release,close:async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));root.fill(0);}};
}

test("HTTP release restores all four identities across independent service starts",async()=>{
    const root=crypto.randomBytes(32),directory=await fs.mkdtemp(path.join(os.tmpdir(),"attested-key-test-"));
    const saved=[];
    try {
        for(let i=0;i<2;i++) {
            const f=await fixture({root});
            try {
                const query=await f.challenge();const released=await f.release(query);
                assert.equal(released.status,200);assert.equal(Object.hasOwn(released.value,"key"),false);
                const key=decryptApplicationKey(released.value,query.claims,query.pair.privateKey);
                const bundle=await loadOrCreateKmsBundle(path.join(directory,"bundle.json"),key,{...context,composeHash:"0x"+context.composeHash,chainId:"31337"});
                saved.push(bundle);key.fill(0);
            } finally {
                // fixture's root is service-owned in normal operation. Keep the
                // independently held test root for the next simulated restart.
                f.server.closeAllConnections();await new Promise(resolve=>f.server.close(resolve));
            }
        }
        assert.deepEqual(saved[0],saved[1]);
        const data=Buffer.from("old publication round key");
        const encrypted=crypto.publicEncrypt({key:crypto.createPublicKey(saved[0].rsa),oaepHash:"sha256"},data);
        assert.deepEqual(crypto.privateDecrypt({key:saved[1].rsa,oaepHash:"sha256"},encrypted),data);
        const stored=await fs.readFile(path.join(directory,"bundle.json"),"utf8");
        for(const secret of Object.values(saved[0])) assert.equal(stored.includes(secret),false);
    } finally {root.fill(0);await fs.rm(directory,{recursive:true,force:true});}
});

test("image substitution, wrong owner and replay produce no key",async()=>{
    const f=await fixture();
    try {
        const bad=await f.challenge({...context,composeHash:"99".repeat(32)});
        assert.equal((await f.release(bad)).status,403);
        const other=new Web3().eth.accounts.privateKeyToAccount("0x"+"13".repeat(32));
        const good=await f.challenge();
        assert.equal((await f.release(good,other.sign(canonicalClaims(good.claims).toString()).signature)).status,403);
        assert.equal((await f.release(good)).status,403);
        const fresh=await f.challenge();assert.equal((await f.release(fresh)).status,200);
        assert.equal((await f.release(fresh)).status,403);
    } finally {await f.close();}
});

test("expired challenges, invalid attestation and revocation during appraisal deny release",async()=>{
    let now=1000;const f=await fixture({now:()=>now});
    try {const q=await f.challenge();now+=60001;assert.equal((await f.release(q)).status,403);} finally {await f.close();}
    const invalid=await fixture({appraise:async()=>{throw new Error("invalid quote");}});
    try {assert.equal((await invalid.release(await invalid.challenge())).status,403);} finally {await invalid.close();}
    let checks=0;
    const revoked=await fixture({backend:{...backend(),allowed:async()=>++checks===1}});
    try {assert.equal((await revoked.release(await revoked.challenge())).status,403);} finally {await revoked.close();}
});

test("recipient binding and ciphertext integrity prevent interception",async()=>{
    const f=await fixture();
    try {
        const q=await f.challenge();const r=await f.release(q);assert.equal(r.status,200);
        const other=crypto.generateKeyPairSync("x25519");
        assert.throws(()=>decryptApplicationKey(r.value,q.claims,other.privateKey));
        assert.throws(()=>decryptApplicationKey({...r.value,ciphertext:"00".repeat(32)},q.claims,q.pair.privateKey));
        assert.throws(()=>decryptApplicationKey(r.value,{...q.claims,challenge:"ff".repeat(32)},q.pair.privateKey));
        assert.deepEqual(deriveApplicationKey(f.root,q.claims),deriveApplicationKey(f.root,{...q.claims,challenge:"ff".repeat(32)}));
        assert.notDeepEqual(deriveApplicationKey(f.root,q.claims),deriveApplicationKey(f.root,{...q.claims,composeHash:"ee".repeat(32)}));
    } finally {await f.close();}
});

test("actual worker client authenticates context and consumes recipient-encrypted key",async()=>{
    const f=await fixture();
    const env={PRIVATE_KEY:account.privateKey,EXPECTED_CHAIN_ID:"31337",KEY_SERVICE_URL:"https://keys.example",
        KEY_SERVICE_POLICY:policy,KEY_SERVICE_ID:serviceId};
    const request=async(url,payload)=>{
        const r=await f.post(new URL(url).pathname,payload);if(r.status!==200) throw new Error("rejected");return r.value;
    };
    try {
        const key=await obtainApplicationKey(context,env,{request,quote:async report=>{assert.equal(report.length,64);return {};}});
        assert.equal(key.length,32);key.fill(0);
        await assert.rejects(obtainApplicationKey(context,{...env,EXPECTED_CHAIN_ID:"1"},{request}),/Anvil/);
        await assert.rejects(obtainApplicationKey(context,{...env,KEY_SERVICE_ID:"ab".repeat(32)},{request}),/identity/);
        assert.throws(()=>serviceOrigin("http://keys.example"),/HTTPS/);
        assert.throws(()=>serviceOrigin("https://keys.example/untrusted"),/HTTPS/);
    } finally {await f.close();}
});

test("root loading rejects permissive files and never silently generates another root",async()=>{
    const directory=await fs.mkdtemp(path.join(os.tmpdir(),"key-root-test-")),filename=path.join(directory,"root");
    try {
        await assert.rejects(readRoot(filename));
        await fs.writeFile(filename,crypto.randomBytes(32),{mode:0o600});
        const key=await readRoot(filename);assert.equal(key.length,32);key.fill(0);
        await fs.chmod(filename,0o644);await assert.rejects(readRoot(filename),/private/);
        await fs.symlink(filename,path.join(directory,"link"));await assert.rejects(readRoot(path.join(directory,"link")));
    } finally {await fs.rm(directory,{recursive:true,force:true});}
});
