/** Ephemeral attested service signer; no identity or worker root is persisted. */
import crypto from 'node:crypto';
import {rawPublicKey} from '../dfl/node_server/dist/attested_key_protocol.js';
import {validateServiceTrustPolicy,validateServiceClaims,servicePolicyHash,serviceReportData,
    signServiceResponse,appraiseServiceEvidence} from '../dfl/node_server/dist/service_attestation.js';

export function createServiceIdentity({scope,policy,evidence,appraise=appraiseServiceEvidence,now=()=>Math.floor(Date.now()/1000)}){
    validateServiceTrustPolicy(policy);
    let signer=crypto.generateKeyPairSync('ed25519').privateKey;
    const binding={serviceInstanceId:crypto.randomBytes(32).toString('hex'),
        servicePublicKey:rawPublicKey(crypto.createPublicKey(signer))};
    let authorisedUntil=0,checking;
    const proof=async nonce=>{
        if(!signer)throw new Error('Service signer closed');
        const claims={version:1,protocol:'onchain-v3',...scope,...binding,nonce,issuedAt:now(),expiresAt:now()+120,
            appId:policy.appId,composeHash:policy.composeHash,trustPolicyHash:servicePolicyHash(policy)};
        validateServiceClaims(claims,policy,nonce,scope,now());
        return {claims,evidence:await evidence(serviceReportData(claims))};
    };
    return {
        binding:()=>({...binding}),
        async ensureAuthorised(){
            if(!signer)throw new Error('Service signer closed');
            if(authorisedUntil>now())return;
            if(!checking)checking=(async()=>{
                const value=await proof(crypto.randomBytes(32).toString('hex'));
                await appraise(value.claims,value.evidence,policy);
                validateServiceClaims(value.claims,policy,value.claims.nonce,scope,now());
                // Only a completed positive appraisal opens the root-use gate.
                authorisedUntil=Math.min(value.claims.expiresAt,now()+60);
            })().finally(()=>{checking=undefined;});
            await checking;
        },
        async attest(nonce){await this.ensureAuthorised();return proof(nonce);},
        sign(operation,requestId,status,payload){
            if(!signer)throw new Error('Service signer closed');
            return signServiceResponse(signer,binding,operation,requestId,status,payload);
        },
        destroy(){authorisedUntil=0;signer=undefined;},
    };
}
