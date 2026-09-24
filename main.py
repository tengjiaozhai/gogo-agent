"""Convenience entrypoint for launching the GoGo Agent API server in PyCharm or terminal."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("gogo_agent.api:app", host="127.0.0.1", port=8000, reload=True)
