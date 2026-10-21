/** Independent random roots held only in the serving process's memory. */
import crypto from 'node:crypto';
import {canonicalClaims,deriveApplicationKey} from '../dfl/node_server/dist/attested_key_protocol.js';

const canonicalScope=({chainId,policy,serviceId,registry})=>{
    const scope={version:1,chainId,policy,serviceId,registry};
    if(chainId!=='31337'||![policy,registry].every(x=>/^0x[a-f0-9]{40}$/.test(x))||
        !/^[a-f0-9]{64}$/.test(serviceId))throw new Error('Invalid root-store scope');
    return JSON.stringify(scope);
};
export function createMemoryWorkerRoots(scope){
    const encoded=canonicalScope(scope),roots=new Map();let destroyed=false;
    return {
        get size(){return roots.size;},
        destroy(){for(const root of roots.values())root.fill(0);roots.clear();destroyed=true;},
        async applicationKey(claims,{restoring}){
            if(destroyed)throw new Error('Worker-root store closed');
            canonicalClaims(claims);
            if(canonicalScope(claims)!==encoded)throw new Error('Worker-root scope mismatch');
            const identity=JSON.stringify([claims.participant,claims.appId]);
            if(restoring){
                // A service restart loses all roots. Never create a substitute
                // for an identity already recorded on-chain.
                if(!roots.has(identity))throw new Error('Worker root no longer available');
            }else{
                if(roots.has(identity))throw new Error('Worker root already exists');
                roots.set(identity,crypto.randomBytes(32));
            }
            return deriveApplicationKey(roots.get(identity),claims);
        }
    };
}
