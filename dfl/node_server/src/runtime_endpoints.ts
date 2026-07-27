export const normalizeDstackAppId = (appId: unknown) => {
    const id = String(appId || "").replace(/^app_/, "");
    if (!/^[0-9a-f]{40}$/i.test(id)) {
        throw new Error(`Invalid dstack app_id: ${appId}`);
    }
    return id.toLowerCase();
};

export const dstackHttpsEndpoint = ({
    appId,
    port,
    gatewayDomain,
}: {
    appId: unknown;
    port: number;
    gatewayDomain: string;
}) => {
    const id = normalizeDstackAppId(appId);
    if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
        throw new Error(`Invalid dstack gateway port: ${port}`);
    }
    const domain = String(gatewayDomain || "").trim().replace(/^\./, "").toLowerCase();
    if (
        !domain ||
        domain.length > 253 ||
        !/^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(domain)
    ) {
        throw new Error(`Invalid dstack gateway domain: ${gatewayDomain}`);
    }
    return `https://${id}-${port}.${domain}`;
};
