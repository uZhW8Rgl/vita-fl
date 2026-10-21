/** Independent replay of a fresh public service proof; no credential input. */
import {verifyServiceResponse,validateServiceClaims,appraiseServiceEvidence}
    from '../dfl/node_server/dist/service_attestation.js';
let text='';for await(const chunk of process.stdin){text+=chunk;if(text.length>1048576)throw new Error('Proof too large');}
const {proof,policy,scope}=JSON.parse(text),envelope=proof.envelope;
const binding={serviceInstanceId:envelope.serviceInstanceId,servicePublicKey:envelope.servicePublicKey};
const value=verifyServiceResponse(envelope,{...binding,operation:'/v3/attestation',requestId:proof.nonce},200);
validateServiceClaims(value.claims,policy,proof.nonce,scope);
if(value.claims.serviceInstanceId!==binding.serviceInstanceId||value.claims.servicePublicKey!==binding.servicePublicKey)
    throw new Error('Attested signer mismatch');
await appraiseServiceEvidence(value.claims,value.evidence,policy);
console.log(JSON.stringify({verified:true,...binding}));
