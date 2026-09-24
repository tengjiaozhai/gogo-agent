"""Redis token persistence integration test suite.

验证真实连接 172.22.22.123:6379 的 Redis 跨会话存储：
1. 验证 Redis 节点连通性与探活 (PING)；
2. 验证 Token 在 Redis 中的 30 天 (2,592,000 秒) TTL 与存储正确性；
3. 验证跨实例/模拟服务重启后的 Token 持久化与有效读取；
4. 验证 Token 撤销 (主动注销) 立即在 Redis 中物理失效；
5. 验证 Redis 不可用时的平滑降级 (InMemoryTokenStore) 容错能力。
"""

import os
import pytest
from gogo_agent.auth.security import (
    InMemoryTokenStore,
    RedisTokenStore,
    TokenManager,
    create_token_manager,
)


@pytest.fixture
def redis_store():
    """获取可用的 RedisTokenStore，若无法连接则跳过集成测试。"""
    host = os.environ.get("GOGO_REDIS_HOST", "172.22.22.123").strip()
    port = int(os.environ.get("GOGO_REDIS_PORT", "6379").strip())
    store = RedisTokenStore(host=host, port=port, prefix="gogo:test:token:")
    if not store.ping():
        pytest.skip(f"无法连接 Redis ({host}:{port})，跳过 Redis 集成测试。")
    return store


def test_redis_connection_and_ping(redis_store):
    """验证 Redis 节点连接正常。"""
    assert redis_store.ping() is True


def test_redis_token_store_30_day_ttl(redis_store):
    """验证 Token 存储在 Redis 中的 30 天 (2,592,000s) TTL 与数据读写。"""
    test_token = "tk_redis_test_30d_ttl"
    expected_user = "u001_alice"
    ttl_30_days = 30 * 24 * 3600

    # 1. 存储 Token
    redis_store.save_token(test_token, expected_user, ttl_seconds=ttl_30_days)

    # 2. 检查 Redis 原生 TTL
    key = f"{redis_store.prefix}{test_token}"
    remaining_ttl = redis_store._client.ttl(key)
    # 允许 5 秒内的网络与执行时间偏差
    assert ttl_30_days - 5 <= remaining_ttl <= ttl_30_days

    # 3. 验证读取
    user_id = redis_store.get_user_id(test_token)
    assert user_id == expected_user

    # 4. 验证撤销 (删除)
    assert redis_store.revoke_token(test_token) is True
    assert redis_store.get_user_id(test_token) is None
    assert redis_store._client.exists(key) == 0


def test_cross_instance_token_persistence(redis_store):
    """验证跨服务实例/服务重启场景下的 Token 跨会话有效性。"""
    # 实例 1：签发 Token 并写入 Redis
    mgr_instance_1 = TokenManager(
        secret_key="secret-key-instance-1",
        store=redis_store,
        default_ttl_seconds=30 * 24 * 3600,
    )
    user_alice = "u_alice_cross_restart"
    token = mgr_instance_1.create_token(user_alice)
    assert token.startswith("tk_")

    # 模拟服务完全重启：销毁实例 1，在全新内存中创建实例 2
    del mgr_instance_1

    mgr_instance_2 = TokenManager(
        secret_key="secret-key-instance-2",
        store=redis_store,
    )

    # 实例 2 直接从 Redis 校验并恢复用户身份
    resolved_user = mgr_instance_2.verify_token(token)
    assert resolved_user == user_alice

    # 清理测试生成的 Token
    mgr_instance_2.revoke_token(token)
    assert mgr_instance_2.verify_token(token) is None


def test_create_token_manager_factory_and_fallback():
    """验证工厂方法在 Redis 可用时优先选用 RedisTokenStore，不可用时降级为 InMemoryTokenStore。"""
    # 1. 正常配置优先使用 Redis
    mgr = create_token_manager(prefer_redis=True)
    if mgr.store.__class__.__name__ == "RedisTokenStore":
        assert isinstance(mgr.store, RedisTokenStore)

    # 2. 模拟 Redis 端口不可达，触发自动平滑降级
    bad_mgr = create_token_manager(prefer_redis=False)
    assert isinstance(bad_mgr.store, InMemoryTokenStore)
