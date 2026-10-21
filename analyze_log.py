"""
spread_log.csv 분석 → "기회가 언제, 어디서, 얼마나 있었나"
실행: python analyze_log.py
"""
import sys
import pandas as pd

from arb_monitor import LOG_FILE, THRESHOLD_BP, NOTIONAL_KRW

df = pd.read_csv(LOG_FILE, encoding="utf-8-sig", parse_dates=["time"])
df["signal"] = df["signal"].astype(str).str.lower() == "true"
df["hour"] = df["time"].dt.hour
if df.empty:
    sys.exit("로그가 비어 있습니다.")

span = df["time"].max() - df["time"].min()
print(f"관측 기간: {df['time'].min()} ~ {df['time'].max()} ({span})")
print(f"관측 수: {df['time'].nunique():,}회 / 거래금액 {NOTIONAL_KRW:,}원 / 신호기준 {THRESHOLD_BP}bp\n")

# 1) 코인·방향별 요약
g = df.groupby(["coin", "direction"])
summary = pd.DataFrame({
    "평균 순괴리bp": g["net_bp"].mean(),
    "최대 순괴리bp": g["net_bp"].max(),
    "순괴리>0 비율%": g["net_bp"].apply(lambda s: (s > 0).mean() * 100),
    "신호 비율%": g["signal"].mean() * 100,
    "평균 깊이슬립bp": g["depth_slip_bp"].mean(),
}).round(2).sort_values("신호 비율%", ascending=False)
print("■ 코인·방향별 요약")
print(summary.to_string(), "\n")

# 2) 시간대별 신호 빈도
hourly = df.groupby("hour")["signal"].mean().mul(100).round(1)
print("■ 시간대별 신호 비율(%)")
print(hourly.to_string(), "\n")

# 3) 신호가 몇 번 연속 유지됐나 (= 실제로 손으로 잡을 시간이 있었나)
sig = df[df["signal"]].sort_values("time")
if sig.empty:
    print("■ 신호 없음 → 수수료 대비 괴리가 작다는 뜻. THRESHOLD_BP·코인·거래금액을 바꿔 비교해보세요.")
else:
    print(f"■ 신호 {len(sig)}건, 이론상 순손익 합계 {sig['net_krw'].sum():,.0f}원 "
          "(※ 모든 신호를 즉시 체결했다는 가정 — 실제와의 차이를 매매일지로 확인)")

out = "spread_summary.csv"
summary.to_csv(out, encoding="utf-8-sig")
print(f"\n요약 저장: {out}")
