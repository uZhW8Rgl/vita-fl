/** Independent random roots, one per participant/app in this service instance. */
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {canonicalClaims,deriveApplicationKey} from '../dfl/node_server/dist/attested_key_protocol.js';

const canonicalScope=({chainId,policy,serviceId,registry})=>{
    const scope={version:1,chainId,policy,serviceId,registry};
    if(chainId!=='31337'||![policy,registry].every(x=>/^0x[a-f0-9]{40}$/.test(x))||
        !/^[a-f0-9]{64}$/.test(serviceId))throw new Error('Invalid root-store scope');
    return JSON.stringify(scope);
};
async function privateDirectory(directory){
    const stat=await fs.lstat(directory);
    if(!stat.isDirectory()||stat.isSymbolicLink()||stat.uid!==process.getuid()||(stat.mode&0o077))throw new Error('Unsafe worker-root directory');
}
async function readPrivate(filename){
    const file=await fs.open(filename,constants.O_RDONLY|constants.O_NOFOLLOW);
    try{
        const stat=await file.stat();
        if(!stat.isFile()||stat.uid!==process.getuid()||(stat.mode&0o077)||stat.size>2048)throw new Error('Unsafe worker-root file');
        return await file.readFile('utf8');
    }finally{await file.close();}
}
async function writeOnce(directory,filename,value){
    const temporary=path.join(directory,'.pending-'+crypto.randomBytes(16).toString('hex'));
    const file=await fs.open(temporary,constants.O_WRONLY|constants.O_CREAT|constants.O_EXCL|constants.O_NOFOLLOW,0o600);
    try{await file.writeFile(value);await file.sync();}finally{await file.close();}
    try{
        // link is atomic and cannot replace an existing worker root.
        await fs.link(temporary,filename);
        const parent=await fs.open(directory,constants.O_RDONLY|constants.O_DIRECTORY);
        try{await parent.sync();}finally{await parent.close();}
    }finally{await fs.unlink(temporary);}
}
export async function initialiseWorkerRoots(directory,scope){
    const encoded=canonicalScope(scope);
    await fs.mkdir(directory,{mode:0o700});
    await privateDirectory(directory);
    await writeOnce(directory,path.join(directory,'scope.json'),encoded);
}
export async function openWorkerRoots(directory,scope){
    const encoded=canonicalScope(scope);
    await privateDirectory(directory);
    if(await readPrivate(path.join(directory,'scope.json'))!==encoded)throw new Error('Worker-root service scope changed');
    return {async applicationKey(claims,{restoring}){
        canonicalClaims(claims);
        if(canonicalScope(claims)!==encoded)throw new Error('Worker-root scope mismatch');
        await privateDirectory(directory);
        const identity=JSON.stringify([claims.participant,claims.appId]);
        const filename=path.join(directory,crypto.createHash('sha256').update(identity).digest('hex')+'.json');
        let root;
        if(restoring){
            // Missing/corrupt persistent state is an error; never replace a lost root.
            const stored=JSON.parse(await readPrivate(filename));
            if(Object.keys(stored).sort().join()!=='identity,root,version'||stored.version!==1||stored.identity!==identity||
                !/^[a-f0-9]{64}$/.test(stored.root))throw new Error('Invalid persisted worker root');
            root=Buffer.from(stored.root,'hex');
        }else{
            root=crypto.randomBytes(32);
            try{await writeOnce(directory,filename,JSON.stringify({version:1,identity,root:root.toString('hex')}));}
            catch(error){root.fill(0);throw error;}
        }
        try{return deriveApplicationKey(root,claims);}finally{root.fill(0);}
    }};
}
