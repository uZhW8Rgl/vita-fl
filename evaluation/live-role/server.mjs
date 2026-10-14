/** Bounded attestable contract-test role. Secrets remain in this TEE process.
 * No arbitrary code, generic signing, file access or value transfer interface.
 * Only the measured test registry/GM/policy/selector may be called.
 */
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import {createRequire} from 'node:module';
import {submitSigned} from './rpc.mjs';
const require=createRequire('/dfl/node_server/package.json');
const {Web3}=await import(require.resolve('web3'));
const {DstackClient}=await import(require.resolve('@phala/dstack-sdk'));
const {loadParticipantActionSigner,signRawDigest}=await import('/dfl/node_server/dist/action_key.js');
const {loadParticipantKey}=await import('/dfl/node_server/dist/participant_key.js');
const env=process.env;
if(env.DOCKER!=='phala'||env.EVALUATION_GATES_ENABLED!=='true')throw new Error('Dedicated Phala evaluation required');
const web3=new Web3(env.RPC_URL);
const client=new DstackClient('/var/run/dstack.sock');
const signer=await loadParticipantActionSigner();
const rsa=await loadParticipantKey({env});
const publicKey='0x'+Buffer.from(rsa.publicKeyDer).toString('hex');
const participant=env.ACCOUNT_ADDRESS.toLowerCase();
const bootstrap=web3.eth.accounts.privateKeyToAccount(env.PRIVATE_KEY);
if(bootstrap.address.toLowerCase()!==participant)throw new Error('Bootstrap identity mismatch');
const abi=name=>JSON.parse(fs.readFileSync('/dfl/node_server/abi/'+name+'.json','utf8'));
const registry=new web3.eth.Contract(abi('registry'),env.REGISTRY_ADDRESS);
const gm=new web3.eth.Contract(abi('gm'),env.GM_STORAGE_ADDRESS);
const selection=new web3.eth.Contract(abi('AggregatorSelection'),env.AGGREGATOR_ADDRESS);
const policy=new web3.eth.Contract(abi('aggregation_policy'),env.AGGREGATION_POLICY_ADDRESS);
const stringify=x=>JSON.stringify(x,(_k,v)=>typeof v==='bigint'?v.toString():v);
const hex=b=>'0x'+Buffer.from(b).toString('hex');
const instance=crypto.randomBytes(16).toString('hex');
let lastEnrollment;
const receiptKey=env.TEE_INFERENCE_ENABLED==='1'?'0x'+crypto.randomBytes(32).toString('hex'):'0x'+'00'.repeat(32);
const allowTargets=new Set([registry.options.address,gm.options.address,selection.options.address].map(a=>a.toLowerCase()));
async function transact(contract,method){
 const to=contract.options.address;
 if(!allowTargets.has(to.toLowerCase()))throw new Error('Unknown destination');
 const data=method.encodeABI();
 const chainId=await web3.eth.getChainId();
 if(chainId!==BigInt(env.EXPECTED_CHAIN_ID))throw new Error('Chain mismatch');
 const nonce=await web3.eth.getTransactionCount(signer.address,'pending');
 const gasPrice=await web3.eth.getGasPrice();
 const raw=await signer.signTransaction({from:signer.address,to,data,chainId,nonce,gas:120000000n,gasPrice,value:0n});
 // Sending raw bytes directly ensures negative cases really reach mining.
 const tx=await submitSigned(web3.provider,raw);
 let receipt;
 for(let n=0;n<300;n++){
  receipt=await web3.eth.getTransactionReceipt(tx);
  if(receipt)break;
  await new Promise(r=>setTimeout(r,200));
 }
 if(!receipt)throw new Error('Receipt unavailable');
 return {transaction_hash:tx,receipt,call:{from:signer.address,to,data},raw_transaction:raw};
}
function events(raw){
 const data=typeof raw==='string'?JSON.parse(raw):raw;
 return data.filter(x=>Number(x.imr)===3).map(x=>({eventType:Number(x.event_type),eventName:String(x.event),eventPayload:typeof x.event_payload==='string'?'0x'+x.event_payload.replace(/^0x/,''):hex(x.event_payload)}));
}
async function enrollment(challenge){
 const info=await client.info();
 const text=info.tcb_info.app_compose;
 if(typeof text!=='string')throw new Error('Raw measured configuration unavailable');
 const compose=Buffer.from(text);
 const identity=await registry.methods.workloadIdentity(hex(compose)).call();
 const composeHash=identity.composeHash??identity[0];
 const digest=identity.workerImageDigest??identity[1];
 const workerPolicy=await registry.methods.workerPolicyHash(hex(compose)).call();
 const nonce=await registry.methods.registrationNonces(participant).call();
 const quoteVerifier=await registry.methods.tdxV4Attestation().call();
 const deployment=await registry.methods.deploymentId().call();
 const chain=await web3.eth.getChainId();
 const origin='https://'+info.app_id+'-8001.'+env.DSTACK_GATEWAY_DOMAIN;
 const binding=web3.utils.keccak256(web3.eth.abi.encodeParameters(
  ['bytes32','bytes32','uint256','address','address','address','address','bytes32','bytes32','bytes32','bytes32','bytes32','bytes32','bytes32','uint256'],
  [web3.utils.keccak256('MasterThesis.DeviceRegistry.registration.v6'),deployment,chain,registry.options.address,quoteVerifier,participant,signer.address,web3.utils.keccak256(origin),web3.utils.keccak256(origin),web3.utils.keccak256(publicKey),composeHash,digest,workerPolicy,receiptKey,nonce]));
 const report=Buffer.concat([Buffer.from(binding.slice(2),'hex'),Buffer.from(web3.utils.padLeft(web3.utils.toHex(nonce),64).slice(2),'hex')]);
 const quote=await client.getQuote(report);
 const authority=await registry.methods.enrollmentDigest(participant,signer.address,receiptKey,hex(compose)).call();
 const authorization=signRawDigest(authority,env.PRIVATE_KEY);
 const eventlog=events(quote.event_log);
 lastEnrollment={quote:quote.quote.startsWith('0x')?quote.quote:'0x'+quote.quote,eventlog,compose:hex(compose),origin,authorization};
 return {challenge,participant,action_key:signer.address,public_key:publicKey,sello_key:receiptKey,app_id:info.app_id,app_compose:text,quote:quote.quote,event_log:quote.event_log,report_data:hex(report),origin,process_instance_id:instance};
}
async function dispatch(p){
 switch(p.operation){
  case 'enrollment-evidence':return await enrollment(p.challenge);
  case 'enroll':{
   if(!lastEnrollment)throw new Error('Enrollment evidence must precede submission');
   const x=lastEnrollment;
   return await transact(registry,registry.methods.registerDeviceWithAttestedAppCompose(x.quote,x.eventlog,x.compose,participant,signer.address,x.origin,x.origin,publicKey,receiptKey,x.authorization));
  }
  case 'contribution':{
   const round=await gm.methods.getRound().call();
   const aggregator=await selection.methods.getCurrentAggregator().call();
   const nonce=await gm.methods.workerSubmissionNonces(participant).call();
   const parent=await gm.methods.currentParentModelHash().call();
   const model=web3.utils.keccak256('chapter7:'+participant+':'+round+':'+String(p.variant||'original'));
   const packageHash=web3.utils.keccak256('chapter7-package:'+model);
   const digest=await gm.methods.modelSubmissionDigest(round,participant,aggregator,parent,model,packageHash,nonce).call();
   const signature=await signer.signDigest(digest);
   return {round,worker:participant,modelHash:model,packageHash,parentModelHash:parent,workerNonce:nonce,workerSignature:signature};
  }
  case 'submit':{
   const x=p.commitment;
   return await transact(gm,gm.methods.recordModelSubmission(x.round,x.worker,x.modelHash,x.packageHash,x.parentModelHash,x.workerNonce,x.workerSignature));
  }
  case 'open':return await transact(gm,gm.methods.openModelSubmissions(p.round));
  case 'close':return await transact(gm,gm.methods.closeModelSubmissions(p.round));
  case 'select':return await transact(selection,selection.methods.triggerAggregatorSelection());
  case 'timeout-vote':return await transact(selection,selection.methods.reportAggregatorTimeout(p.round,p.aggregator));
  case 'penalty':return await transact(gm,gm.methods.penalizeContribution(p.round,p.aggregator,[p.target],p.reason));
  case 'publish':{
   const round=p.round;
   const x=await policy.methods.getRoundPolicy(round).call();
   const model='chapter7-test-model-'+round;
   const signatureCid='chapter7-test-signature-'+round;
   const keyBundle='chapter7-test-keybundle-'+round;
   const outputModelHash=web3.utils.keccak256(model);
   const outputBundleHash=web3.utils.keccak256(web3.eth.abi.encodeParameters(['string','string','string','bytes32'],[model,signatureCid,keyBundle,outputModelHash]));
   const publicationHash=web3.utils.keccak256(web3.eth.abi.encodeParameters(['string','string','string'],[model,signatureCid,keyBundle]));
   const nonce=await policy.methods.aggregationNonces(participant).call();
   const digest=await policy.methods.aggregationStatementDigest(round,participant,x.inputRoot??x[11],x.inputCount??x[5],x.algorithmHash??x[7],x.policyHash??x[10],outputModelHash,outputBundleHash,publicationHash,nonce).call();
   return await transact(gm,gm.methods.finalizeRoundWithAggregation(model,signatureCid,keyBundle,outputModelHash,outputBundleHash,await signer.signDigest(digest)));
  }
  default:throw new Error('Operation is not in the bounded test catalog');
 }
}
let busy=false;
const expected=crypto.createHash('sha256').update('Bearer '+env.PRIVATE_KEY).digest();
http.createServer(async(req,res)=>{
 const send=(status,value)=>res.writeHead(status,{'Content-Type':'application/json'}).end(stringify(value));
 if(req.method==='GET'&&req.url==='/health')return send(200,{ready:true,process_instance_id:instance,participant,action_key:signer.address});
 if(req.method!=='POST'||req.url!=='/v1/evaluation/contract')return send(404,{});
 const actual=crypto.createHash('sha256').update(req.headers.authorization||'').digest();
 if(!crypto.timingSafeEqual(expected,actual))return send(401,{error:'EVALUATION_AUTH_REQUIRED'});
 if(busy)return send(409,{error:'BUSY'});
 busy=true;
 try{
  let size=0,parts=[];for await(const b of req){size+=b.length;if(size>16384)throw new Error('Size');parts.push(b);}
  const p=JSON.parse(Buffer.concat(parts));if(!/^[a-f0-9]{64}$/.test(p.challenge||''))throw new Error('Challenge');
  const value=await dispatch(p);send(200,{schema_version:1,challenge:p.challenge,operation:p.operation,...value});
 }catch(e){send(503,{error:'BOUNDED_ROLE_OPERATION_FAILED',error_type:e.constructor.name});}
 finally{busy=false;}
}).listen(8001,'0.0.0.0');
