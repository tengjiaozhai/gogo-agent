"""自动记忆模型层评测：动作判定一致率、REPLACE 守卫漏判、输出截断。

默认**不调用网关**：只打印数据集规模与将执行的检查项，供复核数据集用。
加 `--run` 才真正调用模型（会产生费用），需要 .env 里的网关配置。

用法：
    python3 evals/auto_memory/run_model_eval.py              # 空跑
    python3 evals/auto_memory/run_model_eval.py --run        # 调用模型
    python3 evals/auto_memory/run_model_eval.py --run --only actions

数据集先由人工标注期望动作，脚本只比对，不让模型自评。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

ACTIONS_PATH = EVAL_DIR / "cases" / "actions.jsonl"
CONFLICT_PATH = EVAL_DIR / "cases" / "conflict_notice.jsonl"

ACTION_KINDS = ("ADD", "NOOP", "REFINE", "REPLACE", "NONE", "AMBIGUOUS")
# NONE（无动作）与 NOOP（引用某条但内容不变）对记忆状态的效果相同，主指标按等价处理。
NO_CHANGE = frozenset(("NONE", "NOOP"))
# Q24 门槛。门槛是设计提议，无数据依据；改动门槛须同时说明理由。
GATE_CONSISTENCY = 0.85
GATE_FALSE_REPLACE = 0.05
GATE_TRUNCATION = 0

# Q22/Q23 的候选提示词条款：只存在于评测脚本，验证通过后才在步骤 10 落入 chat/config.py。
# 评测必须测候选文本；用尚未修改的生产提示词去测，门槛永远不会通过，测的也不是候选方案。
CANDIDATE_PRECEDENCE_CLAUSE = (
    "【长期记忆优先级】自动记忆是系统从过往对话推断出的内容，可能与本人显式偏好不一致。"
    "两者冲突时，一律以【本人显式偏好】为准，自动记忆仅作参考，不得据此给出与偏好相反的结论。"
    "发现冲突时应明确告知用户“自动记忆与偏好设置不一致，以偏好设置页为准”，并提示去偏好设置页查看或修改。"
    "用户在对话中提出与显式偏好相反的新偏好时，不要记录，也不要声称已记录或已更新偏好；"
    "应说明该设置以偏好设置页为准，请在该页面修改。"
    "自动记忆与显式偏好主题不同、或二者一致时，不要提及冲突，正常回答。"
    "任何情况下都不要说“已记录”“已保存”“已更新偏好”这类表述：对话不会修改偏好设置，"
    "唯一生效的入口是偏好设置页；即使内容与现有设置一致，也不要声称替你记录或保存了偏好。"
)


def _load(path: Path, *, required: tuple[str, ...]) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"缺少数据集：{path}")
    cases: list[dict] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        case = json.loads(raw)
        missing = [field for field in required if field not in case]
        if missing:
            raise SystemExit(f"{path.name}:{lineno} 缺少字段 {missing}")
        if case["id"] in seen:
            raise SystemExit(f"{path.name}:{lineno} id 重复：{case['id']}")
        seen.add(case["id"])
        cases.append(case)
    return cases


def _summarize_actions(cases: list[dict]) -> dict:
    labels = Counter(case["expected"] for case in cases)
    non_replace = [c for c in cases if c["expected"] != "REPLACE"]
    guarded = [c for c in non_replace if not _cue(c["utterance"])]
    return {
        "total": len(cases),
        "by_label": dict(labels),
        "non_replace": len(non_replace),
        "non_replace_without_cue": len(guarded),
    }


def _cue(utterance: str) -> bool:
    from gogo_agent.context.cue_guard import guard_fires
    return guard_fires(utterance)


def _print_actions_preview(cases: list[dict]) -> None:
    summary = _summarize_actions(cases)
    print(f"动作数据集：{summary['total']} 条，期望动作分布 {summary['by_label']}")
    print(f"  非 REPLACE {summary['non_replace']} 条（其中无改口词 {summary['non_replace_without_cue']} 条，"
          f"用于测守卫漏判）")
    print(f"  门槛：一致率 ≥ {GATE_CONSISTENCY:.0%}；非 REPLACE 判成 REPLACE ≤ {GATE_FALSE_REPLACE:.0%}；"
          f"截断 = {GATE_TRUNCATION}")
    print("  空跑模式：未调用网关。加 --run 执行。")


async def _run_actions(cases: list[dict], *, timeout_seconds: float) -> int:
    """逐条调用 decide_actions，比对期望动作；返回进程退出码。"""
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel

    from gogo_agent.chat.config import load_chat_agent_settings
    from gogo_agent.context import memory_actions as ma
    from gogo_agent.intent.runtime import load_intent_runtime_settings
    from gogo_agent.model import create_chat_model

    gateway = load_intent_runtime_settings()
    settings = load_chat_agent_settings()
    model = create_chat_model(
        OpenAICredential(api_key=gateway.api_key, base_url=gateway.base_url),
        settings.recommendation_model_name,
        role="recommendation",
        stream=False,
        timeout_seconds=timeout_seconds,
        parameters=OpenAIChatModel.Parameters(thinking_enable=False),
    )

    matched = 0
    matched_known = 0
    known_total = 0
    per_label: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    false_replace = 0
    downgraded = 0
    truncations = 0
    failures: list[str] = []
    try:
        for case in cases:
            entries = [ma.Entry(entry_id=e["id"], fact=e["fact"], day=e.get("day"))
                       for e in case.get("existing", [])]
            decision = await ma.decide_actions(
                model, entries, case["utterance"], case.get("answer", ""),
                timeout_seconds=timeout_seconds,
            )
            if decision.error in ("truncated", "timeout"):
                truncations += 1
            raw = _first_kind(decision)
            # 按设计模拟代码层的降级：守卫未放行时 REPLACE 变成 ADD。
            actual = "ADD" if raw == "REPLACE" and not _cue(case["utterance"]) else raw
            downgraded += int(raw == "REPLACE" and actual == "ADD")
            expected = case["expected"]
            if expected == "AMBIGUOUS":
                continue  # 事后标注的样例只作参考，不进主指标
            ok = actual == expected or (actual in NO_CHANGE and expected in NO_CHANGE)
            known_miss = bool(case.get("known_miss"))
            if known_miss:
                known_total += 1
                matched_known += int(ok)
            # Q24：分母是"守卫放行的非 REPLACE 样例"，未放行的样例不可能判成 REPLACE。
            if expected not in ("REPLACE", *NO_CHANGE) and _cue(case["utterance"]) and actual == "REPLACE":
                false_replace += 1
            per_label[expected][0] += int(ok)
            per_label[expected][1] += 1
            matched += int(ok)
            if not ok:
                failures.append(
                    f"  {case['id']} 期望 {expected} 实际 {actual}"
                    f"（模型原始 {raw}）{' [已知漏判]' if known_miss else ''} | {case['utterance']}"
                )
    finally:
        client = getattr(model, "client", None)
        if client is not None:
            closed = client.close()
            if hasattr(closed, "__await__"):
                await closed

    scored = [c for c in cases if c["expected"] != "AMBIGUOUS"]
    total = len(scored)
    consistency = matched / total if total else 0.0
    strict_total = total - known_total
    strict = (matched - matched_known) / strict_total if strict_total else 0.0
    non_replace = [c for c in scored if c["expected"] not in ("REPLACE", *NO_CHANGE)]
    guarded_non_replace = [c for c in non_replace if _cue(c["utterance"])]

    print(f"动作一致率 {consistency:.1%}（{matched}/{total}）门槛 {GATE_CONSISTENCY:.0%}")
    print(f"  排除已知漏判后 {strict:.1%}（{matched - matched_known}/{strict_total}）"
          f"——已知漏判 {known_total} 条按 Q12 记录，不计入该项")
    if total != len(cases):
        print(f"  另 {len(cases) - total} 条事后标注为 ambiguous，不计入")
    for label, (ok, count) in sorted(per_label.items()):
        print(f"  {label:<10}{ok}/{count}")
    print(f"守卫降级 REPLACE→ADD：{downgraded} 次（跨轮改口的已知漏判会落在这里）")
    false_rate = false_replace / len(guarded_non_replace) if guarded_non_replace else 0.0
    print(f"非 REPLACE 经守卫后仍判 REPLACE：{false_replace}/{len(guarded_non_replace)}"
          f" = {false_rate:.1%}（门槛 ≤ {GATE_FALSE_REPLACE:.0%}）")
    print(f"截断/超时：{truncations}（门槛 {GATE_TRUNCATION}）")
    if failures:
        print("不一致明细：")
        print("\n".join(failures))

    passed = (consistency >= GATE_CONSISTENCY and false_rate <= GATE_FALSE_REPLACE
              and truncations <= GATE_TRUNCATION)
    print("结论：通过" if passed else "结论：未通过门槛，按计划停在接线之前")
    return 0 if passed else 1


def _first_kind(decision) -> str:
    """取本轮第一个动作作为对比目标；空决策归一为 NONE，与数据集口径一致。"""
    return decision.actions[0].kind if decision.actions else "NONE"


async def _run_conflict(cases: list[dict], *, timeout_seconds: float, samples: int = 1) -> int:
    """主模型冲突提示验证：只装配主提示词与显式偏好，不带工具、不经完整 Agent 循环。

    模型对同一输入的回答会漂移，单次采样在 6 条样例上不构成可靠估计；
    `samples` > 1 时按样本汇总，并报出逐条命中次数。
    """
    from agentscope.credential import OpenAICredential
    from agentscope.message import SystemMsg, TextBlock, UserMsg
    from agentscope.model import OpenAIChatModel, ChatResponse

    from gogo_agent.chat.config import master_system_prompt
    from gogo_agent.intent.runtime import load_intent_runtime_settings
    from gogo_agent.model import create_chat_model

    gateway = load_intent_runtime_settings()
    model = create_chat_model(
        OpenAICredential(api_key=gateway.api_key, base_url=gateway.base_url),
        gateway.chat_model_name,
        role="master",
        stream=False,
        timeout_seconds=timeout_seconds,
        # 低温度降低单次采样的措辞漂移；仍非确定性，结论以重复运行为准。
        parameters=OpenAIChatModel.Parameters(thinking_enable=False, temperature=0.0),
    )

    hits: dict[str, int] = {case["id"]: 0 for case in cases}
    confirmed = errors = truncations = 0
    details: list[str] = []
    try:
        for case in cases:
            messages = [
                SystemMsg(name="system", content=master_system_prompt(
                    "普通对话", info_tool_enabled=False) + "\n" + CANDIDATE_PRECEDENCE_CLAUSE),
                UserMsg(name="user", content=(
                    f"【本人显式偏好】{case.get('preference') or '（未设置）'}\n"
                    f"【自动记忆】{case['memory']}\n\n{case['utterance']}"
                )),
            ]
            for _ in range(samples):
                response = None
                for _ in range(2):  # 网络抖动重试一次，仍失败才计为未提示
                    try:
                        candidate = await model(messages, tools=None, tool_choice=None, max_tokens=800)
                    except Exception:
                        continue
                    if isinstance(candidate, ChatResponse) and candidate.is_last:
                        response = candidate
                        break
                if response is None:
                    errors += 1
                    details.append(f"  {case['id']} 调用失败（已重试）")
                    continue
                reason = getattr(response.finished_reason, "value", response.finished_reason)
                if reason in ("length", "max_tokens"):
                    truncations += 1
                text = "".join(
                    block.text for block in response.content if isinstance(block, TextBlock)
                )
                confirmed += int(any(
                    word in text for word in ("已记录", "已保存", "已更新偏好", "已为您记下")))
                if any(hint in text for hint in case["expect_hints"]):
                    hits[case["id"]] += 1
                elif samples == 1:
                    details.append(f"  {case['id']} 未命中：{text[:200]}")
    finally:
        client = getattr(model, "client", None)
        if client is not None:
            closed = client.close()
            if hasattr(closed, "__await__"):
                await closed

    should = [c for c in cases if c["should_notice"]]
    should_not = [c for c in cases if not c["should_notice"]]
    # 逐条命中数除以采样次数，再在组内取平均：避免失败调用被当作"未提示"污染分母。
    rate = {
        case["id"]: hits[case["id"]] / samples for case in cases
    }
    notice_rate = sum(rate[c["id"]] for c in should) / len(should) if should else 0.0
    false_positive = sum(rate[c["id"]] for c in should_not) / len(should_not) if should_not else 0.0
    print(f"应提示提示率 {notice_rate:.1%}（门槛 ≥ 90%，{samples} 次采样平均）")
    print(f"不应提示误提示率 {false_positive:.1%}（门槛 ≤ 10%）")
    print(f"确认类表述（已记录等）出现 {confirmed} 次（门槛 0）")
    print(f"输出被截断 {truncations} 次，调用失败 {errors} 次（失败不计入分母，但会拉低命中数）")
    unstable = [cid for cid, count in hits.items() if 0 < count < samples]
    if unstable:
        print(f"逐条不稳定（同一样例跨采样结果不一致）：{', '.join(unstable)}")
        print("  → 这类样例说明该场景不是确定性行为，单次运行的结论不可复现")
    if details:
        print("\n".join(details))
    passed = notice_rate >= 0.9 and false_positive <= 0.1 and confirmed == 0
    print("结论：通过" if passed else "结论：未通过门槛")
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="自动记忆模型层评测")
    parser.add_argument("--run", action="store_true", help="真正调用网关（产生费用）")
    parser.add_argument("--only", choices=("actions", "conflict"), help="只跑其中一组")
    parser.add_argument("--timeout", type=float, default=20.0, help="单次调用超时秒数")
    parser.add_argument("--samples", type=int, default=3,
                        help="冲突提示每条采样次数；模型措辞会漂移，1 次不足以代表行为")
    args = parser.parse_args()

    cases = _load(ACTIONS_PATH, required=("id", "expected", "utterance"))
    for case in cases:
        if case["expected"] not in ACTION_KINDS:
            raise SystemExit(f"{case['id']} expected 取值非法：{case['expected']}")

    if not args.run:
        _print_actions_preview(cases)
        if CONFLICT_PATH.exists():
            conflict = _load(CONFLICT_PATH, required=("id", "should_notice", "memory", "utterance"))
            print(f"冲突提示数据集：{len(conflict)} 条，其中应提示 {sum(c['should_notice'] for c in conflict)} 条")
        return 0

    # 与生产同一条路径读取 .env，避免评测另有一套配置发现方式。
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")

    if not os.getenv("GOGO_MODEL_NAME"):
        raise SystemExit("--run 需要 GOGO_MODEL_NAME 等网关配置；未配置时保持空跑")

    code = 0
    if args.only in (None, "actions"):
        code |= asyncio.run(_run_actions(cases, timeout_seconds=args.timeout))
    if args.only in (None, "conflict") and CONFLICT_PATH.exists():
        code |= asyncio.run(_run_conflict(
            _load(CONFLICT_PATH, required=("id", "should_notice", "memory", "utterance")),
            timeout_seconds=args.timeout,
            samples=args.samples,
        ))
    return code


if __name__ == "__main__":
    sys.exit(main())
