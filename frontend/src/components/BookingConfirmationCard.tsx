import { useEffect, useState } from 'react';
import type { BookingConfirmation } from '../types';

export default function BookingConfirmationCard({ data, disabled, processed, onConfirm }: {
  data: BookingConfirmation;
  disabled: boolean;
  processed: boolean;
  onConfirm: (data: BookingConfirmation) => void;
}) {
  const expiresAt = data?.expires_at ?? 0;
  const [expired, setExpired] = useState(() => expiresAt * 1000 <= Date.now());
  useEffect(() => {
    const timer = setTimeout(() => setExpired(true), Math.max(0, expiresAt * 1000 - Date.now()));
    return () => clearTimeout(timer);
  }, [expiresAt]);
  const complete = data && [data.confirmation_id, data.token, data.route, data.title, data.start_time,
    data.end_time, data.amount, data.currency].every((value) => typeof value === 'string' && value.trim().length > 0);
  if (!complete || !Number.isFinite(expiresAt) || expiresAt <= 0 || data.simulated !== true) {
    return <section className="booking-card"><p className="booking-notice" role="alert">确认摘要信息不完整，请重新发起预订。</p></section>;
  }
  return (
    <section className="booking-card booking-confirmation" aria-label="待确认的模拟预订">
      <div className="booking-card-header">
        <div className="booking-card-heading">
          <span className="booking-card-type">核对预订项目</span>
          <span className="booking-card-title">{data.title}</span>
        </div>
        <span className="booking-card-status">模拟供应商</span>
      </div>
      <div className="booking-card-body">
        <div className="booking-card-row"><span className="booking-card-label">行程</span><span>{data.route}</span></div>
        <div className="booking-card-row"><span className="booking-card-label">时间</span><span>{data.start_time.replace('T', ' ')} 至 {data.end_time.replace('T', ' ')}</span></div>
        <div className="booking-card-row"><span className="booking-card-label">方案</span><span>{data.proposal_tag}</span></div>
        <div className="booking-card-row"><span className="booking-card-label">确认金额</span><strong className="booking-card-amount">{data.amount} {data.currency}</strong></div>
        <p className="booking-notice">仅预订本卡片所列项目。使用模拟供应商，不会实际出票、订房或扣款。</p>
      </div>
      <div className="booking-card-footer">
        {processed ? <span role="status">本项已处理，请查看预订结果</span> : expired ? <span role="status">确认已过期，请在聊天中重新发起预订</span> : (
          <button type="button" className="booking-card-pay-btn" disabled={disabled} onClick={() => onConfirm(data)}>
            {disabled ? '等待本轮完成' : '确认模拟预订'}
          </button>
        )}
      </div>
    </section>
  );
}
