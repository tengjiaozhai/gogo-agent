"""改口词守卫的规则层评测：只测关键词判定，不调用模型，不读写 ReMe。

守卫定义见 docs/research/长期记忆整理机制-调研与实施方案-P0至P4.md §五 P2：
只有用户本轮原话含改口词时才允许 REPLACE。本脚本用标注集量化它的漏判率与误判率。
词表与判定直接导入生产模块，测的就是线上行为，不是副本。
纯标准库，任意 Python 3 环境可直接运行：python3 evals/auto_memory/run_cue_guard.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gogo_agent.context.cue_guard import CUE_TERMS, guard_fires  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "cases" / "cue_guard.jsonl"
REQUIRED_FIELDS = ("id", "category", "scope", "label", "existing", "utterance")
LABELS = ("replace", "keep", "ambiguous")
SCOPES = ("same_turn", "cross_turn")


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    """读取并校验标注集；字段缺失、取值非法或 id 重复时直接报错，不静默跳过。"""
    cases: list[dict] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        case = json.loads(raw)
        where = f"{path.name}:{lineno}"
        missing = [field for field in REQUIRED_FIELDS if field not in case]
        if missing:
            raise ValueError(f"{where} 缺少字段 {missing}")
        if case["label"] not in LABELS or case["scope"] not in SCOPES:
            raise ValueError(f"{where} label 或 scope 取值非法")
        if case["scope"] == "cross_turn" and not case.get("previous_utterance"):
            raise ValueError(f"{where} cross_turn 必须提供 previous_utterance")
        if case["id"] in seen:
            raise ValueError(f"{where} id 重复：{case['id']}")
        seen.add(case["id"])
        cases.append(case)
    return cases


def _rate(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def report(cases: list[dict]) -> None:
    """打印主指标、分类别结果、词项诊断与明细。"""
    main = [c for c in cases if c["scope"] == "same_turn" and c["label"] in ("replace", "keep")]
    replace = [c for c in main if c["label"] == "replace"]
    keep = [c for c in main if c["label"] == "keep"]
    misses = [c for c in replace if not guard_fires(c["utterance"])]
    false_triggers = [c for c in keep if guard_fires(c["utterance"])]

    print("== 改口词守卫（规则层）==")
    print(f"样例集：{len(cases)} 条（作者起草，未复核；困难样例为主，汇总率不代表线上分布）")
    print(f"基线关键词：{' / '.join(CUE_TERMS)}")
    print()
    print("主指标（same_turn，不含 ambiguous 与 cross_turn）")
    print(f"  应替换 replace：{len(replace)} 条，漏判 {len(misses)}（守卫未放行），漏判率 {_rate(len(misses), len(replace))}")
    print(f"  不应替换 keep ：{len(keep)} 条，误判 {len(false_triggers)}（守卫放行），误判率 {_rate(len(false_triggers), len(keep))}")

    print()
    print("分类别（守卫放行 / 样例数）")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for case in main:
        grouped[case["category"]].append(case)
    for category, items in grouped.items():
        fired = sum(guard_fires(c["utterance"]) for c in items)
        print(f"  {category:<36}{items[0]['label']:<9}{fired}/{len(items)}")

    print()
    print("词项诊断（同一句可含多个词，按句计数）")
    for term in CUE_TERMS:
        in_replace = sum(term in c["utterance"] for c in replace)
        in_keep = sum(term in c["utterance"] for c in keep)
        print(f"  {term}：应替换句含该词 {in_replace}，不应替换句含该词 {in_keep}")

    print()
    print("漏判明细（应替换，守卫未放行）")
    for case in misses:
        print(f"  {case['id']} [{case['existing']}] {case['utterance']}")
    print("误判明细（不应替换，守卫放行）")
    for case in false_triggers:
        print(f"  {case['id']} [{case['existing']}] {case['utterance']}")

    ambiguous = [c for c in cases if c["label"] == "ambiguous"]
    cross = [c for c in cases if c["scope"] == "cross_turn"]
    fired_ambiguous = sum(guard_fires(c["utterance"]) for c in ambiguous)
    print()
    print(f"ambiguous（不计入主指标）：{len(ambiguous)} 条，守卫放行 {fired_ambiguous} 条")
    print(f"cross_turn（守卫只看本轮，不计入主指标）：{len(cross)} 条")
    for case in cross:
        outcome = "放行" if guard_fires(case["utterance"]) else "未放行"
        print(f"  {case['id']} label={case['label']} 守卫{outcome} | {case.get('note', '')}")


def main() -> None:
    report(load_cases())


if __name__ == "__main__":
    main()
