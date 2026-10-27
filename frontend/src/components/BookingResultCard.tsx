import type { BookingResult } from '../types';

// 业务类型对应的图标（机票/酒店/火车票）
function BizIcon({ bizType }: { bizType: BookingResult['bizType'] }) {
  if (bizType === 'flight') {
    return (
      <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
        <path
          d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5L21 16z"
          fill="currentColor"
        />
      </svg>
    );
  }
  if (bizType === 'hotel') {
    return (
      <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
        <path
          d="M3 20V5h2v7h9V8h5a2 2 0 0 1 2 2v10h-2v-3H5v3H3zm5-8a2.5 2.5 0 1 1 0-5 2.5 2.5 0 0 1 0 5z"
          fill="currentColor"
        />
      </svg>
    );
  }
  if (bizType === 'train') {
    return (
      <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
        <path
          d="M12 2c-4 0-8 .5-8 4v9.5A3.5 3.5 0 0 0 7.5 19L6 20.5V21h12v-.5L16.5 19a3.5 3.5 0 0 0 3.5-3.5V6c0-3.5-4-4-8-4zM7.5 17A1.5 1.5 0 1 1 9 15.5 1.5 1.5 0 0 1 7.5 17zm9 0a1.5 1.5 0 1 1 1.5-1.5 1.5 1.5 0 0 1-1.5 1.5zM18 11H6V6h12v5z"
          fill="currentColor"
        />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
      <path d="M4 4h16v4H4V4zm0 6h16v10H4V10z" fill="currentColor" />
    </svg>
  );
}

/**
 * 按服务端三态显示模拟预订事实和查询标识，不提供真实支付入口。
 */
export default function BookingResultCard({ data, onCheck, busy }: { data: BookingResult; onCheck?: (bookingId: string) => void; busy?: boolean }) {
  if (!data || !['CONFIRMED', 'FAILED', 'UNKNOWN'].includes(data.resolution)
    || typeof data.startTime !== 'string' || typeof data.endTime !== 'string') {
    return <section className="booking-card"><p className="booking-notice" role="alert">历史预订结果格式不完整，请到“我的差旅”查询本人记录。</p></section>;
  }
  const bookingId = data.bookingId;
  const confirmed = data.resolution === 'CONFIRMED';
  const heading = confirmed ? '模拟预订已确认' : data.resolution === 'UNKNOWN' ? '预订结果待核实' : '预订未完成';
  const currencySymbol = data.currency === 'USD' ? '$' : '¥';

  return (
    <section className={`booking-card booking-result-${data.resolution.toLowerCase()}`} aria-label={heading}>
      <div className="booking-card-header">
        <span className="booking-card-icon">
          <BizIcon bizType={data.bizType} />
        </span>
        <div className="booking-card-heading">
          <span className="booking-card-type">{heading}</span>
          {data.title && <span className="booking-card-title">{data.title}</span>}
        </div>
        <span className="booking-card-status">{data.statusLabel}</span>
      </div>

      <div className="booking-card-body">
        <div className="booking-card-row"><span className="booking-card-label">行程</span><span>{data.route}</span></div>
        <div className="booking-card-row"><span className="booking-card-label">时间</span><span>{data.startTime.replace('T', ' ')} 至 {data.endTime.replace('T', ' ')}</span></div>
        <div className="booking-card-row">
          <span className="booking-card-label">{confirmed ? '模拟订单号' : '查询标识'}</span>
          <span className="booking-card-value order-no">{data.orderId}</span>
        </div>
        {data.amount && (
          <div className="booking-card-row">
            <span className="booking-card-label">应付金额</span>
            <span className="booking-card-amount">
              {currencySymbol}
              {data.amount}
            </span>
          </div>
        )}
      </div>

      <div className="booking-card-footer">
        <p className="booking-notice" role="status">{data.detail}<br />模拟供应商，不实际出票、订房或扣款。{data.resolution === 'UNKNOWN' && '请保留查询标识，不要换方案重复下单。'}</p>
        {data.resolution === 'UNKNOWN' && bookingId && onCheck && (
          <button type="button" className="booking-card-pay-btn" disabled={busy} onClick={() => onCheck(bookingId)}>核实原预订</button>
        )}
      </div>
    </section>
  );
}
