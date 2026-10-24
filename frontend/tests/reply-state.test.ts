import assert from 'node:assert/strict';
import { test } from 'node:test';
import { useChatStore } from '../src/store/chatStore.ts';

function setup() {
  useChatStore.getState().resetForAuthChange();
  const base = useChatStore.getState().getCurrentConversation()!;
  useChatStore.setState({
    conversations: ['first', 'second'].map((id) => ({ ...base, id, messages: [] })),
    currentConversationId: 'first',
  });
  return useChatStore.getState();
}

function startReply(replyId = 'local-reply') {
  const store = useChatStore.getState();
  store.addMessage({ id: 'user', role: 'user', content: '确认', timestamp: 0 });
  store.addMessage({ id: replyId, role: 'agent', content: '', timestamp: 0, replyStatus: 'streaming' });
  store.setThinking(true);
}

test('失败摘要保留正文和时间轴，清除建议并停止处理中状态', () => {
  const store = setup();
  startReply();
  store.appendAgentReply('first', 'local-reply', '请确认差旅申请摘要');
  store.addTimelineItem({ kind: 'tool', id: 'tool', agentName: 'GoGo', title: '提交', status: 'done' });
  store.setSuggestedQuestions(['确认提交']);
  store.failAgentReply('first', 'local-reply', 'TimeoutError');
  const conversation = useChatStore.getState().getCurrentConversation()!;
  const reply = conversation.messages.at(-1)!;
  assert.equal(reply.replyStatus, 'failed');
  assert.equal(reply.content, '请确认差旅申请摘要');
  assert.equal(reply.replyError, 'TimeoutError');
  assert.equal(reply.timeline?.length, 1);
  assert.equal(conversation.isThinking, false);
  assert.deepEqual(conversation.suggestedQuestions, []);
  assert.deepEqual(conversation.timeline, []);
  store.appendAgentReply('first', 'local-reply', '迟到的成功摘要');
  assert.equal(useChatStore.getState().getCurrentConversation()!.messages.at(-1)!.content, reply.content);
});

test('只有持久化 message_id 才能完成，后续辅助流失败不使已保存回复失效', () => {
  const store = setup();
  startReply();
  store.appendAgentReply('first', 'local-reply', '待确认摘要');
  store.completeAgentReply('first', 'local-reply', 'invalid-id');
  assert.equal(useChatStore.getState().getCurrentConversation()!.messages.at(-1)!.replyStatus, 'streaming');
  store.completeAgentReply('first', 'local-reply', 'msg_saved');
  store.failAgentReply('first', 'local-reply', '建议流断开');
  const reply = useChatStore.getState().getCurrentConversation()!.messages.at(-1)!;
  assert.equal(reply.id, 'msg_saved');
  assert.equal(reply.replyStatus, 'completed');
  assert.equal(reply.replyError, undefined);
});

test('失败回复不能因迟到的 message_id 恢复为成功', () => {
  const store = setup();
  startReply();
  store.failAgentReply('first', 'local-reply', '未收到保存确认');
  store.completeAgentReply('first', 'local-reply', 'msg_late');
  assert.equal(useChatStore.getState().getCurrentConversation()!.messages.at(-1)!.replyStatus, 'failed');
});

test('切换会话后只结束原回复，旧回调不会影响新会话或同会话的新一轮', () => {
  const store = setup();
  startReply('old-reply');
  store.switchConversation('second');
  startReply('new-reply');
  store.failAgentReply('first', 'old-reply', '已中断');
  assert.equal(useChatStore.getState().getCurrentConversation()!.isThinking, true);
  assert.equal(useChatStore.getState().getCurrentConversation()!.messages.at(-1)!.replyStatus, 'streaming');
  store.switchConversation('first');
  startReply('next-reply');
  store.failAgentReply('first', 'old-reply', '迟到的错误');
  assert.equal(useChatStore.getState().getCurrentConversation()!.messages.at(-1)!.replyStatus, 'streaming');
  assert.equal(useChatStore.getState().getCurrentConversation()!.isThinking, true);
});
