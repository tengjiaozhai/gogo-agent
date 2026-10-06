import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { useChatStore } from './chatStore';

interface AuthState {
  token: string | null;
  userId: string | null;
  username: string | null;
  isAdmin: boolean;

  setAuth: (token: string, userId: string, username: string, isAdmin?: boolean) => void;
  setIsAdmin: (isAdmin: boolean) => void;
  clearAuth: () => void;
  isLoggedIn: () => boolean;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      userId: null,
      username: null,
      isAdmin: false,

      setAuth: (token, userId, username, isAdmin = false) => {
        if (get().userId !== userId) useChatStore.getState().resetForAuthChange();
        set({ token, userId, username, isAdmin });
      },

      setIsAdmin: (isAdmin) => set({ isAdmin }),

      clearAuth: () => {
        set({ token: null, userId: null, username: null, isAdmin: false });
        useChatStore.getState().resetForAuthChange();
      },

      isLoggedIn: () => !!get().token,
    }),
    {
      name: 'gogo-auth', // localStorage key
    },
  ),
);
