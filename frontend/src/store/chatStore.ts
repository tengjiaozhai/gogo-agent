import { create } from 'zustand';
import type { Message, PlanProgress, PlanStep, PlanTask, TimelineItem, TravelData } from '../types';

export interface Conversation {
  id: string;
  title: string;
  messages: Message[];
  planProgress: PlanProgress | null;
  /** ItineraryPlanAgent 当前规划的子任务列表 */
  planTasks: PlanTask[];
  /** agentName → 当次请求中该 agent 的多轮推理文本（每个元素为一轮） */
  thinkingByAgent: Record<string, string[]>;
  /** 当前轮次的旅行搜索结果（机票/酒店/火车票） */
  travelData: TravelData[];
  /** 当前轮次按时间顺序的思考/工具/计划/结果时间轴 */
  timeline: TimelineItem[];
  isThinking: boolean;
  activeAgent: string;
  /** 问题推荐智能体生成的推荐问题 */
  suggestedQuestions: string[];
  createdAt: number;
  updatedAt: number;
  /** 是否从后端加载的历史会话 */
  isRemote?: boolean;
  /** 该会话的历史消息是否已从后端加载 */
  isLoaded?: boolean;
}

interface ChatState {
  conversations: Conversation[];
  currentConversationId: string;
  resetForAuthChange: () => void;

  // Conversation management
  createConversation: () => string;
  switchConversation: (id: string) => void;
  deleteConversation: (id: string) => void;
  updateConversationTitle: (id: string, title: string) => void;

  // Remote history
  setConversations: (conversations: Conversation[]) => void;
  loadConversationMessages: (id: string, messages: Message[]) => void;
  markConversationLoaded: (id: string) => void;

  // Derived helpers
  getCurrentConversation: () => Conversation | undefined;

  // Actions on current conversation
  addMessage: (msg: Message) => void;
  appendToLastAgentMessage: (text: string) => void;
  addProgressStep: (step: PlanStep) => void;
  /** 更新 ItineraryPlanAgent 任务列表 */
  updatePlanTasks: (tasks: PlanTask[]) => void;
  /** 添加旅行搜索结果 */
  addTravelData: (data: TravelData) => void;
  /** 将当前进度快照到最后一条 agent 消息，用于下一轮开始前持久历史进度 */
  snapshotProgressToLastMessage: () => void;
  clearProgress: () => void;
  addTimelineItem: (item: TimelineItem) => void;
  updateTimelineTool: (id: string, patch: Partial<TimelineItem & { kind: 'tool' }>) => void;
  appendTimelineThinking: (agentName: string, text: string, roundStart?: boolean) => void;
  setTimelinePlan: (tasks: PlanTask[]) => void;
  clearTimeline: () => void;
  appendThinking: (agentName: string, text: string) => void;
  /** 为指定 Agent 开始新一轮思考（push 空字符串到数组） */
  startThinkingRound: (agentName: string) => void;
  setThinking: (val: boolean) => void;
  setActiveAgent: (name: string) => void;
  setSuggestedQuestions: (questions: string[]) => void;
  clearSuggestedQuestions: () => void;
  /**
   * 设置指定消息的用户反馈（LIKE / DISLIKE / null）。
   * 乐观更新，失败时由调用方决定是否回滚。
   */
  setMessageFeedback: (
    conversationId: string,
    messageId: string,
    feedback: 'LIKE' | 'DISLIKE' | null,
    feedbackAt?: number,
  ) => void;
  /**
   * 用后端下发的持久化 messageId 回填最后一条 agent 消息的 id。
   * 流式渲染时 id 是本地随机生成的（如 y11ibk1w），不能用于反馈接口。
   */
  setLastAgentMessageId: (messageId: string) => void;
}

const generateId = () => Math.random().toString(36).slice(2, 10);

/** 后端持久化消息 ID 的前缀（参见 ChatHistoryService#generateMessageId） */
const PERSISTED_ID_PREFIX = 'msg_';

/**
 * 判断消息 id 是否为后端持久化 ID。
 * 本地流式渲染临时生成的 id（如 y11ibk1w）不能作为反馈接口的 messageId。
 */
export const isPersistedMessageId = (id?: string): boolean =>
  typeof id === 'string' && id.startsWith(PERSISTED_ID_PREFIX);

const createNewConversation = (): Conversation => ({
  id: `session_${Date.now()}`,
  title: '新对话',
  messages: [
    {
      id: generateId(),
      role: 'agent',
      content:
        '您好！我是 GoGo 差旅助手 ✈️\n\n目前可以进行基础对话和普通出行信息咨询。差旅申请、规划、预订与审批功能仍在迁移中。',
      agentName: 'GoGo差旅助手',
      timestamp: Date.now(),
    },
  ],
  planProgress: null,
  planTasks: [],
  thinkingByAgent: {},
  travelData: [],
  timeline: [],
  isThinking: false,
  activeAgent: 'GoGo',
  suggestedQuestions: [],
  createdAt: Date.now(),
  updatedAt: Date.now(),
  isRemote: false,
  isLoaded: false,
});

const initialConversation = createNewConversation();

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [initialConversation],
  currentConversationId: initialConversation.id,

  resetForAuthChange: () => {
    const conversation = createNewConversation();
    set({ conversations: [conversation], currentConversationId: conversation.id });
  },

  getCurrentConversation: () => {
    const { conversations, currentConversationId } = get();
    return conversations.find((c) => c.id === currentConversationId);
  },

  createConversation: () => {
    const newConv = createNewConversation();
    set((state) => ({
      conversations: [newConv, ...state.conversations],
      currentConversationId: newConv.id,
    }));
    return newConv.id;
  },

  switchConversation: (id) => set({ currentConversationId: id }),

  deleteConversation: (id) =>
    set((state) => {
      const filtered = state.conversations.filter((c) => c.id !== id);
      if (filtered.length === 0) {
        const newConv = createNewConversation();
        return { conversations: [newConv], currentConversationId: newConv.id };
      }
      const newCurrentId =
        state.currentConversationId === id ? filtered[0].id : state.currentConversationId;
      return { conversations: filtered, currentConversationId: newCurrentId };
    }),

  updateConversationTitle: (id, title) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === id ? { ...c, title } : c,
      ),
    })),

  setConversations: (conversations) =>
    set({
      conversations,
    }),

  loadConversationMessages: (id, messages) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === id ? { ...c, messages, isLoaded: true } : c,
      ),
    })),

  markConversationLoaded: (id) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === id ? { ...c, isLoaded: true } : c,
      ),
    })),

  addMessage: (msg) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        // Auto-set title from first user message
        const newTitle =
          c.title === '新对话' && msg.role === 'user'
            ? msg.content.slice(0, 24)
            : c.title;
        return {
          ...c,
          title: newTitle,
          messages: [...c.messages, msg],
          updatedAt: Date.now(),
        };
      }),
    })),

  appendToLastAgentMessage: (text) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        const msgs = [...c.messages];
        const last = msgs[msgs.length - 1];
        if (last && last.role === 'agent') {
          msgs[msgs.length - 1] = { ...last, content: last.content + text };
        } else {
          msgs.push({
            id: generateId(),
            role: 'agent',
            content: text,
            agentName: c.activeAgent,
            timestamp: Date.now(),
          });
        }
        return { ...c, messages: msgs, updatedAt: Date.now() };
      }),
    })),

  addProgressStep: (step) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        const existing = c.planProgress?.steps ?? [];
        const idx = existing.findIndex((s) => s.id === step.id);
        const steps: PlanStep[] =
          idx >= 0 ? existing.map((s, i) => (i === idx ? step : s)) : [...existing, step];
        const done = steps.filter((s) => s.status === 'done').length;
        const percent = steps.length > 0 ? Math.round((done / steps.length) * 100) : 0;
        return { ...c, planProgress: { percent, steps } };
      }),
    })),

  clearProgress: () =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === state.currentConversationId
          ? {
              ...c,
              planProgress: null,
              planTasks: [],
              thinkingByAgent: {},
              travelData: [],
              timeline: [],
            }
          : c,
      ),
    })),

  addTimelineItem: (item) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === state.currentConversationId
          ? { ...c, timeline: [...c.timeline, item] }
          : c,
      ),
    })),

  updateTimelineTool: (id, patch) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        const timeline = c.timeline.map((item) =>
          item.kind === 'tool' && item.id === id ? { ...item, ...patch } : item,
        );
        return { ...c, timeline };
      }),
    })),

  appendTimelineThinking: (agentName, text, roundStart) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        const timeline = [...c.timeline];
        const last = timeline[timeline.length - 1];
        if (
          !roundStart &&
          last &&
          last.kind === 'thinking' &&
          last.agentName === agentName
        ) {
          timeline[timeline.length - 1] = { ...last, text: last.text + text };
        } else {
          timeline.push({ kind: 'thinking', agentName, text });
        }
        return { ...c, timeline };
      }),
    })),

  setTimelinePlan: (tasks) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        const timeline = [...c.timeline];
        const lastIdx = timeline.reduceRight(
          (found, item, i) => (found >= 0 ? found : item.kind === 'plan' ? i : -1),
          -1,
        );
        if (lastIdx >= 0) {
          timeline[lastIdx] = { kind: 'plan', tasks };
        } else if (tasks.length > 0) {
          timeline.push({ kind: 'plan', tasks });
        }
        return { ...c, timeline };
      }),
    })),

  clearTimeline: () =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === state.currentConversationId ? { ...c, timeline: [] } : c,
      ),
    })),

  updatePlanTasks: (tasks) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === state.currentConversationId ? { ...c, planTasks: tasks } : c,
      ),
    })),

  addTravelData: (data) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === state.currentConversationId
          ? { ...c, travelData: [...c.travelData, data] }
          : c,
      ),
    })),

  snapshotProgressToLastMessage: () =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        const hasProgress = !!(c.planProgress && c.planProgress.steps.length > 0);
        const hasTimeline = c.timeline.length > 0;
        if (!hasProgress && !hasTimeline) return c;
        const msgs = [...c.messages];
        const snapshot: Partial<Message> = {};
        if (hasProgress && c.planProgress) {
          snapshot.progress = c.planProgress;
        }
        if (Object.keys(c.thinkingByAgent).length > 0) {
          snapshot.thinkingByAgent = { ...c.thinkingByAgent };
        }
        if (c.planTasks.length > 0) {
          snapshot.planTasks = [...c.planTasks];
        }
        if (c.travelData.length > 0) {
          snapshot.travelData = [...c.travelData];
        }
        if (c.timeline.length > 0) {
          snapshot.timeline = [...c.timeline];
        }
        const last = msgs[msgs.length - 1];
        if (last && last.role === 'agent') {
          // 当前轮次已产生正文消息（最后一条即为本轮 agent 消息），直接附上快照
          msgs[msgs.length - 1] = { ...last, ...snapshot };
        } else {
          // 当前轮次尚未产生任何 agent 正文消息（例如工具执行到一半就触发了用户交互），
          // 若仍按“向前查找最近一条 agent 消息”的旧逻辑，会把本轮进度误挂到历史消息上，
          // 导致本轮进度在触发 user_interaction 后从界面上“消失”。这里改为补一条占位消息承载快照。
          msgs.push({
            id: generateId(),
            role: 'agent',
            content: '',
            agentName: c.activeAgent,
            timestamp: Date.now(),
            ...snapshot,
          });
        }
        return { ...c, messages: msgs };
      }),
    })),

  appendThinking: (agentName, text) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        const rounds = c.thinkingByAgent[agentName] ?? [];
        if (rounds.length === 0) {
          // 还没有任何轮次，直接创建第一轮
          return { ...c, thinkingByAgent: { ...c.thinkingByAgent, [agentName]: [text] } };
        }
        const newRounds = [...rounds];
        newRounds[newRounds.length - 1] = newRounds[newRounds.length - 1] + text;
        return { ...c, thinkingByAgent: { ...c.thinkingByAgent, [agentName]: newRounds } };
      }),
    })),

  startThinkingRound: (agentName) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        const rounds = c.thinkingByAgent[agentName] ?? [];
        return { ...c, thinkingByAgent: { ...c.thinkingByAgent, [agentName]: [...rounds, ''] } };
      }),
    })),

  setThinking: (val) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === state.currentConversationId ? { ...c, isThinking: val } : c,
      ),
    })),

  setActiveAgent: (name) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === state.currentConversationId ? { ...c, activeAgent: name } : c,
      ),
    })),

  setSuggestedQuestions: (questions) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === state.currentConversationId
          ? { ...c, suggestedQuestions: questions, updatedAt: Date.now() }
          : c,
      ),
    })),

  clearSuggestedQuestions: () =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === state.currentConversationId
          ? { ...c, suggestedQuestions: [], updatedAt: Date.now() }
          : c,
      ),
    })),

  setMessageFeedback: (conversationId, messageId, feedback, feedbackAt) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== conversationId) return c;
        const messages = c.messages.map((m) =>
          m.id === messageId
            ? {
                ...m,
                feedback,
                feedbackAt: feedback == null ? undefined : feedbackAt ?? Date.now(),
              }
            : m,
        );
        return { ...c, messages };
      }),
    })),

  setLastAgentMessageId: (messageId) =>
    set((state) => ({
      conversations: state.conversations.map((c) => {
        if (c.id !== state.currentConversationId) return c;
        const msgs = [...c.messages];
        const lastIdx = msgs.length - 1;
        const last = msgs[lastIdx];
        // 仅回填本轮刚渲染出的流式回复（必为最后一条 agent 消息），
        // 且已拿到过持久化 id 时不再覆盖，避免改错已有消息的 id
        if (!last || last.role !== 'agent' || isPersistedMessageId(last.id)) return c;
        msgs[lastIdx] = { ...last, id: messageId };
        return { ...c, messages: msgs };
      }),
    })),
}));
