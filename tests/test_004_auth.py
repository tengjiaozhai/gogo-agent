"""004 登录体系测试集。

验收标准：
1. 两用户分别登录并访问自己的信息；
2. 无 token 与伪造 token 失败；
3. 用户 A 不能用用户 B 的 ID 读取数据（身份唯一信源为服务端校验的 Token）；
4. 前端现有 Authorization 头格式契约记录（支持原值 token 与 Bearer 格式）；
5. 账号密码错误与不存在拦截；
6. 退出登录后 Token 立即失效；
7. 旧账号密码平滑透明迁移至安全哈希并在隔离数据上验证。
"""

import pytest
from fastapi.testclient import TestClient

from gogo_agent.api import app
from gogo_agent.auth.dependencies import _default_auth_service, get_auth_service
from gogo_agent.auth.repository import UserAccountRepository
from gogo_agent.auth.security import PasswordManager, TokenManager
from gogo_agent.auth.service import AuthService


@pytest.fixture(autouse=True)
def fresh_auth_service(monkeypatch):
    """每个测试用例使用完全隔离的独立仓储与 Token 管理器。"""
    repo = UserAccountRepository(load_default_seeds=True)
    token_mgr = TokenManager(secret_key="test-secret-key-004")
    service = AuthService(repository=repo, token_manager=token_mgr)

    # 替换全局依赖与默认实例
    monkeypatch.setattr("gogo_agent.auth.dependencies._default_auth_service", service)
    app.dependency_overrides[get_auth_service] = lambda: service
    yield service
    app.dependency_overrides.clear()


def test_login_success_and_contract():
    """验证登录接口契约严格对齐原前端规范 (token 与 tokenName)。"""
    client = TestClient(app)
    resp = client.post("/api/auth/login", json={"username": "alice", "password": "123456"})
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data and len(data["token"]) > 20
    assert data["tokenName"] == "Authorization"


def test_two_users_isolated_login_and_info():
    """验证两用户分别登录并访问各自的信息，身份互不干扰。"""
    client = TestClient(app)

    # 1. Alice 登录
    resp_alice = client.post("/api/auth/login", json={"username": "alice", "password": "123456"})
    token_alice = resp_alice.json()["token"]

    # 2. Bob 登录
    resp_bob = client.post("/api/auth/login", json={"username": "bob", "password": "123456"})
    token_bob = resp_bob.json()["token"]

    # 验证 Token 互不相同
    assert token_alice != token_bob

    # 3. 使用 Alice 的 token 访问 /info
    info_alice = client.get("/api/auth/info", headers={"Authorization": token_alice}).json()
    assert info_alice["userId"] == "u001"
    assert info_alice["admin"] is False
    assert info_alice["realName"] == "张三"

    # 4. 使用 Bob 的 token 访问 /info
    info_bob = client.get("/api/auth/info", headers={"Authorization": token_bob}).json()
    assert info_bob["userId"] == "u002"
    assert info_bob["admin"] is False
    assert info_bob["realName"] == "李四"

    # 5. 管理员登录验证
    resp_admin = client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
    token_admin = resp_admin.json()["token"]
    info_admin = client.get("/api/auth/info", headers={"Authorization": token_admin}).json()
    assert info_admin["userId"] == "u_001"
    assert info_admin["admin"] is True
    assert info_admin["realName"] == "系统管理员"


def test_missing_or_fake_token_rejected():
    """验证无 token、伪造 token、篡改 token 均被拦截为 401。"""
    client = TestClient(app)

    # 1. 无 Authorization 头
    resp_none = client.get("/api/auth/info")
    assert resp_none.status_code == 401
    assert "缺少 Authorization" in resp_none.json()["message"]

    # 2. 伪造的不存在 token
    resp_fake = client.get("/api/auth/info", headers={"Authorization": "fake_token_abc_123"})
    assert resp_fake.status_code == 401
    assert resp_fake.json()["code"] == 401
    assert "未登录或登录态已失效" in resp_fake.json()["message"]

    # 3. 空空白 token
    resp_empty = client.get("/api/auth/info", headers={"Authorization": "   "})
    assert resp_empty.status_code == 401


def test_wrong_credentials_rejected():
    """验证错误密码或不存在的账号返回 400。"""
    client = TestClient(app)

    # 密码错误
    resp_bad_pwd = client.post("/api/auth/login", json={"username": "alice", "password": "wrong_password"})
    assert resp_bad_pwd.status_code == 400
    assert resp_bad_pwd.json()["code"] == 400
    assert "用户名或密码错误" in resp_bad_pwd.json()["message"]

    # 账号不存在
    resp_non_exist = client.post("/api/auth/login", json={"username": "non_exist_user", "password": "123456"})
    assert resp_non_exist.status_code == 400
    assert resp_non_exist.json()["code"] == 400
    assert "用户名或密码错误" in resp_non_exist.json()["message"]


def test_logout_revokes_token_immediately():
    """验证登出后当前 token 立即失效，无法继续访问受保护接口。"""
    client = TestClient(app)

    # 登录
    login_resp = client.post("/api/auth/login", json={"username": "alice", "password": "123456"})
    token = login_resp.json()["token"]

    # 登录态正常访问
    resp_before = client.get("/api/auth/info", headers={"Authorization": token})
    assert resp_before.status_code == 200

    # 执行退出登录
    logout_resp = client.post("/api/auth/logout", headers={"Authorization": token})
    assert logout_resp.status_code == 200
    assert logout_resp.json()["message"] == "退出成功"

    # 退出后再访问 /info 立即返回 401
    resp_after = client.get("/api/auth/info", headers={"Authorization": token})
    assert resp_after.status_code == 401


def test_authorization_header_formats(fresh_auth_service):
    """验证支持原前端格式 (Authorization: <token>) 与标准 Bearer 格式。"""
    client = TestClient(app)
    token = fresh_auth_service.login("alice", "123456")

    # 1. 原前端格式: Authorization: <token>
    res1 = client.get("/api/auth/info", headers={"Authorization": token})
    assert res1.status_code == 200
    assert res1.json()["userId"] == "u001"

    # 2. 标准 Bearer 格式: Authorization: Bearer <token>
    res2 = client.get("/api/auth/info", headers={"Authorization": f"Bearer {token}"})
    assert res2.status_code == 200
    assert res2.json()["userId"] == "u001"


def test_transparent_password_hash_migration(fresh_auth_service):
    """验证旧系统明文密码在用户首次登录时透明自动升级为 PBKDF2 哈希并写回仓储。"""
    repo = fresh_auth_service.repository

    # 检查初始状态: charlie 密码为明文
    user_before = repo.find_by_username("charlie")
    assert user_before.password_hash == "123456"

    # 首次登录
    token = fresh_auth_service.login("charlie", "123456")
    assert token is not None

    # 登录后检查: 仓储中密码已被透明升级为安全哈希
    user_after = repo.find_by_username("charlie")
    assert user_after.password_hash.startswith("pbkdf2:sha256:100000:")
    assert "123456" not in user_after.password_hash

    # 再次使用原密码登录，哈希比对成功
    is_valid, needs_upgrade = PasswordManager.verify_password("123456", user_after.password_hash)
    assert is_valid is True
    assert needs_upgrade is False

    # 再次登录成功
    token2 = fresh_auth_service.login("charlie", "123456")
    assert token2 is not None


def test_token_expiration(fresh_auth_service):
    """验证过期 Token 访问被拒绝。"""
    client = TestClient(app)
    # 生成一个已过期的 Token (TTL = -1s)
    expired_token = fresh_auth_service.token_manager.create_token("u001", ttl_seconds=-1)

    resp = client.get("/api/auth/info", headers={"Authorization": expired_token})
    assert resp.status_code == 401
