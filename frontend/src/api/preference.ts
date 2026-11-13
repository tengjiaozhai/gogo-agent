import { useAuthStore } from '../store/authStore';
import { getApiBase } from './config';
import { ApiError, requestJson } from './request';

/** 单个偏好项定义（与 Python /api/preferences/options 一致） */
export interface PreferenceItem {
  key: string;
  label: string;
  type: 'single' | 'multi';
  options: string[];
}

/** 偏好分类 */
export interface PreferenceCategory {
  category: string;
  label: string;
  icon: string;
  items: PreferenceItem[];
}

/** 本人已保存的一项偏好（与 Python PreferenceView 一致） */
export interface SavedPreference {
  key: string;
  value: string;
  source_message: string;
  updated_at: string;
}

/** 单选为字符串；多选为选项数组，存储为 JSON 数组字符串 */
export type PreferenceChoice = string | string[];

/** 获取选项表。 */
export function fetchPreferenceOptions(): Promise<PreferenceCategory[]> {
  return requestJson<PreferenceCategory[]>('/api/preferences/options');
}

/** 获取本人已保存的偏好。 */
export function fetchSavedPreferences(): Promise<SavedPreference[]> {
  return requestJson<SavedPreference[]>('/api/preferences');
}

/** 保存或替换一项。多选值先序列化为 JSON 数组字符串，与后端校验一致。 */
export function savePreference(key: string, choice: PreferenceChoice, sourceMessage: string): Promise<SavedPreference> {
  const value = Array.isArray(choice) ? JSON.stringify(choice) : choice;
  return requestJson<SavedPreference>(`/api/preferences/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: JSON.stringify({ value, source_message: sourceMessage }),
  });
}

/** 撤回一项（物理删除）。 */
export async function deletePreference(key: string): Promise<void> {
  // 204 无响应体，不能走 requestJson（它会解析 JSON）；只检查状态码。
  const token = useAuthStore.getState().token;
  const response = await fetch(`${getApiBase()}/api/preferences/${encodeURIComponent(key)}`, {
    method: 'DELETE',
    headers: token ? { Authorization: token } : {},
  });
  if (response.status === 401) throw new ApiError(401, '登录已失效，请重新登录');
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const message = body && typeof body === 'object' && 'message' in body && typeof body.message === 'string'
      ? body.message : `HTTP ${response.status}`;
    throw new ApiError(response.status, message);
  }
}
