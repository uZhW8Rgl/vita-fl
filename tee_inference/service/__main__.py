"""Run the development HTTP server from environment configuration."""

import uvicorn

from tee_inference.service.app import from_environment


if __name__ == "__main__":
    uvicorn.run(from_environment(), host="0.0.0.0", port=8080)

