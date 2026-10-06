import { useState, type FormEvent } from 'react';
import { login, fetchUserInfo } from '../api/auth';
import { useAuthStore } from '../store/authStore';

export default function LoginPage() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const setAuth = useAuthStore((s) => s.setAuth);

  const handleLogin = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!username.trim() || !password) {
      setError('请输入用户名和密码');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const result = await login(username.trim(), password);
      // 登录成功后查询用户信息获取 userId 与管理员标识
      const info = await fetchUserInfo(result.token);
      setAuth(result.token, info.userId, username.trim(), info.admin === true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '登录失败，请重试');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-card">
        {/* Brand */}
        <div className="login-brand">
          <span className="login-brand-icon">✈</span>
          <div>
            <div className="login-brand-name">GoGo 差旅</div>
            <div className="login-brand-sub">企业智能差旅助手</div>
          </div>
        </div>

        <h2 className="login-title">登录账号</h2>
        <p className="login-desc">登录后即可开始与 AI 助手对话</p>

        <form className="login-form" onSubmit={handleLogin}>
          <div className="login-field">
            <label className="login-label">用户名</label>
            <input
              className="login-input"
              type="text"
              placeholder="请输入用户名"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              autoComplete="username"
              disabled={loading}
            />
          </div>

          <div className="login-field">
            <label className="login-label">密码</label>
            <input
              className="login-input"
              type="password"
              placeholder="请输入密码"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              disabled={loading}
            />
          </div>

          {error && <div className="login-error">{error}</div>}

          <button
            className="login-btn"
            type="submit"
            disabled={loading || !username.trim() || !password.trim()}
          >
            {loading ? (
              <span className="login-btn-loading">
                <span className="login-spinner" />
                登录中...
              </span>
            ) : (
              '登 录'
            )}
          </button>
        </form>

      </div>
    </div>
  );
}
