"""
업비트 ↔ 빗썸 현물 차익 모니터 (주문 기능 없음, 관찰/기록 전용)

핵심 개념
- 재고(인벤토리) 모델: 양 거래소에 원화와 코인을 미리 나눠 두고
  '싼 곳 매수 + 비싼 곳 매도'를 동시에 실행한다고 가정 → 코인 송금 없음
- 호가 깊이 반영: 설정한 거래금액(NOTIONAL_KRW)만큼 실제로 체결될 때의
  평균가(VWAP)로 계산 → 최우선호가 괴리와의 차이 = 호가 깊이 슬리피지
- 순괴리 = 총괴리 − 양쪽 매매수수료  (단위: bp, 1bp = 0.01%)

실행 (Windows PowerShell / CMD)
    pip install requests
    python arb_monitor.py            # 실시간 모니터링 (Ctrl+C 종료)
    python arb_monitor.py --once     # 1회만 조회
    python arb_monitor.py --mock     # 가짜 호가로 동작 테스트
"""

import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime

import requests

# ───────────────────────── 설정 (본인 운용규칙에 맞게 수정) ─────────────────────────
COINS = ["BTC", "ETH", "XRP", "SOL", "DOGE"]

# 매매수수료(소수). ※ 본인 계정의 실제 수수료 등급·쿠폰 적용 여부를 확인 후 수정
FEES = {
    "UPBIT": 0.0005,    # 0.05%
    "BITHUMB": 0.0004,  # 0.04%
}

NOTIONAL_KRW = 100_000   # 1회 가정 거래금액 (운용규칙의 '1회 최대 거래금액'과 맞출 것)
THRESHOLD_BP = 10        # 이 이상 순괴리일 때 신호 표시 (운용규칙의 '최소 진입 순괴리')
INTERVAL_SEC = 5         # 조회 주기 (너무 짧게 잡지 말 것: API 호출 제한)
LOG_FILE = "spread_log.csv"
TIMEOUT = 5
# ─────────────────────────────────────────────────────────────────────────────


# ───────────────────────── 호가 수집 ─────────────────────────
def _parse_v1(data):
    """업비트 / 빗썸 v1 공통 호가 포맷 → {coin: {'bids': [(p,q)], 'asks': [(p,q)]}}"""
    books = {}
    for item in data:
        coin = item["market"].split("-")[1]
        units = item["orderbook_units"]
        books[coin] = {
            "bids": [(float(u["bid_price"]), float(u["bid_size"])) for u in units],
            "asks": [(float(u["ask_price"]), float(u["ask_size"])) for u in units],
        }
    return books


def fetch_upbit(coins):
    markets = ",".join(f"KRW-{c}" for c in coins)
    r = requests.get("https://api.upbit.com/v1/orderbook",
                     params={"markets": markets}, timeout=TIMEOUT)
    r.raise_for_status()
    return _parse_v1(r.json())


def fetch_bithumb(coins):
    """빗썸 v1 API 우선, 실패 시 구(public) API로 코인별 조회"""
    markets = ",".join(f"KRW-{c}" for c in coins)
    try:
        r = requests.get("https://api.bithumb.com/v1/orderbook",
                         params={"markets": markets}, timeout=TIMEOUT)
        r.raise_for_status()
        return _parse_v1(r.json())
    except Exception:
        books = {}
        for c in coins:
            r = requests.get(f"https://api.bithumb.com/public/orderbook/{c}_KRW",
                             timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()["data"]
            books[c] = {
                "bids": [(float(x["price"]), float(x["quantity"])) for x in d["bids"]],
                "asks": [(float(x["price"]), float(x["quantity"])) for x in d["asks"]],
            }
        return books


def fetch_mock(coins, venue):
    """테스트용 가짜 호가 (거래소마다 약간씩 다른 가격)"""
    base = {"BTC": 140_000_000, "ETH": 5_000_000, "XRP": 3_400, "SOL": 250_000, "DOGE": 300}
    books = {}
    for c in coins:
        mid = base.get(c, 1000) * (1 + random.uniform(-0.002, 0.002))
        tick = mid * 0.0001
        books[c] = {
            "bids": [(mid - tick * (i + 1), random.uniform(0.001, 5) * 1e6 / mid) for i in range(15)],
            "asks": [(mid + tick * (i + 1), random.uniform(0.001, 5) * 1e6 / mid) for i in range(15)],
        }
    return books


# ───────────────────────── 체결 시뮬레이션 ─────────────────────────
def buy_with_krw(asks, krw):
    """매도호가를 위에서부터 먹으며 krw만큼 매수 → (수량, 실제 지출, 전량체결 여부)"""
    qty, spent = 0.0, 0.0
    for price, size in asks:
        level_krw = price * size
        if spent + level_krw >= krw:
            q = (krw - spent) / price
            return qty + q, krw, True
        qty += size
        spent += level_krw
    return qty, spent, False


def sell_qty(bids, qty):
    """매수호가에 qty만큼 매도 → (매도대금, 전량체결 여부)"""
    left, proceeds = qty, 0.0
    for price, size in bids:
        q = min(left, size)
        proceeds += q * price
        left -= q
        if left <= 1e-12:
            return proceeds, True
    return proceeds, False


def evaluate(coin, buy_venue, buy_book, sell_venue, sell_book):
    best_ask = buy_book["asks"][0][0]
    best_bid = sell_book["bids"][0][0]
    top_gross_bp = (best_bid - best_ask) / best_ask * 1e4

    qty, cost, f1 = buy_with_krw(buy_book["asks"], NOTIONAL_KRW)
    proceeds, f2 = sell_qty(sell_book["bids"], qty)
    gross_bp = (proceeds - cost) / cost * 1e4
    fee_krw = cost * FEES[buy_venue] + proceeds * FEES[sell_venue]
    fee_bp = fee_krw / cost * 1e4
    net_krw = proceeds - cost - fee_krw
    net_bp = gross_bp - fee_bp

    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "coin": coin,
        "direction": f"{buy_venue}->{sell_venue}",
        "best_ask_buy": best_ask,
        "best_bid_sell": best_bid,
        "top_gross_bp": round(top_gross_bp, 2) + 0.0,
        "vwap_gross_bp": round(gross_bp, 2) + 0.0,
        "depth_slip_bp": round(top_gross_bp - gross_bp, 2) + 0.0,
        "fee_bp": round(fee_bp, 2) + 0.0,
        "net_bp": round(net_bp, 2) + 0.0,
        "net_krw": round(net_krw, 1),
        "qty": qty,
        "fully_filled": f1 and f2,
        "signal": (net_bp >= THRESHOLD_BP) and f1 and f2,
    }


# ───────────────────────── 기록 / 출력 ─────────────────────────
FIELDS = ["time", "coin", "direction", "best_ask_buy", "best_bid_sell", "top_gross_bp",
          "vwap_gross_bp", "depth_slip_bp", "fee_bp", "net_bp", "net_krw", "qty",
          "fully_filled", "signal"]


def append_log(rows):
    new = not os.path.exists(LOG_FILE)
    # utf-8-sig: 엑셀에서 열어도 한글이 깨지지 않음
    with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def print_table(rows):
    print(f"\n[{rows[0]['time']}]  거래금액 {NOTIONAL_KRW:,}원 / 신호기준 순괴리 ≥ {THRESHOLD_BP}bp")
    print(f"{'코인':<5}{'방향':<17}{'총괴리':>8}{'깊이슬립':>9}{'수수료':>8}{'순괴리':>8}{'순손익(원)':>12}")
    for r in sorted(rows, key=lambda x: x["net_bp"], reverse=True):
        mark = "  ◀ 신호" if r["signal"] else ("  (호가부족)" if not r["fully_filled"] else "")
        print(f"{r['coin']:<6}{r['direction']:<18}{r['vwap_gross_bp']:>8.1f}"
              f"{r['depth_slip_bp']:>9.1f}{r['fee_bp']:>8.1f}{r['net_bp']:>8.1f}"
              f"{r['net_krw']:>12,.0f}{mark}")


def run_once(mock=False):
    if mock:
        up, bt = fetch_mock(COINS, "UPBIT"), fetch_mock(COINS, "BITHUMB")
    else:
        up, bt = fetch_upbit(COINS), fetch_bithumb(COINS)
    rows = []
    for c in COINS:
        if c not in up or c not in bt:
            continue
        rows.append(evaluate(c, "UPBIT", up[c], "BITHUMB", bt[c]))
        rows.append(evaluate(c, "BITHUMB", bt[c], "UPBIT", up[c]))
    append_log(rows)
    print_table(rows)
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--mock", action="store_true")
    a = p.parse_args()

    if a.once:
        run_once(a.mock)
        return
    print("모니터링 시작 (Ctrl+C로 종료). 로그:", os.path.abspath(LOG_FILE))
    while True:
        try:
            run_once(a.mock)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[오류] {e} — {INTERVAL_SEC}초 후 재시도", file=sys.stderr)
        try:
            time.sleep(INTERVAL_SEC)
        except KeyboardInterrupt:
            break
    print("\n종료. 분석: python analyze_log.py")


if __name__ == "__main__":
    main()
