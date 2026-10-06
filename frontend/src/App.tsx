import { useEffect, useState } from 'react';
import { useAuthStore } from './store/authStore';
import { fetchUserInfo } from './api/auth';
import Sidebar from './components/Sidebar';
import ChatWindow from './components/ChatWindow';
import LoginPage from './components/LoginPage';
import './App.css';

export default function App() {
  const { token, clearAuth, setIsAdmin } = useAuthStore();
  const isLoggedIn = useAuthStore((s) => s.isLoggedIn());

  /**
   * 页面刺新/首次加载时，若 localStorage 存有 token，则立即对后端进行驗证。
   * - 驗证通过（200）：保持已登录状态，正常展示主界面。
   * - 401 清除登录态；网络或服务故障保留 token 并允许重试。
   * checking 状态技巧：仅当 token 存在时才请求后端，不存在时直接展示登录页。
   */
  const [checking, setChecking] = useState(!!token);
  const [validationError, setValidationError] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  const validateToken = () => {
    const currentToken = useAuthStore.getState().token;
    if (!currentToken) return;
    fetchUserInfo(currentToken)
      .then((info) => {
        if (useAuthStore.getState().token === currentToken) setIsAdmin(info.admin === true);
      })
      .catch((error: unknown) => {
        if (useAuthStore.getState().token !== currentToken) return;
        if (error instanceof Error && error.message === 'UNAUTHORIZED') clearAuth();
        else setValidationError(true);
      })
      .finally(() => setChecking(false));
  };

  useEffect(() => {
    if (token) validateToken();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // 仅在组件首次挂载（页面初始化/刷新）时运行一次

  // token 正在驗证中：返回空白屏避免闪现错误页面
  if (checking) return null;

  if (!isLoggedIn) {
    return <LoginPage />;
  }

  if (validationError) {
    return (
      <div className="login-page">
        <div className="login-card">
          <h2 className="login-title">暂时无法验证登录态</h2>
          <p className="login-desc">请检查后端服务，然后重试连接。</p>
          <button className="login-btn" type="button" onClick={() => {
            setValidationError(false);
            setChecking(true);
            validateToken();
          }}>
            重试连接
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="app-root">
      {mobileSidebarOpen && (
        <button
          className="sidebar-overlay"
          type="button"
          aria-label="关闭会话菜单"
          onClick={() => setMobileSidebarOpen(false)}
        />
      )}
      <Sidebar mobileOpen={mobileSidebarOpen} onClose={() => setMobileSidebarOpen(false)} />
      <ChatWindow onOpenSidebar={() => setMobileSidebarOpen(true)} />
    </div>
  );
}
