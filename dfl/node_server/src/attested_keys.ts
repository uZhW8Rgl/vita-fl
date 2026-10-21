/** Independent broker profile. GetKey is deliberately never called. */
import crypto from "crypto";
import fs from "fs/promises";
import https from "https";
import path from "path";
import {Web3} from "web3";
import {DstackClient} from "@phala/dstack-sdk";
import {canonicalClaims, decryptApplicationKey, rawPublicKey, releaseReportData,
    type ReleaseContext, type ReleaseClaims, type KeyEnvelope} from "./attested_key_protocol.js";
import {loadOrCreateKmsBundle} from "./kms_keys.js";
import {requireVolatileDirectory, RUNTIME_KEY_DIRECTORY} from "./runtime_keys.js";

export const BROKER_STATE_PATH = "/var/lib/vita-fl/participant-keys.broker.v1.sealed.json";
export const serviceOrigin = (value: string): string => {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) throw new Error("KEY_SERVICE_URL must be an HTTPS origin");
    return url.origin;
};
export const keyServiceHttpRequest = async (url: string, payload: unknown, ca?: string): Promise<{status:number;body:any}> => {
    const body = Buffer.from(JSON.stringify(payload));
    return new Promise((resolve,reject) => {
        const request = https.request(url, {method:"POST",rejectUnauthorized:true,ca,
            headers:{"Content-Type":"application/json","Content-Length":body.length},timeout:240000}, response => {
            const chunks: Buffer[] = [];let count = 0;
            response.on("data", (chunk: Buffer) => {count += chunk.length;if(count > 1048576) response.destroy(new Error("Key service response too large"));else chunks.push(chunk);});
            response.on("error",reject);
            response.on("end",()=>{
                try {
                    if (!/^application\/json(?:;|$)/i.test(String(response.headers["content-type"]))) throw new Error("Invalid key service response");
                    resolve({status:response.statusCode||0,body:JSON.parse(Buffer.concat(chunks).toString())});
                } catch(error) {reject(error);}
            });
        });
        request.on("timeout",()=>request.destroy(new Error("Key service deadline exceeded")));
        request.on("error",reject);request.end(body);
    });
};
export const keyServiceRequest = async (url:string,payload:unknown,ca?:string):Promise<any> => {
    const result=await keyServiceHttpRequest(url,payload,ca);
    if(result.status!==200)throw Object.assign(new Error('Key service rejected request'),{statusCode:result.status});
    return result.body;
};
export const obtainApplicationKey = async (context: ReleaseContext, env: NodeJS.ProcessEnv = process.env,
    dependencies?: {request?: typeof keyServiceRequest; quote?: (report: Buffer) => Promise<any>}) => {
    if (env.EXPECTED_CHAIN_ID !== "31337") throw new Error("Attested key service requires Anvil chain 31337");
    const origin = serviceOrigin(env.KEY_SERVICE_URL || "");
    const account = new Web3().eth.accounts.privateKeyToAccount(env.PRIVATE_KEY || "");
    if (account.address.toLowerCase() !== context.participant) throw new Error("Participant credential mismatch");
    const request = dependencies?.request || keyServiceRequest;
    const ca = env.KEY_SERVICE_CA_FILE ? await fs.readFile(env.KEY_SERVICE_CA_FILE, "utf8") : undefined;
    const pair = crypto.generateKeyPairSync("x25519");
    const challenge = await request(origin+"/v1/challenge", {...context,recipientPublicKey:rawPublicKey(pair.publicKey)}, ca);
    if (!challenge || Object.keys(challenge).join() !== "claims") throw new Error("Invalid key challenge");
    const claims = challenge.claims as ReleaseClaims;
    canonicalClaims(claims);
    for (const name of ["participant","appId","composeHash","registry"] as const) {
        if (claims[name] !== context[name]) throw new Error("Key challenge context mismatch");
    }
    if (claims.policy !== String(env.KEY_SERVICE_POLICY || "").toLowerCase() || claims.serviceId !== env.KEY_SERVICE_ID ||
        claims.recipientPublicKey !== rawPublicKey(pair.publicKey)) throw new Error("Key service identity mismatch");
    const quote = dependencies?.quote || (async (report: Buffer) => {
        const client = new DstackClient("/var/run/dstack.sock");
        const [evidence, info] = await Promise.all([client.getQuote(report),client.info()]);
        return {quote:evidence.quote.replace(/^0x/,""),
            eventLog:typeof evidence.event_log === "string" ? evidence.event_log : JSON.stringify(evidence.event_log),
            appCompose:info.tcb_info.app_compose};
    });
    const evidence = await quote(releaseReportData(claims));
    const signature = account.sign(canonicalClaims(claims).toString()).signature;
    const envelope = await request(origin+"/v1/release", {challenge:claims.challenge,signature,evidence}, ca);
    return decryptApplicationKey(envelope as KeyEnvelope,claims,pair.privateKey);
};

export const initializeAttestedRuntimeKeys = async (env = process.env) => {
    if(env.KEY_SERVICE_PROTOCOL && !['onchain-v3','online-v1'].includes(env.KEY_SERVICE_PROTOCOL))
        throw new Error('Unsupported historical key-service protocol; no attestation downgrade');
    if(env.KEY_SERVICE_PROTOCOL !== 'online-v1') return (await import('./key_admission.js')).initialiseOnchainKeys(env);
    if (env.DOCKER !== "phala" || env.PARTICIPANT_KEY_PROVIDER !== "broker") throw new Error("Explicit Phala broker profile required");
    await requireVolatileDirectory();
    const client = new DstackClient("/var/run/dstack.sock");
    const info = await client.info();
    const context: ReleaseContext = {appId:"0x"+info.app_id.replace(/^(0x|app_)/,"").toLowerCase(),
        composeHash:crypto.createHash("sha256").update(info.tcb_info.app_compose).digest("hex"),
        participant:String(env.ACCOUNT_ADDRESS || "").toLowerCase(),
        registry:String(env.REGISTRY_ADDRESS || env.EXPECTED_DEVICE_REGISTRY_ADDRESS || "").toLowerCase()};
    const root = await obtainApplicationKey(context,env);
    const created: string[] = [];
    try {
        const bundle = await loadOrCreateKmsBundle(BROKER_STATE_PATH,root,{...context,composeHash:"0x"+context.composeHash,chainId:"31337"});
        const entries: [string,Buffer][] = [["action-root.seed",Buffer.from(bundle.action,"hex")],
            ["sello-signing.seed",Buffer.from(bundle.sello,"hex")],["air-signing.seed",Buffer.from(bundle.air,"hex")],
            ["participant-private.pem",Buffer.from(bundle.rsa)]];
        for (const [name,bytes] of entries) {
            const filename = path.join(RUNTIME_KEY_DIRECTORY,name);
            try {await fs.writeFile(filename,bytes,{mode:0o600,flag:"wx"});created.push(filename);}
            finally {bytes.fill(0);}
        }
    } catch(error) {for(const file of created) await fs.rm(file,{force:true});throw error;}
    finally {root.fill(0);}
};
