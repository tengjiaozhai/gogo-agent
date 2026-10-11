import { useCallback, useEffect, useState } from 'react';
import {
  fetchApprovals,
  decideApproval,
  type AdminApproval,
  type ApprovalForm,
  type ApprovalStatusFilter,
} from '../api/admin';
import { ApiError } from '../api/request';
import { useAuthStore } from '../store/authStore';

const STATUS_TABS: { label: string; value: ApprovalStatusFilter }[] = [
  { label: '待审批', value: 'PENDING' },
  { label: '已通过', value: 'APPROVED' },
  { label: '已拒绝', value: 'REJECTED' },
  { label: '全部', value: '' },
];

const STATUS_CLASS: Record<string, string> = {
  PENDING: 'pending',
  APPROVED: 'approved',
  REJECTED: 'rejected',
  CANCELLED: 'cancelled',
};

function formatTime(ts: number | null): string {
  if (!ts) return '-';
  return new Date(ts).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function FormField({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <div className="admin-form-field">
      <span className="admin-form-label">{label}</span>
      <span className="admin-form-value">{value}</span>
    </div>
  );
}

function ApprovalCard({
  item,
  onDecide,
  busy,
}: {
  item: AdminApproval;
  onDecide: (id: string, decision: 'agree' | 'refuse', remark: string) => void;
  busy: boolean;
}) {
  const [remark, setRemark] = useState('');
  const form: ApprovalForm = item.form;
  const isPending = item.status === 'PENDING';

  return (
    <div className="admin-card">
      <div className="admin-card-head">
        <div className="admin-card-title">{item.title || '（无标题）'}</div>
        <span className={`admin-status admin-status-${STATUS_CLASS[item.status] || 'pending'}`}>
          {item.statusLabel || item.status}
        </span>
      </div>

      <div className="admin-card-meta">
        <span>申请人：{item.userId}</span>
        <span>提交：{formatTime(item.submitTime)}</span>
        {item.orderId && <span>差旅单：{item.orderId}</span>}
      </div>

      {form && (
        <div className="admin-form-grid">
          <FormField label="出差事由" value={form.purpose} />
          <FormField label="出发城市" value={form.departureCity} />
          <FormField label="目的地" value={form.destination} />
          <FormField label="出发日期" value={form.departureDate} />
          <FormField label="返回日期" value={form.returnDate} />
        </div>
      )}

      {item.remark && (
        <div className="admin-card-remark">审批备注：{item.remark}</div>
      )}

      {isPending && (
        <div className="admin-card-actions">
          <input
            className="admin-remark-input"
            placeholder="审批备注（可选）"
            value={remark}
            onChange={(e) => setRemark(e.target.value)}
            disabled={busy}
            maxLength={512}
          />
          <button
            className="admin-btn admin-btn-approve"
            disabled={busy}
            onClick={() => onDecide(item.processInstanceId, 'agree', remark)}
          >
            通过
          </button>
          <button
            className="admin-btn admin-btn-reject"
            disabled={busy}
            onClick={() => onDecide(item.processInstanceId, 'refuse', remark)}
          >
            拒绝
          </button>
        </div>
      )}
    </div>
  );
}

export default function AdminApprovalPage({ onOpenSidebar }: { onOpenSidebar: () => void }) {
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const [status, setStatus] = useState<ApprovalStatusFilter>('PENDING');
  const [list, setList] = useState<AdminApproval[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(
    async (s: ApprovalStatusFilter) => {
      setLoading(true);
      setError('');
      try {
        const data = await fetchApprovals(s);
        setList(data);
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 401) {
          clearAuth();
          return;
        }
        setError(err instanceof ApiError && err.status === 403
          ? '无权访问后台管理'
          : err instanceof Error ? err.message : '加载审批单失败');
        setList([]);
      } finally {
        setLoading(false);
      }
    },
    [clearAuth],
  );

  useEffect(() => {
    // 页面进入或筛选切换时同步远端列表；load 内部统一处理 loading/error 状态。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load(status);
  }, [status, load]);

  const handleDecide = async (id: string, decision: 'agree' | 'refuse', remark: string) => {
    setBusyId(id);
    setError('');
    try {
      await decideApproval(id, decision, remark);
      await load(status);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 401) {
        clearAuth();
        return;
      }
      setError(err instanceof Error ? err.message : '操作失败');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <main className="admin-page">
      <div className="admin-header">
        <div className="admin-heading">
          <button className="mobile-menu-btn" type="button" onClick={onOpenSidebar} aria-label="打开会话菜单">☰</button>
          <div>
          <h1 className="admin-title">审批管理</h1>
          <p className="admin-subtitle">查看并处理差旅审批单</p>
          </div>
        </div>
        <button className="admin-refresh" onClick={() => load(status)} disabled={loading}>
          刷新
        </button>
      </div>

      <div className="admin-tabs">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab.value || 'all'}
            className={`admin-tab${status === tab.value ? ' active' : ''}`}
            onClick={() => setStatus(tab.value)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {error && <div className="admin-error">{error}</div>}

      <div className="admin-list">
        {loading ? (
          <div className="admin-empty">加载中...</div>
        ) : list.length === 0 ? (
          <div className="admin-empty">暂无审批单</div>
        ) : (
          list.map((item) => (
            <ApprovalCard
              key={item.processInstanceId}
              item={item}
              onDecide={handleDecide}
              busy={busyId === item.processInstanceId}
            />
          ))
        )}
      </div>
    </main>
  );
}
