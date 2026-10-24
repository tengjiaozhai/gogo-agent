"""013 Java L0/L1 规则及 015 未归类多动作子句的保守弃权。"""

from dataclasses import dataclass
import re

from .models import (
    ConfidenceLevel,
    FastMatch,
    IntentCandidate,
    IntentCategory,
    IntentItem,
    IntentResult,
    MatchStatus,
    QueryInput,
    RecognitionLayer,
)


_STRONG_CONJUNCTIONS = (
    r"然后|接着|顺便|顺带|以及|并且|另外|同时|完了再|之后再|再帮我|再给我|外加"
)
_STRONG_PATTERN = re.compile(_STRONG_CONJUNCTIONS)
_CLAUSE_SPLITTER = re.compile(
    r"[，。；！？!?;,、]|" + _STRONG_CONJUNCTIONS
    + r"|并|再(?=查|看|找|规划|安排|申请|报销|取消|修改|订)|还要|还想|再帮|再给|再查|再订|再看|和|跟"
)
_ACTION_PATTERN = re.compile(
    r"(?P<query>查询|查看|查|看|找)|(?P<plan>规划|安排)"
    r"|(?P<apply>申请|提交|发起)|(?P<reimburse>报销)"
    r"|(?P<cancel>取消|撤回)|(?P<modify>修改|变更|改签|退票)"
    r"|(?P<book>预[定订]|下单|订(?!单))"
    r"|(?P<select>(?:选择|选中|选用|就选|我选择|我选)(?=.{0,100}(?:方案|计划)))"
)
_BARE_CONFIRMATION = re.compile(r"(?:请帮我|请|帮我|我)?(?:确认|确定|同意|好的|好|是的)(?:提交)?|(?:请|帮我)?提交")
_BARE_PLAN_SELECTION = re.compile(
    r"(?:选择|选中|选用|就选|我选择|我选)(?:(?:行程|出行)的?)?"
    r"(?:综合最佳|价格最低|时间最短|最符合偏好|第[一二三四五\d]+[个种套]?|这个|该|上述|以上)?"
    r"(?:行程|出行)?(?:方案|计划)"
)
_QUESTION_PATTERN = re.compile(r"多少|怎么|如何|哪些|哪家|哪里|哪儿|是否|能否|[吗么？?]")
_NEGATED_ACTION_PREFIXES = (
    "不想", "不要", "不用", "无需", "不需", "不需要", "不必", "请勿", "不打算", "不准备", "不能", "不再", "别", "不",
)
_NEGATED_ACTION_PATTERN = re.compile(
    rf"(?:{'|'.join(re.escape(prefix) for prefix in _NEGATED_ACTION_PREFIXES)})"
    rf"(?=(?:{_ACTION_PATTERN.pattern}))"
    r"[^，。；！？!?;,、]*"
)
_DOUBLE_NEGATED_ACTION_PATTERN = re.compile(
    rf"(?:不能|不可|不该|不得|不应)不(?=(?:{_ACTION_PATTERN.pattern}))"
)
_NEGATED_THEN_AFFIRMATIVE = re.compile(
    rf"但(?:是)?(?=[^，。；！？!?;,、]{{0,3}}(?:{_ACTION_PATTERN.pattern}))"
)


def _affirmative_text(text: str) -> str:
    """仅保留肯定请求；否定动作所在子句不参与快速层分类。"""
    text = _NEGATED_THEN_AFFIRMATIVE.sub("，", text)
    double_negations = [match.span() for match in _DOUBLE_NEGATED_ACTION_PATTERN.finditer(text)]

    def retain_or_remove(match: re.Match[str]) -> str:
        if any(start <= match.start() < end for start, end in double_negations):
            return match.group(0)
        return ""

    return _NEGATED_ACTION_PATTERN.sub(retain_or_remove, text)


def requires_action_context(text: str) -> bool:
    """确认缺少对象或只指向某个方案时先承接历史，不把方案名当提交对象。"""
    text = _affirmative_text(text).strip(" ，。；！？!?;,. ")
    if _BARE_CONFIRMATION.fullmatch(text):
        return True
    action = _ACTION_PATTERN.search(text)
    if action is None or action.start() > 5:
        return False
    if action.lastgroup == "select":
        label = re.split(r"[（(]", text[action.start():], maxsplit=1)[0].strip()
        return re.search(r"(?:行程|出行).{0,12}(?:方案|计划)", text) is None or bool(_BARE_PLAN_SELECTION.fullmatch(label))
    return bool(
        action.lastgroup == "apply"
        and re.search(r"方案|计划|这个|这份|上述|以上", text)
        and not re.search(r"差旅|出差|申请|报销|审批|订单", text)
    )


_GREET = (
    r"你好|您好|哈喽|哈啰|嗨|hi|hello|hey|早上好|早安|上午好|中午好|下午好|晚上好"
    r"|在吗|在不在|在么|在不|有人吗|有人在吗|你在吗|请问|请教一下|打扰一下|打扰了|方便吗"
)
_GREETING_PATTERN = rf"^(?:{_GREET})(?:[\s,，。.!！?？～~、]*(?:{_GREET}))*[\s,，。.!！?？～~、]*$"

# 此映射仅给 L1 判断不同职责组的复合歧义使用，不是业务 Agent 调度表。
_TARGET_GROUP = {
    IntentCategory.TRAVEL_APPLICATION: "manage",
    IntentCategory.TRAVEL_CANCEL: "manage",
    IntentCategory.TRAVEL_MODIFY: "manage",
    IntentCategory.APPROVAL_QUERY: "manage",
    IntentCategory.TRAVEL_ORDER_QUERY: "manage",
    IntentCategory.ITINERARY_PLANNING: "plan",
    IntentCategory.FLIGHT_SEARCH: "plan",
    IntentCategory.TRAIN_SEARCH: "plan",
    IntentCategory.HOTEL_SEARCH: "plan",
    IntentCategory.BOOKING: "booking",
    IntentCategory.REIMBURSEMENT: "reimbursement",
    IntentCategory.POLICY_QUERY: "info",
    IntentCategory.ATTRACTIONS_QUERY: "info",
    IntentCategory.GENERAL_INFO: "info",
    IntentCategory.GREETING: "master",
    IntentCategory.UNKNOWN: "master",
}


@dataclass(frozen=True)
class _Rule:
    """单条意图快速匹配规则。"""

    category: IntentCategory  # 意图类别：规则匹配成功后归属的目标业务意图
    keyword: re.Pattern[str]  # 正向匹配模式：文本命中该正则表达式时初步满足该规则
    negative_keywords: tuple[re.Pattern[str], ...]  # 负向排除模式元组：文本命中任一排除正则时立即一票否决弃权


def _rule(category: IntentCategory, keyword: str, *negative_keywords: str) -> _Rule:
    flags = re.IGNORECASE
    return _Rule(
        category=category,
        keyword=re.compile(keyword, flags),
        negative_keywords=tuple(re.compile(value, flags) for value in negative_keywords),
    )


# 顺序即 Java L1 优先级；两条规划规则刻意相邻，未知意图不设置关键词规则。
_RULES = (
    _rule(IntentCategory.GREETING, _GREETING_PATTERN),
    _rule(
        IntentCategory.REIMBURSEMENT,
        r"(报销|报账|报帐|贴票|发票|报销单|费用报销|差旅报销|出差费用|生成报销单|识别发票|发票识别|提交报销|报一下|帮我报|报个销|走报销|电子发票|机票行程单)",
        "政策", "标准", "规定", "制度", "额度", "限额", "能不能报", "能报吗", "报销吗", "可以报", "怎么报", "报销范围", "报销比例",
    ),
    _rule(
        IntentCategory.POLICY_QUERY,
        r"(差旅政策|差旅规定|差旅制度|差旅标准|差标|超标|餐标|餐费标准|住宿标准|酒店标准|机票标准|舱位标准|高铁标准|座位标准|费用标准|报销标准|报销政策|报销规定|报销额度|报销范围|能不能报|可以报销吗|能报销吗|预[定订]规定|订票规定|购票规定|签证|入境政策|出差政策|出行政策|差旅管理|出差规定|出差标准)",
    ),
    _rule(
        IntentCategory.APPROVAL_QUERY,
        r"(审批进度|审批状态|审批结果|审批通过了?吗?|审批到哪|审批到哪个|审批环节|审批意见|审批人|审批流程|我的审批|审批单状态|批了吗|批没批|审没审|通过了没|领导.*批|谁.*审批)",
    ),
    _rule(
        IntentCategory.TRAVEL_CANCEL,
        r"(取消出差|取消差旅|取消审批|取消我的(差旅|出差)|撤回(差旅|出差|审批)?申请|撤销(差旅|出差|审批)?申请|撤回审批|这次不去了?|不出差了|出差取消了?|把.*(差旅|出差|申请).*撤了?)",
    ),
    _rule(
        IntentCategory.TRAVEL_MODIFY,
        r"((修改|变更).*(差旅|出差|申请|行程|订单)|改期|延期|(差旅|出差).*改一?下?|改一下.*(日期|时间|目的地|行程)|调整.*(日期|时间|行程)|把.*(日期|时间|目的地).*改)",
        "取消", "撤回", "撤销", "改签", "退票", "退订",
    ),
    _rule(
        IntentCategory.TRAVEL_ORDER_QUERY,
        r"(差旅单|出差单|差旅订单|差旅详情|差旅记录|出差记录|我的差旅|我的出差"
        r"|(我的|上次|最近|近期|历史|本周|本月|下周|有|查|看)[^，。；！？、]{0,6}(出差|差旅)(安排|行程)"
        r"|差旅单详情|出差单状态|差旅单状态|上次的?(差旅|出差)|历史(差旅|出差))",
        "提交", "发起", "提个", "新建", "报备", "取消", "规划", "报销", "做一份", "做个", "做一下", "出一份", "方案", "处理", "帮我办", "我要办", "安排一下",
    ),
    _rule(
        IntentCategory.ATTRACTIONS_QUERY,
        r"(有什么好玩|好玩的地方|景点|景区|风景区|名胜|游玩|游览|打卡|必去|必玩|一日游|周边游|当地特色|有什么好吃|美食推荐|特产)",
    ),
    _rule(
        IntentCategory.GENERAL_INFO,
        r"(天气|气温|多少度|冷不冷|热不热|下雨|下雪|限行|路况|堵不堵|怎么去|怎么走|地铁|公交|打车|时差|汇率|新闻|资讯)",
        "机票", "航班", "火车票", "高铁票", "订", r"预[定订]", "报销",
    ),
    _rule(IntentCategory.ITINERARY_PLANNING, r"(安排.*行程|帮我安排一下)", "出差", "差旅"),
    _rule(
        IntentCategory.ITINERARY_PLANNING,
        r"(规划.*行程|做.*行程|行程规划|行程安排|行程方案|出行方案|做一份行程|出一份行程|做个行程|帮我规划|规划一下"
        r"|(?:选择|选中|选用|就选|我选择|我选).{0,100}(?:行程|出行).{0,12}(?:方案|计划))",
    ),
    _rule(
        IntentCategory.FLIGHT_SEARCH,
        r"(查机票|订机票|搜机票|看机票|买机票|机票|航班|飞机票|航班信息|航班时刻|头等舱|经济舱|公务舱|往返机票|单程机票|直飞|廉价航班)",
        "发票", "报销", "标准", "政策", "取消", "退票", "改签", r"预[定订]", "订这个", "订下来", "下单",
    ),
    _rule(
        IntentCategory.TRAIN_SEARCH,
        r"(查火车|订火车|搜火车|看火车|买火车票|高铁|动车|火车票|火车|车次|列车|高铁票|城际|二等座|一等座|商务座)",
        "标准", "政策", "取消", "退票", "改签", r"预[定订]", "订这个", "订下来", "下单",
    ),
    _rule(
        IntentCategory.HOTEL_SEARCH,
        r"(查酒店|订酒店|搜酒店|看酒店|住酒店|附近.*酒店|酒店|住宿|住哪里?|住哪儿|入住|宾馆|民宿|快捷酒店|连锁酒店|标间|大床房)",
        "标准", "政策", "报销", "取消", "退订", r"预[定订]", "订这个", "订下来", "下单",
    ),
    _rule(
        IntentCategory.BOOKING,
        r"(预[定订]|下单|订这个|订下来|就订(这个|它)|帮我订(这个|下)|确认(预[定订]|下单)|改签|退票|退订|取消(预[定订]|订单|机票|酒店|火车票))",
    ),
    _rule(
        IntentCategory.TRAVEL_APPLICATION,
        r"(申请出差|出差申请|申请.{0,10}出差|发起(差旅|出差)|提个.*(出差|申请)|提交.*(出差|差旅|申请)|帮我提.*(出差|申请)|我要出差|我想出差|我需要出差|我要去.*出差|(下周|下个月|明天|后天|下下周).*出差|新建(差旅|出差)|报备出差|出差报备)",
        "审批进度", "审批状态", "审批结果", "取消", "查", "规划", "报销",
    ),
)


def _first_match(text: str) -> tuple[_Rule, str] | None:
    matches: list[tuple[_Rule, re.Match]] = []
    for rule in _RULES:
        positive = rule.keyword.search(text)
        if positive and not any(negative.search(text) for negative in rule.negative_keywords):
            matches.append((rule, positive))
    action = _ACTION_PATTERN.search(text)
    if action is not None and action.start() <= 5 and action.lastgroup != "query":
        # 命中片段必须解释当前动作，后面的酒店/机票名词不能抢占提交或选择。
        for rule, positive in matches:
            if positive.start() <= action.start() < positive.end():
                return rule, positive.group(0)
        if action.lastgroup in {"apply", "select", "plan"}:
            return None
    return (matches[0][0], matches[0][1].group(0)) if matches else None


class IntentRuleMatcher:
    """L0 强连接词弃权；L1 按 Java 顺序处理子句、排除词和跨职责歧义。"""

    def match(self, query: QueryInput) -> FastMatch:
        if not isinstance(query, QueryInput):
            raise TypeError("规则匹配需要经过校验的 QueryInput")
        original = query.question
        if len(original) >= 10:
            conjunction = _STRONG_PATTERN.search(original)
            if conjunction is not None and conjunction.start() >= 4:
                return FastMatch(
                    status=MatchStatus.AMBIGUOUS,
                    result=None,
                    reason=f"L0 在句中发现强连接词「{conjunction.group(0)}」，疑似复合意图",
                )
        if _DOUBLE_NEGATED_ACTION_PATTERN.search(original):
            return FastMatch(
                status=MatchStatus.AMBIGUOUS, result=None,
                reason="L1 出现双重否定动作，规则不裁定其肯定含义；跳过 L2",
            )
        # 否定的动作不是待执行事项；整段否定子句不参与关键词优先级和排除词。
        negated = _NEGATED_ACTION_PATTERN.search(original) is not None
        text = _affirmative_text(original) if negated else original
        if negated and not text.strip(" ，。；！？!?;,、并"):
            return FastMatch(
                status=MatchStatus.AMBIGUOUS, result=None,
                reason="L1 仅有明确否定的动作，不把它当作待办理意图；交给 L3",
            )
        if requires_action_context(text):
            return FastMatch(
                status=MatchStatus.AMBIGUOUS, result=None,
                reason="L1 确认或选择动作缺少明确业务对象，先结合历史改写；不能凭酒店等方案明细分类",
            )
        found: dict[IntentCategory, str] = {}
        action_clauses: list[tuple[str, _Rule | None, str]] = []
        clause_hits: list[tuple[str, _Rule, str, str | None]] = []
        greeting = False
        for index, clause in enumerate(_CLAUSE_SPLITTER.split(text)):
            clause = clause.strip()
            hit = _first_match(clause)
            action = _ACTION_PATTERN.search(clause)
            if index == 0 and hit is None and action is not None and action.lastgroup != "query":
                # “修改一下，我的出差申请”把动作和对象分开了；同一规则可跨逗号绑定。
                full_hit = _first_match(text)
                if full_hit is not None:
                    span = full_hit[0].keyword.search(text)
                    if span.start() <= action.start() < span.end():
                        hit = full_hit
            prefix = clause[max(0, action.start() - 3):action.start()] if action else ""
            if (
                action is not None
                and action.start() <= 3
                and not prefix.endswith(_NEGATED_ACTION_PREFIXES)
            ):
                action_clauses.append((clause, hit[0] if hit else None, action.lastgroup))
            if hit is None:
                continue
            rule, _ = hit
            clause_hits.append((clause, rule, hit[1], action.lastgroup if action else None))
            if rule.category is IntentCategory.GREETING:
                greeting = True
            else:
                found.setdefault(rule.category, clause.strip())
        if greeting and not found and re.fullmatch(_GREETING_PATTERN, text, re.IGNORECASE):
            found[IntentCategory.GREETING] = text

        if any(rule is None and kind in {"apply", "select", "plan"} for _, rule, kind in action_clauses):
            return FastMatch(status=MatchStatus.AMBIGUOUS, result=None,
                             reason="L1 明确动作未能归类，不用背景名词代替动作；跳过 L2")
        if any(rule is not None and kind in {"apply", "select", "plan"} for _, rule, kind in action_clauses):
            # 日期、交通和住宿明细是该事项的参数，独立的查询动作仍参与多意图判断。
            background = {IntentCategory.FLIGHT_SEARCH, IntentCategory.TRAIN_SEARCH, IntentCategory.HOTEL_SEARCH}
            found = {rule.category: phrase for clause, rule, phrase, action_kind in clause_hits
                     if action_kind is not None or rule.category not in background or _QUESTION_PATTERN.search(clause)}

        # L1 不能给未归类的独立动作强行补单标签；同职责组且均已归类仍可快路由。
        if (
            len(action_clauses) >= 2
            and len({kind for _, _, kind in action_clauses}) >= 2
            and any(rule is None for _, rule, _ in action_clauses)
        ):
            return FastMatch(
                status=MatchStatus.AMBIGUOUS,
                result=None,
                candidates=[
                    IntentCandidate(
                        intent=rule.category, layer=RecognitionLayer.RULE, score=None,
                        reason=f"子句命中「{clause}」",
                    )
                    for clause, rule, _ in action_clauses if rule is not None
                ],
                reason="L1 连接词或标点分开的子句含不同动作，至少一项无法由规则归类；跳过 L2",
            )
        if not found:
            return FastMatch(
                status=MatchStatus.AMBIGUOUS if negated else MatchStatus.MISS,
                result=None,
                reason="L1 否定动作后无肯定规则命中，跳过 L2" if negated else "L1 无规则命中",
            )

        if len({_TARGET_GROUP[category] for category in found}) >= 2:
            return FastMatch(
                status=MatchStatus.AMBIGUOUS,
                result=None,
                candidates=[
                    IntentCandidate(
                        intent=category, layer=RecognitionLayer.RULE, score=None,
                        reason=f"子句命中「{clause}」",
                    )
                    for category, clause in found.items()
                ],
                reason="L1 不同职责组的子句同时命中，跳过单意图向量层",
            )

        # 沿用已有类别优先级，但不再用全句名词覆盖子句中已裁定的动作。
        rule = next(rule for rule in _RULES if rule.category in found)
        phrase = found[rule.category]
        reason = (
            ("L1 排除否定动作后，" if negated else "L1 ")
            + f"规则命中 {rule.category.value}，匹配片段「{phrase}」"
        )
        item = IntentItem(
            intent=rule.category,
            confidence=ConfidenceLevel.HIGH,
            reason=reason,
            evidence=[phrase],
        )
        return FastMatch(
            status=MatchStatus.HIT,
            result=IntentResult(
                intents=[item], primary_intent=rule.category,
                multi_intent=False, overall_reason=reason,
            ),
            candidates=[IntentCandidate(
                intent=rule.category, layer=RecognitionLayer.RULE, score=None,
                reason=f"关键词「{phrase}」按规则优先级命中",
            )],
            reason=reason,
        )
