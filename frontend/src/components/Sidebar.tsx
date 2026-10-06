import { useChatStore } from '../store/chatStore';
import { useAuthStore } from '../store/authStore';
import { logout } from '../api/auth';
import { fetchConversations, deleteConversation as deleteRemoteConversation } from '../api/chat';
import type { Conversation } from '../store/chatStore';
import { useEffect } from 'react';

function formatTime(ts: number) {
  const d = new Date(ts);
  const now = new Date();
  const isToday =
    d.getDate() === now.getDate() &&
    d.getMonth() === now.getMonth() &&
    d.getFullYear() === now.getFullYear();
  if (isToday) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
}

function ConversationItem({
  conv,
  isActive,
  onSwitch,
  onDelete,
}: {
  conv: Conversation;
  isActive: boolean;
  onSwitch: () => void;
  onDelete: (e: React.MouseEvent) => void;
}) {
  return (
    <div className={`conv-item${isActive ? ' active' : ''}`} onClick={onSwitch}>
      <svg className="conv-icon" viewBox="0 0 16 16" fill="none">
        <path
          d="M8 1.5C4.41 1.5 1.5 4.06 1.5 7.25c0 1.66.73 3.15 1.89 4.22L2.5 14.5l3.22-1.61c.71.23 1.47.36 2.28.36 3.59 0 6.5-2.56 6.5-5.75S11.59 1.5 8 1.5z"
          stroke="currentColor"
          strokeWidth="1.2"
          strokeLinejoin="round"
        />
      </svg>
      <div className="conv-body">
        <span className="conv-title">{conv.title}</span>
        <span className="conv-time">{formatTime(conv.updatedAt)}</span>
      </div>
      <button
        className="conv-delete"
        onClick={onDelete}
        title="删除对话"
      >
        <svg viewBox="0 0 12 12" fill="none" width="12" height="12">
          <path
            d="M2 3h8M5 3V2h2v1M3 3l.6 6.3a.8.8 0 00.8.7h3.2a.8.8 0 00.8-.7L9 3"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
    </div>
  );
}

const emptyConversation = (): Conversation => ({
  id: `session_${Date.now()}`,
  title: '新对话',
  messages: [],
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

export default function Sidebar({ mobileOpen, onClose }: { mobileOpen: boolean; onClose: () => void }) {
  const {
    conversations,
    currentConversationId,
    createConversation,
    switchConversation,
    deleteConversation,
    setConversations,
  } = useChatStore();

  const { token, username, userId, clearAuth } = useAuthStore();
  const generating = conversations.some((conversation) => conversation.isThinking);

  useEffect(() => {
    let cancelled = false;
    fetchConversations()
      .then((remote) => {
        if (cancelled || remote.length === 0) return;

        const state = useChatStore.getState();
        const localConvs = state.conversations.filter((c) => !c.isRemote);
        const remoteIds = new Set(remote.map((c) => c.id));

        const merged: Conversation[] = remote.map((c) => {
          const local = localConvs.find((lc) => lc.id === c.id);
          if (local) {
            // 本地已有同一会话，保留已有消息与快照，仅标记为远端 persisted
            return { ...local, title: c.title, updatedAt: c.updatedAt, isRemote: true, isLoaded: true };
          }
          return {
            ...emptyConversation(),
            id: c.id,
            title: c.title,
            createdAt: c.createdAt,
            updatedAt: c.updatedAt,
            isRemote: true,
            isLoaded: false,
          };
        });

        // 保留已经输入的本地会话，不把刷新时生成的空白草稿混入历史列表。
        const extraLocal = localConvs.filter(
          (c) => !remoteIds.has(c.id) && c.messages.some((message) => message.role === 'user'),
        );
        const all = [...merged, ...extraLocal].sort((a, b) => b.updatedAt - a.updatedAt);

        setConversations(all);
        const selected = state.conversations.find((conversation) => conversation.id === state.currentConversationId);
        if (selected?.isRemote || selected?.messages.some((message) => message.role === 'user')) {
          // 保留正在查看或已输入的会话。
          return;
        }
        switchConversation(merged[0].id);
      })
      .catch((err) => {
        if (err?.message === 'UNAUTHORIZED') {
          clearAuth();
        }
        // 忽略其他加载错误，保留本地新建会话
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleLogout = async () => {
    clearAuth();
    await logout(token ?? '');
  };

  const initial = username ? username.charAt(0).toUpperCase() : '?';

  return (
    <aside className={`sidebar${mobileOpen ? ' open' : ''}`}>
      {/* Logo */}
      <div className="sidebar-logo">
        <div className="sidebar-brand">
          <span className="sidebar-brand-icon">✈</span>
          <div>
            <div className="sidebar-brand-name">GoGo差旅</div>
            <div className="sidebar-brand-sub">智能差旅助手</div>
          </div>
        </div>
      </div>

      {/* New chat button */}
      <div className="sidebar-new">
        <button
          className="new-chat-btn"
          onClick={() => {
            createConversation();
            onClose();
          }}
          disabled={generating}
          title={generating ? '请等待当前回复完成' : '新建对话'}
        >
          <svg viewBox="0 0 16 16" fill="none" width="15" height="15">
            <path
              d="M8 2v12M2 8h12"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            />
          </svg>
          新建对话
        </button>
      </div>

      {/* 当前 Python 后端已接入的对话入口。 */}
      <div className="sidebar-nav">
        <button className="sidebar-nav-btn active" type="button">
          💬 对话助手
        </button>
      </div>

      {/* Conversation list */}
      <div className="sidebar-section-label">历史对话</div>
      <div className="conv-list">
        {conversations.map((conv) => (
          <ConversationItem
            key={conv.id}
            conv={conv}
            isActive={conv.id === currentConversationId}
            onSwitch={() => {
              if (!generating) {
                switchConversation(conv.id);
                onClose();
              }
            }}
            onDelete={async (e) => {
              e.stopPropagation();
              if (generating) return;
              if (conv.isRemote) {
                try {
                  await deleteRemoteConversation(conv.id);
                } catch (err: unknown) {
                  if (err instanceof Error && err.message === 'UNAUTHORIZED') {
                    clearAuth();
                    return;
                  }
                  console.error('删除对话失败', err);
                  return;
                }
              }
              deleteConversation(conv.id);
            }}
          />
        ))}
      </div>

      {/* Footer: user info + logout */}
      <div className="sidebar-footer">
        <div className="sidebar-user">
          <div className="sidebar-user-avatar">{initial}</div>
          <div className="sidebar-user-info">
            <div className="sidebar-user-name">{username}</div>
            <div className="sidebar-user-id">{userId}</div>
          </div>
          <button className="sidebar-logout-btn" onClick={handleLogout} title="退出登录">
            <svg viewBox="0 0 16 16" fill="none" width="14" height="14">
              <path
                d="M10 2h3a1 1 0 011 1v10a1 1 0 01-1 1h-3M7 11l3-3-3-3M10 8H2"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      </div>
    </aside>
  );
}
