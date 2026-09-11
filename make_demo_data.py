"""
바르너 매출 대시보드 '템플릿(데모)' 버전 생성기.

실제 매출/광고 숫자를 전혀 사용하지 않고, refresh.py의 집계 함수(build_*)에
그대로 넣을 수 있는 형태의 가짜 원본 행(row)들을 처음부터 만들어낸다.
따라서 실제 파이프라인과 완전히 동일한 코드 경로로 집계되어 구조/수식이
항상 일치하고(합계가 어긋나는 등의 티가 나지 않음), 숫자만 전부 가짜다.

사용법:
    python3 make_demo_data.py
    -> dashboard_demo_output.html 생성 (Artifact로 별도 배포해서 외부 공유용으로 사용)
"""
import calendar
import json
import random
from collections import defaultdict
from pathlib import Path

import refresh as R

HERE = Path(__file__).parent
OUTPUT_PATH = HERE / "dashboard_demo_output.html"

random.seed(7)

START = "2024-01-01"
END = "2024-09-10"


def daterange(start, end):
    y, m, d = map(int, start.split("-"))
    ey, em, ed = map(int, end.split("-"))
    import datetime
    cur = datetime.date(y, m, d)
    last = datetime.date(ey, em, ed)
    out = []
    while cur <= last:
        out.append(cur.isoformat())
        cur += datetime.timedelta(days=1)
    return out


DATES = daterange(START, END)

# 제품별 상대 비중(합이 1일 필요는 없음 — 나머지는 '기타 제품'으로 묶인다)
PRODUCT_WEIGHTS = {
    "EMS 미니스팟": 0.30,
    "아치스본 스포츠": 0.16,
    "아치스본 슬리퍼": 0.12,
    "아치스본 밸런스": 0.08,
    "바디괄사 세럼": 0.07,
    "클리어풋 제로 파우더": 0.05,
    "아치스본 프로": 0.05,
    "아치스본": 0.04,
    "EMS 버닝벨트": 0.04,
    "버닝벨트": 0.03,
    "지압 스텝퍼": 0.02,
    "EMS 시너지 패치": 0.02,
    "아치스본 EMS 슬리퍼": 0.02,
}
PRODUCTS = list(PRODUCT_WEIGHTS.keys())

# R열(광고소재 제품 태그) 값 목록 — refresh.R_VALUE_TO_PRODUCT 키를 그대로 재사용
R_TAGS = [k for k in R.R_VALUE_TO_PRODUCT if k]


def won(v):
    return f"₩{round(v):,}"


def channel_daily_base(day_idx, total_days):
    """채널별 그날의 '기준' 매출 규모(원) — 성장 추세 + 요일 패턴 + 신규 채널 합류 시점."""
    growth = 1 + (day_idx / total_days) * 2.2  # 초반 대비 후반 약 3.2배 성장
    dow = (day_idx % 7)
    weekend_boost = 1.15 if dow in (5, 6) else 1.0
    noise = random.uniform(0.85, 1.15)
    base = growth * weekend_boost * noise

    out = {
        "CAFE24": 9_000_000 * base,
        "SMART_STORE": 2_800_000 * base * random.uniform(0.8, 1.2),
        "COUPANG": 400_000 * base * random.uniform(0.5, 1.5),
        "COUPANG_ZET": 0,
        "KAKAO_GIFT": 0,
        "TALKSTORE": 0,
        "OLIVE_YOUNG": 0,
        "ZIGZAG": 0,
    }
    if day_idx >= 10:
        out["COUPANG_ZET"] = 900_000 * base * random.uniform(0.6, 1.3)
    if day_idx >= 95:  # 4월 무렵부터 신규 채널 합류 (원본 데이터 서사 재현)
        out["KAKAO_GIFT"] = 350_000 * (base - 1.0 if base > 1 else 0.3) * random.uniform(0.5, 1.5)
        out["TALKSTORE"] = 300_000 * (base - 1.0 if base > 1 else 0.3) * random.uniform(0.5, 1.5)
    if day_idx >= 150:
        out["OLIVE_YOUNG"] = 60_000 * random.uniform(0.3, 2.0)
    if day_idx >= 60 and random.random() < 0.2:
        out["ZIGZAG"] = 30_000 * random.uniform(0.2, 1.5)
    return {k: max(0, v) for k, v in out.items()}


def gen_revenue_rows():
    rows = [[
        "RAW_ID", "일자", "판매처", "페이지명", "옵션명", "옵션 판매량", "결제금액", "매핑키",
        "분류유형", "단일 제품코드", "단일 제품명", "매핑상태", "매출구분", "공구명", "프로모션명", "결제금액(-vat)",
    ]]
    rid = 0
    total_days = len(DATES)
    for day_idx, date in enumerate(DATES):
        ch_amounts = channel_daily_base(day_idx, total_days)
        for ch, amt in ch_amounts.items():
            if amt <= 0:
                continue
            # 이 채널의 그날 매출을 제품별로 쪼갠다 (상위 비중 제품일수록 더 자주/많이 등장)
            remaining = amt
            shuffled = random.sample(PRODUCTS, k=len(PRODUCTS))
            for i, prod in enumerate(shuffled):
                if remaining <= 0:
                    break
                share = PRODUCT_WEIGHTS[prod] * random.uniform(0.6, 1.4)
                piece = min(remaining, amt * share)
                if piece < 1000:
                    continue
                remaining -= piece
                sales_type = "일반"
                if ch == "CAFE24" and random.random() < 0.03:
                    sales_type = random.choice(["프로모션", "공구"])
                rid += 1
                gross = piece * 1.1
                rows.append([
                    str(rid), date, ch, prod, f"옵션={prod} 기본구성", str(random.randint(1, 8)),
                    won(gross), f"{ch}|{prod}", "자동분류", prod[:8].upper().replace(" ", "_"), prod,
                    "자동분류 완료", sales_type,
                    "공구팀" if sales_type == "공구" else "",
                    "프로모션기획전" if sales_type == "프로모션" else "",
                    won(piece),
                ])
    return rows


def gen_quantity_rows():
    rows = [["RAW_ID", "일자", "판매처", "옵션명1", "옵션명2", "옵션명3", "옵션명4", "표준제품명", "구성", "수량"]]
    rid = 0
    total_days = len(DATES)
    for day_idx, date in enumerate(DATES):
        growth = 1 + (day_idx / total_days) * 2.0
        for prod in R.CORE_PRODUCTS:
            if random.random() < 0.08:
                continue
            qty = max(0, round(random.uniform(20, 90) * growth * PRODUCT_WEIGHTS.get(prod, 0.05) * 8))
            if qty <= 0:
                continue
            rid += 1
            rows.append([str(rid), date, "CAFE24", "", "", "", prod, prod, "1", str(qty)])
    return rows


def gen_master_rows():
    rows = [["RAW_ID", "in_use2", "표준제품명", "제품군", "col4", "사용여부", "col6", "KPI구분"]]
    groups = {
        "아치스본 시리즈": ["아치스본", "아치스본 프로", "아치스본 슬리퍼", "아치스본 스포츠", "아치스본 밸런스", "아치스본 EMS 슬리퍼"],
        "EMS 시리즈": ["EMS 미니스팟", "EMS 버닝벨트", "EMS 시너지 패치"],
        "바디케어": ["바디괄사 세럼", "버닝벨트", "지압 스텝퍼"],
        "풋케어": ["클리어풋 제로 파우더", "필라스밴드", "필라스볼"],
        "액세서리": ["바르너 쇼핑백", "바르너 짐색", "필라엑스본", "필렉스 노즈 밴드"],
    }
    rid = 0
    for group, names in groups.items():
        for name in names:
            rid += 1
            kpi = "핵심제품" if name in R.CORE_PRODUCTS else random.choice(["기타제품", "GWP"])
            rows.append([str(rid), "Y", name, group, "", "사용중", "", kpi])
    return rows


AD_CAMPAIGNS = {
    "FACEBOOK": [
        ("240115_미니스팟_컨버전_후킹A", False),
        ("240220_아치스포츠_컨버전_리타겟", False),
        ("240310_올영세일_프로모션", True),
        ("240415_올리브영_런칭전용", True),
    ],
    "GOOGLE_ADS": [("PMAX_전체제품", False), ("SA_브랜드검색", False)],
    "TIKTOK": [("틱톡_숏폼_챌린지", False), ("틱톡_인플루언서_클립", False)],
    "NAVER_SA": [("자사몰_브랜드검색", False), ("브스_키워드검색", False)],
    "NAVER_GFA": [("GFA_배너_노출", False), ("브스_GFA_타겟팅", False)],
    "COUPANG_AD": [("쿠팡_스폰서드프로덕트", False)],
    "KAKAOMOMENT": [("자사몰_카카오모먼트", False), ("톡딜_카카오모먼트", False)],
}


def gen_ads_rows():
    header = ["일자", "매체", "캠페인명", "광고세트명", "광고명", "col5", "지출금액", "노출", "클릭", "전환수", "전환값"]
    header += [f"col{i}" for i in range(11, 18)]  # 11..17, index17 = R열
    rows = [header]
    total_days = len(DATES)

    creatives = []
    for ch, campaigns in AD_CAMPAIGNS.items():
        for campaign, _is_oy in campaigns:
            n_adsets = 2
            for a in range(1, n_adsets + 1):
                adset = f"{campaign}_세트{a}"
                for ad_i in range(1, 3):
                    ad_name = f"{adset}_광고{ad_i}"
                    r_tag = random.choice(R_TAGS + [""])
                    base_spend = random.uniform(15_000, 220_000)
                    creatives.append((ch, campaign, adset, ad_name, r_tag, base_spend))

    for day_idx, date in enumerate(DATES):
        growth = 1 + (day_idx / total_days) * 1.8
        for (ch, campaign, adset, ad_name, r_tag, base_spend) in creatives:
            if random.random() < 0.15:
                continue  # 그 날은 소재가 꺼져 있었다고 가정
            spend = base_spend * growth * random.uniform(0.5, 1.6)
            impr = spend / random.uniform(3.0, 9.0) * 1000  # CPM 3~9천원대
            clicks = impr * random.uniform(0.008, 0.02)
            conv = clicks * random.uniform(0.02, 0.07)
            roas = random.uniform(1.1, 2.6)
            value = spend * roas
            rows.append([
                date, ch, campaign, adset, ad_name, "",
                f"{spend:.0f}", f"{impr:.0f}", f"{clicks:.0f}", f"{conv:.1f}", f"{value:.0f}",
                "", "", "", "", "", "", r_tag,
            ])
    return rows


MONTH_COL_LABELS = ["1월", "2월", "3월", "4월", "5월", "6월", "7월", "8월", "9월", "10월", "11월", "12월"]

TARGET_PRODUCT_LABELS = [
    "미니스팟", "아치스본", "아치스본 프로", "아치스본 슬리퍼", "아치스본 스포츠", "아치스본 밸런스",
    "괄사세럼", "스텝퍼", "시너지패치", "풋파우더", "바르너EMS버닝벨트", "바르너EMS슬리퍼", "바르너버닝벨트",
    "프로모션 매출", "공구",
]


def gen_target_rows():
    rows = [
        ["", "ㅁ (자사몰) 하반기 월별 목표 (데모)"],
        ["", "월매출"],
        ["", "(26) 목표"] + MONTH_COL_LABELS,
    ]
    total_row = ["", "바르너-자사몰"] + [won(random.uniform(6, 13) * 1e8) for _ in MONTH_COL_LABELS]
    rows.append(total_row)
    for label in TARGET_PRODUCT_LABELS:
        scale = random.uniform(0.01, 0.08)
        monthly = [won(random.uniform(0.3, 1.3) * scale * 1e9) for _ in MONTH_COL_LABELS]
        rows.append(["", label] + monthly)
    rows.append(["", ""])  # 종료 표시용 빈 행
    return rows


def main():
    print("generating synthetic 매출_RAW_CLEAN ...")
    revenue_rows = gen_revenue_rows()
    print("generating synthetic 제품수량_RAW_CLEAN ...")
    qty_rows = gen_quantity_rows()
    print("generating synthetic 표준제품_MASTER ...")
    master_rows = gen_master_rows()
    print("generating synthetic 바르너_광고소재 ...")
    ads_rows = gen_ads_rows()
    print("generating synthetic 자사몰 월별 목표 매출 ...")
    target_rows = gen_target_rows()

    agg = {}
    agg.update(R.build_revenue(revenue_rows))
    agg.update(R.build_quantity(qty_rows))
    agg.update(R.build_product_master(master_rows))
    agg.update(R.build_ads(ads_rows))
    agg["cafe24"] = R.build_channel_products(ads_rows, revenue_rows, "CAFE24")
    agg["cafe24"]["creative_winners"] = R.build_creative_winners(ads_rows, "CAFE24")["creative_winners"]
    agg["cafe24"]["targets"] = R.build_targets(
        target_rows, agg["cafe24"]["revenue_daily"], R.build_bundle_revenue(revenue_rows),
        agg["meta"]["revenue_date_range"][1],
    )["targets"]

    agg["oliveyoung"] = R.build_channel_products(ads_rows, revenue_rows, "OLIVE_YOUNG")
    agg["oliveyoung"]["creative_winners"] = R.build_creative_winners(ads_rows, "OLIVE_YOUNG")["creative_winners"]

    ad_spend_by_ch = agg["ads_by_channel"]["channel_totals"]
    channel_efficiency = []
    for ch in ("CAFE24", "SMART_STORE", "OLIVE_YOUNG", "COUPANG"):
        spend = ad_spend_by_ch.get(ch, 0)
        revenue = agg["channel_totals"].get(ch, 0)
        channel_efficiency.append({
            "channel": ch, "ad_spend": spend, "revenue": revenue,
            "spend_pct_of_revenue": round(spend / revenue * 100, 1) if revenue else 0,
        })
    agg["channel_ad_efficiency"] = channel_efficiency

    data_json = json.dumps(agg, ensure_ascii=False, separators=(",", ":"))
    template = R.TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "__SALES_DATA_JSON__" in template
    output = template.replace("__SALES_DATA_JSON__", data_json)
    # 실제 브랜드명이 아니라 템플릿임을 표시
    output = output.replace(
        "<title>바르너 매출 대시보드</title>",
        "<title>매출 대시보드 (템플릿)</title>",
    )
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    print("wrote", OUTPUT_PATH, "size", len(output))
    print("grand_total(revenue)=", agg["grand_total"], "grand_spend(ads)=", agg["ads"]["grand_spend"])


if __name__ == "__main__":
    main()
