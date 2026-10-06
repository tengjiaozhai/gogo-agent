/** 默认同源请求；开发时 Vite 将 /api 转发到 Python 后端。 */
const apiBase = (import.meta.env.VITE_API_BASE ?? '').trim().replace(/\/$/, '');

export function getApiBase(): string {
  return apiBase;
}
