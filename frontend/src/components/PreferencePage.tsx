import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  deletePreference,
  fetchPreferenceOptions,
  fetchSavedPreferences,
  savePreference,
  type PreferenceCategory,
  type PreferenceChoice,
} from '../api/preference';
import { ApiError } from '../api/request';
import { useAuthStore } from '../store/authStore';

type Draft = Record<string, PreferenceChoice>;

/** 判断一项是否有有效选择：单选为非空字符串，多选为非空数组 */
function hasChoice(choice: PreferenceChoice | undefined): boolean {
  if (Array.isArray(choice)) return choice.length > 0;
  return typeof choice === 'string' && choice !== '';
}

/** 后端保存的多选值是 JSON 数组字符串，这里还原为数组以便页面编辑 */
function parseSaved(value: string, multi: boolean): PreferenceChoice {
  if (!multi) return value;
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : [];
  } catch {
    return [];
  }
}

export default function PreferencePage({ onOpenSidebar }: { onOpenSidebar?: () => void }) {
  const { clearAuth } = useAuthStore();
  const [categories, setCategories] = useState<PreferenceCategory[]>([]);
  const [saved, setSaved] = useState<Draft>({});
  const [draft, setDraft] = useState<Draft>({});
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [failed, setFailed] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const multiKeys = useMemo(() => {
    const set = new Set<string>();
    for (const cat of categories) for (const item of cat.items) if (item.type === 'multi') set.add(item.key);
    return set;
  }, [categories]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [opts, rows] = await Promise.all([fetchPreferenceOptions(), fetchSavedPreferences()]);
      setCategories(opts);
      const multi = new Set(opts.flatMap((c) => c.items.filter((i) => i.type === 'multi').map((i) => i.key)));
      const next: Draft = {};
      for (const row of rows) next[row.key] = parseSaved(row.value, multi.has(row.key));
      setSaved(next);
      setDraft(next);
      setFailed({});
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearAuth();
        return;
      }
      setError('加载偏好设置失败');
    } finally {
      setLoading(false);
    }
  }, [clearAuth]);

  useEffect(() => {
    // 异步启动加载：不在 effect 体内同步 setState（react-hooks/set-state-in-effect）。
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const toggleSingle = (key: string, option: string) => {
    setDraft((prev) => ({ ...prev, [key]: prev[key] === option ? '' : option }));
  };

  const toggleMulti = (key: string, option: string) => {
    setDraft((prev) => {
      const current = Array.isArray(prev[key]) ? (prev[key] as string[]) : [];
      const next = current.includes(option) ? current.filter((v) => v !== option) : [...current, option];
      return { ...prev, [key]: next };
    });
  };

  /** 逐项保存：有改动的项单独提交，失败项保留在页面上并可重试。成功项立即生效。 */
  const saveOne = async (key: string) => {
    const choice = draft[key];
    setPending((prev) => new Set(prev).add(key));
    setFailed((prev) => Object.fromEntries(Object.entries(prev).filter(([k]) => k !== key)));
    try {
      if (hasChoice(choice)) {
        await savePreference(key, choice as PreferenceChoice, '偏好设置页显式保存');
      } else {
        await deletePreference(key);
      }
      setSaved((prev) => ({ ...prev, [key]: choice ?? '' }));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearAuth();
        return;
      }
      const message = err instanceof ApiError ? err.message : '保存失败，请重试';
      setFailed((prev) => ({ ...prev, [key]: message }));
    } finally {
      setPending((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
  };

  const dirtyKeys = useMemo(() => {
    const keys = new Set([...Object.keys(saved), ...Object.keys(draft)]);
    return [...keys].filter((key) => {
      const a = saved[key];
      const b = draft[key];
      return JSON.stringify(a ?? (multiKeys.has(key) ? [] : '')) !== JSON.stringify(b ?? (multiKeys.has(key) ? [] : ''));
    });
  }, [saved, draft, multiKeys]);

  const saveAll = async () => {
    for (const key of dirtyKeys) await saveOne(key);
  };

  if (loading) {
    return (
      <div className="admin-page pref-page">
        <div className="admin-empty">加载中...</div>
      </div>
    );
  }

  return (
    <div className="admin-page pref-page">
      <div className="admin-header">
        {onOpenSidebar && (
          <button className="mobile-menu-btn" onClick={onOpenSidebar} aria-label="打开菜单">☰</button>
        )}
        <div>
          <h1 className="admin-title">差旅偏好设置</h1>
          <p className="admin-subtitle">
            设置您的差旅偏好，智能助手将根据您的偏好推荐最合适的方案
          </p>
        </div>
        <button className="pref-save-btn" onClick={saveAll} disabled={pending.size > 0 || dirtyKeys.length === 0}>
          {pending.size > 0 ? '保存中...' : dirtyKeys.length === 0 ? '已保存' : `保存偏好（${dirtyKeys.length} 项）`}
        </button>
      </div>

      {error && <div className="admin-error">{error}</div>}
      {Object.keys(failed).length > 0 && (
        <div className="admin-error">
          以下项保存失败，可再次点击保存重试：{Object.keys(failed).join('、')}
        </div>
      )}

      <div className="pref-grid">
        {categories.map((cat) => (
          <div key={cat.category} className="pref-category">
            <div className="pref-category-header">
              <span className="pref-category-icon">{cat.icon}</span>
              <span className="pref-category-label">{cat.label}</span>
            </div>
            <div className="pref-items">
              {cat.items.map((item) => {
                const current = draft[item.key];
                return (
                  <div key={item.key} className="pref-item">
                    <div className="pref-item-label">
                      {item.label}
                      {pending.has(item.key) && <span> · 保存中</span>}
                      {failed[item.key] && <span className="pref-item-error"> · {failed[item.key]}</span>}
                    </div>
                    <div className="pref-item-options">
                      {item.options.map((opt) => {
                        const isSelected = item.type === 'single'
                          ? current === opt
                          : Array.isArray(current) && current.includes(opt);
                        return (
                          <button
                            key={opt}
                            type="button"
                            className={`pref-option${isSelected ? ' selected' : ''}${item.type === 'multi' ? ' multi' : ''}`}
                            disabled={pending.has(item.key)}
                            onClick={() => (item.type === 'single' ? toggleSingle(item.key, opt) : toggleMulti(item.key, opt))}
                          >
                            {item.type === 'multi' && (
                              <span className="pref-option-check">{isSelected ? '✓' : ''}</span>
                            )}
                            {opt}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
