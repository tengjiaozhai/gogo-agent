import { useState } from 'react';
import type { PlanProgress, PlanStep, PlanTask } from '../types';

// ── Constants ──────────────────────────────────────────────────────────────

/** 一级流水线 Agent，按展示顺序排列 */
const PIPELINE_AGENTS = ['QueryRewritingAgent', 'IntentRecognitionAgent', 'MasterAgent'];

/** 由 MasterAgent 调用的二级子智能体 */
const SUB_AGENT_SET = new Set([
  'ItineraryPlanAgent', 'ItineraryManageAgent', 'BookingAgent', 'ReimbursementAgent', 'InfoAgent',
]);

/** 由 ItineraryPlanAgent 调用的三级子智能体 */
const PLAN_SUB_AGENT_SET = new Set(['ItineraryReviewAgent']);

const AGENT_LABELS: Record<string, string> = {
  MasterAgent:            'GoGo助手',
  QueryRewritingAgent:    '问题改写智能体',
  IntentRecognitionAgent: '意图识别智能体',
  ItineraryPlanAgent:     '行程规划智能体',
  ItineraryReviewAgent:   '行程审核智能体',
  ItineraryManageAgent:  '行程管理智能体',
  BookingAgent:           '预订执行智能体',
  ReimbursementAgent:     '报销处理智能体',
  InfoAgent:              '信息查询智能体',
};

function agentLabel(name: string) {
  return AGENT_LABELS[name] || name;
}

// ── Internal types ─────────────────────────────────────────────────────────

/** MasterAgent 下的有序子项：直接工具调用 或 二级子智能体 */
type MasterChild =
  | { kind: 'tool';     step: PlanStep }
  | { kind: 'subagent'; name: string; agentStep: PlanStep };

// ── PlanTaskList ────────────────────────────────────────────────────────────
// ItineraryPlanAgent plan-and-execute 规划的子任务列表

const TASK_STATE_LABELS: Record<string, string> = {
  todo: '待处理',
  in_progress: '进行中',
  done: '已完成',
  abandoned: '已放弃',
};

function TaskStateIcon({ state }: { state: PlanTask['state'] }) {
  if (state === 'done') return <span className="step-icon done">✓</span>;
  if (state === 'in_progress') return <span className="step-icon spinning" />;
  if (state === 'abandoned') return <span className="plan-task-abandoned">×</span>;
  return <span className="step-icon pending" />;
}

function PlanTaskList({ tasks }: { tasks: PlanTask[] }) {
  if (!tasks || tasks.length === 0) return null;
  return (
    <div className="plan-task-list">
      <div className="plan-task-list-header">
        <svg viewBox="0 0 16 16" fill="none" width="11" height="11" style={{ flexShrink: 0 }}>
          <path d="M3 4h10M3 8h10M3 12h6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        <span>计划任务</span>
      </div>
      {tasks.map((task) => (
        <div
          key={task.idx}
          className={`plan-task-row plan-task-${task.state}`}
        >
          <TaskStateIcon state={task.state} />
          <span className="plan-task-name">{task.name}</span>
          <span className="plan-task-state-badge">{TASK_STATE_LABELS[task.state] ?? task.state}</span>
        </div>
      ))}
    </div>
  );
}

export { PlanTaskList };

// ── ThinkingPanel ──────────────────────────────────────────────────────────

function getPreview(text: string): string {
  const lines = text.split('\n').map((l) => l.trim()).filter(Boolean);
  return lines[lines.length - 1] ?? '';
}

function ThinkingPanel({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const preview = getPreview(text);

  return (
    <div className="thinking-panel">
      <div className="thinking-header" onClick={() => setExpanded((v) => !v)}>
        <svg className="thinking-icon" viewBox="0 0 16 16" fill="none" width="12" height="12">
          <path
            d="M8 2C5.24 2 3 4.02 3 6.5c0 .92.3 1.77.8 2.46L3 13l2.5-1.25c.75.33 1.6.5 2.5.5 2.76 0 5-2.02 5-4.5S10.76 2 8 2z"
            fill="currentColor"
            opacity="0.6"
          />
        </svg>
        <span className="thinking-preview">{preview || '思考中...'}</span>
        <span className={`thinking-chevron${expanded ? '' : ' collapsed'}`}>›</span>
      </div>
      {expanded && <div className="thinking-body">{text}</div>}
    </div>
  );
}

// ── StepStatus ─────────────────────────────────────────────────────────────

function StepStatus({ status }: { status: PlanStep['status'] }) {
  if (status === 'done') return <span className="step-icon done">✓</span>;
  if (status === 'in-progress') return <span className="step-icon spinning" />;
  return <span className="step-icon pending" />;
}

/** 将工具参数 JSON 字符串格式化为可读文本 */
function formatArguments(raw: string): string {
  try {
    const obj = JSON.parse(raw);
    return JSON.stringify(obj, null, 2);
  } catch {
    return raw;
  }
}

// ── ToolRow ────────────────────────────────────────────────────────────────

function ToolRow({ step }: { step: PlanStep }) {
  const [expanded, setExpanded] = useState(false);
  const hasResult = !!(step.result && step.result.trim());
  const hasArgs = !!(step.arguments && step.arguments.trim());
  const expandable = hasResult || hasArgs;

  return (
    <div className="progress-tool-row-wrap">
      <div
        className={`progress-tool-row ${step.status}${expandable ? ' has-result' : ''}`}
        onClick={expandable ? () => setExpanded((v) => !v) : undefined}
      >
        <StepStatus status={step.status} />
        <span className="progress-tool-title">{step.title}</span>
        {expandable && (
          <span className={`tool-result-chevron${expanded ? '' : ' collapsed'}`}>›</span>
        )}
      </div>
      {expandable && expanded && (
        <div className="tool-result-body">
          {hasArgs && (
            <div className="tool-arguments-section">
              <div className="tool-section-label">参数</div>
              <pre>{formatArguments(step.arguments!)}</pre>
            </div>
          )}
          {hasResult && (
            <div className="tool-result-section">
              <div className="tool-section-label">结果</div>
              <pre>{step.result}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── AgentGroup ─────────────────────────────────────────────────────────────
// 用于一级流水线 Agent（QueryRewriting、IntentRecognition）
// 及二级子智能体（isSub=true，挂载在 MasterAgent 下）

function AgentGroup({
  agentName,
  agentStep,
  tools,
  thoughts = [],
  isSub = false,
  planTasks = [],
  subAgents = [],
  allToolsByAgent = {},
  allThinkingByAgent = {},
}: {
  agentName: string;
  agentStep: PlanStep;
  tools: PlanStep[];
  thoughts?: string[];
  isSub?: boolean;
  planTasks?: PlanTask[];
  subAgents?: { name: string; agentStep: PlanStep }[];
  allToolsByAgent?: Record<string, PlanStep[]>;
  allThinkingByAgent?: Record<string, string[]>;
}) {
  const [open, setOpen] = useState(true);
  const nonEmptyThoughts = thoughts.filter((t) => t.trim());
  const hasContent = !!(nonEmptyThoughts.length > 0 || tools.length > 0 || planTasks.length > 0 || subAgents.length > 0);

  return (
    <div className={`progress-agent-group${isSub ? ' sub-agent' : ''}`}>
      <div className="progress-agent-header" onClick={() => setOpen((v) => !v)}>
        <StepStatus status={agentStep.status} />
        <span className="progress-agent-name">{agentLabel(agentName)}</span>
        {hasContent && (
          <span className={`progress-chevron${open ? '' : ' collapsed'}`}>›</span>
        )}
      </div>
      {open && hasContent && (
        <div className="progress-agent-tools">
          {nonEmptyThoughts.map((t, i) => (
            <ThinkingPanel key={i} text={t} />
          ))}
          {tools.map((t) => (
            <ToolRow key={t.id} step={t} />
          ))}
          {subAgents.map((sub) => (
            <AgentGroup
              key={sub.agentStep.id}
              agentName={sub.name}
              agentStep={sub.agentStep}
              tools={allToolsByAgent[sub.name] ?? []}
              thoughts={allThinkingByAgent[sub.name] ?? []}
              isSub
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── MasterAgentGroup ───────────────────────────────────────────────────────
// 一级，但其子项（直接工具 + 二级子智能体）按执行顺序混排

function MasterAgentGroup({
  agentStep,
  masterChildren,
  toolsByAgent,
  thinkingByAgent,
  planTasks,
  planSubAgents,
}: {
  agentStep: PlanStep;
  masterChildren: MasterChild[];
  toolsByAgent: Record<string, PlanStep[]>;
  thinkingByAgent: Record<string, string[]>;
  planTasks: PlanTask[];
  planSubAgents: { name: string; agentStep: PlanStep }[];
}) {
  const [open, setOpen] = useState(true);
  const masterThoughts = (thinkingByAgent['MasterAgent'] ?? []).filter((t) => t.trim());
  const hasContent = !!(masterThoughts.length > 0 || masterChildren.length > 0);

  return (
    <div className="progress-agent-group">
      <div className="progress-agent-header" onClick={() => setOpen((v) => !v)}>
        <StepStatus status={agentStep.status} />
        <span className="progress-agent-name">{agentLabel('MasterAgent')}</span>
        {hasContent && (
          <span className={`progress-chevron${open ? '' : ' collapsed'}`}>›</span>
        )}
      </div>
      {open && hasContent && (
        <div className="progress-agent-tools">
          {masterThoughts.map((t, i) => (
            <ThinkingPanel key={i} text={t} />
          ))}
          {masterChildren.map((child) => {
            if (child.kind === 'tool') {
              return <ToolRow key={child.step.id} step={child.step} />;
            }
            // 二级子智能体
            return (
              <AgentGroup
                key={child.agentStep.id}
                agentName={child.name}
                agentStep={child.agentStep}
                tools={toolsByAgent[child.name] ?? []}
                thoughts={thinkingByAgent[child.name] ?? []}
                planTasks={child.name === 'ItineraryPlanAgent' ? planTasks : []}
                subAgents={child.name === 'ItineraryPlanAgent' ? planSubAgents : []}
                allToolsByAgent={toolsByAgent}
                allThinkingByAgent={thinkingByAgent}
                isSub
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── ProgressCard (main) ────────────────────────────────────────────────────

export default function ProgressCard({
  progress,
  thinkingByAgent = {},
  planTasks = [],
}: {
  progress: PlanProgress;
  thinkingByAgent?: Record<string, string[]>;
  planTasks?: PlanTask[];
}) {
  const { steps } = progress;
  if (!steps || steps.length === 0) return null;

  // 按 agentName 归组所有工具步骤
  const toolsByAgent: Record<string, PlanStep[]> = {};
  steps
    .filter((s) => s.id.startsWith('tool_'))
    .forEach((s) => {
      const key = s.agentName ?? 'unknown';
      (toolsByAgent[key] ??= []).push(s);
    });

  // 按实际执行顺序构建 MasterAgent 的子项列表（直接工具 + 二级子智能体交织）
  const masterChildren: MasterChild[] = [];
  const seenSubAgents = new Set<string>();
  steps.forEach((step) => {
    if (step.id.startsWith('tool_') && step.agentName === 'MasterAgent') {
      masterChildren.push({ kind: 'tool', step });
    } else if (step.id.startsWith('agent_')) {
      const name = step.id.replace('agent_', '');
      if (SUB_AGENT_SET.has(name) && !seenSubAgents.has(name)) {
        seenSubAgents.add(name);
        masterChildren.push({ kind: 'subagent', name, agentStep: step });
      }
    }
  });

  // 构建 ItineraryPlanAgent 下的三级子智能体列表
  const planSubAgents: { name: string; agentStep: PlanStep }[] = [];
  steps.forEach((step) => {
    if (step.id.startsWith('agent_')) {
      const name = step.id.replace('agent_', '');
      if (PLAN_SUB_AGENT_SET.has(name)) {
        planSubAgents.push({ name, agentStep: step });
      }
    }
  });

  // 按固定顺序取一级流水线 Agent 步骤（存在才渲染）
  const pipelineAgentSteps = PIPELINE_AGENTS
    .map((name) => steps.find((s) => s.id === `agent_${name}`))
    .filter((s): s is PlanStep => s !== undefined);

  // 当没有任何流水线 Agent （continuation 直连子智能体），直接展示所有 agent_ 步骤
  const directAgentSteps = pipelineAgentSteps.length === 0
    ? steps.filter((s) => s.id.startsWith('agent_'))
    : [];

  const doneCount = steps.filter((s) => s.status === 'done').length;
  const inProgress = steps.some((s) => s.status === 'in-progress');

  return (
    <div className="progress-card">
      {/* 卡片标题 */}
      <div className="progress-card-header">
        <div className="progress-card-avatar">
          <svg viewBox="0 0 20 20" fill="none" width="16" height="16">
            <circle cx="10" cy="10" r="8" stroke="currentColor" strokeWidth="1.5" />
            <path
              d="M7 10l2 2 4-4"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <span className="progress-card-label">GoGo智能差旅智能体</span>
        {inProgress ? (
          <span className="progress-status-badge working">处理中</span>
        ) : (
          <span className="progress-status-badge done">
            已完成 {doneCount}/{steps.length}
          </span>
        )}
      </div>

      {/* 三级进度树 */}
      <div className="progress-steps">
        {pipelineAgentSteps.length > 0 ? (
          // 正常流水线渲染
          pipelineAgentSteps.map((agentStep) => {
            const name = agentStep.id.replace('agent_', '');

            if (name === 'MasterAgent') {
              return (
                <MasterAgentGroup
                  key={agentStep.id}
                  agentStep={agentStep}
                  masterChildren={masterChildren}
                  toolsByAgent={toolsByAgent}
                  thinkingByAgent={thinkingByAgent}
                  planTasks={planTasks}
                  planSubAgents={planSubAgents}
                />
              );
            }

            // 一级流水线 Agent（QueryRewriting / IntentRecognition）
            return (
              <AgentGroup
                key={agentStep.id}
                agentName={name}
                agentStep={agentStep}
                tools={toolsByAgent[name] ?? []}
                thoughts={thinkingByAgent[name] ?? []}
              />
            );
          })
        ) : (
          // continuation 直连子智能体：无流水线时直接渲染 agent 节点（过滤掉三级子智能体）
          directAgentSteps
            .filter((s) => !PLAN_SUB_AGENT_SET.has(s.id.replace('agent_', '')))
            .map((agentStep) => {
            const name = agentStep.id.replace('agent_', '');
            return (
              <AgentGroup
                key={agentStep.id}
                agentName={name}
                agentStep={agentStep}
                tools={toolsByAgent[name] ?? []}
                thoughts={thinkingByAgent[name] ?? []}
                planTasks={name === 'ItineraryPlanAgent' ? planTasks : []}
                subAgents={name === 'ItineraryPlanAgent' ? planSubAgents : []}
                allToolsByAgent={toolsByAgent}
                allThinkingByAgent={thinkingByAgent}
              />
            );
          })
        )}
      </div>
    </div>
  );
}
