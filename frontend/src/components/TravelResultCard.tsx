import type { TravelData, FlightItem, HotelItem, TrainItem } from '../types';

interface Props {
  data: TravelData;
}

const TYPE_LABELS: Record<string, string> = {
  flight: '✈️ 航班搜索结果',
  hotel: '🏨 酒店搜索结果',
  train: '🚄 火车票搜索结果',
  unknown: '🔍 搜索结果',
};

function formatTime(dateTime: string): string {
  // "2026-03-28 21:00:00" → "21:00"
  const parts = dateTime.split(' ');
  if (parts.length >= 2) {
    return parts[1].slice(0, 5);
  }
  return dateTime;
}

function formatDate(dateTime: string): string {
  // "2026-03-28 21:00:00" → "03-28"
  const parts = dateTime.split(' ');
  if (parts.length >= 1) {
    const dateParts = parts[0].split('-');
    if (dateParts.length === 3) {
      return `${dateParts[1]}-${dateParts[2]}`;
    }
  }
  return dateTime;
}

// ── 通用兜底卡片：当类型未知或字段不匹配时展示 ───────────────────────────
function GenericItemCard({ item }: { item: Record<string, unknown> }) {
  const entries = Object.entries(item).filter(
    ([, v]) => v !== null && v !== undefined && String(v).length > 0,
  );
  const jumpUrlRaw = (item.jumpUrl || item.detailUrl || item.bookingUrl) as string | undefined;
  const jumpUrl = jumpUrlRaw && typeof jumpUrlRaw === 'string' ? jumpUrlRaw : undefined;
  const title = String(item.name || item.title || item.scenicName || item.scenic_name || '');

  return (
    <div className="travel-item-card generic-card">
      {title && <div className="generic-card-title">{title}</div>}
      <div className="generic-card-body">
        {entries.slice(0, 8).map(([key, value]) => (
          <div key={key} className="generic-card-row">
            <span className="generic-card-key">{key}:</span>
            <span className="generic-card-value">{String(value)}</span>
          </div>
        ))}
      </div>
      {jumpUrl && (
        <div className="generic-card-footer">
          <a href={String(jumpUrl)} target="_blank" rel="noopener noreferrer" className="travel-link">
            查看详情
          </a>
        </div>
      )}
    </div>
  );
}

// ── 航班卡片 ─────────────────────────────────────────────────────────────
function FlightCard({ item }: { item: FlightItem }) {
  const journey = item.journeys?.[0];
  const seg = journey?.segments?.[0];
  if (!seg) return <GenericItemCard item={item as unknown as Record<string, unknown>} />;

  return (
    <div className="travel-item-card flight-card">
      <div className="flight-card-header">
        <span className="flight-airline">{seg.marketingTransportName || ''}</span>
        <span className="flight-no">{seg.marketingTransportNo}</span>
        <span className="flight-class">{seg.seatClassName}</span>
        {journey.journeyType && <span className="flight-type-badge">{journey.journeyType}</span>}
      </div>
      <div className="flight-card-body">
        <div className="flight-endpoint">
          <div className="flight-time">{formatTime(seg.depDateTime)}</div>
          <div className="flight-station">{seg.depStationShortName || seg.depStationName}</div>
          {seg.depTerm && <div className="flight-term">{seg.depTerm}</div>}
          <div className="flight-date">{formatDate(seg.depDateTime)}</div>
        </div>
        <div className="flight-middle">
          <div className="flight-duration">{seg.duration || journey.totalDuration}</div>
          <div className="flight-line">
            <div className="flight-line-bar" />
            <svg viewBox="0 0 12 12" width="10" height="10" className="flight-plane-icon">
              <path d="M6 1L2 5h3v4h2V5h3L6 1z" fill="currentColor" />
            </svg>
          </div>
          <div className="flight-cities">{seg.depCityName} → {seg.arrCityName}</div>
        </div>
        <div className="flight-endpoint">
          <div className="flight-time">{formatTime(seg.arrDateTime)}</div>
          <div className="flight-station">{seg.arrStationShortName || seg.arrStationName}</div>
          {seg.arrTerm && <div className="flight-term">{seg.arrTerm}</div>}
          <div className="flight-date">{formatDate(seg.arrDateTime)}</div>
        </div>
      </div>
      <div className="flight-card-footer">
        <span className="travel-price">{item.adultPrice}</span>
        {item.jumpUrl && (
          <a href={item.jumpUrl} target="_blank" rel="noopener noreferrer" className="travel-link">
            查看详情
          </a>
        )}
      </div>
    </div>
  );
}

// ── 酒店卡片 ─────────────────────────────────────────────────────────────
function HotelCard({ item }: { item: HotelItem }) {
  return (
    <div className="travel-item-card hotel-card">
      <div className="hotel-card-content">
        {item.mainPic && (
          <div className="hotel-image">
            <img src={item.mainPic} alt={item.name} loading="lazy" />
          </div>
        )}
        <div className="hotel-info">
          <div className="hotel-name-row">
            <span className="hotel-name">{item.name}</span>
            {item.star && <span className="hotel-star">{item.star}</span>}
            {item.brandName && <span className="hotel-brand">{item.brandName}</span>}
          </div>
          {item.score && (
            <div className="hotel-score-row">
              <span className="hotel-score">{item.score}</span>
              {item.scoreDesc && <span className="hotel-score-desc">{item.scoreDesc}</span>}
            </div>
          )}
          {item.address && <div className="hotel-address">📍 {item.address}</div>}
          {item.interestsPoi && <div className="hotel-poi">{item.interestsPoi}</div>}
          {item.review && <div className="hotel-review">"{item.review}"</div>}
        </div>
      </div>
      <div className="hotel-card-footer">
        <span className="travel-price">{item.price}<span className="price-unit">/晚</span></span>
        {item.detailUrl && (
          <a href={item.detailUrl} target="_blank" rel="noopener noreferrer" className="travel-link">
            查看详情
          </a>
        )}
      </div>
    </div>
  );
}

// ── 火车票卡片 ───────────────────────────────────────────────────────────
function TrainCard({ item }: { item: TrainItem }) {
  const journey = item.journeys?.[0];
  const seg = journey?.segments?.[0];
  if (!seg) return <GenericItemCard item={item as unknown as Record<string, unknown>} />;

  return (
    <div className="travel-item-card train-card">
      <div className="train-card-header">
        <span className="train-no">{seg.marketingTransportNo}</span>
        <span className="train-class">{seg.seatClassName}</span>
        {journey.journeyType && <span className="train-type-badge">{journey.journeyType}</span>}
      </div>
      <div className="train-card-body">
        <div className="train-endpoint">
          <div className="train-time">{formatTime(seg.depDateTime)}</div>
          <div className="train-station">{seg.depStationName}</div>
          <div className="train-date">{formatDate(seg.depDateTime)}</div>
        </div>
        <div className="train-middle">
          <div className="train-duration">{seg.duration || journey.totalDuration}</div>
          <div className="train-line">
            <div className="train-line-bar" />
            <div className="train-line-dot" />
          </div>
          <div className="train-cities">{seg.depCityName} → {seg.arrCityName}</div>
        </div>
        <div className="train-endpoint">
          <div className="train-time">{formatTime(seg.arrDateTime)}</div>
          <div className="train-station">{seg.arrStationName}</div>
          <div className="train-date">{formatDate(seg.arrDateTime)}</div>
        </div>
      </div>
      <div className="train-card-footer">
        <span className="travel-price">{item.adultPrice}</span>
        {item.jumpUrl && (
          <a href={item.jumpUrl} target="_blank" rel="noopener noreferrer" className="travel-link">
            查看详情
          </a>
        )}
      </div>
    </div>
  );
}

// ── 主组件 ───────────────────────────────────────────────────────────────
export default function TravelResultCard({ data }: Props) {
  const label = TYPE_LABELS[data.type] || '搜索结果';

  return (
    <div className="travel-result-card">
      <div className="travel-result-header">
        <span className="travel-result-title">{label}</span>
        <span className="travel-result-count">{data.items.length} 条结果</span>
      </div>
      <div className="travel-result-list">
        {data.type === 'flight' &&
          (data.items as FlightItem[]).map((item, i) => <FlightCard key={i} item={item} />)}
        {data.type === 'hotel' &&
          (data.items as HotelItem[]).map((item, i) => <HotelCard key={i} item={item} />)}
        {data.type === 'train' &&
          (data.items as TrainItem[]).map((item, i) => <TrainCard key={i} item={item} />)}
        {(data.type === 'unknown' || !['flight', 'hotel', 'train'].includes(data.type)) &&
          (data.items as Record<string, unknown>[]).map((item, i) => <GenericItemCard key={i} item={item} />)}
      </div>
    </div>
  );
}
