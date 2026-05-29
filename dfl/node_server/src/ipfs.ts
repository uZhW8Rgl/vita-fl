import axios from "axios";
import fs from "fs";
import FormData from "form-data";

import { setGlobalModelAndSignature, getCurrentGM, getCurrentGMSignature, getRound } from "./bc_client.js";

export const pinFile = async (filePath: string) => {
    try {
      const formData = new FormData();
      const file = fs.createReadStream(filePath);
      formData.append("file", file);

      if (process.env.IPFS_PROVIDER === "kubo") {
        const api = kuboApiBaseUrl();
        if (!api) {
          throw new Error("IPFS_PROVIDER=kubo requires KUBO_API");
        }

        const res = await axios.post(
          `${api}/api/v0/add?pin=true&cid-version=1&wrap-with-directory=false`,
          formData,
          {
            headers: formData.getHeaders(),
            timeout: ipfsTimeoutMs(),
          },
        );
        return res.data.Hash;
      }
  
      const pinataMetadata = JSON.stringify({
        name: "File name",
      });
      formData.append("pinataMetadata", pinataMetadata);
  
      const pinataOptions = JSON.stringify({
        cidVersion: 1,
      });
      formData.append("pinataOptions", pinataOptions);
  
      const res = await axios.post(
        "https://api.pinata.cloud/pinning/pinFileToIPFS",
        formData,
        {
          headers: {
            Authorization: `Bearer ${process.env.PINATA_JWT}`,
          },
        }
      );
      return res.data.IpfsHash;
    } catch (error) {
      console.error(`Failed to pin ${filePath} to IPFS`, error);
      throw error;
    }
  }

  const ipfsTimeoutMs = () => Number(process.env.IPFS_FETCH_TIMEOUT_MS || 30000);

  const kuboApiBaseUrl = () => (process.env.KUBO_API || "").replace(/\/+$/, "");

  const ipfsArchiveDir = () => {
    const raw = (process.env.IPFS_ARCHIVE_DIR || "/").trim();
    if (raw === "/" || raw === "") {
      return "/";
    }
    return raw.replace(/\/+$/, "");
  };

  const timestampedArtifactName = async (baseName: string) => {
    const round = Number(await getRound().catch(() => 0)) + 1;
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    return `round-${round}-${stamp}-${baseName}`;
  };

  const ensureKuboMfsDir = async (dir: string) => {
    const api = kuboApiBaseUrl();
    if (!api) {
      throw new Error("IPFS_PROVIDER=kubo requires KUBO_API");
    }
    await axios.post(`${api}/api/v0/files/mkdir`, null, {
      params: {
        arg: dir,
        parents: true,
      },
      timeout: ipfsTimeoutMs(),
    });
  };

  const copyCidToKuboMfs = async (cid: string, destPath: string) => {
    const api = kuboApiBaseUrl();
    if (!api) {
      throw new Error("IPFS_PROVIDER=kubo requires KUBO_API");
    }
    await axios.post(`${api}/api/v0/files/cp`, null, {
      params: {
        arg: [`/ipfs/${cid}`, destPath],
      },
      timeout: ipfsTimeoutMs(),
      paramsSerializer: {
        serialize: (params) => {
          const values = Array.isArray(params.arg) ? params.arg : [params.arg];
          return values.map((value) => `arg=${encodeURIComponent(String(value))}`).join("&");
        },
      },
    });
  };

  const archivePinnedFileToKubo = async (cid: string, baseName: string) => {
    if (process.env.IPFS_PROVIDER !== "kubo") {
      return;
    }
    const dir = ipfsArchiveDir();
    const filename = await timestampedArtifactName(baseName);
    const destPath = dir === "/" ? `/${filename}` : `${dir}/${filename}`;
    if (dir !== "/") {
      await ensureKuboMfsDir(dir);
    }
    await copyCidToKuboMfs(cid, destPath);
    console.log(`Archived ${baseName} in Kubo MFS: ${destPath}`);
  };
  
  const ipfsGatewayUrl = (hash: string) => {
    const configuredGateway = process.env.IPFS_PROVIDER === "kubo"
      ? process.env.KUBO_GATEWAY
      : process.env.IPFS_GATEWAY;
    const gateway = (configuredGateway || process.env.IPFS_GATEWAY || process.env.KUBO_GATEWAY || "").replace(/\/+$/, "");
    if (!gateway) {
      throw new Error("Missing IPFS gateway configuration");
    }
    return gateway.endsWith("/ipfs") ? `${gateway}/${hash}` : `${gateway}/ipfs/${hash}`;
  }

  const kuboApiCatUrl = (hash: string) => {
    const api = kuboApiBaseUrl();
    return api ? `${api}/api/v0/cat?arg=${encodeURIComponent(hash)}` : "";
  }

  export const getFileFromIPFS = async (hash: string, outPath: string = "./data/gm.bin") => {
    const catUrl = process.env.IPFS_PROVIDER === "kubo" ? kuboApiCatUrl(hash) : "";
    const url = catUrl || ipfsGatewayUrl(hash);
    try {
      const res = catUrl
        ? await axios.post(url, null, { responseType: "arraybuffer", timeout: ipfsTimeoutMs() })
        : await axios.get(url, { responseType: "arraybuffer", timeout: ipfsTimeoutMs() });
      fs.writeFileSync(outPath, Buffer.from(res.data));
      console.log(`File written to ${outPath} from ${url}`);
    } catch (error) {
      if (catUrl) {
        const gatewayUrl = ipfsGatewayUrl(hash);
        console.warn(`Kubo API cat failed for ${hash}; falling back to gateway ${gatewayUrl}`);
        const res = await axios.get(gatewayUrl, { responseType: "arraybuffer", timeout: ipfsTimeoutMs() });
        fs.writeFileSync(outPath, Buffer.from(res.data));
        console.log(`File written to ${outPath} from ${gatewayUrl}`);
        return;
      }
      console.error(`Failed to fetch IPFS file ${hash} from ${url}`, error);
      throw error;
    }
  }
  
  export const updateGM = async () => {
    const modelPath = "./data/results_iid/aggregated.bin";
    const sigPath = "./data/results_iid/aggregated.bin.sig";

    const modelCid = await pinFile(modelPath);
    if (!modelCid) throw new Error("Pinning failed for model, no CID returned");
    await archivePinnedFileToKubo(modelCid, "aggregated.bin");

    const sigCid = await pinFile(sigPath);
    if (!sigCid) throw new Error("Pinning failed for signature, no CID returned");
    await archivePinnedFileToKubo(sigCid, "aggregated.bin.sig");

    console.log("New GM CID:", modelCid);
    console.log("New GM SIG CID:", sigCid);

    await setGlobalModelAndSignature(modelCid, sigCid);
    console.log("Global model + signature updated (on-chain) ");
  }
  
  export const getCurrentModel = async () => {
    const modelCid = String(await getCurrentGM() || "");
    const sigCid = String(await getCurrentGMSignature() || "");
    console.log("Model CID:", modelCid);
    console.log("Sig CID:", sigCid);

    if (modelCid.length > 0) {
      await getFileFromIPFS(modelCid, "./data/gm.bin");
    }
    if (sigCid.length > 0) {
      await getFileFromIPFS(sigCid, "./data/gm.bin.sig");
    }
    console.log("Global model fetched" + (sigCid ? " + signature" : ""));
  }
