# GoGo Agent：001 启动练习

本仓库正在按 [学习路线](docs/学习路线/README.md) 从 Java 版迁移到 AgentScope Python。001 目前提供最小终端 Agent、只读日期工具和无凭证健康检查；登录、会话等功能属于后续编号。

## 环境与启动

安装 [uv](https://docs.astral.sh/uv/) 后，在仓库根目录运行一条命令。`uv run` 会创建 Python 3.12 环境、按 `uv.lock` 安装依赖并启动本地检查：

```sh
uv run --locked gogo-agent --health
```

`.python-version` 固定 Python 3.12.13；预期输出包含 `AgentScope 2.0.8` 和 `本地启动检查通过`。查看参数：`uv run --locked gogo-agent --help`。

## 调用测试模型

在项目根目录的 `.env` 中设置三个环境变量。需要新建文件时，可从 `.env.example` 复制；已有 `.env` 请保留并补齐变量：

- `GOGO_MODEL_BASE_URL`：OpenAI 兼容网关的根地址；CLI 使用 `/v1/chat/completions`。
- `GOGO_MODEL_NAME`：网关接受的模型标识。
- `GOGO_MODEL_API_KEY`：测试密钥，仅从环境变量或 `.env` 读取，不写入仓库。

CLI 启动时会从项目根目录加载 `.env`。该文件已加入 Git 忽略规则，不要提交密钥。配置后运行：

```sh
uv run --locked gogo-agent
```

终端会显示 AgentScope 版本、`get_current_date` 工具结果和模型回答。模型未成功调用工具或未返回文本时，命令以非零状态退出。未配置三个变量时，命令会列出缺失项，且不会发起网络请求。

## HTTP 健康检查与测试

```sh
uv run --locked uvicorn gogo_agent.health:app --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000/health` 得到 `status` 和 AgentScope 版本。该端点只证明进程和依赖可用，不检查模型网关。

```sh
uv run --locked pytest -q tests/test_001.py
```

测试覆盖纯日期函数、配置错误、健康检查，以及用本地模拟 `/v1/chat/completions` 完成一次工具调用和模型回答；无需真实模型密钥。
