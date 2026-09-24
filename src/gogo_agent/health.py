"""Credential-free HTTP liveness endpoint for the first startup exercise."""

from importlib.metadata import version

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agentscope_version": version("agentscope")}
