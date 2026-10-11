import { requestJson } from './request';
import { utcIsoToEpoch } from './time';

export interface ApprovalForm {
  purpose?: string;
  destination?: string;
  departureCity?: string;
  departureDate?: string;
  returnDate?: string;
}

export interface AdminApproval {
  processInstanceId: string;
  userId: string;
  title: string;
  status: string;
  statusLabel: string;
  orderId: string;
  remark: string | null;
  submitTime: number | null;
  updateTime: number | null;
  form: ApprovalForm;
}

export type ApprovalStatusFilter = 'PENDING' | 'APPROVED' | 'REJECTED' | 'CANCELLED' | '';

interface RawApproval {
  process_instance_id: string;
  user_id: string;
  order_id: string;
  title: string;
  status: string;
  approval_form: Record<string, unknown>;
  remark: string | null;
  submit_time: string;
  update_time: string;
}

const STATUS_LABELS: Record<string, string> = {
  PENDING: '待审批',
  APPROVED: '已通过',
  REJECTED: '已拒绝',
  CANCELLED: '已撤销',
};

function formText(form: Record<string, unknown>, key: string): string | undefined {
  return typeof form[key] === 'string' ? form[key] : undefined;
}

/** 将 Python 的 snake_case、UTC ISO 时间转换为源页面使用的展示契约。 */
function toAdminApproval(raw: RawApproval): AdminApproval {
  const form = raw.approval_form ?? {};
  return {
    processInstanceId: raw.process_instance_id,
    userId: raw.user_id,
    title: raw.title,
    status: raw.status,
    statusLabel: STATUS_LABELS[raw.status] ?? raw.status,
    orderId: raw.order_id,
    remark: raw.remark,
    submitTime: utcIsoToEpoch(raw.submit_time),
    updateTime: utcIsoToEpoch(raw.update_time),
    form: {
      purpose: formText(form, 'purpose'),
      destination: formText(form, 'destination'),
      departureCity: formText(form, 'departure_city'),
      departureDate: formText(form, 'departure_date'),
      returnDate: formText(form, 'return_date'),
    },
  };
}

export async function fetchApprovals(status: ApprovalStatusFilter = ''): Promise<AdminApproval[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  const rows = await requestJson<RawApproval[]>(`/api/admin/approvals${query}`);
  return rows.map(toAdminApproval);
}

export async function decideApproval(
  processInstanceId: string,
  decision: 'agree' | 'refuse',
  remark: string,
): Promise<AdminApproval> {
  const row = await requestJson<RawApproval>(
    `/api/admin/approvals/${encodeURIComponent(processInstanceId)}/decision`,
    { method: 'POST', body: JSON.stringify({ decision, remark: remark.trim() || null }) },
  );
  return toAdminApproval(row);
}
