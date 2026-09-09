"""
바르너 매출 대시보드 데이터 새로고침 파이프라인.

구글시트가 '링크가 있는 모든 사용자(뷰어)'로 공유되어 있어 인증 없이
export CSV URL로 직접 원본을 받아올 수 있다. 4개 탭을 받아 집계하고,
template.html의 __SALES_DATA_JSON__ 자리에 채워 최종 대시보드 HTML을 만든다.

사용법:
    python3 refresh.py
    -> 이 디렉터리에 dashboard_output.html 생성
    -> 이후 Artifact publish 도구로 기존 URL에 재배포하면 됨
"""
import calendar
import csv
import json
import subprocess
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
SHEET_ID = "10AxJQ3RBH_otWksMy6zLEZ4H7c-lWsp7bSIm9J4B7c4"
GIDS = {
    "revenue": "26090317",       # 매출_RAW_CLEAN
    "quantity": "26090318",      # 제품수량_RAW_CLEAN
    "product_master": "26090301",  # 표준제품_MASTER
    "ads": "748935331",          # 바르너_광고소재
    "targets": "360330701",      # 자사몰 월별 목표 매출
}
TEMPLATE_PATH = HERE / "template.html"
OUTPUT_PATH = HERE / "dashboard_output.html"

CHANNEL_ORDER = ['CAFE24', 'SMART_STORE', 'COUPANG', 'COUPANG_ZET', 'KAKAO_GIFT', 'TALKSTORE', 'OLIVE_YOUNG', 'ZIGZAG']
CH_MAP_RAW = {'지그재그': 'ZIGZAG'}
CORE_PRODUCTS = ['EMS 미니스팟', '아치스본 스포츠', '아치스본 슬리퍼', '아치스본 밸런스', '바디괄사 세럼', '클리어풋 제로 파우더']
TOP_N_PRODUCTS = 8
AD_CHANNEL_ORDER = ['FACEBOOK', 'GOOGLE_ADS', 'NAVER_GFA', 'NAVER_SA', 'KAKAOMOMENT', 'COUPANG_AD', 'TIKTOK']

# 자사몰(CAFE24) 탭의 제품별 광고 성과용 — 바르너_광고소재 탭 R열(사용자가 직접 채워넣은 제품 태그)을
# 대시보드 전반에서 쓰는 표준 제품명으로 맞춰준다. R열 자체가 없거나 목록에 없는 값은 미분류로 묶는다.
AD_PRODUCT_COLUMN_INDEX = 17  # R열 (0-indexed)
AD_UNCLASSIFIED = '광고 미분류'
AD_PROMOTION = '프로모션(복수제품)'
R_VALUE_TO_PRODUCT = {
    '미니스팟': 'EMS 미니스팟',
    '아치스포츠': '아치스본 스포츠',
    '아치슬리퍼': '아치스본 슬리퍼',
    '아치밸런스': '아치스본 밸런스',
    '아치프로': '아치스본 프로',
    'EMS벨트': 'EMS 버닝벨트',
    '괄사세럼': '바디괄사 세럼',
    '버닝벨트': '버닝벨트',
    '아치시리즈': '아치스본',
    '풋파우더': '클리어풋 제로 파우더',
    'ems슬리퍼': '아치스본 EMS 슬리퍼',
    '프로모션': AD_PROMOTION,
    '기타': AD_UNCLASSIFIED,
    '': AD_UNCLASSIFIED,
}


def product_from_row(row: list[str]) -> str:
    r_val = row[AD_PRODUCT_COLUMN_INDEX].strip() if len(row) > AD_PRODUCT_COLUMN_INDEX else ''
    return R_VALUE_TO_PRODUCT.get(r_val, AD_UNCLASSIFIED)

# 광고 소재의 캠페인명을 실제 판매채널로 매핑 (사용자 확정 규칙):
#   FACEBOOK: 캠페인명에 올영세일/올리브영 -> 올리브영, 그 외 -> 자사몰(CAFE24)
#   NAVER_SA/NAVER_GFA: 캠페인명에 '브스' -> 스마트스토어, 그 외(자사몰_.., 브랜드검색 등) -> 자사몰(CAFE24)
#   GOOGLE_ADS, TIKTOK: 전량 자사몰(CAFE24)
#   COUPANG_AD: 전량 쿠팡(COUPANG)
#   KAKAOMOMENT: 캠페인명에 '자사몰' -> 자사몰(CAFE24), 그 외(톡딜/선물하기/카탈로그 등) -> 카카오 외부몰
AD_SALES_CHANNEL_ORDER = ['CAFE24', 'SMART_STORE', 'OLIVE_YOUNG', 'COUPANG', 'KAKAO_EXTERNAL']


def map_ad_to_sales_channel(ad_channel: str, campaign: str) -> str:
    if ad_channel == 'FACEBOOK':
        return 'OLIVE_YOUNG' if ('올영' in campaign or '올리브영' in campaign) else 'CAFE24'
    if ad_channel in ('NAVER_SA', 'NAVER_GFA'):
        return 'SMART_STORE' if '브스' in campaign else 'CAFE24'
    if ad_channel in ('GOOGLE_ADS', 'TIKTOK'):
        return 'CAFE24'
    if ad_channel == 'COUPANG_AD':
        return 'COUPANG'
    if ad_channel == 'KAKAOMOMENT':
        return 'CAFE24' if '자사몰' in campaign else 'KAKAO_EXTERNAL'
    return ad_channel


def fetch_csv(gid: str) -> list[list[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    out = subprocess.run(["curl", "-sL", url], capture_output=True, check=True)
    text = out.stdout.decode("utf-8")
    rows = list(csv.reader(text.splitlines()))
    return rows


def parse_won(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    neg = s.startswith("-")
    s = s.replace("-", "").replace("₩", "").replace(",", "").replace("원", "").strip()
    if not s:
        return 0.0
    v = float(s)
    return -v if neg else v


def parse_pct_or_num(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    neg = s.startswith("-")
    s = s.replace("-", "").replace("%", "").replace(",", "").replace("원", "").strip()
    if not s:
        return 0.0
    v = float(s)
    return -v if neg else v


def build_revenue(rows: list[list[str]]):
    header, *data = rows
    data = [r for r in data if len(r) > 15 and r[1].strip() and r[2].strip()]

    daily = defaultdict(lambda: defaultdict(float))
    dates = set()
    for row in data:
        date, ch = row[1], CH_MAP_RAW.get(row[2], row[2])
        if ch not in CHANNEL_ORDER:
            continue
        daily[date][ch] += parse_won(row[15])
        dates.add(date)
    dates = sorted(dates)

    daily_series = []
    for dt in dates:
        r = {"date": dt}
        for c in CHANNEL_ORDER:
            r[c] = round(daily[dt].get(c, 0))
        r["total"] = sum(r[c] for c in CHANNEL_ORDER)
        daily_series.append(r)

    monthly = defaultdict(lambda: defaultdict(float))
    for r in daily_series:
        ym = r["date"][:7]
        for c in CHANNEL_ORDER:
            monthly[ym][c] += r[c]
    monthly_series = []
    for ym in sorted(monthly.keys()):
        r = {"month": ym}
        for c in CHANNEL_ORDER:
            r[c] = round(monthly[ym][c])
        r["total"] = sum(r[c] for c in CHANNEL_ORDER)
        monthly_series.append(r)

    channel_totals = {c: sum(r[c] for r in daily_series) for c in CHANNEL_ORDER}
    grand_total = sum(channel_totals.values())

    # product revenue (single-product mapped transactions)
    prod_rev = defaultdict(float)
    for row in data:
        prod = row[10].strip()
        key = prod if prod else "__UNMAPPED__"
        prod_rev[key] += parse_won(row[15])
    sorted_prods = sorted(prod_rev.items(), key=lambda x: -x[1])
    top = [(p, v) for p, v in sorted_prods if p != "__UNMAPPED__"][:TOP_N_PRODUCTS]
    top_names = [p for p, _ in top]
    rest_sum = sum(v for p, v in sorted_prods if p != "__UNMAPPED__" and p not in top_names)
    unmapped_sum = prod_rev.get("__UNMAPPED__", 0)

    product_revenue = [{"name": p, "amount": round(v), "kind": "product"} for p, v in top]
    if rest_sum > 0:
        product_revenue.append({"name": "기타 제품 (12종)", "amount": round(rest_sum), "kind": "other"})
    if unmapped_sum > 0:
        product_revenue.append({"name": "미매핑 (분류 안 됨)", "amount": round(unmapped_sum), "kind": "unmapped"})

    # monthly product revenue (top products + other + unmapped)
    monthly_prod = defaultdict(lambda: defaultdict(float))
    for row in data:
        ym = row[1][:7]
        prod = row[10].strip()
        if not prod:
            key = "미매핑 (분류 안 됨)"
        elif prod in top_names:
            key = prod
        else:
            key = "기타 제품 (12종)"
        monthly_prod[ym][key] += parse_won(row[15])
    keys = top_names + ["기타 제품 (12종)", "미매핑 (분류 안 됨)"]
    monthly_product_revenue = []
    for ym in sorted(monthly_prod.keys()):
        r = {"month": ym}
        for k in keys:
            r[k] = round(monthly_prod[ym].get(k, 0))
        r["total"] = sum(r[k] for k in keys)
        monthly_product_revenue.append(r)

    return {
        "channels": CHANNEL_ORDER,
        "daily_series": daily_series,
        "monthly_series": monthly_series,
        "channel_totals": channel_totals,
        "grand_total": grand_total,
        "product_revenue": product_revenue,
        "product_revenue_total": round(grand_total),
        "product_revenue_keys": keys,
        "monthly_product_revenue": monthly_product_revenue,
        "meta": {
            "revenue_date_range": [dates[0], dates[-1]],
            "source": "매출_RAW_CLEAN (거래건별 원본), 결제금액(-vat, 부가세 제외 공급가) 기준 집계",
        },
    }


def build_quantity(rows: list[list[str]]):
    header, *data = rows
    data = [r for r in data if len(r) > 9 and r[1].strip() and r[2].strip()]

    qty_daily = defaultdict(lambda: defaultdict(int))
    qdates = set()
    for row in data:
        date, prod = row[1], row[7]
        if prod not in CORE_PRODUCTS:
            continue
        try:
            qty = int(float(row[9])) if row[9].strip() else 0
        except ValueError:
            qty = 0
        qty_daily[date][prod] += qty
        qdates.add(date)
    qdates = sorted(qdates)

    qty_series = []
    for dt in qdates:
        r = {"date": dt}
        for p in CORE_PRODUCTS:
            r[p] = qty_daily[dt].get(p, 0)
        qty_series.append(r)
    qty_totals = {p: sum(r[p] for r in qty_series) for p in CORE_PRODUCTS}

    return {
        "qty_products": CORE_PRODUCTS,
        "qty_series": qty_series,
        "qty_totals": qty_totals,
        "meta_qty_date_range": [qdates[0], qdates[-1]] if qdates else None,
    }


def build_product_master(rows: list[list[str]]):
    header, *data = rows
    data = [r for r in data if len(r) > 7 and r[1].strip()]

    by_group = defaultdict(list)
    group_counts = defaultdict(int)
    kpi_counts = defaultdict(int)
    for row in data:
        name, group, in_use, kpi = row[2], row[3], row[5], row[7]
        by_group[group].append({"name": name, "kpi": kpi, "in_use": in_use})
        group_counts[group] += 1
        kpi_counts[kpi] += 1

    return {
        "product_master_by_group": dict(by_group),
        "product_group_counts": dict(group_counts),
        "kpi_category_counts": dict(kpi_counts),
    }


def _aggregate_ads(data, channel_order, channel_of):
    daily = defaultdict(lambda: defaultdict(lambda: {"spend": 0.0, "value": 0.0, "conv": 0.0}))
    dates = set()
    for row in data:
        date = row[0]
        ch = channel_of(row)
        if ch not in channel_order:
            continue
        d = daily[date][ch]
        d["spend"] += parse_pct_or_num(row[6])
        d["conv"] += parse_pct_or_num(row[9])
        d["value"] += parse_pct_or_num(row[10])
        dates.add(date)
    dates = sorted(dates)

    daily_series = []
    for dt in dates:
        r = {"date": dt}
        tot_spend = tot_value = tot_conv = 0.0
        for c in channel_order:
            d = daily[dt].get(c)
            spend = round(d["spend"]) if d else 0
            r[c] = spend
            tot_spend += spend
            if d:
                tot_value += d["value"]
                tot_conv += d["conv"]
        r["spend_total"] = round(tot_spend)
        r["value_total"] = round(tot_value)
        r["conversions_total"] = round(tot_conv)
        daily_series.append(r)

    monthly = defaultdict(lambda: defaultdict(lambda: {"spend": 0.0, "value": 0.0, "conv": 0.0}))
    for dt in dates:
        ym = dt[:7]
        for c in channel_order:
            d = daily[dt].get(c)
            if not d:
                continue
            m = monthly[ym][c]
            m["spend"] += d["spend"]
            m["value"] += d["value"]
            m["conv"] += d["conv"]

    monthly_series = []
    grand_conv = 0.0
    for ym in sorted(monthly.keys()):
        r = {"month": ym}
        tot_spend = tot_value = tot_conv = 0.0
        for c in channel_order:
            m = monthly[ym][c]
            r[c] = round(m["spend"])
            tot_spend += m["spend"]
            tot_value += m["value"]
            tot_conv += m["conv"]
        r["spend_total"] = round(tot_spend)
        r["value_total"] = round(tot_value)
        r["roas"] = round(tot_value / tot_spend * 100, 1) if tot_spend else 0
        r["cpa"] = round(tot_spend / tot_conv) if tot_conv else 0
        r["conversions"] = round(tot_conv)
        grand_conv += tot_conv
        monthly_series.append(r)

    channel_totals = {c: round(sum(daily[dt][c]["spend"] for dt in dates if c in daily[dt])) for c in channel_order}
    grand_spend = sum(channel_totals.values())
    grand_value = sum(r["value_total"] for r in monthly_series)

    return {
        "channels": channel_order,
        "daily_series": daily_series,
        "monthly_series": monthly_series,
        "channel_totals": channel_totals,
        "grand_spend": grand_spend,
        "grand_value": grand_value,
        "grand_roas": round(grand_value / grand_spend * 100, 1) if grand_spend else 0,
        "grand_conversions": round(grand_conv),
        "grand_cpa": round(grand_spend / grand_conv) if grand_conv else 0,
        "date_range": [dates[0], dates[-1]] if dates else None,
    }


def build_ads(rows: list[list[str]]):
    header, *data = rows
    data = [r for r in data if len(r) > 10 and r[0].strip()]

    ads = _aggregate_ads(data, AD_CHANNEL_ORDER, lambda row: row[1])
    ads_by_channel = _aggregate_ads(
        data, AD_SALES_CHANNEL_ORDER, lambda row: map_ad_to_sales_channel(row[1], row[2])
    )
    return {"ads": ads, "ads_by_channel": ads_by_channel}


def build_cafe24_products(ad_rows: list[list[str]], revenue_rows: list[list[str]]):
    """자사몰(CAFE24) 탭: 일자·제품별 광고비/노출/클릭/전환/전환값 + 매출.

    광고 쪽 제품 태그는 바르너_광고소재 탭 R열(사용자가 직접 입력)을 그대로 쓴다.
    매출 쪽은 매출_RAW_CLEAN의 단일 제품명을 그대로 쓴다.
    """
    ad_header, *ad_data = ad_rows
    ad_data = [r for r in ad_data if len(r) > 10 and r[0].strip()]

    ad_daily = defaultdict(lambda: defaultdict(lambda: {
        "spend": 0.0, "impr": 0.0, "clicks": 0.0, "conv": 0.0, "value": 0.0
    }))
    for row in ad_data:
        date, ch, campaign, adset, ad = row[0], row[1], row[2], row[3], row[4]
        if map_ad_to_sales_channel(ch, campaign) != 'CAFE24':
            continue
        product = product_from_row(row)
        d = ad_daily[date][product]
        d["spend"] += parse_pct_or_num(row[6])
        d["impr"] += parse_pct_or_num(row[7])
        d["clicks"] += parse_pct_or_num(row[8])
        d["conv"] += parse_pct_or_num(row[9])
        d["value"] += parse_pct_or_num(row[10])

    ad_daily_rows = []
    for date, by_product in ad_daily.items():
        for product, d in by_product.items():
            ad_daily_rows.append({
                "date": date, "product": product,
                "spend": round(d["spend"]), "impr": round(d["impr"]), "clicks": round(d["clicks"]),
                "conv": round(d["conv"]), "value": round(d["value"]),
            })

    rev_header, *rev_data = revenue_rows
    rev_data = [r for r in rev_data if len(r) > 15 and r[1].strip() and r[2].strip()]

    rev_daily = defaultdict(lambda: defaultdict(float))
    for row in rev_data:
        date, ch = row[1], row[2]
        if ch != 'CAFE24':
            continue
        product = row[10].strip() or '미매핑 (분류 안 됨)'
        rev_daily[date][product] += parse_won(row[15])

    rev_daily_rows = []
    for date, by_product in rev_daily.items():
        for product, amt in by_product.items():
            rev_daily_rows.append({"date": date, "product": product, "revenue": round(amt)})

    return {
        "cafe24": {
            "ad_daily": ad_daily_rows,
            "revenue_daily": rev_daily_rows,
            "ad_unclassified_label": AD_UNCLASSIFIED,
        }
    }


# '자사몰 월별 목표 매출' 탭의 제품 라벨을 대시보드 표준 제품명으로 매핑 (신규/미출시 제품, 프로모션·공구 등
# 묶음성 항목은 실적 데이터가 제품 단위로 없어 매핑하지 않고 실적 0으로 처리한다)
TARGET_LABEL_TO_PRODUCT = {
    '미니스팟': 'EMS 미니스팟',
    '아치스본': '아치스본',
    '아치스본 프로': '아치스본 프로',
    '아치스본 슬리퍼': '아치스본 슬리퍼',
    '아치스본 스포츠': '아치스본 스포츠',
    '아치스본 밸런스': '아치스본 밸런스',
    '괄사세럼': '바디괄사 세럼',
    '스텝퍼': '지압 스텝퍼',
    '시너지패치': 'EMS 시너지 패치',
    '풋파우더': '클리어풋 제로 파우더',
    '바르너EMS버닝벨트': 'EMS 버닝벨트',
    '바르너EMS슬리퍼': '아치스본 EMS 슬리퍼',
    '바르너버닝벨트': '버닝벨트',
}
# 단일 제품이 아니라 여러 제품이 섞인 묶음성 목표라 제품 단위로는 실적을 추적할 수 없지만,
# 매출_RAW_CLEAN의 '매출구분' 컬럼(프로모션/공구)으로 CAFE24 채널 내 실적은 집계 가능한 항목
BUNDLE_LABEL_TO_SALES_TYPE = {'프로모션 매출': '프로모션', '공구': '공구'}
# 신규브랜드-자사몰(오덴틱 치약 라인)과 그 하위 제품(바르너가 아닌 별도 신규 브랜드),
# 그리고 자사몰 채널 밖이라 실적을 아예 집계할 수 없는 '기타 (외부 공구)'는 표에서 제외
TARGET_EXCLUDED_LABELS = {'신규브랜드-자사몰', '블랙 치약', '핑크 치약', '액상 치약', '기타 (외부 공구)'}
# 이 라벨들만 표에서 단독 행으로 남기고, 나머지 제품은 모두 '기타'로 합산
TARGET_KEEP_SEPARATE_LABELS = {
    '미니스팟', '아치스본 슬리퍼', '아치스본 스포츠', '아치스본 밸런스',
    '괄사세럼', '스텝퍼', '풋파우더', '프로모션 매출',
}


def build_bundle_revenue(revenue_rows: list[list[str]]):
    """CAFE24 채널의 '매출구분'이 프로모션/공구인 거래를 일자별로 집계.

    '자사몰 월별 목표 매출' 탭의 '프로모션 매출'/'공구' 항목은 여러 제품이 섞인 묶음이라
    단일 제품명(row[10])으로는 추적이 안 되지만, 매출구분(row[12]) 값으로는 정확히 집계할 수 있다.
    """
    header, *data = revenue_rows
    data = [r for r in data if len(r) > 15 and r[1].strip() and r[2].strip()]
    daily = defaultdict(lambda: defaultdict(float))
    for row in data:
        if row[2].strip() != 'CAFE24':
            continue
        sales_type = row[12].strip()
        if sales_type not in BUNDLE_LABEL_TO_SALES_TYPE.values():
            continue
        daily[row[1]][sales_type] += parse_won(row[15])
    return [{"date": d, **{k: round(v) for k, v in types.items()}} for d, types in daily.items()]


def build_targets(rows: list[list[str]], cafe24_revenue_daily: list[dict], bundle_daily: list[dict], latest_date: str):
    """자사몰(CAFE24) 탭 상단 목표 대비 실적.

    '자사몰 월별 목표 매출' 원본 탭(사용자가 직접 관리)에서 '바르너-자사몰' 행의 월별 총 목표와
    그 아래(37행부터) 제품별 월별 목표를 읽어, 실제 매출(카페24 채널)과 비교한다.
    '해당 월'은 시스템 날짜가 아니라 실제 매출 데이터가 존재하는 마지막 날짜(latest_date)의 달로
    정해서, 새로고침 시점과 무관하게 데이터가 실제로 커버하는 달과 항상 일치시킨다.
    """
    total_row_idx = next(i for i, r in enumerate(rows) if len(r) > 1 and r[1].strip() == '바르너-자사몰')
    month_header_row = rows[total_row_idx - 1]
    month_cols = []  # [(컬럼 인덱스, 월 번호), ...]
    for col in range(2, 14):
        label = month_header_row[col].strip() if col < len(month_header_row) else ''
        num = ''.join(ch for ch in label.split('(')[0] if ch.isdigit())
        if num:
            month_cols.append((col, int(num)))

    target_by_month = {}
    total_row = rows[total_row_idx]
    for col, m in month_cols:
        target_by_month[m] = parse_won(total_row[col]) if col < len(total_row) else 0.0

    product_targets = []
    for r in rows[total_row_idx + 1:]:
        label = r[1].strip() if len(r) > 1 else ''
        if not label:
            break
        if label in TARGET_EXCLUDED_LABELS:
            continue
        monthly = {m: (parse_won(r[col]) if col < len(r) else 0.0) for col, m in month_cols}
        product_targets.append({"label": label, "monthly": monthly})

    year_s, month_s, day_s = latest_date.split('-')
    year, month, day = int(year_s), int(month_s), int(day_s)
    days_in_month = calendar.monthrange(year, month)[1]

    actual_by_product = defaultdict(float)
    actual_total = 0.0
    ym_prefix = f"{year}-{month:02d}"
    for r in cafe24_revenue_daily:
        if r["date"][:7] != ym_prefix:
            continue
        actual_by_product[r["product"]] += r["revenue"]
        actual_total += r["revenue"]

    actual_by_sales_type = defaultdict(float)
    for r in bundle_daily:
        if r["date"][:7] != ym_prefix:
            continue
        for k, v in r.items():
            if k == "date":
                continue
            actual_by_sales_type[k] += v

    target_this_month = target_by_month.get(month, 0.0)
    products = []
    other_target = 0.0
    other_actual = 0.0
    for pt in product_targets:
        target_amt = pt["monthly"].get(month, 0.0)
        sales_type = BUNDLE_LABEL_TO_SALES_TYPE.get(pt["label"])
        if sales_type:
            mapped = None
            actual_amt = actual_by_sales_type.get(sales_type, 0.0)
        else:
            mapped = TARGET_LABEL_TO_PRODUCT.get(pt["label"])
            actual_amt = actual_by_product.get(mapped, 0.0) if mapped else 0.0

        if pt["label"] not in TARGET_KEEP_SEPARATE_LABELS:
            other_target += target_amt
            other_actual += actual_amt
            continue

        products.append({
            "label": pt["label"],
            "product": mapped,
            "target": round(target_amt),
            "actual": round(actual_amt),
            "achievement_pct": round(actual_amt / target_amt * 100, 1) if target_amt else None,
            "untrackable": False,
        })
    if other_target or other_actual:
        products.append({
            "label": "기타",
            "product": None,
            "target": round(other_target),
            "actual": round(other_actual),
            "achievement_pct": round(other_actual / other_target * 100, 1) if other_target else None,
            "untrackable": False,
        })

    return {
        "targets": {
            "month": f"{year}-{month:02d}",
            "month_label": f"{month}월",
            "as_of_date": latest_date,
            "days_elapsed": day,
            "days_in_month": days_in_month,
            "month_progress_pct": round(day / days_in_month * 100, 1),
            "target_revenue": round(target_this_month),
            "actual_revenue": round(actual_total),
            "achievement_pct": round(actual_total / target_this_month * 100, 1) if target_this_month else None,
            "products": products,
        }
    }


WINNER_MIN_SPEND = 300000  # 이 금액(원) 미만 광고비를 쓴 소재는 위너 후보에서 제외 (노이즈 방지)
WINNER_TOP_N = 20
# 자사몰(CAFE24) 위너 소재는 이 3개 매체만 본다 (사용자 확정 범위)
WINNER_CHANNELS = ('FACEBOOK', 'GOOGLE_ADS', 'TIKTOK')


def build_creative_winners(ad_rows: list[list[str]]):
    """자사몰(CAFE24) 탭: 위너 소재 — 대시보드 상단의 기간 필터를 그대로 따라가도록,
    서버에서 월별로 미리 순위를 매기지 않고 일자·소재 단위 원본을 그대로 내려보낸다
    (클라이언트가 현재 선택된 기간에 맞춰 그때그때 합산·정렬한다).

    FACEBOOK/GOOGLE_ADS/TIKTOK 중 자사몰(CAFE24)로 매핑되는 소재만 대상으로 하고,
    전체 기간 광고비 합계가 WINNER_MIN_SPEND 미만인 소재는 애초에 위너가 될 수 없으므로
    제외해 용량을 줄인다(짧은 기간에 몰아써서 반짝 상위에 오르는 극단적 케이스는 배제됨).
    """
    header, *data = ad_rows
    data = [r for r in data if len(r) > 10 and r[0].strip()]
    data = [r for r in data if r[1] in WINNER_CHANNELS and map_ad_to_sales_channel(r[1], r[2]) == 'CAFE24']

    lifetime_spend: dict[tuple, float] = {}
    for row in data:
        key = (row[1], row[2], row[3], row[4])
        lifetime_spend[key] = lifetime_spend.get(key, 0.0) + parse_pct_or_num(row[6])
    relevant_keys = {k for k, v in lifetime_spend.items() if v >= WINNER_MIN_SPEND}

    # 같은 소재의 캠페인/광고세트/광고명이 일자마다 반복돼 용량을 크게 잡아먹으므로,
    # 소재 메타데이터는 creatives 배열에 한 번만 두고 daily 행은 짧은 정수 id로만 참조한다.
    creative_id: dict[tuple, int] = {}
    creatives_list = []
    daily_rows = []
    for row in data:
        date, ch, campaign, adset, ad = row[0], row[1], row[2], row[3], row[4]
        key = (ch, campaign, adset, ad)
        if key not in relevant_keys:
            continue
        spend = parse_pct_or_num(row[6])
        impr = parse_pct_or_num(row[7])
        clicks = parse_pct_or_num(row[8])
        conv = parse_pct_or_num(row[9])
        value = parse_pct_or_num(row[10])
        if spend == 0 and impr == 0 and clicks == 0 and conv == 0 and value == 0:
            continue
        if key not in creative_id:
            creative_id[key] = len(creatives_list)
            creatives_list.append({
                "channel": ch, "campaign": campaign, "adset": adset, "ad": ad,
                "product": product_from_row(row),
            })
        daily_rows.append({
            "date": date, "id": creative_id[key],
            "spend": round(spend), "impr": round(impr), "clicks": round(clicks),
            "conv": round(conv), "value": round(value),
        })

    return {
        "creative_winners": {
            "min_spend": WINNER_MIN_SPEND,
            "top_n": WINNER_TOP_N,
            "channels": ["ALL"] + list(WINNER_CHANNELS),
            "creatives": creatives_list,
            "daily": daily_rows,
        }
    }


def main():
    print("fetching 매출_RAW_CLEAN ...")
    revenue_rows = fetch_csv(GIDS["revenue"])
    print("fetching 제품수량_RAW_CLEAN ...")
    qty_rows = fetch_csv(GIDS["quantity"])
    print("fetching 표준제품_MASTER ...")
    master_rows = fetch_csv(GIDS["product_master"])
    print("fetching 바르너_광고소재 ...")
    ads_rows = fetch_csv(GIDS["ads"])
    print("fetching 자사몰 월별 목표 매출 ...")
    target_rows = fetch_csv(GIDS["targets"])

    agg = {}
    agg.update(build_revenue(revenue_rows))
    agg.update(build_quantity(qty_rows))
    agg.update(build_product_master(master_rows))
    agg.update(build_ads(ads_rows))
    agg.update(build_cafe24_products(ads_rows, revenue_rows))
    agg["cafe24"]["creative_winners"] = build_creative_winners(ads_rows)["creative_winners"]
    agg["cafe24"]["targets"] = build_targets(
        target_rows, agg["cafe24"]["revenue_daily"], build_bundle_revenue(revenue_rows),
        agg["meta"]["revenue_date_range"][1]
    )["targets"]

    # 광고비(판매채널 매핑) vs 실제 매출 비교 — CAFE24/스마트스토어/올리브영만 판매채널로 확정 매핑됨
    ad_spend_by_ch = agg["ads_by_channel"]["channel_totals"]
    channel_efficiency = []
    for ch in ("CAFE24", "SMART_STORE", "OLIVE_YOUNG", "COUPANG"):
        spend = ad_spend_by_ch.get(ch, 0)
        revenue = agg["channel_totals"].get(ch, 0)
        channel_efficiency.append({
            "channel": ch,
            "ad_spend": spend,
            "revenue": revenue,
            "spend_pct_of_revenue": round(spend / revenue * 100, 1) if revenue else 0,
        })
    agg["channel_ad_efficiency"] = channel_efficiency

    agg_path = HERE / "dashboard_agg.json"
    agg_path.write_text(json.dumps(agg, ensure_ascii=False), encoding="utf-8")
    print("wrote", agg_path)

    data_json = json.dumps(agg, ensure_ascii=False, separators=(",", ":"))
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "__SALES_DATA_JSON__" in template, "template placeholder missing"
    output = template.replace("__SALES_DATA_JSON__", data_json)
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    print("wrote", OUTPUT_PATH, "size", len(output))
    print("grand_total(revenue)=", agg["grand_total"], "grand_spend(ads)=", agg["ads"]["grand_spend"])


if __name__ == "__main__":
    main()
