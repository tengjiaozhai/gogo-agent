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
 * 预订下单成功卡片：展示订单号、标题、金额，并提供“立即支付”按钮跳转支付链接。
 * 支付链接缺失时按钮不可用，仅展示订单信息。
 */
export default function BookingResultCard({ data }: { data: BookingResult }) {
  const hasPayUrl = !!(data.payUrl && data.payUrl.trim());
  const currencySymbol = data.currency === 'USD' ? '$' : '¥';

  return (
    <div className="booking-card">
      <div className="booking-card-header">
        <span className="booking-card-icon">
          <BizIcon bizType={data.bizType} />
        </span>
        <div className="booking-card-heading">
          <span className="booking-card-type">{data.bizTypeLabel || '预订'}下单成功</span>
          {data.title && <span className="booking-card-title">{data.title}</span>}
        </div>
        <span className="booking-card-status">{data.statusLabel || '待支付'}</span>
      </div>

      <div className="booking-card-body">
        <div className="booking-card-row">
          <span className="booking-card-label">订单号</span>
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
        {hasPayUrl ? (
          <a
            className="booking-card-pay-btn"
            href={data.payUrl!}
            target="_blank"
            rel="noopener noreferrer"
          >
            立即支付
          </a>
        ) : (
          <span className="booking-card-pay-btn disabled">支付链接暂不可用</span>
        )}
      </div>
    </div>
  );
}
