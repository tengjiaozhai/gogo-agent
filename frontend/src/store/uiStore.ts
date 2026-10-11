import { create } from 'zustand';

export type AppView = 'chat' | 'travel' | 'admin';

interface UiState {
  view: AppView;
  setView: (view: AppView) => void;
  resetView: () => void;
}

/** 管理登录后顶层页面；页面状态不混入聊天会话或 AgentState。 */
export const useUiStore = create<UiState>((set) => ({
  view: 'chat',
  setView: (view) => set({ view }),
  resetView: () => set({ view: 'chat' }),
}));
