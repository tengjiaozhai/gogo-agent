import { useAuthStore } from '../store/authStore';
import { getApiBase } from './config';

export class ApiError extends Error {
  public readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 阶段 D 页面共用的已认证 JSON 请求；用户身份只由 Token 决定。 */
export async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = useAuthStore.getState().token;
  if (token) headers.set('Authorization', token);
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');

  const response = await fetch(getApiBase() + path, { ...init, headers });
  if (!response.ok) {
    if (response.status === 401) throw new ApiError(401, '登录已失效，请重新登录');
    const body: unknown = await response.json().catch(() => null);
    const message = body && typeof body === 'object' && 'message' in body &&
      typeof body.message === 'string' ? body.message : `HTTP ${response.status}`;
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}
