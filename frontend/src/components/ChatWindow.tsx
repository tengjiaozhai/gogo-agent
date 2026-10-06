import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { flushSync } from 'react-dom';
import { useChatStore, isPersistedMessageId } from '../store/chatStore';
import { useAuthStore } from '../store/authStore';
import { sendChatMessageSSE, fetchMessages, updateMessageFeedback } from '../api/chat';
import AgentMessageBlock from './AgentMessageBlock';
import type { Message, TimelineItem, PlanTask, TravelData, BookingResult, PlanHtmlData } from '../types';

const emptyMessages: Message[] = [];
const emptyTimeline: TimelineItem[] = [];

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function systemMessage(content: string): Message {
  return {
    id: Math.random().toString(36).slice(2, 10),
    role: 'system',
    content,
    timestamp: Date.now(),
  };
}

// ── Message bubble components ─────────────────────────────────────────────
function UserBubble({ msg, username }: { msg: Message; username: string }) {
  // 取用户名首字符作为头像
  const initial = username ? username.charAt(0).toUpperCase() : '我';
  return (
    <div className="msg-row user">
      <div className="msg-user-bubble">{msg.content}</div>
      <div className="msg-avatar user-avatar">{initial}</div>
    </div>
  );
}

function SuggestionChips({
  questions,
  onClick,
}: {
  questions: string[];
  onClick: (q: string) => void;
}) {
  if (!questions.length) return null;
  return (
    <div className="msg-row agent">
      <div className="msg-avatar agent-avatar" style={{ visibility: 'hidden' }} />
      <div className="msg-agent-content">
        <div className="suggestion-chips">
          {questions.map((q) => (
            <button
              key={q}
              className="suggestion-chip"
              onClick={() => onClick(q)}
              title={q}
              type="button"
            >
              {q}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function SystemBubble({ msg }: { msg: Message }) {
  return (
    <div className="msg-row system">
      <div className="msg-system-bubble">
        <svg viewBox="0 0 16 16" fill="none" width="13" height="13" style={{ flexShrink: 0 }}>
          <circle cx="8" cy="8" r="6.5" stroke="#d46b08" strokeWidth="1.2" />
          <path d="M8 5v3.5M8 10.5v.5" stroke="#d46b08" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
        {msg.content}
      </div>
    </div>
  );
}

// ── Thinking indicator ────────────────────────────────────────────────────
function ThinkingDots() {
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
        <div className="msg-agent-name">GoGo差旅助手</div>
        <div className="thinking-indicator">
          <span className="dot" />
          <span className="dot" />
          <span className="dot" />
        </div>
      </div>
    </div>
  );
}

// ── Main ChatWindow component ─────────────────────────────────────────────

/**
 * 判断条目是否为 MCP 内容块（形如 {type:'text', text:'...'}）。
 * 这类条目是工具的原始输出包装，而非可展示的业务数据。
 */
function isRawContentBlock(item: unknown): boolean {
  if (!item || typeof item !== 'object') return false;
  const obj = item as Record<string, unknown>;
  return obj.type === 'text' && typeof obj.text === 'string';
}

/**
 * 清洗后端推送的 travel_data：
 * - 过滤掉 MCP 内容块类条目，避免将原始 JSON 直接展示为卡片；
 * - 若无有效条目则返回 null（不渲染卡片，由正文 Markdown 展示）。
 */
function sanitizeTravelData(td: TravelData | null | undefined): TravelData | null {
  if (!td || !td.type || !Array.isArray(td.items)) return null;
  const items = (td.items as unknown[]).filter((it) => !isRawContentBlock(it));
  if (items.length === 0) return null;
  return { ...td, items: items as TravelData['items'] };
}

export default function ChatWindow({ onOpenSidebar }: { onOpenSidebar: () => void }) {
  const [inputText, setInputText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const activeRequest = useRef<AbortController | null>(null);

  useEffect(() => () => activeRequest.current?.abort(), []);

  const username = useAuthStore((s) => s.username) ?? '我';
  const clearAuth = useAuthStore((state) => state.clearAuth);


  /** Token 失效时统一登出并跳回登录页 */
  const handleUnauthorized = () => {
    clearAuth();
  };

  const {
    conversations,
    currentConversationId,
    addMessage,
    appendToLastAgentMessage,
    addProgressStep,
    updatePlanTasks,
    addTravelData,
    snapshotProgressToLastMessage,
    clearProgress,
    appendThinking,
    startThinkingRound,
    setThinking,
    setActiveAgent,
    setSuggestedQuestions,
    clearSuggestedQuestions,
    loadConversationMessages,
    addTimelineItem,
    updateTimelineTool,
    appendTimelineThinking,
    setTimelinePlan,
    setMessageFeedback,
    setLastAgentMessageId,
  } = useChatStore();

  const currentConv = conversations.find((c) => c.id === currentConversationId);
  const messages = currentConv?.messages ?? emptyMessages;
  const isThinking = currentConv?.isThinking ?? false;
  const timeline = currentConv?.timeline ?? emptyTimeline;
  const suggestedQuestions = currentConv?.suggestedQuestions ?? [];

  const lastMsg = messages.length > 0 ? messages[messages.length - 1] : null;
  const lastAgentIsStreaming =
    isThinking && lastMsg?.role === 'agent' && !lastMsg.timeline;

  // 从后端加载远程会话的历史消息
  useEffect(() => {
    if (!currentConv || !currentConv.isRemote || currentConv.isLoaded) return;
    let cancelled = false;
    fetchMessages(currentConversationId)
      .then((remoteMessages) => {
        if (cancelled) return;
        const messages: Message[] = remoteMessages.map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content ?? '',
          agentName: m.agentName ?? undefined,
          timestamp: m.timestamp,
          feedback: m.feedback ?? null,
          feedbackAt: m.feedbackAt ?? undefined,
        }));
        loadConversationMessages(currentConversationId, messages);
      })
      .catch((err) => {
        if (err?.message === 'UNAUTHORIZED') {
          handleUnauthorized();
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentConversationId]);

  // Auto-scroll to bottom
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isThinking, timeline]);

  // 流式输出结束时，把当前时间轴快照到最后一条 agent 消息，避免正文出现后进度丢失
  const prevIsThinkingRef = useRef(isThinking);
  useEffect(() => {
    if (prevIsThinkingRef.current && !isThinking) {
      snapshotProgressToLastMessage();
    }
    prevIsThinkingRef.current = isThinking;
  }, [isThinking, snapshotProgressToLastMessage]);

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 140) + 'px';
  }, [inputText]);

  const handleEvent = (event: string, data: string) => {
    switch (event) {
      case 'agent-switch':
        setActiveAgent(data);
        break;
      case 'message':
        appendToLastAgentMessage(data);
        break;
      case 'message_id':
        // 后端回复落库后下发的真实 messageId，回填后点赞/点踩才能命中数据库记录
        setLastAgentMessageId(data.trim());
        break;
      case 'thinking': {
        try {
          const d = JSON.parse(data) as { agentName: string; text?: string; roundStart?: string };
          if (!d.agentName) break;
          if (d.roundStart === 'true') {
            // 新一轮思考开始
            startThinkingRound(d.agentName);
            appendTimelineThinking(d.agentName, '', true);
          } else if (d.text) {
            appendThinking(d.agentName, d.text);
            appendTimelineThinking(d.agentName, d.text);
          }
        } catch {
          // ignore malformed
        }
        break;
      }
      case 'plan_update': {
        try {
          const d = JSON.parse(data) as {
            type: string;
            agentName: string;
            planName: string;
            tasks: PlanTask[];
          };
          if (Array.isArray(d.tasks)) {
            try {
              flushSync(() => {
                updatePlanTasks(d.tasks);
                setTimelinePlan(d.tasks);
              });
            } catch {
              updatePlanTasks(d.tasks);
              setTimelinePlan(d.tasks);
            }
          }
        } catch {
          // ignore malformed
        }
        break;
      }
      case 'progress': {
        let progressData: {
          type: string;
          stepId: string;
          agentName: string;
          toolName?: string;
          message: string;
          result?: string;
          arguments?: string;
        };
        try {
          progressData = JSON.parse(data);
        } catch {
          break;
        }
        if (!progressData.stepId) break;
        const isDone = progressData.type === 'tool_done' || progressData.type === 'agent_done';
        const step = {
          id: progressData.stepId,
          title: progressData.message,
          status: (isDone ? 'done' : 'in-progress') as 'done' | 'in-progress',
          agentName: progressData.agentName,
          result: progressData.result,
          arguments: progressData.arguments,
        };
        try {
          flushSync(() => addProgressStep(step));
        } catch {
          addProgressStep(step);
        }
        if (progressData.type === 'tool_call') {
          addTimelineItem({
            kind: 'tool',
            id: progressData.stepId,
            agentName: progressData.agentName,
            title: progressData.message,
            status: 'in-progress',
            arguments: progressData.arguments,
          });
        } else if (progressData.type === 'tool_done') {
          updateTimelineTool(progressData.stepId, {
            status: 'done',
            title: progressData.message,
            result: progressData.result,
          });
        }
        break;
      }
      case 'error':
        addMessage({
          id: Math.random().toString(36).slice(2, 10),
          role: 'system',
          content: `错误：${data}`,
          timestamp: Date.now(),
        });
        break;
      case 'user_interaction': {
        addMessage({
          id: Math.random().toString(36).slice(2, 10),
          role: 'system',
          content: '当前版本尚未支持交互式回复，请稍后再试。',
          timestamp: Date.now(),
        });
        break;
      }
      case 'interrupted':
        setThinking(false);
        clearSuggestedQuestions();
        addMessage({
          id: Math.random().toString(36).slice(2, 10),
          role: 'system',
          content: '已停止生成',
          timestamp: Date.now(),
        });
        break;
      case 'suggestions': {
        try {
          const questions = JSON.parse(data) as string[];
          if (Array.isArray(questions)) {
            setSuggestedQuestions(questions.filter((q) => typeof q === 'string' && q.length > 0));
          }
        } catch {
          // ignore malformed
        }
        break;
      }
      case 'travel_data': {
        try {
          const td = JSON.parse(data) as TravelData;
          const sanitized = sanitizeTravelData(td);
          if (sanitized) {
            addTravelData(sanitized);
            addTimelineItem({ kind: 'travel', data: sanitized });
          }
        } catch {
          // ignore malformed
        }
        break;
      }
      case 'booking_result': {
        try {
          const br = JSON.parse(data) as BookingResult;
          if (br && br.orderId) {
            addTimelineItem({ kind: 'booking', data: br });
          }
        } catch {
          // ignore malformed
        }
        break;
      }
      case 'plan_html': {
        try {
          const d = JSON.parse(data) as PlanHtmlData;
          if (d && d.html) {
            addTimelineItem({ kind: 'plan_html', data: d });
          }
        } catch {
          // ignore malformed
        }
        break;
      }
      default:
        break;
    }
  };

  /**
   * 处理用户对 AI 回复的点赞/点踩反馈。
   * 先乐观更新本地状态，调用失败时回滚到上一次状态。
   */
  const handleFeedback = async (
    messageId: string,
    next: 'LIKE' | 'DISLIKE' | null,
  ) => {
    // 本地临时 id（未收到后端 message_id）无法反馈，直接忽略避免请求 404
    if (!isPersistedMessageId(messageId)) {
      return;
    }
    const target = messages.find((m) => m.id === messageId);
    const prev = target?.feedback ?? null;
    // 再次点击同一选项视为取消
    const finalNext: 'LIKE' | 'DISLIKE' | null = prev === next ? null : next;
    setMessageFeedback(currentConversationId, messageId, finalNext);
    try {
      await updateMessageFeedback(currentConversationId, messageId, finalNext);
    } catch (err: unknown) {
      // 失败时回滚
      setMessageFeedback(currentConversationId, messageId, prev);
      if (errorText(err, '') === 'UNAUTHORIZED') {
        handleUnauthorized();
        return;
      }
      addMessage(systemMessage(`反馈提交失败：${errorText(err, '未知错误')}`));
    }
  };

  const sendText = async (content: string) => {
    if (!content.trim() || isThinking) return;
    const requestToken = useAuthStore.getState().token;
    const requestSessionId = currentConversationId;
    const controller = new AbortController();
    activeRequest.current = controller;
    const isCurrentRequest = () =>
      useAuthStore.getState().token === requestToken &&
      useChatStore.getState().currentConversationId === requestSessionId;

    // 先把上一轮进度快照到上一条 agent 消息、清空 store，再追加本轮 user 消息。
    // 顺序不能反：若在追加 user 消息后再 snapshot，此时 messages 末尾是 user，
    // snapshotProgressToLastMessage 会走“无 agent 兜底”分支新建一条空白 agent 消息，
    // 导致上一轮的进度/时间轴以 phantom 消息形式出现在用户对话下方。
    snapshotProgressToLastMessage();
    clearProgress();
    clearSuggestedQuestions();

    addMessage({
      id: Math.random().toString(36).slice(2, 10),
      role: 'user',
      content,
      timestamp: Date.now(),
    });
    setThinking(true);
    const onEvt = (evt: { event: string; data: string }) => {
      if (isCurrentRequest()) handleEvent(evt.event, evt.data);
    };
    const onErr = (err: Error) => {
      if (!isCurrentRequest()) return;
      if (err.message === 'UNAUTHORIZED') {
        setThinking(false);
        handleUnauthorized();
        return;
      }
      addMessage(systemMessage(`连接异常：${errorText(err, '未知错误')}`));
    };
    const onDone = () => {
      if (isCurrentRequest()) setThinking(false);
    };

    try {
      await sendChatMessageSSE(requestSessionId, content, onEvt, onErr, onDone, controller.signal);
    } catch (err: unknown) {
      if (!isCurrentRequest()) return;
      setThinking(false);
      if (errorText(err, '') === 'UNAUTHORIZED') {
        // Token 已失效，自动登出（clearAuth 会触发 App 渲染登录页）
        handleUnauthorized();
        return;
      }
      addMessage(systemMessage(`发送失败：${errorText(err, '请检查后端服务是否启动')}`));
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
    }
  };

  const handleSend = async () => {
    if (!inputText.trim()) return;
    const content = inputText.trim();
    setInputText('');
    await sendText(content);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="chat-window">
      {/* Header */}
      <div className="chat-header">
        <div className="chat-header-left">
          <button className="mobile-menu-btn" type="button" onClick={onOpenSidebar} aria-label="打开会话菜单">
            ☰
          </button>
          <svg viewBox="0 0 20 20" fill="none" width="20" height="20" className="chat-header-icon">
            <path
              d="M10 2C5.58 2 2 5.36 2 9.5c0 2.1.9 4 2.34 5.35L3.5 18l3.5-1.75c.93.31 1.93.5 2.99.5 4.42 0 8-3.36 8-7.5S14.42 2 10 2z"
              fill="#1677ff"
            />
          </svg>
          <span className="chat-header-title">{currentConv?.title || '新对话'}</span>
        </div>
        <div className="chat-header-right">
          {isThinking && (
            <div className="chat-header-status">
              <span className="status-dot" />
              AI 处理中...
            </div>
          )}
        </div>
      </div>

      <div className="chat-scope-note">当前开放基础对话与普通信息咨询；差旅申请、规划和预订仍在迁移中。</div>

      {/* Message list */}
      <div className="message-list">
        {messages.map((msg, idx) => {
          if (msg.role === 'user') return <UserBubble key={msg.id} msg={msg} username={username} />;
          if (msg.role === 'agent') {
            const isLast = idx === messages.length - 1;
            const streaming = isLast && lastAgentIsStreaming;
            return (
              <AgentMessageBlock
                key={msg.id}
                msg={msg}
                liveTimeline={streaming ? timeline : undefined}
                isStreaming={streaming}
                onFeedback={handleFeedback}
              />
            );
          }
          return <SystemBubble key={msg.id} msg={msg} />;
        })}

        {/* 处理中但还没有 agent 消息时，先用占位消息展示时间轴 */}
        {isThinking && !lastAgentIsStreaming && timeline.length > 0 && (
          <AgentMessageBlock
            key="streaming-placeholder"
            msg={{
              id: 'streaming-placeholder',
              role: 'agent',
              content: '',
              agentName: currentConv?.activeAgent,
              timestamp: currentConv?.updatedAt ?? 0,
            }}
            liveTimeline={timeline}
            isStreaming
          />
        )}

        {/* 兜底思考动画：处理中且没有任何时间轴内容 */}
        {isThinking && !lastAgentIsStreaming && timeline.length === 0 && <ThinkingDots />}

        {/* 推荐问题：MasterAgent 返回后由问题推荐智能体生成 */}
        {!isThinking && suggestedQuestions.length > 0 && (
          <SuggestionChips questions={suggestedQuestions} onClick={sendText} />
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input area */}
      <div className="input-area">
        <div className={`input-box${isThinking ? ' disabled' : ''}`}>
          <textarea
            ref={textareaRef}
            className="input-textarea"
            placeholder={
              isThinking
                ? 'AI 正在处理，请稍候...'
                : '输入问题，按 Enter 发送，Shift+Enter 换行'
            }
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isThinking}
            rows={1}
          />
          <button
            className="send-btn"
            onClick={handleSend}
            disabled={isThinking || !inputText.trim()}
            title={isThinking ? '请等待当前回复完成' : '发送 (Enter)'}
          >
            <svg viewBox="0 0 20 20" fill="none" width="18" height="18">
              <path d="M4 10L16 4l-4 6 4 6L4 10z" fill="currentColor" />
            </svg>
          </button>
        </div>
        <div className="input-hint">Enter 发送 · Shift+Enter 换行 · 支持中英文</div>
      </div>
    </div>
  );
}
