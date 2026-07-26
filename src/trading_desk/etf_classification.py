from __future__ import annotations

from dataclasses import dataclass


CLASSIFICATION_VERSION = "2026-07-v3"
CLASSIFICATION_EFFECTIVE_FROM = "2026-07-26"


@dataclass(frozen=True)
class EtfClassification:
    eligible: bool
    category: str
    group_type: str
    asset_class: str
    source: str
    confidence: str
    rule_id: str
    include_in_group: bool
    exclusion_reason: str | None = None


def _contains(value: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword.upper() in value for keyword in keywords)


def classify_etf(
    *,
    name: str,
    code: str = "",
    market: str = "",
    etf_type: str | None = None,
    investment_type: str | None = None,
) -> EtfClassification:
    """Classify one exchange-listed fund using ordered, auditable rules.

    The official files currently do not expose a canonical tracked-index code for
    both exchanges. Asset class and product eligibility therefore use official
    fields where available, while equity group assignment remains a conservative
    name-based inference. Unmatched products stay visible in the terminal but do
    not enter group aggregates.
    """

    normalized_name = str(name or "").upper().replace(" ", "")
    normalized_code = str(code or "").zfill(6)
    normalized_market = str(market or "").upper()
    normalized_etf_type = "" if etf_type is None else str(etf_type).upper()
    normalized_investment = (
        "" if investment_type is None else str(investment_type).upper()
    )

    if "LOF" in normalized_name or (
        normalized_market == "SZSE"
        and normalized_etf_type
        and "ETF" not in normalized_etf_type
    ):
        return EtfClassification(
            eligible=False,
            category="不纳入",
            group_type="excluded",
            asset_class="其他",
            source="official_product_type",
            confidence="high",
            rule_id="exclude_non_etf",
            include_in_group=False,
            exclusion_reason="非ETF产品",
        )

    if "货币" in normalized_investment or _contains(
        normalized_name, ("货币", "现金", "快线", "快钱")
    ):
        source = (
            "official_investment_type"
            if "货币" in normalized_investment
            else "name_rule"
        )
        return EtfClassification(
            True,
            "货币",
            "money",
            "货币",
            source,
            "high" if source == "official_investment_type" else "medium",
            "money",
            True,
        )

    if (
        "债券" in normalized_investment
        or normalized_code.startswith(("511", "551"))
        or _contains(
            normalized_name,
            (
                "国债",
                "地债",
                "政金债",
                "公司债",
                "信用债",
                "科创债",
                "科债",
                "转债",
                "短融",
                "同业存单",
                "城投",
                "债券",
                "债基",
                "债指",
                "债汇",
            ),
        )
    ):
        source = (
            "official_investment_type"
            if "债券" in normalized_investment
            else "exchange_code_rule"
            if normalized_code.startswith(("511", "551"))
            else "name_rule"
        )
        return EtfClassification(
            True,
            "债券",
            "bond",
            "债券",
            source,
            "high" if source != "name_rule" else "medium",
            "bond",
            True,
        )

    if (
        "跨境" in normalized_etf_type
        or _contains(
            normalized_name,
            (
                "恒生",
                "港股",
                "中概",
                "纳指",
                "标普",
                "日经",
                "德国",
                "法国",
                "海外",
                "中韩",
                "东南亚",
                "沙特",
                "印度",
            ),
        )
    ):
        source = "official_etf_type" if "跨境" in normalized_etf_type else "name_rule"
        return EtfClassification(
            True,
            "港股海外",
            "cross_border",
            "跨境",
            source,
            "high" if source == "official_etf_type" else "medium",
            "cross_border",
            True,
        )

    if (
        "商品" in normalized_investment
        or normalized_code.startswith("518")
        or _contains(
            normalized_name,
            (
                "黄金",
                "上海金",
                "金ETF",
                "豆粕",
                "有色期货",
                "能源化工",
                "商品期货",
            ),
        )
    ):
        source = (
            "official_investment_type"
            if "商品" in normalized_investment
            else "exchange_code_rule"
            if normalized_code.startswith("518")
            else "name_rule"
        )
        return EtfClassification(
            True,
            "商品",
            "commodity",
            "商品",
            source,
            "high" if source != "name_rule" else "medium",
            "commodity",
            True,
        )

    ordered_equity_rules: tuple[
        tuple[str, str, str, tuple[str, ...]], ...
    ] = (
        (
            "红利价值",
            "strategy",
            "strategy",
            ("红利", "价值", "低波", "高股息", "质量", "成长", "增强"),
        ),
        (
            "医药",
            "industry",
            "healthcare",
            ("医药", "医疗", "创新药", "生物", "疫苗", "中药"),
        ),
        (
            "金融地产",
            "industry",
            "financial_real_estate",
            ("银行", "证券", "券商", "保险", "金融", "地产"),
        ),
        (
            "消费",
            "industry",
            "consumer",
            ("消费", "食品", "酒", "家电", "旅游", "养殖", "农业"),
        ),
        (
            "周期资源",
            "industry",
            "cyclical_resources",
            ("有色", "煤炭", "钢铁", "化工", "稀土", "资源", "材料"),
        ),
        (
            "新能源制造",
            "industry",
            "new_energy_manufacturing",
            (
                "新能源",
                "光伏",
                "电池",
                "汽车",
                "军工",
                "机械",
                "制造",
                "新材",
                "电力",
            ),
        ),
        (
            "人工智能机器人",
            "theme",
            "ai_robotics",
            ("人工智能", "AI", "机器人", "智能驾驶", "云计算", "大数据"),
        ),
        (
            "科技",
            "industry",
            "technology",
            (
                "半导",
                "芯片",
                "通信",
                "5G",
                "软件",
                "计算机",
                "信息技术",
                "信息ETF",
                "TMT",
                "电子",
            ),
        ),
        (
            "宽基",
            "broad",
            "broad_market",
            (
                "沪深",
                "中证",
                "上证",
                "深证",
                "创业板",
                "科创",
                "双创",
                "A50",
                "A100",
                "A500",
                "300",
                "500",
                "800",
                "1000",
                "2000",
                "50ETF",
                "180ETF",
                "综指",
                "全指",
            ),
        ),
    )
    for category, group_type, rule_id, keywords in ordered_equity_rules:
        if _contains(normalized_name, keywords):
            return EtfClassification(
                True,
                category,
                group_type,
                "股票",
                "name_rule",
                "medium",
                rule_id,
                True,
            )

    return EtfClassification(
        eligible=True,
        category="其他",
        group_type="other",
        asset_class="股票" if "股票" in normalized_investment else "其他",
        source="unmatched",
        confidence="low",
        rule_id="unmatched",
        include_in_group=False,
    )
