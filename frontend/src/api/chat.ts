import { useAuthStore } from '../store/authStore';
import { getApiBase } from './config';

export interface SSEEvent {
  event: string;
  data: string;
}

export interface RemoteConversation {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
}

export interface RemoteMessage {
  id: string;
  role: 'user' | 'agent' | 'system';
  content: string | null;
  agentName: string | null;
  timestamp: number;
  feedback: 'LIKE' | 'DISLIKE' | null;
  feedbackAt: number | null;
}

function authHeaders(): Record<string, string> {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: token } : {};
}

function requireOk(response: Response): void {
  if (response.status === 401) throw new Error('UNAUTHORIZED');
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

/** 解析 POST 响应流；同一个事件的 data 行可能被网络分块拆开。 */
export async function sendChatMessageSSE(
  sessionId: string,
  message: string,
  onEvent: (event: SSEEvent) => void,
  onError?: (error: Error) => void,
  onComplete?: () => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${getApiBase()}/api/chat/${encodeURIComponent(sessionId)}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...authHeaders(),
    },
    body: JSON.stringify({ message }),
    signal,
  });
  requireOk(response);
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new Error('后端未返回聊天事件流');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let eventName = 'message';
  let dataLines: string[] = [];

  const flush = () => {
    if (dataLines.length > 0) onEvent({ event: eventName, data: dataLines.join('\n') });
    eventName = 'message';
    dataLines = [];
  };
  const consumeLine = (raw: string) => {
    const line = raw.replace(/\r$/, '');
    if (line === '') flush();
    else if (line.startsWith('event:')) eventName = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (value) buffer += decoder.decode(value, { stream: !done });
      if (done) buffer += decoder.decode();
      let newline = buffer.indexOf('\n');
      while (newline !== -1) {
        consumeLine(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf('\n');
      }
      if (done) {
        if (buffer) consumeLine(buffer);
        flush();
        break;
      }
    }
  } catch (error) {
    onError?.(error instanceof Error ? error : new Error('聊天事件流读取失败'));
  } finally {
    reader.releaseLock();
    onComplete?.();
  }
}

export async function fetchConversations(): Promise<RemoteConversation[]> {
  const response = await fetch(`${getApiBase()}/api/chat/conversations`, { headers: authHeaders() });
  requireOk(response);
  return response.json();
}

export async function fetchMessages(sessionId: string): Promise<RemoteMessage[]> {
  const response = await fetch(`${getApiBase()}/api/chat/${encodeURIComponent(sessionId)}/messages`, {
    headers: authHeaders(),
  });
  requireOk(response);
  return response.json();
}

export async function deleteConversation(sessionId: string): Promise<void> {
  const response = await fetch(`${getApiBase()}/api/chat/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });
  requireOk(response);
}

export async function updateMessageFeedback(
  sessionId: string,
  messageId: string,
  feedback: 'LIKE' | 'DISLIKE' | null,
): Promise<void> {
  const response = await fetch(
    `${getApiBase()}/api/chat/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/feedback`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ feedback }),
    },
  );
  requireOk(response);
}
