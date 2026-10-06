import { getApiBase } from './config';

export interface LoginResult {
  token: string;
  tokenName: string;
}

/** 登录，返回 Token */
export async function login(username: string, password: string): Promise<LoginResult> {
  const res = await fetch(`${getApiBase()}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const data = await res.json();
  if (!res.ok || data.code === 400 || data.code === 401) {
    throw new Error(data.message || data.detail || '登录失败');
  }
  return data as LoginResult;
}

/** 退出登录 */
export async function logout(token: string): Promise<void> {
  await fetch(`${getApiBase()}/api/auth/logout`, {
    method: 'POST',
    headers: { Authorization: token },
  }).catch(() => {
    // 忽略退出接口网络错误，本地状态照常清除
  });
}

/** 获取当前用户信息（可用于验证 token 有效性） */
export async function fetchUserInfo(token: string): Promise<{ userId: string; admin?: boolean }> {
  const res = await fetch(`${getApiBase()}/api/auth/info`, {
    headers: { Authorization: token },
  });
  if (res.status === 401) throw new Error('UNAUTHORIZED');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
