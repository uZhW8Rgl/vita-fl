import http from 'node:http';
/** Stop collection without letting an unfinished HTTP body delay aggregation. */
export function createDrainingUploadServer(handleRequest, onError) {
    const active = new Set();
    const sockets = new Set();
    let closing = false;
    let draining;
    const server = http.createServer((request, response) => {
        if (closing) {
            // This can be a pipelined request on an existing connection.
            request.destroy();
            return;
        }
        const entry = { request, response, task: Promise.resolve() };
        active.add(entry);
        entry.task = Promise.resolve()
            .then(() => handleRequest(request, response))
            .then(() => undefined)
            .catch(error => {
            try {
                onError(error, request, response);
            }
            catch (reportError) {
                console.error('Failed to report model-upload error:', reportError);
            }
        })
            .finally(async () => {
            // A handler can finish immediately after response.end(), while
            // its bytes are still flushing. Keep that socket protected too.
            if (!response.writableFinished && !response.destroyed) {
                await new Promise(resolve => {
                    response.once('finish', resolve);
                    response.once('close', resolve);
                });
            }
            active.delete(entry);
        });
    });
    server.on('connection', socket => {
        if (closing) {
            socket.destroy();
            return;
        }
        sockets.add(socket);
        socket.once('close', () => sockets.delete(socket));
    });
    function drain() {
        if (draining)
            return draining;
        closing = true;
        draining = (async () => {
            const closed = new Promise((resolve, reject) => {
                server.close(error => {
                    if (error && error.code !== 'ERR_SERVER_NOT_RUNNING')
                        reject(error);
                    else
                        resolve();
                });
            });
            const completedSockets = new Set();
            for (const { request, response } of active) {
                if (request.complete) {
                    // Fully received work can already be waiting in the serial
                    // model-validation/commit queue. Its task must finish.
                    completedSockets.add(request.socket);
                    if (!response.headersSent)
                        response.setHeader('Connection', 'close');
                }
                else {
                    request.destroy();
                }
            }
            // Include connections that have not even supplied complete headers;
            // those have no request handler to be found in `active` yet.
            for (const socket of sockets) {
                if (!completedSockets.has(socket))
                    socket.destroy();
            }
            while (active.size > 0) {
                await Promise.allSettled(Array.from(active, entry => entry.task));
            }
            server.closeIdleConnections();
            await closed;
        })();
        return draining;
    }
    return { server, drain };
}
