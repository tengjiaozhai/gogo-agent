import { useState, useEffect, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { PlanTaskList } from './ProgressCard';
import TravelResultCard from './TravelResultCard';
import BookingResultCard from './BookingResultCard';
import type { Message, TimelineItem, PlanHtmlData } from '../types';
import { isPersistedMessageId } from '../store/chatStore';

// ── Agent display names ───────────────────────────────────────────────────
const AGENT_LABELS: Record<string, string> = {
  MasterAgent: 'GoGo助手',
  QueryRewritingAgent: '查询改写智能体',
  IntentRecognitionAgent: '意图识别智能体',
  ItineraryPlanAgent: '行程规划智能体',
  ItineraryManageAgent: '行程管理智能体',
  ItineraryReviewAgent: '行程审核智能体',
  BookingAgent: '预订执行智能体',
  ReimbursementAgent: '报销处理智能体',
  InfoAgent: '信息查询智能体',
};

function agentDisplayName(name?: string) {
  if (!name) return 'GoGo差旅助手';
  return AGENT_LABELS[name] || name;
}

/** 将工具参数 JSON 字符串格式化为可读文本 */
function formatToolArgs(raw: string): string {
  try {
    const obj = JSON.parse(raw);
    return JSON.stringify(obj, null, 2);
  } catch {
    return raw;
  }
}

// ── Timeline 分组 ─────────────────────────────────────────────────────────
type ToolItem = Extract<TimelineItem, { kind: 'tool' }>;

type TimelineUnit =
  | { type: 'tool-group'; items: ToolItem[] }
  | { type: 'single'; item: TimelineItem; idx: number };

/** 前端仅展示这几类搜索结果卡片，其余（如未知类型）不渲染 */
const TRAVEL_ALLOWED = new Set(['flight', 'hotel', 'train']);

/** 将连续且标题相同的工具调用合并为一组，其余条目原样保留 */
function buildTimelineUnits(timeline: TimelineItem[]): TimelineUnit[] {
  const units: TimelineUnit[] = [];
  timeline.forEach((item, idx) => {
    if (item.kind === 'tool') {
      const prev = units[units.length - 1];
      if (
        prev &&
        prev.type === 'tool-group' &&
        prev.items[prev.items.length - 1].title === item.title
      ) {
        prev.items.push(item);
        return;
      }
      units.push({ type: 'tool-group', items: [item] });
      return;
    }
    units.push({ type: 'single', item, idx });
  });
  return units;
}

// ── Status icons ──────────────────────────────────────────────────────────
function StatusIcon({ status }: { status: TimelineItem & { kind: 'tool' } extends { status: infer S } ? S : never }) {
  if (status === 'done') {
    return (
      <span className="timeline-status done">
        <svg viewBox="0 0 16 16" fill="none" width="12" height="12">
          <circle cx="8" cy="8" r="7" stroke="#52c41a" strokeWidth="1.5" />
          <path d="M5 8l2 2 3-4" stroke="#52c41a" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
    );
  }
  if (status === 'in-progress') {
    return <span className="timeline-status spinning" />;
  }
  return (
    <span className="timeline-status pending">
      <svg viewBox="0 0 16 16" fill="none" width="12" height="12">
        <circle cx="8" cy="8" r="6.5" stroke="#d9d9d9" strokeWidth="1.5" />
      </svg>
    </span>
  );
}

// ── Thinking block ────────────────────────────────────────────────────────
function getPreview(text: string): string {
  const lines = text.split('\n').map((l) => l.trim()).filter(Boolean);
  return lines[lines.length - 1] ?? '';
}

function ThinkingBlock({
  agentName,
  text,
  active = false,
}: {
  agentName: string;
  text: string;
  /** 是否为当前正在流式输出的最新条目：为 true 时自动展开，后续内容出现后自动折叠 */
  active?: boolean;
}) {
  const [expanded, setExpanded] = useState(active);
  const prevActive = useRef(active);
  // 仅在 active 状态发生切换时自动同步展开/折叠，中间过程允许用户手动切换
  useEffect(() => {
    if (prevActive.current !== active) {
      setExpanded(active);
      prevActive.current = active;
    }
  }, [active]);
  const preview = getPreview(text);
  if (!text.trim()) return null;
  return (
    <div className="timeline-thinking">
      <div className="timeline-thinking-header" onClick={() => setExpanded((v) => !v)}>
        <span className="timeline-thinking-icon">
          <svg viewBox="0 0 16 16" fill="none" width="13" height="13">
            <path
              d="M8 2C5.24 2 3 4.02 3 6.5c0 .92.3 1.77.8 2.46L3 13l2.5-1.25c.75.33 1.6.5 2.5.5 2.76 0 5-2.02 5-4.5S10.76 2 8 2z"
              fill="currentColor"
              opacity="0.65"
            />
          </svg>
        </span>
        <span className="timeline-thinking-label">深度思考</span>
        <span className="timeline-thinking-agent">· {agentDisplayName(agentName)}</span>
        <span className="timeline-thinking-preview">{preview || '思考中...'}</span>
        <span className={`timeline-thinking-chevron${expanded ? '' : ' collapsed'}`}>›</span>
      </div>
      {expanded && <div className="timeline-thinking-body">{text}</div>}
    </div>
  );
}

// ── Tool block ────────────────────────────────────────────────────────────
function ToolBlock({ items }: { items: ToolItem[] }) {
  const [expanded, setExpanded] = useState(false);
  const count = items.length;
  const anyInProgress = items.some((i) => i.status === 'in-progress');
  const allDone = items.every((i) => i.status === 'done');
  const status: ToolItem['status'] = anyInProgress
    ? 'in-progress'
    : allDone
    ? 'done'
    : 'pending';
  const hasResult = items.some((i) => !!(i.result && i.result.trim()));
  const hasArgs = items.some((i) => !!(i.arguments && i.arguments.trim()));
  const hasDetail = hasResult || hasArgs;
  const clickable = hasDetail && !anyInProgress;
  const title = items[0].title;

  return (
    <div className="timeline-tool">
      <div
        className={`timeline-tool-header ${status}${clickable ? ' clickable' : ''}`}
        onClick={clickable ? () => setExpanded((v) => !v) : undefined}
      >
        <StatusIcon status={status} />
        <span className="timeline-tool-title">{title}</span>
        {clickable && (
          <span className="timeline-tool-toggle">
            {expanded ? '收起' : `查看 ${count} 个步骤`}
            <span className={`timeline-tool-chevron${expanded ? '' : ' collapsed'}`}>›</span>
          </span>
        )}
      </div>
      {expanded && hasDetail && (
        <div className="timeline-tool-result">
          {count === 1 ? (
            <>
              {items[0].arguments && items[0].arguments.trim() && (
                <div className="tool-arguments-section">
                  <div className="tool-section-label">参数</div>
                  <pre>{formatToolArgs(items[0].arguments)}</pre>
                </div>
              )}
              {items[0].result && items[0].result.trim() && (
                <div className="tool-result-section">
                  <div className="tool-section-label">结果</div>
                  <pre>{items[0].result}</pre>
                </div>
              )}
            </>
          ) : (
            items.map((it, i) => (
              <div key={it.id} className="timeline-tool-step">
                <div className="timeline-tool-step-title">{`${i + 1}. ${it.title}`}</div>
                {it.arguments && it.arguments.trim() && (
                  <div className="tool-arguments-section">
                    <div className="tool-section-label">参数</div>
                    <pre>{formatToolArgs(it.arguments)}</pre>
                  </div>
                )}
                {it.result && it.result.trim() ? (
                  <div className="tool-result-section">
                    <div className="tool-section-label">结果</div>
                    <pre>{it.result}</pre>
                  </div>
                ) : (
                  <div className="timeline-tool-step-empty">无输出</div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ── Plan block ────────────────────────────────────────────────────────────
function PlanBlock({ tasks }: { tasks: { idx: number; name: string; state: 'todo' | 'in_progress' | 'done' | 'abandoned' }[] }) {
  if (!tasks.length) return null;
  return (
    <div className="timeline-plan">
      <PlanTaskList tasks={tasks} />
    </div>
  );
}

// ── Plan HTML block ──────────────────────────────────────────────────────
function PlanHtmlBlock({ data }: { data: PlanHtmlData }) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [expanded, setExpanded] = useState(true);

  const handleLoad = () => {
    try {
      const iframe = iframeRef.current;
      if (iframe?.contentWindow?.document) {
        const body = iframe.contentWindow.document.body;
        const html = iframe.contentWindow.document.documentElement;
        const height = Math.max(body.scrollHeight, html.scrollHeight);
        if (height > 0) {
          iframe.style.height = `${height + 24}px`;
        }
      }
    } catch {
      // cross-origin or other error, keep default height
    }
  };

  return (
    <div className="timeline-plan-html">
      <div className="timeline-plan-html-header" onClick={() => setExpanded((v) => !v)}>
        <span className="timeline-plan-html-icon">📋</span>
        <span className="timeline-plan-html-title">{data.title || '差旅行程方案'}</span>
        <span className={`timeline-plan-html-chevron${expanded ? '' : ' collapsed'}`}>›</span>
      </div>
      {expanded && (
        <iframe
          ref={iframeRef}
          srcDoc={data.html}
          title={data.title || '差旅行程方案'}
          onLoad={handleLoad}
          className="plan-html-iframe"
          style={{ width: '100%', minHeight: '400px', border: 'none', borderRadius: '8px' }}
        />
      )}
    </div>
  );
}

// ── Markdown content ──────────────────────────────────────────────────────
function MarkdownContent({ content }: { content: string }) {
  if (!content.trim()) return null;
  return (
    <div className="msg-agent-bubble markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children, ...props }) => (
            <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
              {children}
            </a>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────
interface Props {
  msg: Message;
  liveTimeline?: TimelineItem[];
  isStreaming?: boolean;
  /** 反馈提交回调。流式输出中的占位消息不传此回调，反馈按钮自动隐藏。 */
  onFeedback?: (messageId: string, feedback: 'LIKE' | 'DISLIKE' | null) => void;
}

function FeedbackButtons({
  messageId,
  current,
  onFeedback,
}: {
  messageId: string;
  current: 'LIKE' | 'DISLIKE' | null | undefined;
  onFeedback: (messageId: string, feedback: 'LIKE' | 'DISLIKE' | null) => void;
}) {
  if (!onFeedback) return null;
  // 流式占位消息 id 为 'streaming-placeholder'，不应允许反馈
  if (messageId === 'streaming-placeholder') return null;
  // 尚未拿到后端持久化 messageId（如本地临时 id y11ibk1w）时隐藏，避免反馈到不存在的消息
  if (!isPersistedMessageId(messageId)) return null;

  const isLike = current === 'LIKE';
  const isDislike = current === 'DISLIKE';

  return (
    <div className="msg-feedback">
      <button
        type="button"
        className={`msg-feedback-btn${isLike ? ' active like' : ''}`}
        onClick={() => onFeedback(messageId, 'LIKE')}
        title="有帮助"
        aria-label="点赞"
      >
        <svg viewBox="0 0 16 16" fill="none" width="14" height="14">
          <path
            d="M6.5 13.5V6.5M6.5 6.5L8.5 3.2C8.7 2.9 9 2.7 9.4 2.7C10.1 2.7 10.7 3.3 10.7 4V6H13.2C13.9 6 14.5 6.6 14.5 7.3C14.5 7.4 14.5 7.5 14.4 7.6L13.1 12.4C13 13 12.5 13.5 11.9 13.5H6.5Z"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <rect
            x="2.5"
            y="6.5"
            width="2.5"
            height="7"
            rx="0.8"
            stroke="currentColor"
            strokeWidth="1.4"
          />
        </svg>
      </button>
      <button
        type="button"
        className={`msg-feedback-btn${isDislike ? ' active dislike' : ''}`}
        onClick={() => onFeedback(messageId, 'DISLIKE')}
        title="回答不满意"
        aria-label="点踩"
      >
        <svg viewBox="0 0 16 16" fill="none" width="14" height="14">
          <path
            d="M9.5 2.5V9.5M9.5 9.5L7.5 12.8C7.3 13.1 7 13.3 6.6 13.3C5.9 13.3 5.3 12.7 5.3 12V10H2.8C2.1 10 1.5 9.4 1.5 8.7C1.5 8.6 1.5 8.5 1.6 8.4L2.9 3.6C3 3 3.5 2.5 4.1 2.5H9.5Z"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <rect
            x="11"
            y="2.5"
            width="2.5"
            height="7"
            rx="0.8"
            stroke="currentColor"
            strokeWidth="1.4"
          />
        </svg>
      </button>
    </div>
  );
}

export default function AgentMessageBlock({ msg, liveTimeline = [], isStreaming, onFeedback }: Props) {
  const timeline = msg.timeline && msg.timeline.length > 0 ? msg.timeline : liveTimeline;
  const hasContent = msg.content.trim().length > 0;
  const lastIdx = timeline.length - 1;
  const units = buildTimelineUnits(timeline);

  return (
    <div className="msg-row agent">
      <div className="msg-avatar agent-avatar">
        <svg viewBox="0 0 20 20" fill="none" width="18" height="18">
          <path
            d="M10 2C5.58 2 2 5.36 2 9.5c0 2.1.9 4 2.34 5.35L3.5 18l3.5-1.75c.93.31 1.93.5 2.99.5 4.42 0 8-3.36 8-7.5S14.42 2 10 2z"
            fill="white"
            fillOpacity="0.9"
          />
        </svg>
      </div>
      <div className="msg-agent-content">
        <div className="msg-agent-name">{agentDisplayName(msg.agentName)}</div>
        <div className="agent-message-body">
          {timeline.length > 0 && (
            <div className="agent-timeline">
              {units.map((unit) => {
                if (unit.type === 'tool-group') {
                  return <ToolBlock key={`tool-${unit.items[0].id}`} items={unit.items} />;
                }
                const { item, idx } = unit;
                if (item.kind === 'thinking') {
                  return (
                    <ThinkingBlock
                      key={`think-${idx}`}
                      agentName={item.agentName}
                      text={item.text}
                      active={isStreaming === true && idx === lastIdx}
                    />
                  );
                }
                if (item.kind === 'plan') {
                  return <PlanBlock key={`plan-${idx}`} tasks={item.tasks} />;
                }
                if (item.kind === 'travel') {
                  if (!TRAVEL_ALLOWED.has(item.data.type)) return null;
                  return (
                    <div key={`travel-${idx}`} className="timeline-travel">
                      <TravelResultCard data={item.data} />
                    </div>
                  );
                }
                if (item.kind === 'booking') {
                  return (
                    <div key={`booking-${idx}`} className="timeline-booking">
                      <BookingResultCard data={item.data} />
                    </div>
                  );
                }
                if (item.kind === 'plan_html') {
                  return <PlanHtmlBlock key={`plan-html-${idx}`} data={item.data} />;
                }
                return null;
              })}
            </div>
          )}

          {hasContent && <MarkdownContent content={msg.content} />}

          {isStreaming && !hasContent && (
            <div className="thinking-indicator">
              <span className="dot" />
              <span className="dot" />
              <span className="dot" />
            </div>
          )}

        </div>
        <div className="msg-agent-footer">
          <div className="msg-agent-time">
            {new Date(msg.timestamp).toLocaleTimeString('zh-CN', {
              hour: '2-digit',
              minute: '2-digit',
            })}
          </div>
          <FeedbackButtons messageId={msg.id} current={msg.feedback} onFeedback={onFeedback!} />
        </div>
      </div>
    </div>
  );
}
