"""Authoritative HTTP entry for the GoGo Agent backend."""

from importlib.metadata import version

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from scalar_fastapi import add_scalar_reference

from gogo_agent.auth.router import router as auth_router
from gogo_agent.chat.router import router as chat_router

app = FastAPI(title="GoGo Agent")

app.include_router(auth_router)
app.include_router(chat_router)
add_scalar_reference(app, route="/scalar", title="GoGo Agent API 文档")


@app.get("/doc.html", include_in_schema=False)
async def doc_html_redirect() -> RedirectResponse:
    """兼容习惯 Knife4j (/doc.html) 的开发者，自动重定向至 Scalar 文档。"""
    return RedirectResponse(url="/scalar")



@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """统一 HTTP 异常响应结构，兼容原前端 code 与 message 字段解析。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": exc.detail,
            "detail": exc.detail,
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agentscope_version": version("agentscope")}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("gogo_agent.api:app", host="127.0.0.1", port=8000, reload=True)

