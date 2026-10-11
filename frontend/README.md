# GoGo 前端（提前联调）

本目录从 Java 基线项目的 React 前端迁入，当前连接 Python 后端的登录、会话和基础聊天能力。已迁移与后续范围见[前端迁移记录](../docs/迁移/前端提前迁移.md)。

想通过页面逐项调试阶段 D（029–041），使用[前端与 Scalar 逐步调试指南](../docs/契约样例/阶段D-前端与Scalar逐步调试.md)。当前 React 已提供“我的差旅”和管理员审批页面；行程方案 HTML、资料编辑、真实供应商预订和供应商取消仍按后端契约暂不展示。

## 启动

先在仓库根目录配置 `.env` 中的模型网关变量并启动 Python 服务：

```sh
uv run --locked uvicorn gogo_agent.api:app --host 127.0.0.1 --port 8000
```

另开终端，在仓库根目录运行：

```sh
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

打开 Vite 输出的本地地址。前端默认使用同源 `/api`；开发代理将请求转发到 `127.0.0.1:8000`。无需设置 `VITE_API_BASE`。若部署为静态资源，反向代理也须将同源 `/api` 指向 Python 服务。Node 需满足当前 `package.json` 所锁定的 Vite 运行要求；本次使用 Node 24 验证。

若 `8000` 已被其他服务占用，可将 Python 改在其他端口启动，并在运行 Vite 时设置 `GOGO_API_PROXY_TARGET=http://127.0.0.1:<端口>`。

未设置数据库或 Redis 时，后端可能使用内存账号、聊天历史和登录态。真实聊天还需要模型网关；离线前端构建与 Lint 不需要它。当前界面已开放登录、聊天、我的差旅和管理员审批；偏好、调试直连、HITL 回复、打断和集群轮询仍在后续编号恢复。

## 检查

```sh
npm run build
npm run lint
```
