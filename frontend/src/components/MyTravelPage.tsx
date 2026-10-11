import { useCallback, useEffect, useState, type FormEvent } from 'react';
import {
  cancelBooking, cancelTravelOrder, checkTravelConflicts, deleteBooking,
  fetchMyBookings, fetchMyTravelOrders, fetchOrderApprovals,
  modifyTravelOrder, submitTravelOrder,
  type ApprovalRecord, type BookingRecord, type ConflictReport,
  type MyTravelBooking, type MyTravelOrder, type TravelDraft,
} from '../api/travel';
import { ApiError } from '../api/request';
import { utcIsoToEpoch } from '../api/time';
import { useAuthStore } from '../store/authStore';

const ORDER_STATUS_CLASS: Record<string, string> = {
  DRAFT: 'pending', SUBMITTED: 'pending', APPROVED: 'approved',
  REJECTED: 'rejected', COMPLETED: 'approved', CANCELLED: 'cancelled',
};
const APPROVAL_LABEL: Record<string, string> = {
  PENDING: '待审批', APPROVED: '已通过', REJECTED: '已拒绝', CANCELLED: '已撤销',
};
const BOOKING_LABEL: Record<string, string> = {
  CREATED: '已创建', PENDING_PAYMENT: '待支付', PAID: '已支付',
  CONFIRMED: '已确认', COMPLETED: '已完成', CANCELLED: '已取消',
  REFUNDED: '已退款', FAILED: '失败',
};
const BOOKING_TYPE_LABEL: Record<string, string> = {
  HOTEL: '酒店', FLIGHT: '机票', TRAIN: '火车',
};
const CANCELLABLE_BOOKING = new Set(['CREATED', 'PENDING_PAYMENT', 'PAID', 'CONFIRMED']);
const EMPTY_DRAFT: TravelDraft = {
  departure_city: '', destination: '', departure_date: '', return_date: '', purpose: '',
};

type EditorMode = 'create' | 'edit' | null;

function formatTime(value: number | null): string {
  return value == null ? '-' : new Date(value).toLocaleString('zh-CN');
}

function StatusBadge({ code, label }: { code: string | null; label?: string | null }) {
  if (!code) return null;
  return <span className={`admin-status admin-status-${ORDER_STATUS_CLASS[code] ?? 'pending'}`}>
    {label || code}
  </span>;
}

function OrderCard({ order, active, onClick }: {
  order: MyTravelOrder; active: boolean; onClick: () => void;
}) {
  return <button type="button" className={`travel-card${active ? ' active' : ''}`} onClick={onClick}>
    <div className="travel-card-head">
      <span className="travel-card-route">{order.departureCity} <span className="travel-card-arrow">→</span> {order.destination}</span>
      {order.international && <span className="travel-card-tag">国际</span>}
    </div>
    <div className="travel-card-dates">{order.departureDate} ~ {order.returnDate}</div>
    <div className="travel-card-purpose">事由：{order.purpose}</div>
    <div className="travel-card-meta">
      <StatusBadge code={order.status} label={order.statusLabel} />
      <StatusBadge code={order.approvalStatus} label={order.approvalStatusLabel ? `审批：${order.approvalStatusLabel}` : null} />
      {order.bookingCount > 0 && <span className="travel-card-count">预订 {order.bookingCount} 项</span>}
    </div>
    <div className="travel-card-time">创建：{formatTime(order.createdAt)}</div>
  </button>;
}

function BookingItem({ booking, busy, onCancel, onDelete }: {
  booking: MyTravelBooking; busy: boolean;
  onCancel: (id: string) => void; onDelete: (id: string) => void;
}) {
  const localHotelCancellation = booking.bizType === 'HOTEL' && CANCELLABLE_BOOKING.has(booking.status);
  return <div className="booking-item">
    <div className="booking-item-body">
      <div className="booking-item-head">
        <span className="booking-item-type">{booking.bizTypeLabel || BOOKING_TYPE_LABEL[booking.bizType] || booking.bizType}</span>
        <span className="booking-item-title">{booking.title || booking.externalOrderNo || booking.bookingId}</span>
        <StatusBadge code={booking.status} label={BOOKING_LABEL[booking.status] || booking.statusLabel} />
      </div>
      <div className="booking-item-meta">
        <span>平台：{booking.platform}</span>
        {booking.externalOrderNo && <span>单号：{booking.externalOrderNo}</span>}
        {booking.totalAmount && <span>金额：{booking.currency} {booking.totalAmount}</span>}
        {booking.bookedAt && <span>下单：{formatTime(booking.bookedAt)}</span>}
      </div>
    </div>
    <div className="booking-item-actions">
      {localHotelCancellation && <button type="button" className="booking-item-action-btn booking-item-action-btn--cancel"
        disabled={busy} onClick={() => onCancel(booking.bookingId)}>本地取消</button>}
      <button type="button" className="booking-item-action-btn booking-item-action-btn--delete"
        disabled={busy} onClick={() => onDelete(booking.bookingId)}>删除记录</button>
    </div>
  </div>;
}

function unlinkedBooking(record: BookingRecord): MyTravelBooking {
  return {
    bookingId: record.booking_id,
    bizType: record.biz_type,
    bizTypeLabel: BOOKING_TYPE_LABEL[record.biz_type] ?? record.biz_type,
    title: record.title,
    status: record.status,
    statusLabel: BOOKING_LABEL[record.status] ?? record.status,
    externalStatus: record.external_status,
    totalAmount: record.total_amount,
    currency: record.currency,
    platform: record.platform,
    externalOrderNo: record.external_order_no,
    paymentStatus: null,
    startTime: null,
    endTime: null,
    bookedAt: null,
    orderUrl: null,
  };
}

function OrderEditor({ mode, draft, busy, report, onChange, onCheck, onSubmit, onClose }: {
  mode: Exclude<EditorMode, null>;
  draft: TravelDraft;
  busy: boolean;
  report: ConflictReport | null;
  onChange: (field: keyof TravelDraft, value: string) => void;
  onCheck: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onClose: () => void;
}) {
  const fields: { key: keyof TravelDraft; label: string; type: string }[] = [
    { key: 'departure_city', label: '出发城市', type: 'text' },
    { key: 'destination', label: '目的地', type: 'text' },
    { key: 'departure_date', label: '出发日期', type: 'date' },
    { key: 'return_date', label: '返回日期', type: 'date' },
    { key: 'purpose', label: '出差事由', type: 'text' },
  ];
  return <form className="travel-editor" onSubmit={onSubmit}>
    <h2>{mode === 'create' ? '新建差旅申请' : '修改差旅申请'}</h2>
    <p className="travel-editor-hint">请先核对字段与冲突；提交后会产生一条本地待审批记录。</p>
    <div className="travel-editor-grid">
      {fields.map((field) => <label className="travel-field" key={field.key}>
        <span>{field.label}</span>
        <input type={field.type} value={draft[field.key]} required disabled={busy}
          maxLength={field.key === 'purpose' ? 512 : 128}
          onChange={(event) => onChange(field.key, event.target.value)} />
      </label>)}
    </div>
    {report && <div className="travel-conflict-report" role="status">
      <strong>冲突检查</strong>
      {report.conflicts.length === 0 && report.uncertain.length === 0 && <div>没有发现已知冲突。</div>}
      {report.conflicts.map((item) => <div key={item.order_id}>{item.severity} · {item.order_id}：{item.reason}</div>)}
      {report.uncertain.map((item) => <div key={item}>待核实：{item}</div>)}
      <small>冲突检查是提示，不会自动替你提交申请。</small>
    </div>}
    <div className="travel-actions">
      <button type="button" className="travel-action-btn" disabled={busy} onClick={onCheck}>检查行程冲突</button>
      <button type="submit" className="travel-action-btn primary" disabled={busy}>{busy ? '处理中...' : '核对并提交审批'}</button>
      <button type="button" className="travel-action-btn" disabled={busy} onClick={onClose}>返回详情</button>
    </div>
  </form>;
}

export default function MyTravelPage({ onOpenSidebar }: { onOpenSidebar: () => void }) {
  const clearAuth = useAuthStore((state) => state.clearAuth);
  const [orders, setOrders] = useState<MyTravelOrder[]>([]);
  const [unlinked, setUnlinked] = useState<MyTravelBooking[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [approvalRevision, setApprovalRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editor, setEditor] = useState<EditorMode>(null);
  const [draft, setDraft] = useState<TravelDraft>(EMPTY_DRAFT);
  const [conflict, setConflict] = useState<ConflictReport | null>(null);

  const handleError = useCallback((cause: unknown, fallback: string) => {
    if (cause instanceof ApiError && cause.status === 401) {
      clearAuth();
      return;
    }
    setError(cause instanceof Error ? cause.message : fallback);
  }, [clearAuth]);

  const load = useCallback(async (preferredId?: string) => {
    setLoading(true);
    setError('');
    try {
      const [data, bookings] = await Promise.all([fetchMyTravelOrders(), fetchMyBookings()]);
      setOrders(data);
      setUnlinked(bookings.filter((item) => !item.travel_order_id).map(unlinkedBooking));
      setSelectedId((previous) => {
        if (preferredId && data.some((item) => item.orderId === preferredId)) return preferredId;
        if (previous && data.some((item) => item.orderId === previous)) return previous;
        return data[0]?.orderId ?? null;
      });
      setApprovalRevision((revision) => revision + 1);
    } catch (cause: unknown) {
      handleError(cause, '加载差旅单失败');
    } finally {
      setLoading(false);
    }
  }, [handleError]);

  useEffect(() => {
    // 页面进入时加载当前用户的差旅单和预订摘要。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);
  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    fetchOrderApprovals(selectedId)
      .then((items) => { if (!cancelled) setApprovals(items); })
      .catch((cause: unknown) => { if (!cancelled) handleError(cause, '加载审批历史失败'); });
    return () => { cancelled = true; };
  }, [selectedId, approvalRevision, handleError]);

  const selected = orders.find((item) => item.orderId === selectedId);
  const startCreate = () => {
    setDraft(EMPTY_DRAFT);
    setConflict(null);
    setEditor('create');
    setNotice('');
  };
  const startEdit = () => {
    if (!selected) return;
    setDraft({
      departure_city: selected.departureCity, destination: selected.destination,
      departure_date: selected.departureDate, return_date: selected.returnDate,
      purpose: selected.purpose,
    });
    setConflict(null);
    setEditor('edit');
    setNotice('');
  };
  const changeDraft = (field: keyof TravelDraft, value: string) => {
    setDraft((previous) => ({ ...previous, [field]: value }));
    setConflict(null);
  };

  const checkConflicts = async () => {
    setBusy(true);
    setError('');
    try {
      setConflict(await checkTravelConflicts(draft,
        editor === 'edit' ? selected?.orderId : undefined));
    } catch (cause: unknown) {
      handleError(cause, '冲突检查失败');
    } finally {
      setBusy(false);
    }
  };

  const saveOrder = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized: TravelDraft = {
      ...draft,
      departure_city: draft.departure_city.trim(),
      destination: draft.destination.trim(),
      purpose: draft.purpose.trim(),
    };
    if (!normalized.departure_city || !normalized.destination || !normalized.purpose) {
      setError('请填写出发城市、目的地和事由');
      return;
    }
    if (normalized.return_date < normalized.departure_date) {
      setError('返回日期不能早于出发日期');
      return;
    }
    const changes: Partial<TravelDraft> = {};
    if (editor === 'edit' && selected) {
      const original: TravelDraft = {
        departure_city: selected.departureCity, destination: selected.destination,
        departure_date: selected.departureDate, return_date: selected.returnDate,
        purpose: selected.purpose,
      };
      for (const key of Object.keys(normalized) as (keyof TravelDraft)[]) {
        if (normalized[key] !== original[key]) changes[key] = normalized[key];
      }
      if (Object.keys(changes).length === 0) {
        setError('没有修改任何字段');
        return;
      }
    }
    const force = editor === 'edit' && selected?.status === 'APPROVED';
    const message = force
      ? '这张差旅单已审批通过。确认撤销旧审批并按修改内容重新提交吗？关联预订不会自动更改。'
      : `确认${editor === 'create' ? '创建' : '修改'}差旅申请并提交本地审批吗？`;
    if (!window.confirm(message)) return;
    setBusy(true);
    setError('');
    try {
      const result = editor === 'create'
        ? await submitTravelOrder(normalized, crypto.randomUUID())
        : selected && await modifyTravelOrder(selected.orderId, changes, crypto.randomUUID(), force);
      if (!result) return;
      setEditor(null);
      setNotice(editor === 'create' ? '差旅申请已提交本地审批' : '修改已提交，新审批正在等待处理');
      await load(result.order_id);
    } catch (cause: unknown) {
      handleError(cause, '保存差旅单失败');
    } finally {
      setBusy(false);
    }
  };

  const cancelOrder = async () => {
    if (!selected) return;
    const force = selected.status === 'APPROVED';
    if (!window.confirm(force
      ? '这张差旅单已审批通过。确认撤销审批并取消差旅单吗？关联预订不会自动取消。'
      : '确认取消这张差旅单及当前审批吗？关联预订不会自动取消。')) return;
    setBusy(true);
    setError('');
    try {
      await cancelTravelOrder(selected.orderId, crypto.randomUUID(), force);
      setNotice('差旅单已取消；请单独核对关联预订');
      await load(selected.orderId);
    } catch (cause: unknown) {
      handleError(cause, '取消差旅单失败');
    } finally {
      setBusy(false);
    }
  };

  const changeBooking = async (bookingId: string, action: 'cancel' | 'delete') => {
    const message = action === 'cancel'
      ? '只会将酒店预订标记为内部取消；请联系酒店确认外部订单。继续吗？'
      : '只删除这条本地预订记录，不取消任何供应商订单。继续吗？';
    if (!window.confirm(message)) return;
    setBusy(true);
    setError('');
    try {
      if (action === 'cancel') {
        const result = await cancelBooking(bookingId, crypto.randomUUID());
        setNotice(result.message);
      } else {
        await deleteBooking(bookingId);
        setNotice('本地预订记录已删除；外部订单状态未改变');
      }
      await load(selectedId ?? undefined);
    } catch (cause: unknown) {
      handleError(cause, '处理预订记录失败');
    } finally {
      setBusy(false);
    }
  };

  return <main className="admin-page travel-page">
    <div className="admin-header">
      <div className="admin-heading">
        <button className="mobile-menu-btn" type="button" onClick={onOpenSidebar} aria-label="打开会话菜单">☰</button>
        <div><h1 className="admin-title">我的差旅</h1>
          <p className="admin-subtitle">查看申请、审批和内部预订记录</p></div>
      </div>
      <div className="travel-header-actions">
        <button type="button" className="admin-refresh" onClick={() => void load()} disabled={loading || busy}>刷新</button>
        <button type="button" className="travel-action-btn primary" onClick={startCreate} disabled={busy}>新建申请</button>
      </div>
    </div>
    {error && <div className="admin-error" role="alert">{error}</div>}
    {notice && <div className="travel-notice" role="status">{notice}</div>}
    {loading ? <div className="admin-empty">加载中...</div> :
      orders.length === 0 && editor === null ?
        <div className="admin-empty">还没有差旅申请。点击“新建申请”可以提交第一张本地审批单。</div> :
        <div className="travel-layout">
          <div className="travel-list">
            {orders.map((order) => <OrderCard key={order.orderId} order={order}
              active={selectedId === order.orderId}
              onClick={() => { setSelectedId(order.orderId); setEditor(null); setConflict(null); }} />)}
            {orders.length === 0 && <div className="travel-detail-empty">暂无差旅单</div>}
          </div>
          <div className="travel-main">
            {editor ? <OrderEditor mode={editor} draft={draft} busy={busy} report={conflict}
              onChange={changeDraft} onCheck={() => void checkConflicts()}
              onSubmit={(event) => void saveOrder(event)} onClose={() => setEditor(null)} /> :
              selected ? <>
                <div className="travel-detail-head">
                  <div><div className="travel-detail-title">{selected.departureCity} → {selected.destination}</div>
                    <div className="travel-detail-sub">差旅单号：{selected.orderId}
                      <span className="travel-detail-sep">·</span>{selected.departureDate} ~ {selected.returnDate}</div></div>
                  <StatusBadge code={selected.status} label={selected.statusLabel} />
                </div>
                <div className="travel-detail-block">
                  <div className="travel-detail-label">出差事由</div>
                  <div className="travel-detail-value">{selected.purpose}</div>
                </div>
                <div className="travel-detail-block">
                  <div className="travel-detail-label">当前审批</div>
                  {selected.approvalId ? <div className="travel-detail-approval">
                    <StatusBadge code={selected.approvalStatus} label={selected.approvalStatusLabel} />
                    <span className="travel-detail-approval-id">{selected.approvalId}</span>
                    {selected.approvalRemark && <div className="travel-detail-approval-remark">备注：{selected.approvalRemark}</div>}
                  </div> : <div className="travel-detail-empty">暂无审批记录</div>}
                </div>
                {approvals.length > 0 && <div className="travel-detail-block">
                  <div className="travel-detail-label">审批历史（{approvals.length}）</div>
                  <div className="travel-approval-history">{approvals.map((item) => <div key={item.process_instance_id}>
                    <strong>{APPROVAL_LABEL[item.status] ?? item.status}</strong>
                    <span>{formatTime(utcIsoToEpoch(item.submit_time))}</span>
                    {item.remark && <span>备注：{item.remark}</span>}
                  </div>)}</div>
                </div>}
                <div className="travel-detail-block">
                  <div className="travel-detail-label">关联预订（{selected.bookingCount}）</div>
                  {selected.bookings.length ? <div className="booking-group-list">
                    {selected.bookings.map((booking) => <BookingItem key={booking.bookingId} booking={booking} busy={busy}
                      onCancel={(id) => void changeBooking(id, 'cancel')}
                      onDelete={(id) => void changeBooking(id, 'delete')} />)}
                  </div> : <div className="travel-detail-empty">暂无关联预订</div>}
                </div>
                <div className="travel-actions">
                  {selected.status !== 'CANCELLED' && selected.status !== 'COMPLETED' && <>
                    <button type="button" className="travel-action-btn" onClick={startEdit} disabled={busy}>修改申请</button>
                    <button type="button" className="travel-action-btn danger" onClick={() => void cancelOrder()} disabled={busy}>取消差旅单</button>
                  </>}
                </div>
              </> : <div className="admin-empty">请选择差旅单查看详情</div>}
          </div>
        </div>}
    {!loading && unlinked.length > 0 && <section className="travel-extra-bookings">
      <h2>未关联差旅单的预订记录（{unlinked.length}）</h2>
      <p>这些是内部记录；当前页面不提供供应商下单或机票、火车票取消。</p>
      <div className="booking-group-list">{unlinked.map((booking) => <BookingItem key={booking.bookingId}
        booking={booking} busy={busy}
        onCancel={(id) => void changeBooking(id, 'cancel')}
        onDelete={(id) => void changeBooking(id, 'delete')} />)}</div>
    </section>}
  </main>;
}
