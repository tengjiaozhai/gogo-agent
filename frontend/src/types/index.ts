export type TimelineItem =
  | { kind: 'thinking'; agentName: string; text: string }
  | {
      kind: 'tool';
      id: string;
      agentName: string;
      title: string;
      status: 'pending' | 'done' | 'in-progress';
      result?: string;
      /** 工具调用的入参 JSON 字符串 */
      arguments?: string;
    }
  | { kind: 'plan'; tasks: PlanTask[] }
  | { kind: 'travel'; data: TravelData }
  | { kind: 'booking'; data: BookingResult }
  | { kind: 'plan_html'; data: PlanHtmlData };

export interface Message {
  id: string;
  role: 'user' | 'agent' | 'system';
  content: string;
  agentName?: string;
  timestamp: number;
  /** 快照的该轮进度树，用于历史对话中持久展示 */
  progress?: PlanProgress;
  /** 快照的该轮思考过程（每个 Agent 有多轮） */
  thinkingByAgent?: Record<string, string[]>;
  /** 快照的 ItineraryPlanAgent 任务列表 */
  planTasks?: PlanTask[];
  /** 快照的旅行搜索结果卡片 */
  travelData?: TravelData[];
  /** 按时间顺序快照的思考/工具/计划/结果时间轴 */
  timeline?: TimelineItem[];
  /** 用户对当前模型回答的反馈：LIKE 点赞 / DISLIKE 点踩 / undefined 未反馈 */
  feedback?: 'LIKE' | 'DISLIKE' | null;
  /** 反馈时间戳（毫秒） */
  feedbackAt?: number;
}

export interface PlanProgress {
  percent: number;
  steps: PlanStep[];
}

export interface PlanStep {
  id: string;
  title: string;
  status: 'pending' | 'done' | 'in-progress';
  /** 该步骤所属的 Agent 名称（tool 步骤需要此字段来归组） */
  agentName?: string;
  /** 工具执行结果文本（可展开查看） */
  result?: string;
  /** 工具调用的入参 JSON 字符串 */
  arguments?: string;
}

/** ItineraryPlanAgent plan-and-execute 规划的单个子任务 */
export interface PlanTask {
  idx: number;
  name: string;
  state: 'todo' | 'in_progress' | 'done' | 'abandoned';
}

// ── 旅行搜索结果类型 ───────────────────────────────────────────────────

export interface FlightSegment {
  depCityName: string;
  depStationName: string;
  depStationShortName?: string;
  depTerm?: string;
  depDateTime: string;
  depWeekAbbrName?: string;
  arrCityName: string;
  arrStationName: string;
  arrStationShortName?: string;
  arrTerm?: string;
  arrDateTime: string;
  arrWeekAbbrName?: string;
  duration: string;
  transportType: string;
  marketingTransportName?: string;
  marketingTransportNo: string;
  seatClassName: string;
}

export interface FlightItem {
  adultPrice: string;
  journeys: Array<{
    journeyType: string;
    segments: FlightSegment[];
    totalDuration: string;
  }>;
  jumpUrl?: string;
  totalDuration?: string;
}

export interface HotelItem {
  name: string;
  address: string;
  brandName?: string;
  price: string;
  score?: string;
  scoreDesc?: string;
  star?: string;
  review?: string;
  mainPic?: string;
  interestsPoi?: string;
  detailUrl?: string;
}

export interface TrainSegment {
  depCityName: string;
  depStationName: string;
  depDateTime: string;
  arrCityName: string;
  arrStationName: string;
  arrDateTime: string;
  duration: string;
  transportType: string;
  marketingTransportNo: string;
  seatClassName: string;
}

export interface TrainItem {
  adultPrice: string;
  journeys: Array<{
    journeyType: string;
    segments: TrainSegment[];
    totalDuration: string;
  }>;
  jumpUrl?: string;
}

export interface TravelData {
  type: 'flight' | 'hotel' | 'train' | 'unknown';
  items: FlightItem[] | HotelItem[] | TrainItem[] | Record<string, unknown>[];
}

/** 预订下单成功结果，用于渲染带“立即支付”按钮的支付卡片 */
export interface BookingResult {
  type: 'booking_result';
  bizType: 'flight' | 'hotel' | 'train' | 'unknown';
  bizTypeLabel: string;
  orderId: string;
  title?: string;
  amount?: string | null;
  currency?: string | null;
  payUrl?: string | null;
  statusLabel?: string;
}

/** 行程方案 HTML，用于在 iframe 中渲染完整方案页面 */
export interface PlanHtmlData {
  type: 'plan_html';
  html: string;
  title?: string;
}
