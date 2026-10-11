import { requestJson } from './request';

/** 与 Java「我的差旅」列表一致的服务端聚合视图。 */
export interface MyTravelBooking {
  bookingId: string;
  bizType: string;
  bizTypeLabel: string;
  title: string | null;
  status: string;
  statusLabel: string;
  externalStatus: string | null;
  totalAmount: string | null;
  currency: string;
  platform: string;
  externalOrderNo: string | null;
  paymentStatus: string | null;
  startTime: number | null;
  endTime: number | null;
  bookedAt: number | null;
  orderUrl: string | null;
}

export interface MyTravelOrder {
  orderId: string;
  destination: string;
  departureCity: string;
  departureDate: string;
  returnDate: string;
  purpose: string;
  status: string;
  statusLabel: string;
  createdAt: number;
  updatedAt: number;
  international: boolean;
  approvalId: string | null;
  approvalStatus: string | null;
  approvalStatusLabel: string | null;
  approvalRemark: string | null;
  approvalSubmitTime: number | null;
  approvalUpdateTime: number | null;
  bookingCount: number;
  bookings: MyTravelBooking[];
  planHtmlUrl: string | null;
}

export interface TravelDraft {
  destination: string;
  departure_city: string;
  departure_date: string;
  return_date: string;
  purpose: string;
}

export interface OrderWriteResult extends TravelDraft {
  order_id: string;
  user_id: string;
  status: string;
  approval_id: string | null;
}

export interface ConflictReport {
  conflicts: { order_id: string; severity: 'LOW' | 'MEDIUM' | 'HIGH'; reason: string }[];
  uncertain: string[];
}

export interface BookingRecord {
  booking_id: string;
  user_id: string;
  travel_order_id: string | null;
  biz_type: 'HOTEL' | 'FLIGHT' | 'TRAIN';
  platform: string;
  external_order_no: string | null;
  status: string;
  external_status: string | null;
  title: string | null;
  total_amount: string | null;
  currency: string;
}

export interface ApprovalRecord {
  process_instance_id: string;
  status: string;
  remark: string | null;
  submit_time: string;
  update_time: string;
}

export interface BookingCancelResult {
  cancelled: boolean;
  supplier_cancelled: boolean;
  message: string;
}

export const fetchMyTravelOrders = () =>
  requestJson<MyTravelOrder[]>('/api/my-travel/orders');

export const fetchMyBookings = () =>
  requestJson<BookingRecord[]>('/api/my-travel/bookings');

export const fetchOrderApprovals = (orderId: string) =>
  requestJson<ApprovalRecord[]>(`/api/my-travel/orders/${encodeURIComponent(orderId)}/approvals`);

export const submitTravelOrder = (draft: TravelDraft, idempotencyKey: string) =>
  requestJson<OrderWriteResult>('/api/my-travel/orders', {
    method: 'POST',
    body: JSON.stringify({ ...draft, confirmed: true, idempotency_key: idempotencyKey }),
  });

export const modifyTravelOrder = (
  orderId: string, changes: Partial<TravelDraft>, idempotencyKey: string, force: boolean,
) => requestJson<OrderWriteResult>(`/api/my-travel/orders/${encodeURIComponent(orderId)}`, {
  method: 'PUT',
  body: JSON.stringify({ confirmed: true, idempotency_key: idempotencyKey, force, changes }),
});

export const cancelTravelOrder = (orderId: string, idempotencyKey: string, force: boolean) =>
  requestJson<{ cancelled: boolean; approval_cancelled: boolean }>(
    `/api/my-travel/orders/${encodeURIComponent(orderId)}/cancel`,
    { method: 'POST', body: JSON.stringify({ confirmed: true, idempotency_key: idempotencyKey, force }) },
  );

export const checkTravelConflicts = (draft: TravelDraft, excludeOrderId?: string) => {
  const query = excludeOrderId ? `?exclude_order_id=${encodeURIComponent(excludeOrderId)}` : '';
  return requestJson<ConflictReport>(`/api/my-travel/conflicts${query}`, {
    method: 'POST', body: JSON.stringify(draft),
  });
};

export const deleteBooking = (bookingId: string) =>
  requestJson<{ deleted: boolean }>(`/api/my-travel/bookings/${encodeURIComponent(bookingId)}`, {
    method: 'DELETE',
  });

export const cancelBooking = (bookingId: string, idempotencyKey: string) =>
  requestJson<BookingCancelResult>(`/api/my-travel/bookings/${encodeURIComponent(bookingId)}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ confirmed: true, idempotency_key: idempotencyKey }),
  });
