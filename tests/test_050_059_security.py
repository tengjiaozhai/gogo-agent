"""阶段 E 050/051/055/056/057/059：凭证与第三方鉴权。所有授权方、CLI 与航班管家均为替身。"""

import asyncio
import json
import logging
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import text

from gogo_agent.security.apikey import (
    ApiKeyError, ApiKeyService, UserApiKeyModel, create_memory_session_factory,
)
from gogo_agent.security.cipher import ENV_NAME, SecretCipher, SecretCipherError, generate_keys_env
from gogo_agent.security.credentials import (
    INTEGRATIONS, CredentialError, CredentialHolder, CredentialResolver,
)
from gogo_agent.security.exercises import flight_manager_key as fm
from gogo_agent.security.exercises.rgh_workspace import InMemoryTokenStore, RghNode, RghWorkspaceError
from gogo_agent.security.masking import SensitiveLogFilter, SensitiveMasker
from gogo_agent.security.pkce import (
    MockAuthorizationServer, MockPkceCli, PkceError, PkceLoginService, code_challenge_s256, generate_code_verifier,
)
from gogo_agent.security.token_header import TokenHeaderAuth, TokenHeaderError, TokenHeaderPolicy

FAKE_KEY = "sk-fake-ALICE-1234567890abcd"
BOB_KEY = "sk-fake-BOB-0987654321wxyz"


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_cipher(*versions: str) -> SecretCipher:
    return SecretCipher.from_env({ENV_NAME: ",".join(generate_keys_env(v) for v in versions)})


def make_service(cipher: SecretCipher | None = None, factory=None) -> ApiKeyService:
    return ApiKeyService(factory or create_memory_session_factory(), cipher or make_cipher("v1"))


def dump_table(factory) -> str:
    with factory() as session:
        return str(session.execute(text("select * from user_api_key")).fetchall())


# ───────────── 059 加密与轮换 ─────────────

def test_cipher_roundtrip_nonce_aad_and_tamper():
    cipher = make_cipher("v1")
    a, b = cipher.encrypt("secret-value-1", "alice"), cipher.encrypt("secret-value-1", "alice")
    assert a != b and a.startswith("v1.") and "secret-value-1" not in a
    assert cipher.decrypt(a, "alice") == "secret-value-1"
    for bad in (lambda: cipher.decrypt(a, "bob"), lambda: cipher.decrypt(a[:-3] + "AAA", "alice")):
        with pytest.raises(SecretCipherError) as error:
            bad()
        assert "secret-value-1" not in str(error.value) and a not in str(error.value)


def test_cipher_env_missing_or_invalid_fails_explicitly_without_echoing_value():
    with pytest.raises(SecretCipherError) as missing:
        SecretCipher.from_env({})
    assert missing.value.code == "SECRET_KEYS_MISSING"
    with pytest.raises(SecretCipherError) as invalid:
        SecretCipher.from_env({ENV_NAME: "v1:not-a-32-byte-key-VALUE"})
    assert invalid.value.code == "SECRET_KEYS_INVALID" and "not-a-32-byte-key-VALUE" not in str(invalid.value)


def test_cipher_rotation_plan():
    v1 = generate_keys_env("v1")
    token = SecretCipher.from_env({ENV_NAME: v1}).encrypt("rotating-secret", "ctx")
    new = SecretCipher.from_env({ENV_NAME: f"{generate_keys_env('v2')},{v1}"})
    assert new.current_version == "v2" and new.needs_rotation(token)
    assert new.decrypt(token, "ctx") == "rotating-secret"
    moved = new.reencrypt(token, "ctx")
    assert new.key_version_of(moved) == "v2" and new.reencrypt(moved, "ctx") == moved
    only_new = SecretCipher.from_env({ENV_NAME: generate_keys_env("v2")})
    with pytest.raises(SecretCipherError) as error:
        only_new.decrypt(token, "ctx")
    assert error.value.code == "SECRET_KEY_VERSION_UNKNOWN"


# ───────────── 059 脱敏 ─────────────

def test_masker_keeps_minimal_tail_and_masks_known_secrets():
    text_in = ("手机 13812345678 身份证 310101200001011234 卡 6222021234567890 邮箱 user@example.com "
               f"key={FAKE_KEY} Authorization: Bearer abcdefghij123456 password: abc123 乘客: 张三")
    masked = SensitiveMasker.mask(text_in)
    for leaked in ("13812345678", "310101200001011234", "6222021234567890", "user@example.com", FAKE_KEY,
                   "abcdefghij123456", "abc123", "张三"):
        assert leaked not in masked
    assert "138****5678" in masked and "****abcd" in masked and "u***@example.com" in masked and "Bearer ****3456" in masked
    assert SensitiveMasker.mask("值=shortsecret1", secrets=["shortsecret1"]) == "值=****ret1"
    assert SensitiveMasker.mask_secret("short") == "****"


def test_log_filter_masks_message_args_and_exception_text():
    logger = logging.getLogger("test.mask")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Capture()
    handler.addFilter(SensitiveLogFilter())
    logger.addHandler(handler)
    try:
        logger.info("调用 key=%s", FAKE_KEY)
        try:
            raise RuntimeError(f"token={FAKE_KEY}")
        except RuntimeError:
            logger.exception("失败 %s", "13812345678")
    finally:
        logger.removeHandler(handler)
    joined = "\n".join(records)
    assert FAKE_KEY not in joined and "13812345678" not in joined and "****abcd" in joined


# ───────────── 059 API Key 服务 ─────────────

def test_table_has_no_plaintext_and_listing_is_masked():
    factory = create_memory_session_factory()
    service = make_service(factory=factory)
    info = service.save("alice", "tuniu-cli", FAKE_KEY)
    assert info.masked_key == "****abcd" and info.key_version == "v1"
    dumped = dump_table(factory)
    assert FAKE_KEY not in dumped and "1234567890" not in dumped and "v1." in dumped
    assert [item.model_dump()["masked_key"] for item in service.list_for_user("alice")] == ["****abcd"]
    assert FAKE_KEY not in repr(service.list_for_user("alice")) and FAKE_KEY not in repr(service.resolve_for_use("alice", "tuniu-cli"))
    assert service.resolve_for_use("alice", "tuniu-cli").get_secret_value() == FAKE_KEY


def test_users_only_manage_their_own_keys():
    service = make_service()
    service.save("alice", "tuniu-cli", FAKE_KEY)
    assert service.list_for_user("bob") == [] and service.get_info("bob", "tuniu-cli") is None
    assert service.revoke("bob", "tuniu-cli") is False
    with pytest.raises(ApiKeyError) as error:
        service.resolve_for_use("bob", "tuniu-cli")
    assert error.value.code == "API_KEY_NOT_CONFIGURED"
    service.save("bob", "tuniu-cli", BOB_KEY)
    assert service.resolve_for_use("alice", "tuniu-cli").get_secret_value() == FAKE_KEY
    assert service.resolve_for_use("bob", "tuniu-cli").get_secret_value() == BOB_KEY


def test_update_and_revoke_make_old_key_unusable_immediately():
    factory = create_memory_session_factory()
    service = make_service(factory=factory)
    service.save("alice", "tuniu-cli", FAKE_KEY)
    service.save("alice", "tuniu-cli", "sk-fake-NEW-aaaaaaaaaaaa9999")
    assert service.resolve_for_use("alice", "tuniu-cli").get_secret_value().endswith("9999")
    assert FAKE_KEY not in dump_table(factory)
    assert service.revoke("alice", "tuniu-cli") is True
    with pytest.raises(ApiKeyError) as error:
        service.resolve_for_use("alice", "tuniu-cli")
    assert error.value.code == "API_KEY_NOT_CONFIGURED"
    with factory() as session:
        row = session.query(UserApiKeyModel).one()
        assert (row.status, row.api_key_enc, row.key_version) == ("revoked", "", "")
    assert service.get_info("alice", "tuniu-cli").masked_key == "****"
    service.save("alice", "tuniu-cli", FAKE_KEY)  # 撤销后可重新配置
    assert service.has_active("alice", "tuniu-cli")


def test_table_rotation_and_ciphertext_cannot_be_moved_to_another_user():
    factory = create_memory_session_factory()
    v1 = generate_keys_env("v1")
    make_service(SecretCipher.from_env({ENV_NAME: v1}), factory).save("alice", "tuniu-cli", FAKE_KEY)
    new = SecretCipher.from_env({ENV_NAME: f"{generate_keys_env('v2')},{v1}"})
    service = make_service(new, factory)
    assert service.resolve_for_use("alice", "tuniu-cli").get_secret_value() == FAKE_KEY
    assert service.rotate_to_current_key() == 1 and service.rotate_to_current_key() == 0
    assert service.get_info("alice", "tuniu-cli").key_version == "v2"
    assert service.resolve_for_use("alice", "tuniu-cli").get_secret_value() == FAKE_KEY
    # 把 alice 的密文拷贝给 bob：绑定数据不一致，必须解不开
    service.save("bob", "tuniu-cli", BOB_KEY)
    with factory() as session, session.begin():
        alice = session.query(UserApiKeyModel).filter_by(user_id="alice").one()
        session.query(UserApiKeyModel).filter_by(user_id="bob").one().api_key_enc = alice.api_key_enc
    with pytest.raises(ApiKeyError) as error:
        service.resolve_for_use("bob", "tuniu-cli")
    assert error.value.code == "API_KEY_UNREADABLE"
    # 旧密钥从配置里移除后，没迁移的行读取失败，不会降级成别的值
    gone = make_service(SecretCipher.from_env({ENV_NAME: generate_keys_env("v3")}), factory)
    with pytest.raises(ApiKeyError):
        gone.resolve_for_use("alice", "tuniu-cli")


def test_invalid_input_rejected_without_echoing_key():
    service = make_service()
    for user, provider, key in (("alice", "tuniu-cli", ""), ("alice", "tuniu-cli", "has space key"), ("alice", "Bad Provider!", FAKE_KEY), ("", "tuniu-cli", FAKE_KEY)):
        with pytest.raises(ApiKeyError) as error:
            service.save(user, provider, key)
        assert error.value.code == "API_KEY_INVALID_INPUT" and FAKE_KEY not in str(error.value)


def test_no_plaintext_in_logs_or_exceptions(caplog):
    caplog.set_level(logging.DEBUG)
    factory = create_memory_session_factory()
    service = make_service(factory=factory)
    errors: list[str] = []
    service.save("alice", "tuniu-cli", FAKE_KEY)
    service.resolve_for_use("alice", "tuniu-cli")
    service.rotate_to_current_key()
    service.revoke("alice", "tuniu-cli")
    for call in (lambda: service.resolve_for_use("alice", "tuniu-cli"), lambda: service.save("alice", "tuniu-cli", FAKE_KEY + " x")):
        with pytest.raises(ApiKeyError) as error:
            call()
        errors.append(str(error.value) + repr(error.value))
    broken = make_service(SecretCipher.from_env({ENV_NAME: generate_keys_env("v9")}), factory)
    service.save("alice", "tuniu-cli", FAKE_KEY)
    with pytest.raises(ApiKeyError) as unreadable:
        broken.resolve_for_use("alice", "tuniu-cli")
    errors.append(str(unreadable.value) + repr(unreadable.value) + repr(unreadable.value.__cause__) + repr(unreadable.value.__context__))
    assert "[ApiKey]" in caplog.text
    assert FAKE_KEY not in caplog.text and FAKE_KEY not in "".join(errors)


# ───────────── 055 授权归属 ─────────────

def test_user_level_provider_uses_only_the_requesting_users_credential():
    service = make_service()
    service.save("alice", "tuniu-cli", FAKE_KEY)
    resolver = CredentialResolver({"tuniu-cli": service.has_active}, service_configured={"tuniu-cli", "weather-mcp"})
    decision = resolver.resolve("alice", "tuniu-cli")
    assert (decision.holder, decision.authorized_as, decision.credential_ref) == (CredentialHolder.USER, "alice", "user:tuniu-cli:alice")
    assert FAKE_KEY not in decision.model_dump_json()
    for other in ("bob", "admin"):  # 即使 alice 有 Key、服务级里也有同名配置，也不兜底
        with pytest.raises(CredentialError) as error:
            resolver.resolve(other, "tuniu-cli")
        assert error.value.code == "CREDENTIAL_USER_NOT_CONFIGURED"


def test_service_level_only_for_providers_marked_service_level():
    resolver = CredentialResolver({}, service_configured={"weather-mcp"})
    decision = resolver.resolve("alice", "weather-mcp")
    assert decision.holder is CredentialHolder.SERVICE and decision.authorized_as == "service:weather-mcp"
    with pytest.raises(CredentialError) as error:
        resolver.resolve("alice", "orizn-visa-mcp")
    assert error.value.code == "CREDENTIAL_SERVICE_NOT_CONFIGURED"
    for user, provider, code in (("", "weather-mcp", "CREDENTIAL_USER_REQUIRED"), ("alice", "nope", "CREDENTIAL_PROVIDER_UNKNOWN")):
        with pytest.raises(CredentialError) as bad:
            resolver.resolve(user, provider)
        assert bad.value.code == code


def test_integration_inventory_marks_unregistered_skills():
    by_provider = {item.provider: item for item in INTEGRATIONS}
    assert len(by_provider) == len(INTEGRATIONS)
    assert by_provider["tuniu-cli"].enabled_in_java and not by_provider["flight-manager"].enabled_in_java
    assert not by_provider["rolling-go-hotel"].enabled_in_java
    assert by_provider["rolling-go-hotel"].holder is CredentialHolder.USER
    assert all(item.resources and item.lifecycle and item.revocation and item.evidence for item in INTEGRATIONS)
    decision = CredentialResolver({"flight-manager": lambda u, p: True}).resolve("alice", "flight-manager")
    assert decision.integration_enabled is False


# ───────────── 056 Token 请求头注入 ─────────────

POLICY = TokenHeaderPolicy(provider="vendor-x", allowed_host="api.vendor-x.test", env_var="VENDOR_X_TOKEN")
TOKEN = "cfg-token-ABCDEF123456"


def make_client(environ, calls, handler=None):
    def default(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.Client(auth=TokenHeaderAuth(POLICY, environ), transport=httpx.MockTransport(recording if handler else default), follow_redirects=True)


def test_token_injected_for_target_host_only_and_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []
    client = make_client({"VENDOR_X_TOKEN": TOKEN}, calls)
    assert client.get("https://api.vendor-x.test/v1/ping").status_code == 200
    assert calls[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in caplog.text and "[TokenHeader]" in caplog.text


def test_missing_token_means_request_never_sent_and_error_has_no_token():
    calls: list[httpx.Request] = []
    with pytest.raises(TokenHeaderError) as error:
        make_client({}, calls).get("https://api.vendor-x.test/v1/ping")
    assert error.value.code == "TOKEN_MISSING" and calls == []
    with pytest.raises(TokenHeaderError) as invalid:
        make_client({"VENDOR_X_TOKEN": TOKEN + "\r\nX-Evil: 1"}, calls).get("https://api.vendor-x.test/v1/ping")
    assert invalid.value.code == "TOKEN_INVALID" and TOKEN not in str(invalid.value) and calls == []


@pytest.mark.parametrize("url,code", [
    ("https://evil.test/v1", "TOKEN_HOST_NOT_ALLOWED"),
    ("https://api.vendor-x.test.evil.test/v1", "TOKEN_HOST_NOT_ALLOWED"),
    ("http://api.vendor-x.test/v1", "TOKEN_INSECURE_SCHEME"),
])
def test_token_never_sent_to_other_hosts_or_plain_http(url, code):
    calls: list[httpx.Request] = []
    with pytest.raises(TokenHeaderError) as error:
        make_client({"VENDOR_X_TOKEN": TOKEN}, calls).get(url)
    assert error.value.code == code and calls == [] and TOKEN not in str(error.value)


def test_cross_origin_redirect_does_not_carry_token():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.vendor-x.test":
            return httpx.Response(302, headers={"Location": "https://cdn.other.test/file"})
        return httpx.Response(200)

    make_client({"VENDOR_X_TOKEN": TOKEN}, calls, handler).get("https://api.vendor-x.test/v1/download")
    assert [call.url.host for call in calls] == ["api.vendor-x.test", "cdn.other.test"]
    assert "Authorization" in calls[0].headers and "Authorization" not in calls[1].headers


# ───────────── 057 PKCE ─────────────

def test_pkce_s256_matches_rfc7636_example_and_verifier_length():
    assert code_challenge_s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert 43 <= len(generate_code_verifier()) <= 128


def make_pkce():
    clock = FakeClock()
    server = MockAuthorizationServer(clock)
    return clock, server, PkceLoginService(server, clock)


def test_pkce_login_restores_original_user_and_session():
    _, server, service = make_pkce()
    start = service.start("alice", "session-1")
    query = parse_qs(urlparse(start.authorize_url).query)
    assert query["code_challenge_method"] == ["S256"] and query["state"] == [start.state]
    assert service._states[start.state].verifier not in start.authorize_url
    params = server.approve(start.authorize_url, "vendor-alice")
    result = service.handle_callback("alice", params.state, params.code)
    assert (result.user_id, result.session_id, result.account) == ("alice", "session-1", "vendor-alice")
    assert service.token_for("alice") is not None and service.token_for("bob") is None
    assert service.token_for("alice").access_token.get_secret_value() not in repr(service.token_for("alice"))


def test_pkce_negative_state_cases():
    clock, server, service = make_pkce()
    start = service.start("alice", "s1")
    params = server.approve(start.authorize_url, "vendor-alice")
    with pytest.raises(PkceError) as wrong:
        service.handle_callback("alice", "forged-state", params.code)
    assert wrong.value.code == "PKCE_STATE_UNKNOWN"
    service.handle_callback("alice", params.state, params.code)
    with pytest.raises(PkceError) as repeated:
        service.handle_callback("alice", params.state, params.code)
    assert repeated.value.code == "PKCE_STATE_UNKNOWN"
    stale = service.start("alice", "s2")
    clock.advance(601)
    with pytest.raises(PkceError) as expired:
        service.handle_callback("alice", stale.state, "anything")
    assert expired.value.code == "PKCE_STATE_EXPIRED"


def test_pkce_negative_code_cases():
    clock, server, service = make_pkce()
    start = service.start("alice", "s1")
    params = server.approve(start.authorize_url, "vendor-alice")
    clock.advance(61)  # state 仍有效（600 秒），授权码已过期（60 秒）
    with pytest.raises(PkceError) as expired:
        service.handle_callback("alice", params.state, params.code)
    assert expired.value.code == "PKCE_CODE_EXPIRED"
    again = service.start("alice", "s1")
    fresh = server.approve(again.authorize_url, "vendor-alice")
    service.handle_callback("alice", fresh.state, fresh.code)
    with pytest.raises(PkceError) as reused:
        server.exchange(fresh.code, "whatever")
    assert reused.value.code == "PKCE_CODE_REUSED"
    with pytest.raises(PkceError) as unknown:
        server.exchange("no-such-code", "whatever")
    assert unknown.value.code == "PKCE_CODE_UNKNOWN"
    with pytest.raises(PkceError) as bad_method:
        server.approve("https://auth.mock.test/authorize?response_type=code&state=s&code_challenge=c&code_challenge_method=plain", "x")
    assert bad_method.value.code == "PKCE_REQUEST_INVALID"


def test_pkce_cross_user_state_and_stolen_code_fail():
    _, server, service = make_pkce()
    alice = service.start("alice", "sa")
    alice_params = server.approve(alice.authorize_url, "vendor-alice")
    with pytest.raises(PkceError) as mismatch:  # bob 用 alice 的 state
        service.handle_callback("bob", alice_params.state, alice_params.code)
    assert mismatch.value.code == "PKCE_STATE_USER_MISMATCH"
    assert service.handle_callback("alice", alice_params.state, alice_params.code).user_id == "alice"  # 越权失败不烧掉 alice 的 state
    carol = service.start("alice", "sc")  # 第二轮：bob 截获 alice 新发的 code，但只有自己的 verifier
    carol_params = server.approve(carol.authorize_url, "vendor-alice")
    bob = service.start("bob", "sb")
    with pytest.raises(PkceError) as stolen:
        service.handle_callback("bob", bob.state, carol_params.code)
    assert stolen.value.code == "PKCE_VERIFIER_MISMATCH" and service.token_for("bob") is None


# ───────────── 051 + 057 CLI 边界 ─────────────

class Cluster:
    """两个节点共享授权方与密文存储，各有自己的磁盘和 CLI 进程。"""

    def __init__(self, tmp_path, nodes=("A", "B")):
        self.clock = FakeClock()
        self.server = MockAuthorizationServer(self.clock)
        self.store = InMemoryTokenStore(make_cipher("v1"), self.clock)
        self.nodes = {name: RghNode(tmp_path / name, MockPkceCli(self.server, self.clock), self.store) for name in nodes}

    def login(self, node: str, user: str, session: str, account: str) -> str:
        link = self.nodes[node].run(user, session, "login")
        assert link.status == "authorization_link"
        self.nodes[node].deliver_callback(self.server.approve(link.authorize_url, account))
        return self.nodes[node].capture_login(user, session).status


def test_cli_boundary_login_flow_isolation_and_file_permissions(tmp_path):
    cluster = Cluster(tmp_path)
    node = cluster.nodes["A"]
    assert node.run("alice", "s1", "whoami").status == "login_required"
    link = node.run("alice", "s1", "login")
    assert "code_challenge=" in link.authorize_url and "code_verifier" not in link.authorize_url
    lease = node.lease("alice", "s1")
    assert (lease.session_dir / "rgh_login_output.txt").exists()
    node.deliver_callback(cluster.server.approve(link.authorize_url, "vendor-alice"))
    assert node.capture_login("alice", "s1").account == "vendor-alice"
    assert not (lease.session_dir / "rgh_login_output.txt").exists()
    token_file = lease.home / ".hotel-cli" / "token.json"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600 and stat.S_IMODE(os.stat(lease.home).st_mode) == 0o700
    raw_token = json.loads(token_file.read_text())["access_token"]
    assert raw_token not in str(cluster.store._data) and lease.credential_ref == "user:rolling-go-hotel:alice"
    assert node.run("bob", "s9", "whoami").status == "login_required"  # bob 不会以 alice 的身份登录
    assert raw_token not in repr(node.run("alice", "s2", "whoami"))


def test_cli_boundary_negative_callbacks_and_unsafe_ids(tmp_path):
    cluster = Cluster(tmp_path)
    node = cluster.nodes["A"]
    link = node.run("alice", "s1", "login")
    params = cluster.server.approve(link.authorize_url, "vendor-alice")
    with pytest.raises(PkceError) as wrong_state:
        node.deliver_callback(params.model_copy(update={"state": "forged"}))
    assert wrong_state.value.code == "PKCE_STATE_UNKNOWN"
    cluster.clock.advance(61)
    with pytest.raises(PkceError) as expired_code:
        node.deliver_callback(params)
    assert expired_code.value.code == "PKCE_CODE_EXPIRED"
    with pytest.raises(PkceError) as repeated:  # state 已被第一次回调消耗
        node.deliver_callback(params)
    assert repeated.value.code == "PKCE_STATE_UNKNOWN"
    assert node.capture_login("alice", "s1").status == "login_required"
    for bad in ("../evil", "a/b", "", "x" * 65):
        with pytest.raises(RghWorkspaceError):
            node.lease(bad, "s1")


def test_cross_node_login_logout_and_expiry(tmp_path):
    cluster = Cluster(tmp_path)
    assert cluster.login("A", "alice", "s1", "vendor-alice") == "logged_in"
    assert cluster.nodes["B"].run("alice", "s2", "whoami").status == "logged_in"  # 节点 B 从共享存储恢复
    assert cluster.nodes["B"].run("alice", "s2", "logout").status == "logged_out"
    assert cluster.nodes["A"].run("alice", "s1", "whoami").status == "login_required"  # 本地残留被清掉
    assert not (cluster.nodes["A"].lease("alice", "s1").home / ".hotel-cli" / "token.json").exists()
    assert cluster.login("A", "alice", "s1", "vendor-alice") == "logged_in"
    cluster.clock.advance(3601)  # Token 过期（1 小时），存储仍有记录
    expired = cluster.nodes["B"].run("alice", "s3", "whoami")
    assert expired.status == "login_required" and "重新授权" in expired.message


def test_two_users_concurrently_never_share_token_or_files(tmp_path):
    cluster = Cluster(tmp_path, nodes=("A",))
    node, barrier = cluster.nodes["A"], Barrier(2)

    def worker(user: str):
        barrier.wait()
        link = node.run(user, f"s-{user}", "login")
        barrier.wait()
        node.deliver_callback(cluster.server.approve(link.authorize_url, f"vendor-{user}"))
        node.capture_login(user, f"s-{user}")
        return [node.run(user, f"s-{user}", "whoami").account for _ in range(20)]

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(worker, ("alice", "bob")))
    assert set(results[0]) == {"vendor-alice"} and set(results[1]) == {"vendor-bob"}
    files = {user: node.lease(user, "x").home / ".hotel-cli" / "token.json" for user in ("alice", "bob")}
    assert files["alice"] != files["bob"]
    assert json.loads(files["alice"].read_text())["account"] == "vendor-alice"
    assert json.loads(files["bob"].read_text())["account"] == "vendor-bob"
    assert cluster.store.get("alice") != cluster.store.get("bob")


# ───────────── 050 航班管家练习（当前未启用） ─────────────

def test_flight_conversions_valid_and_invalid():
    assert fm.normalize_flight_no(" ca1501 ") == "CA1501" and fm.cabin_code("商务舱") == "C" and fm.cabin_code("y") == "Y"
    assert str(fm.parse_price("¥1,280.5")) == "1280.50"
    assert fm.parse_flight_time("2026-05-15 08:30").hour == 8
    for call, code in (
        (lambda: fm.normalize_flight_no("1501CA"), "FLIGHT_INVALID_FLIGHT_NO"),
        (lambda: fm.parse_flight_time("明天上午"), "FLIGHT_INVALID_TIME"),
        (lambda: fm.cabin_code("豪华舱"), "FLIGHT_INVALID_CABIN"),
        (lambda: fm.parse_price("-5"), "FLIGHT_INVALID_PRICE"),
        (lambda: fm.parse_price("NaN"), "FLIGHT_INVALID_PRICE"),
        (lambda: fm.parse_offer({"flightNo": "CA1", "departureTime": "2026-05-15 10:00", "arrivalTime": "2026-05-15 09:00", "cabin": "Y", "price": "100"}), "FLIGHT_INVALID_TIME"),
    ):
        with pytest.raises(fm.FlightExerciseError) as error:
            call()
        assert error.value.code == code
    offer = fm.parse_offer({"flightNo": "mu5101", "departureTime": "2026-05-15T08:30", "arrivalTime": "2026-05-15 10:45", "cabin": "经济舱", "price": 980})
    assert (offer.flight_no, offer.cabin, str(offer.price)) == ("MU5101", "Y", "980.00")


def flight_setup():
    factory = create_memory_session_factory()
    service = make_service(factory=factory)
    wire: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wire.append(request)
        return httpx.Response(200, json={"result": {"structuredContent": {"code": 0}}})

    return service, fm.FlightKeyInjector(service), httpx.Client(transport=httpx.MockTransport(handler)), wire


def test_flight_key_injected_on_wire_but_not_in_model_context():
    service, injector, client, wire = flight_setup()
    service.save("alice", "flight-manager", "sk_per_ALICEFAKE0123456789")
    call = fm.build_search_call(fm.FlightSearchArgs(departure_city_code="bjs", arrival_city_code="sha", date="2026-05-15", cabin_preference="经济舱"))
    context = call.model_dump_json()
    assert fm.PLACEHOLDER in context and "ALICEFAKE" not in context
    with fm.user_scope("alice"):
        response = injector.send(call, client)
    assert wire[0].headers["Authorization"] == "Bearer sk_per_ALICEFAKE0123456789"
    assert json.loads(wire[0].content)["params"]["arguments"] == {
        "departureCityCode": "BJS", "arrivalCityCode": "SHA", "date": "2026-05-15", "adultNum": 1, "cabinPreference": "Y",
    }
    assert "ALICEFAKE" not in response.text and "ALICEFAKE" not in call.model_dump_json() and "ALICEFAKE" not in repr(call)


def test_flight_injection_refuses_without_user_key_or_wrong_target():
    service, injector, client, wire = flight_setup()
    call = fm.build_search_call(fm.FlightSearchArgs(departure_city_code="BJS", arrival_city_code="SHA", date="2026-05-15"))
    with pytest.raises(fm.FlightExerciseError) as no_scope:
        injector.send(call, client)
    assert no_scope.value.code == "FLIGHT_NO_USER_SCOPE"
    service.save("alice", "flight-manager", "sk_per_ALICEFAKE0123456789")
    with fm.user_scope("bob"), pytest.raises(ApiKeyError) as no_key:  # bob 没有 Key，不会用 alice 的
        injector.send(call, client)
    assert no_key.value.code == "API_KEY_NOT_CONFIGURED"
    with fm.user_scope("alice"):
        for bad in (call.model_copy(update={"url": "https://evil.test/mcp"}),
                    call.model_copy(update={"body": {"leak": fm.PLACEHOLDER}}),
                    call.model_copy(update={"url": "http://fly.huoli.com/mcp/flight_server"})):
            with pytest.raises(fm.FlightExerciseError):
                injector.send(bad, client)
    assert wire == []


def test_flight_contextvar_isolates_concurrent_users():
    service, injector, client, wire = flight_setup()
    service.save("alice", "flight-manager", "sk_per_ALICEFAKE0123456789")
    service.save("bob", "flight-manager", "sk_per_BOBFAKE9876543210")
    call = fm.build_search_call(fm.FlightSearchArgs(departure_city_code="BJS", arrival_city_code="SHA", date="2026-05-15"))

    async def one(user: str):
        with fm.user_scope(user):
            await asyncio.sleep(0)  # 让出控制权，另一个任务在此期间设置自己的用户
            await asyncio.sleep(0)
            return injector.send(call, client).request.headers["Authorization"]

    async def main():
        return await asyncio.gather(one("alice"), one("bob"))

    assert asyncio.run(main()) == ["Bearer sk_per_ALICEFAKE0123456789", "Bearer sk_per_BOBFAKE9876543210"]
    assert fm._current_user.get() is None
