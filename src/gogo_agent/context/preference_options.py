"""差旅偏好选项表：唯一权威来源是 Java 基线 `PreferenceController.buildPreferenceOptions`，文案逐字一致。

分类与 key 一一对应；`type` 为 `single` 时值为单个选项，`multi` 时值为选项子集。
"""
from __future__ import annotations

from typing import Literal, TypedDict


class PreferenceItemSpec(TypedDict):
    key: str
    label: str
    type: Literal["single", "multi"]
    options: list[str]


class PreferenceCategorySpec(TypedDict):
    category: str
    label: str
    icon: str
    items: list[PreferenceItemSpec]


PREFERENCE_OPTIONS: list[PreferenceCategorySpec] = [
    {"category": "flight", "label": "机票偏好", "icon": "✈️", "items": [
        {"key": "flight_cabin", "label": "舱位偏好", "type": "single",
         "options": ["经济舱", "超级经济舱", "公务舱", "头等舱"]},
        {"key": "flight_airline", "label": "偏好航司", "type": "multi",
         "options": ["国航(CA)", "东航(MU)", "南航(CZ)", "海航(HU)", "厦航(MF)", "深航(ZH)", "川航(3U)", "春秋(9C)", "吉祥(HO)", "山航(SC)"]},
        {"key": "flight_seat", "label": "座位位置", "type": "single",
         "options": ["靠窗", "靠过道", "前排", "紧急出口排", "无偏好"]},
        {"key": "flight_time", "label": "航班时间", "type": "single",
         "options": ["早班(6:00-9:00)", "上午(9:00-12:00)", "下午(12:00-18:00)", "晚班(18:00-21:00)", "红眼航班也可以", "无偏好"]},
        {"key": "flight_direct", "label": "中转偏好", "type": "single",
         "options": ["只选直飞", "可接受一次中转", "价格优先不限中转", "无偏好"]},
    ]},
    {"category": "hotel", "label": "酒店偏好", "icon": "🏨", "items": [
        {"key": "hotel_star", "label": "星级偏好", "type": "single",
         "options": ["经济型", "舒适型(三星)", "高档型(四星)", "豪华型(五星)", "无偏好"]},
        {"key": "hotel_brand", "label": "偏好品牌", "type": "multi",
         "options": ["全季", "亚朵", "如家商旅", "汉庭", "维也纳", "桔子", "希尔顿", "万豪", "洲际", "凯悦", "香格里拉", "华住"]},
        {"key": "hotel_room", "label": "房型偏好", "type": "single",
         "options": ["大床房", "双床房", "无偏好"]},
        {"key": "hotel_floor", "label": "楼层偏好", "type": "single",
         "options": ["高楼层", "低楼层(方便出行)", "无偏好"]},
        {"key": "hotel_location", "label": "位置偏好", "type": "multi",
         "options": ["靠近办公/会议地点", "靠近地铁/交通枢纽", "靠近市中心", "安静环境", "有停车场"]},
        {"key": "hotel_facilities", "label": "设施需求", "type": "multi",
         "options": ["健身房", "早餐", "免费Wi-Fi", "商务中心", "洗衣服务", "接机服务"]},
    ]},
    {"category": "train", "label": "高铁/火车偏好", "icon": "🚄", "items": [
        {"key": "train_seat", "label": "座位等级", "type": "single",
         "options": ["二等座", "一等座", "商务座", "无偏好"]},
        {"key": "train_time", "label": "出发时段", "type": "single",
         "options": ["早班(6:00-9:00)", "上午(9:00-12:00)", "下午(12:00-18:00)", "晚班(18:00-21:00)", "无偏好"]},
        {"key": "train_position", "label": "座位位置", "type": "single",
         "options": ["靠窗", "靠过道", "无偏好"]},
    ]},
    {"category": "general", "label": "出行习惯", "icon": "🧳", "items": [
        {"key": "transport", "label": "市内交通", "type": "single",
         "options": ["地铁/公交优先", "打车优先", "自驾/租车", "无偏好"]},
        {"key": "reimburse_priority", "label": "费用敏感度", "type": "single",
         "options": ["价格优先(尽量省钱)", "性价比优先(合理范围选舒适)", "体验优先(预算内最舒适)", "无偏好"]},
        {"key": "schedule_priority", "label": "时间安排", "type": "single",
         "options": ["尽量当天往返", "提前一晚到达", "会议/办事结束当天返回", "灵活安排", "无偏好"]},
        {"key": "meal", "label": "餐饮偏好", "type": "multi",
         "options": ["无特殊要求", "清淡饮食", "素食", "清真", "无辣", "不含海鲜", "无麸质"]},
    ]},
]

SPEC_BY_KEY: dict[str, PreferenceItemSpec] = {
    item["key"]: item for category in PREFERENCE_OPTIONS for item in category["items"]
}
