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
}
TEMPLATE_PATH = HERE / "template.html"
OUTPUT_PATH = HERE / "dashboard_output.html"

CHANNEL_ORDER = ['CAFE24', 'SMART_STORE', 'COUPANG', 'COUPANG_ZET', 'KAKAO_GIFT', 'TALKSTORE', 'OLIVE_YOUNG', 'ZIGZAG']
CH_MAP_RAW = {'지그재그': 'ZIGZAG'}
CORE_PRODUCTS = ['EMS 미니스팟', '아치스본 스포츠', '아치스본 슬리퍼', '아치스본 밸런스', '바디괄사 세럼', '클리어풋 제로 파우더']
TOP_N_PRODUCTS = 8
AD_CHANNEL_ORDER = ['FACEBOOK', 'GOOGLE_ADS', 'NAVER_GFA', 'NAVER_SA', 'KAKAOMOMENT', 'COUPANG_AD', 'TIKTOK']

# 자사몰(CAFE24) 탭의 제품별 광고 성과용 — 캠페인/광고세트/광고명에서 제품을 추정하는 키워드
# (사이트의 옵션_PRODUCT_MAP과는 별개로, 광고 소재명 표기 관행에 맞춰 별도로 확인한 키워드다.
#  길게/구체적인 키워드부터 먼저 매칭해서 오탐을 줄인다.)
PRODUCT_AD_KEYWORDS = [
    ('미니스팟', 'EMS 미니스팟'),
    ('아치스포츠', '아치스본 스포츠'),
    ('아치슬리퍼', '아치스본 슬리퍼'),
    ('아치밸런스', '아치스본 밸런스'),
    ('아치프로', '아치스본 프로'),
    ('아치액션', '아치스본 액션'),
    ('EMS버닝벨트', 'EMS 버닝벨트'),
    ('괄사세럼', '바디괄사 세럼'),
    ('바디괄사', '바디괄사 세럼'),
    ('지압스텝퍼', '지압 스텝퍼'),
    ('클리어풋', '클리어풋 제로 파우더'),
]
AD_UNCLASSIFIED = '광고 미분류(복수제품·프로모션)'


def match_product_from_ad_text(text: str) -> str:
    norm = text.replace(' ', '')
    for kw, product in PRODUCT_AD_KEYWORDS:
        if kw in norm:
            return product
    return AD_UNCLASSIFIED

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
    data = [r for r in data if len(r) > 14 and r[1].strip() and r[2].strip()]

    daily = defaultdict(lambda: defaultdict(float))
    dates = set()
    for row in data:
        date, ch = row[1], CH_MAP_RAW.get(row[2], row[2])
        if ch not in CHANNEL_ORDER:
            continue
        daily[date][ch] += parse_won(row[14])
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
        prod_rev[key] += parse_won(row[14])
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
        monthly_prod[ym][key] += parse_won(row[14])
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
            "source": "매출_RAW_CLEAN (거래건별 원본), 결제금액(+vat, 부가세 제외 공급가) 기준 집계",
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

    광고 쪽은 캠페인/광고세트/광고명에서 제품을 키워드로 추정하고(PRODUCT_AD_KEYWORDS),
    매칭되지 않으면 AD_UNCLASSIFIED로 묶는다. 매출 쪽은 매출_RAW_CLEAN의 단일 제품명을 그대로 쓴다.
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
        product = match_product_from_ad_text(f"{campaign} {adset} {ad}")
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
    rev_data = [r for r in rev_data if len(r) > 14 and r[1].strip() and r[2].strip()]

    rev_daily = defaultdict(lambda: defaultdict(float))
    for row in rev_data:
        date, ch = row[1], row[2]
        if ch != 'CAFE24':
            continue
        product = row[10].strip() or '미매핑 (분류 안 됨)'
        rev_daily[date][product] += parse_won(row[14])

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


WINNER_MIN_SPEND = 300000  # 이 금액(원) 미만 광고비를 쓴 소재는 위너 후보에서 제외 (노이즈 방지)
WINNER_TOP_N = 10
# 자사몰(CAFE24) 위너 소재는 이 3개 매체만 본다 (사용자 확정 범위)
WINNER_CHANNELS = ('FACEBOOK', 'GOOGLE_ADS', 'TIKTOK')


def build_creative_winners(ad_rows: list[list[str]]):
    """자사몰(CAFE24) 탭: 기간별/월별 위너 소재.

    FACEBOOK/GOOGLE_ADS/TIKTOK 중 자사몰(CAFE24)로 매핑되는 소재만 대상으로,
    캠페인/광고세트/광고 단위로 집계해 ROAS 상위 N개를 뽑는다.
    """
    header, *data = ad_rows
    data = [r for r in data if len(r) > 10 and r[0].strip()]
    data = [r for r in data if r[1] in WINNER_CHANNELS and map_ad_to_sales_channel(r[1], r[2]) == 'CAFE24']

    # (채널,캠페인,광고세트,광고) -> {'all': totals, 'by_month': {ym: totals}}
    creatives: dict[tuple, dict] = {}

    def blank_totals():
        return {"spend": 0.0, "impr": 0.0, "clicks": 0.0, "conv": 0.0, "value": 0.0}

    for row in data:
        date, ch, campaign, adset, ad = row[0], row[1], row[2], row[3], row[4]
        key = (ch, campaign, adset, ad)
        if key not in creatives:
            creatives[key] = {"all": blank_totals(), "by_month": {}}
        entry = creatives[key]
        ym = date[:7]
        if ym not in entry["by_month"]:
            entry["by_month"][ym] = blank_totals()
        for scope in (entry["all"], entry["by_month"][ym]):
            scope["spend"] += parse_pct_or_num(row[6])
            scope["impr"] += parse_pct_or_num(row[7])
            scope["clicks"] += parse_pct_or_num(row[8])
            scope["conv"] += parse_pct_or_num(row[9])
            scope["value"] += parse_pct_or_num(row[10])

    def rank(rows_for_scope, channel_filter=None):
        candidates = []
        for (ch, campaign, adset, ad), totals in rows_for_scope:
            if channel_filter and ch != channel_filter:
                continue
            if totals["spend"] < WINNER_MIN_SPEND:
                continue
            roas = round(totals["value"] / totals["spend"] * 100, 1) if totals["spend"] else 0
            candidates.append({
                "channel": ch, "campaign": campaign, "adset": adset, "ad": ad,
                "spend": round(totals["spend"]), "value": round(totals["value"]),
                "roas": roas,
                "cpm": round(totals["spend"] / totals["impr"] * 1000) if totals["impr"] else 0,
                "ctr": round(totals["clicks"] / totals["impr"] * 100, 2) if totals["impr"] else 0,
                "conversions": round(totals["conv"]),
            })
        candidates.sort(key=lambda c: -c["roas"])
        return candidates[:WINNER_TOP_N]

    all_scope = [(key, entry["all"]) for key, entry in creatives.items()]
    months = sorted({ym for entry in creatives.values() for ym in entry["by_month"]})

    channel_keys = ("ALL",) + WINNER_CHANNELS
    by_channel = {}
    for ch_key in channel_keys:
        cf = None if ch_key == "ALL" else ch_key
        month_ranked = {}
        for ym in months:
            month_scope = [(key, entry["by_month"][ym]) for key, entry in creatives.items() if ym in entry["by_month"]]
            month_ranked[ym] = rank(month_scope, cf)
        by_channel[ch_key] = {"all": rank(all_scope, cf), "by_month": month_ranked}

    return {
        "creative_winners": {
            "min_spend": WINNER_MIN_SPEND,
            "top_n": WINNER_TOP_N,
            "channels": list(channel_keys),
            "by_channel": by_channel,
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

    agg = {}
    agg.update(build_revenue(revenue_rows))
    agg.update(build_quantity(qty_rows))
    agg.update(build_product_master(master_rows))
    agg.update(build_ads(ads_rows))
    agg.update(build_cafe24_products(ads_rows, revenue_rows))
    agg["cafe24"]["creative_winners"] = build_creative_winners(ads_rows)["creative_winners"]

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
