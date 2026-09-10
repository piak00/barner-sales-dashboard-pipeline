"""
오덴틱 매출·광고 대시보드 데이터 새로고침 파이프라인.

바르너 파이프라인(../refresh.py)과 동일한 방식: 구글시트가 '링크가 있는 모든 사용자
(뷰어)'로 공유돼 있으면 인증 없이 export CSV URL로 원본을 받아올 수 있다. 오덴틱 시트는
바르너 시트를 복제해서 만들어져 있어 탭 gid가 바르너와 동일하다(매출_RAW_CLEAN=26090317 등).

사용법:
    python3 refresh.py
    -> 이 디렉터리에 dashboard_output.html 생성
    -> 이후 Artifact publish 도구로 기존 오덴틱 대시보드 URL에 재배포하면 됨

전제 조건: 아래 SHEET_ID 스프레드시트가 "링크가 있는 모든 사용자 - 뷰어"로 공유돼 있어야 한다.
현재는 비공개라 fetch_csv가 로그인 페이지 HTML을 받아온다 — 공유 설정을 바꾼 뒤 실행할 것.
"""
import csv
import json
import subprocess
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
SHEET_ID = "1fYXpZiI_TfSKx6Wo34d1YpyIYiAjl6uqM5RFU9QHF1A"
GIDS = {
    "revenue": "26090317",       # 매출_RAW_CLEAN
    "product_master": "26090301",  # 표준제품_MASTER
    "ads": "748935331",          # 오덴틱_광고소재 (IMPORTRANGE, 15만+ 행 — curl로 그대로 받아도 됨)
}
TEMPLATE_PATH = HERE / "template.html"
OUTPUT_PATH = HERE / "dashboard_output.html"
AGG_PATH = HERE / "dashboard_agg.json"

# 자사몰 채널 코드. 외부몰(COUPANG 등)은 매출_RAW_CLEAN에 나타나는 값을 그대로 채널명으로 쓴다.
SELF_MALL_CHANNEL = "CAFE24"
# 손익 가정: 원가율 15% / 배송비율 7.5% / 수수료율 3.2% (오덴틱 9월 시나리오 탭 기준, 전 채널 공통 적용 근사치)
COGS_RATE, SHIP_RATE, FEE_RATE = 0.15, 0.075, 0.032

# 매출_RAW_CLEAN 컬럼: RAW_ID,일자,판매처,페이지명,옵션명,옵션판매량,결제금액,매핑키,분류유형,
#                      단일제품코드,단일제품명,매핑상태,매출구분,공구명,프로모션명,결제금액(-vat)
COL_DATE, COL_CHANNEL, COL_PRODUCT_CODE, COL_PRODUCT_NAME, COL_AMOUNT_VAT_EX = 1, 2, 9, 10, 15

# 오덴틱_광고소재 컬럼(바르너_광고소재와 동일 레이아웃):
# date,광고채널,캠페인,광고세트,광고,이미지,광고비,노출수,클릭수,전환수,전환값,...
COL_AD_DATE, COL_AD_CHANNEL, COL_AD_CAMPAIGN, COL_AD_ADSET, COL_AD_NAME = 0, 1, 2, 3, 4
COL_AD_SPEND, COL_AD_IMPR, COL_AD_CLICKS, COL_AD_CONV, COL_AD_VALUE = 6, 7, 8, 9, 10

# 소재별 성과 세그먼트에서 고를 매체. 지금은 Meta만 데이터가 있고, Google·TikTok은 시트에
# 값이 생기는 대로 이 목록 안에서 자동으로 표시된다(코드 수정 불필요) — 목록 자체를 늘리려면
# 여기에 채널 코드만 추가하면 됨.
CREATIVE_CHANNELS = ["FACEBOOK", "GOOGLE_ADS", "TIKTOK"]
CREATIVE_CHANNEL_LABEL = {"FACEBOOK": "Meta", "GOOGLE_ADS": "Google", "TIKTOK": "TikTok"}


def fetch_csv(gid: str) -> list[list[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    out = subprocess.run(["curl", "-sL", url], capture_output=True, check=True)
    text = out.stdout.decode("utf-8")
    rows = list(csv.reader(text.splitlines()))
    if rows and "<!DOCTYPE" in (rows[0][0] if rows[0] else ""):
        raise RuntimeError(
            f"gid={gid}: CSV 대신 로그인/권한 페이지가 돌아왔습니다. "
            f"시트 공유 설정을 '링크가 있는 모든 사용자 - 뷰어'로 바꿔주세요."
        )
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


def build_revenue(rows: list[list[str]]):
    header, *data = rows
    data = [r for r in data if len(r) > COL_AMOUNT_VAT_EX and r[COL_DATE].strip() and r[COL_CHANNEL].strip()]

    daily = defaultdict(lambda: defaultdict(float))
    prod_daily = defaultdict(lambda: defaultdict(float))
    dates, channels_seen, products_seen = set(), set(), set()
    for row in data:
        date, ch = row[COL_DATE], row[COL_CHANNEL]
        amt = parse_won(row[COL_AMOUNT_VAT_EX])
        daily[date][ch] += amt
        dates.add(date)
        channels_seen.add(ch)
        prod = row[COL_PRODUCT_NAME].strip()
        if prod:
            prod_daily[date][prod] += amt
            products_seen.add(prod)
    dates = sorted(dates)
    # 채널 순서: 자사몰을 항상 맨 앞에, 나머지는 누적 매출 내림차순 (신규 채널이 생기면 자동으로 뒤에 붙는다)
    channel_totals_tmp = {c: sum(daily[d].get(c, 0) for d in dates) for c in channels_seen}
    channel_order = [SELF_MALL_CHANNEL] + sorted(
        (c for c in channels_seen if c != SELF_MALL_CHANNEL),
        key=lambda c: -channel_totals_tmp.get(c, 0),
    )
    product_order = sorted(products_seen, key=lambda p: -sum(prod_daily[d].get(p, 0) for d in dates))

    daily_series = []
    for dt in dates:
        r = {"date": dt}
        for c in channel_order:
            r[c] = round(daily[dt].get(c, 0))
        r["total"] = sum(r[c] for c in channel_order)
        for p in product_order:
            r["product__" + p] = round(prod_daily[dt].get(p, 0))
        daily_series.append(r)

    channel_totals = {c: round(channel_totals_tmp.get(c, 0)) for c in channel_order}
    grand_total = sum(channel_totals.values())
    product_totals = {p: round(sum(prod_daily[d].get(p, 0) for d in dates)) for p in product_order}

    return {
        "channels": channel_order,
        "products": product_order,
        "daily_series": daily_series,
        "channel_totals": channel_totals,
        "product_totals": product_totals,
        "grand_total": grand_total,
        "meta": {"revenue_date_range": [dates[0], dates[-1]] if dates else None},
    }


def build_ads(rows: list[list[str]]):
    header, *data = rows
    data = [r for r in data if len(r) > COL_AD_VALUE and r[COL_AD_DATE].strip()]

    daily = defaultdict(lambda: defaultdict(lambda: {"spend": 0.0, "impr": 0.0, "clicks": 0.0, "conv": 0.0, "value": 0.0}))
    dates, channels_seen = set(), set()
    for row in data:
        date, ch = row[COL_AD_DATE], row[COL_AD_CHANNEL]
        d = daily[date][ch]
        d["spend"] += parse_won(row[COL_AD_SPEND])
        d["impr"] += parse_won(row[COL_AD_IMPR])
        d["clicks"] += parse_won(row[COL_AD_CLICKS])
        d["conv"] += parse_won(row[COL_AD_CONV])
        d["value"] += parse_won(row[COL_AD_VALUE])
        dates.add(date)
        channels_seen.add(ch)
    dates = sorted(dates)
    # 매체 순서: 누적 광고비 내림차순 — Google/TikTok 등 신규 매체가 생기면 자동으로 목록에 들어온다
    channel_totals_tmp = {c: sum(daily[d][c]["spend"] for d in dates if c in daily[d]) for c in channels_seen}
    channel_order = sorted(channels_seen, key=lambda c: -channel_totals_tmp.get(c, 0))

    daily_series = []
    daily_by_channel = []  # long format: {date, channel, spend, impr, clicks, conv, value} — 날짜 범위로
    # 필터링한 뒤 매체별로 다시 집계할 수 있도록 매체별 일별 값을 그대로 내려보낸다(원본 15만+ 행이
    # 아니라 날짜x매체 조합 수준이라 매우 작다).
    for dt in dates:
        r = {"date": dt, "spend": 0.0, "impr": 0.0, "clicks": 0.0, "conv": 0.0, "value": 0.0}
        for c in channel_order:
            d = daily[dt].get(c)
            if not d:
                continue
            for k in ("spend", "impr", "clicks", "conv", "value"):
                r[k] += d[k]
            daily_by_channel.append({
                "date": dt, "channel": c,
                "spend": round(d["spend"]), "impr": round(d["impr"]), "clicks": round(d["clicks"]),
                "conv": round(d["conv"]), "value": round(d["value"]),
            })
        for k in ("spend", "impr", "clicks", "conv", "value"):
            r[k] = round(r[k])
        daily_series.append(r)

    channel_totals = {c: round(channel_totals_tmp.get(c, 0)) for c in channel_order}
    grand_spend = sum(channel_totals.values())
    grand_value = round(sum(daily[d][c]["value"] for d in dates for c in daily[d]))
    grand_impr = round(sum(daily[d][c]["impr"] for d in dates for c in daily[d]))
    grand_clicks = round(sum(daily[d][c]["clicks"] for d in dates for c in daily[d]))
    grand_conv = round(sum(daily[d][c]["conv"] for d in dates for c in daily[d]))

    channel_stats = []
    for c in channel_order:
        spend = channel_totals[c]
        impr = round(sum(daily[d][c]["impr"] for d in dates if c in daily[d]))
        clicks = round(sum(daily[d][c]["clicks"] for d in dates if c in daily[d]))
        value = round(sum(daily[d][c]["value"] for d in dates if c in daily[d]))
        channel_stats.append({
            "channel": c, "spend": spend, "impr": impr, "clicks": clicks, "value": value,
            "ctr": round(clicks / impr * 100, 2) if impr else 0,
            "cpc": round(spend / clicks) if clicks else 0,
            "roas": round(value / spend * 100, 1) if spend else 0,
        })

    # 소재(크리에이티브) 단위 — CREATIVE_CHANNELS에 포함된 매체만, 소재명 기준으로 합산
    creatives_by_channel = {}
    for ch in CREATIVE_CHANNELS:
        agg = defaultdict(lambda: {"spend": 0.0, "impr": 0.0, "clicks": 0.0, "conv": 0.0, "value": 0.0})
        for row in data:
            if row[COL_AD_CHANNEL] != ch:
                continue
            ad = row[COL_AD_NAME]
            a = agg[ad]
            a["spend"] += parse_won(row[COL_AD_SPEND])
            a["impr"] += parse_won(row[COL_AD_IMPR])
            a["clicks"] += parse_won(row[COL_AD_CLICKS])
            a["conv"] += parse_won(row[COL_AD_CONV])
            a["value"] += parse_won(row[COL_AD_VALUE])
        items = []
        for ad, a in agg.items():
            items.append({
                "ad": ad, "spend": round(a["spend"]), "impr": round(a["impr"]), "clicks": round(a["clicks"]),
                "conv": round(a["conv"]), "value": round(a["value"]),
                "ctr": round(a["clicks"] / a["impr"] * 100, 2) if a["impr"] else 0,
                "roas": round(a["value"] / a["spend"] * 100, 1) if a["spend"] else 0,
            })
        items.sort(key=lambda x: -x["value"])
        creatives_by_channel[ch] = items

    return {
        "ad_channels": channel_order,
        "ad_channel_label": {c: CREATIVE_CHANNEL_LABEL.get(c, c) for c in channel_order},
        "ad_daily_series": daily_series,
        "ad_daily_by_channel": daily_by_channel,
        "ad_channel_totals": channel_totals,
        "ad_channel_stats": channel_stats,
        "ad_grand_spend": grand_spend,
        "ad_grand_value": grand_value,
        "ad_grand_impr": grand_impr,
        "ad_grand_clicks": grand_clicks,
        "ad_grand_conv": grand_conv,
        "ad_grand_roas": round(grand_value / grand_spend * 100, 1) if grand_spend else 0,
        "creative_channels": CREATIVE_CHANNELS,
        "creative_channel_label": CREATIVE_CHANNEL_LABEL,
        "creatives_by_channel": creatives_by_channel,
        "ad_meta": {"date_range": [dates[0], dates[-1]] if dates else None},
    }


def build_margin(revenue: dict, ads: dict):
    """일별 공헌이익 = 매출 - (광고비 + 원가15% + 배송비7.5% + 수수료3.2%).

    시트1(오덴틱 마스터)의 자사몰/판매처별 트래커가 쓰는 것과 같은 비용율 가정을 매출_RAW_CLEAN
    기준으로 재계산한 근사치 — 채널별로 세분화된 수수료율(MIX%)까지는 반영하지 않는다.
    """
    spend_by_date = {r["date"]: r["spend"] for r in ads["ad_daily_series"]}
    out = []
    for r in revenue["daily_series"]:
        rev = r["total"]
        spend = spend_by_date.get(r["date"], 0)
        cost = rev * (COGS_RATE + SHIP_RATE + FEE_RATE)
        margin = rev - spend - cost
        out.append({
            "date": r["date"], "revenue": rev, "spend": spend, "margin": round(margin),
            "roas": round(rev / spend * 100, 1) if spend else None,
        })
    return out


def main():
    print("fetching 매출_RAW_CLEAN ...")
    revenue_rows = fetch_csv(GIDS["revenue"])
    print("fetching 오덴틱_광고소재 ...", "(15만+ 행, 시간이 걸릴 수 있음)")
    ads_rows = fetch_csv(GIDS["ads"])

    agg = {}
    agg["revenue"] = build_revenue(revenue_rows)
    agg["ads"] = build_ads(ads_rows)
    agg["margin_daily"] = build_margin(agg["revenue"], agg["ads"])
    agg["breakeven_roas"] = round((1 / (1 - COGS_RATE - SHIP_RATE - FEE_RATE)) * 100, 1)

    AGG_PATH.write_text(json.dumps(agg, ensure_ascii=False), encoding="utf-8")
    print("wrote", AGG_PATH)

    data_json = json.dumps(agg, ensure_ascii=False, separators=(",", ":"))
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "__ODENTIC_DATA_JSON__" in template, "template placeholder missing"
    output = template.replace("__ODENTIC_DATA_JSON__", data_json)
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    print("wrote", OUTPUT_PATH, "size", len(output))
    print("grand_total(revenue)=", agg["revenue"]["grand_total"], "grand_spend(ads)=", agg["ads"]["ad_grand_spend"])


if __name__ == "__main__":
    main()
