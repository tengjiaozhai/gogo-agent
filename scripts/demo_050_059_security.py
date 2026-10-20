"""050/051/055/056/057/059：凭证与第三方鉴权的离线断点示例。

最短运行路线（仓库根目录；不需要数据库、Redis、模型密钥或任何外部账号）：
    PYTHONPATH=src .venv/bin/python scripts/demo_050_059_security.py --case all
    PYTHONPATH=src .venv/bin/python scripts/demo_050_059_security.py --case 059
可选 --case：050 | 051 | 055 | 056 | 057 | 059 | all。
只有 059 用进程环境变量 GOGO_SECRET_KEYS 里的密钥；未设置时脚本只为本进程生成一把随机密钥（不打印、不落盘）。
其余场景各自生成私有密钥，互不依赖。
逐步准备、IDE 参数、断点表、Java 对照和练习答案见
docs/契约样例/阶段E-050-059-调试指南.md。

所有授权方、CLI、航班管家和供应商都是替身：授权方是 MockAuthorizationServer，CLI 是 MockPkceCli，
HTTP 用 httpx.MockTransport。050、051 在 Java 当前规划/预订 Agent 中未启用，只是独立练习。
输出里每次会变化的值（授权码、state、Token、密文）一律不打印，只打印稳定的布尔值、错误码和脱敏串。
"""

import argparse
import asyncio
import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
from sqlalchemy import text

from gogo_agent.security.apikey import ApiKeyError, create_api_key_service, create_memory_session_factory
from gogo_agent.security.cipher import ENV_NAME, SecretCipher, generate_keys_env
from gogo_agent.security.credentials import INTEGRATIONS, CredentialError, CredentialResolver
from gogo_agent.security.exercises import flight_manager_key as fm
from gogo_agent.security.exercises.rgh_workspace import InMemoryTokenStore, RghNode
from gogo_agent.security.masking import SensitiveMasker
from gogo_agent.security.pkce import (
    MockAuthorizationServer, MockPkceCli, PkceError, PkceLoginService, code_challenge_s256,
)
from gogo_agent.security.token_header import TokenHeaderAuth, TokenHeaderError, TokenHeaderPolicy

ALICE_KEY = "sk-fake-ALICE-1234567890abcd"
BOB_KEY = "sk-fake-BOB-0987654321wxyz"


class FakeClock:
    """可手动拨快的时钟，用来确定性地触发过期。"""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class LogCapture(logging.Handler):
    """收集所有日志文本，用于断言没有明文。"""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def say(tag: str, **values: object) -> None:
    print(f"[{tag}] " + " ".join(f"{key}={value}" for key, value in values.items()))


def code_of(call) -> str:
    try:
        call()
    except Exception as error:  # 演示只关心稳定错误码
        return getattr(error, "code", type(error).__name__)
    return "NO_ERROR"


def case_059() -> None:
    capture = LogCapture()
    logging.getLogger().addHandler(capture)
    logging.getLogger().setLevel(logging.DEBUG)
    factory = create_memory_session_factory()
    cipher = SecretCipher.from_env()  # 断点 059-0：密钥只从环境变量 GOGO_SECRET_KEYS 读取
    service = create_api_key_service(cipher, factory)
    info = service.save("alice", "tuniu-cli", ALICE_KEY)  # 断点 059-1：save → _upsert 里加密
    service.save("bob", "tuniu-cli", BOB_KEY)
    say("059", saved_masked=info.masked_key, key_version_is_current=info.key_version == cipher.current_version)
    with factory() as session:
        dumped = str(session.execute(text("select * from user_api_key")).fetchall())
    say("059", db_contains_plaintext=ALICE_KEY in dumped or "1234567890" in dumped, ciphertext_has_version_prefix=f"{cipher.current_version}." in dumped)
    say("059", alice_list=[item.masked_key for item in service.list_for_user("alice")], bob_list=[item.masked_key for item in service.list_for_user("bob")])
    secret = service.resolve_for_use("alice", "tuniu-cli")  # 断点 059-2：唯一取明文入口
    say("059", resolve_repr=repr(secret), resolved_equals_saved=secret.get_secret_value() == ALICE_KEY)
    say("059", bob_resolves_alice_key=service.resolve_for_use("bob", "tuniu-cli").get_secret_value() == ALICE_KEY)
    say("059", missing_provider=code_of(lambda: service.resolve_for_use("alice", "flight-manager")))
    service.save("alice", "tuniu-cli", "sk-fake-ALICE-NEW-aaaaaaaa9999")
    say("059", after_update_old_key_still_works=service.resolve_for_use("alice", "tuniu-cli").get_secret_value() == ALICE_KEY)
    say("059", revoked=service.revoke("alice", "tuniu-cli"), after_revoke=code_of(lambda: service.resolve_for_use("alice", "tuniu-cli")))
    rotated = create_api_key_service(SecretCipher.from_env({ENV_NAME: f"{generate_keys_env('rotated')},{os.environ[ENV_NAME]}"}), factory)
    say("059", bob_version_before=rotated.get_info("bob", "tuniu-cli").key_version == cipher.current_version,
        rotated_rows=rotated.rotate_to_current_key(), bob_version_after=rotated.get_info("bob", "tuniu-cli").key_version,
        bob_still_resolves=rotated.resolve_for_use("bob", "tuniu-cli").get_secret_value() == BOB_KEY)
    say("059", missing_env=code_of(lambda: SecretCipher.from_env({})))
    masked = SensitiveMasker.mask(f"手机 13812345678 key={ALICE_KEY} Authorization: Bearer abcdefghij123456")
    say("059", masked_text=masked)
    logging.getLogger().removeHandler(capture)
    say("059", log_lines=len(capture.lines) > 0, plaintext_in_logs=any(ALICE_KEY in line or BOB_KEY in line for line in capture.lines))


def case_055() -> None:
    for item in INTEGRATIONS:
        say("055", provider=item.provider, holder=item.holder.value, kind=item.kind.value, enabled_in_java=item.enabled_in_java)
    service = create_api_key_service(SecretCipher.from_env({ENV_NAME: generate_keys_env("v1")}), create_memory_session_factory())
    service.save("alice", "tuniu-cli", ALICE_KEY)
    resolver = CredentialResolver({"tuniu-cli": service.has_active}, service_configured={"weather-mcp", "tuniu-cli"})
    decision = resolver.resolve("alice", "tuniu-cli")  # 断点 055-1：用户级只认本人
    say("055", alice_tuniu=decision.authorized_as, holder=decision.holder.value, ref=decision.credential_ref)
    say("055", bob_tuniu=code_of(lambda: resolver.resolve("bob", "tuniu-cli")), admin_tuniu=code_of(lambda: resolver.resolve("admin", "tuniu-cli")))
    say("055", weather=resolver.resolve("alice", "weather-mcp").authorized_as, orizn_unconfigured=code_of(lambda: resolver.resolve("alice", "orizn-visa-mcp")))
    say("055", decision_contains_key=ALICE_KEY in decision.model_dump_json())


def case_056() -> None:
    capture = LogCapture()
    logging.getLogger().addHandler(capture)
    logging.getLogger().setLevel(logging.DEBUG)
    token = "cfg-token-ABCDEF123456"
    policy = TokenHeaderPolicy(provider="vendor-x", allowed_host="api.vendor-x.test", env_var="VENDOR_X_TOKEN")
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.host == "api.vendor-x.test" and request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "https://cdn.other.test/file"})
        return httpx.Response(200, json={"ok": True})

    def client(environ):
        return httpx.Client(auth=TokenHeaderAuth(policy, environ), transport=httpx.MockTransport(handler), follow_redirects=True)

    client({"VENDOR_X_TOKEN": token}).get("https://api.vendor-x.test/v1/ping")  # 断点 056-1：auth_flow 注入
    say("056", header_injected=sent[-1].headers.get("Authorization") == f"Bearer {token}")
    sent.clear()
    say("056", missing_token=code_of(lambda: client({}).get("https://api.vendor-x.test/v1/ping")), requests_sent=len(sent))
    say("056", wrong_host=code_of(lambda: client({"VENDOR_X_TOKEN": token}).get("https://evil.test/v1")),
        http_scheme=code_of(lambda: client({"VENDOR_X_TOKEN": token}).get("http://api.vendor-x.test/v1")), requests_sent=len(sent))
    client({"VENDOR_X_TOKEN": token}).get("https://api.vendor-x.test/redirect")
    say("056", redirect_hosts=[r.url.host for r in sent], redirect_hop_has_auth="Authorization" in sent[-1].headers)
    try:
        client({}).get("https://api.vendor-x.test/v1/ping")
    except TokenHeaderError as error:
        say("056", error_text_has_token=token in str(error))
    logging.getLogger().removeHandler(capture)
    say("056", log_lines=len(capture.lines) > 0, token_in_logs=any(token in line for line in capture.lines))


def browser_approve(server: MockAuthorizationServer, url: str, account: str):
    """模拟用户在浏览器打开授权链接并点击同意。"""
    return server.approve(url, account)


def case_057() -> None:
    say("057", rfc7636_example=code_challenge_s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    clock = FakeClock()
    server = MockAuthorizationServer(clock)
    service = PkceLoginService(server, clock)
    start = service.start("alice", "session-1")  # 断点 057-1：生成 verifier/challenge/state
    params = browser_approve(server, start.authorize_url, "vendor-alice")
    result = service.handle_callback("alice", params.state, params.code)  # 断点 057-2：校验 state 后换令牌
    say("057", login_user=result.user_id, login_session=result.session_id, alice_has_token=service.token_for("alice") is not None,
        bob_has_token=service.token_for("bob") is not None, verifier_in_url="code_verifier" in start.authorize_url)
    say("057", wrong_state=code_of(lambda: service.handle_callback("alice", "forged", params.code)),
        repeated_callback=code_of(lambda: service.handle_callback("alice", params.state, params.code)))
    second = service.start("alice", "session-2")
    second_params = browser_approve(server, second.authorize_url, "vendor-alice")
    say("057", cross_user_state=code_of(lambda: service.handle_callback("bob", second_params.state, second_params.code)))
    bob = service.start("bob", "session-b")
    say("057", stolen_code_with_other_verifier=code_of(lambda: service.handle_callback("bob", bob.state, second_params.code)))
    third = service.start("alice", "session-3")
    third_params = browser_approve(server, third.authorize_url, "vendor-alice")
    clock.now += 61
    say("057", expired_code=code_of(lambda: service.handle_callback("alice", third_params.state, third_params.code)))
    stale = service.start("alice", "session-4")
    clock.now += 601
    say("057", expired_state=code_of(lambda: service.handle_callback("alice", stale.state, "x")))
    print("[057] --- CLI 边界：应用不接触 verifier 与授权码 ---")
    with tempfile.TemporaryDirectory() as base:
        cluster = Cluster(Path(base))
        link = cluster.nodes["A"].run("alice", "s1", "login")  # 断点 057-3：交给 CLI 登录，应用只拿到链接
        cluster.nodes["A"].deliver_callback(browser_approve(cluster.server, link.authorize_url, "vendor-alice"))
        captured = cluster.nodes["A"].capture_login("alice", "s1")
        say("057", cli_link_status=link.status, cli_link_has_challenge="code_challenge=" in link.authorize_url, captured=captured.status, account=captured.account)


class Cluster:
    """两个节点共享授权方与密文存储，各有独立磁盘目录和 CLI 实例。"""

    def __init__(self, base: Path) -> None:
        self.clock = FakeClock()
        self.server = MockAuthorizationServer(self.clock)
        self.store = InMemoryTokenStore(SecretCipher.from_env({ENV_NAME: generate_keys_env("v1")}), self.clock)
        self.nodes = {name: RghNode(base / name, MockPkceCli(self.server, self.clock), self.store) for name in ("A", "B")}

    def login(self, node: str, user: str, session: str, account: str) -> str:
        link = self.nodes[node].run(user, session, "login")
        self.nodes[node].deliver_callback(self.server.approve(link.authorize_url, account))
        return self.nodes[node].capture_login(user, session).status


def case_051() -> None:
    print("[051] 当前未启用：Java 规划/预订 Agent 未注册 rolling-go-hotel Skill，这里只练习假 CLI 的隔离与同步。")
    with tempfile.TemporaryDirectory() as base:
        cluster = Cluster(Path(base))
        node_a, node_b = cluster.nodes["A"], cluster.nodes["B"]
        say("051", first_whoami=node_a.run("alice", "s1", "whoami").status)
        say("051", login_on_A=cluster.login("A", "alice", "s1", "vendor-alice"))  # 断点 051-1：登录并回写共享存储
        say("051", whoami_on_B=node_b.run("alice", "s2", "whoami").status, bob_on_B=node_b.run("bob", "s9", "whoami").status)
        say("051", logout_on_B=node_b.run("alice", "s2", "logout").status, whoami_on_A_after_logout=node_a.run("alice", "s1", "whoami").status)
        cluster.login("A", "alice", "s1", "vendor-alice")
        cluster.clock.now += 3601
        expired = node_b.run("alice", "s3", "whoami")
        say("051", after_token_expiry=expired.status, message=expired.message)
        say("051", unsafe_user_id=code_of(lambda: node_a.lease("../evil", "s1")))
        barrier = Barrier(2)

        def worker(user: str) -> list[str]:
            barrier.wait()
            link = node_a.run(user, f"s-{user}", "login")
            barrier.wait()
            node_a.deliver_callback(cluster.server.approve(link.authorize_url, f"vendor-{user}"))
            node_a.capture_login(user, f"s-{user}")
            return sorted({node_a.run(user, f"s-{user}", "whoami").account for _ in range(20)})

        with ThreadPoolExecutor(2) as pool:  # 断点 051-2：两个线程并发登录两个用户
            carol, dave = pool.map(worker, ("carol", "dave"))
        homes = {user: node_a.lease(user, "x").home for user in ("carol", "dave")}
        say("051", carol_accounts=carol, dave_accounts=dave, same_home=homes["carol"] == homes["dave"],
            token_files_differ=(homes["carol"] / ".hotel-cli/token.json").read_text() != (homes["dave"] / ".hotel-cli/token.json").read_text())


def case_050() -> None:
    print("[050] 当前未启用：Java 规划/预订 Agent 未注册 flight-manager Skill，这里只用假数据练习契约与占位符替换。")
    offer = fm.parse_offer({"flightNo": " mu5101 ", "departureTime": "2026-05-15 08:30", "arrivalTime": "2026-05-15T10:45", "cabin": "经济舱", "price": "¥1,280.5"})
    say("050", flight_no=offer.flight_no, cabin=offer.cabin, cabin_name=offer.cabin_name, price=offer.price, departure=offer.departure_time.isoformat())
    say("050", bad_flight_no=code_of(lambda: fm.normalize_flight_no("1501CA")), bad_price=code_of(lambda: fm.parse_price("-5")))
    service = create_api_key_service(SecretCipher.from_env({ENV_NAME: generate_keys_env("v1")}), create_memory_session_factory())
    service.save("alice", "flight-manager", "sk_per_ALICEFAKE0123456789")
    service.save("bob", "flight-manager", "sk_per_BOBFAKE9876543210")
    wire: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wire.append(request)
        return httpx.Response(200, json={"result": {"structuredContent": {"code": 0}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    injector = fm.FlightKeyInjector(service)
    call = fm.build_search_call(fm.FlightSearchArgs(departure_city_code="bjs", arrival_city_code="sha", date="2026-05-15", cabin_preference="经济舱"))
    context = call.model_dump_json()  # 模型/提示词层只能看到这份 JSON
    say("050", model_context_has_placeholder=fm.PLACEHOLDER in context, model_context_has_key="ALICEFAKE" in context)
    with fm.user_scope("alice"):
        injector.send(call, client)  # 断点 050-1：最后一刻替换占位符
    say("050", wire_auth_is_alice_key=wire[-1].headers["Authorization"] == "Bearer sk_per_ALICEFAKE0123456789", call_still_placeholder=fm.PLACEHOLDER in call.model_dump_json())

    async def one(user: str) -> str:
        with fm.user_scope(user):
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return injector.send(call, client).request.headers["Authorization"]

    async def both() -> list[str]:
        return list(await asyncio.gather(one("alice"), one("bob")))

    say("050", concurrent_tasks_got_own_keys=asyncio.run(both()) == ["Bearer sk_per_ALICEFAKE0123456789", "Bearer sk_per_BOBFAKE9876543210"])  # 断点 050-2：两个 asyncio 任务各用各的 contextvar
    sent_before = len(wire)
    with fm.user_scope("carol"):
        say("050", user_without_key=code_of(lambda: injector.send(call, client)), requests_sent=len(wire) - sent_before)
    say("050", no_user_scope=code_of(lambda: injector.send(call, client)))
    say("050", key_in_planned_call_repr="ALICEFAKE" in repr(call))


CASES = {"050": case_050, "051": case_051, "055": case_055, "056": case_056, "057": case_057, "059": case_059}


def main() -> None:
    parser = argparse.ArgumentParser(description="050-059 凭证与第三方鉴权离线演示")
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    selected = parser.parse_args().case
    if not os.environ.get(ENV_NAME):
        os.environ[ENV_NAME] = generate_keys_env("v1")  # 仅本进程；生产密钥由运维通过环境变量提供
    for name in (CASES if selected == "all" else (selected,)):
        print(f"\n===== {name} =====")
        CASES[name]()


if __name__ == "__main__":
    main()
