#!/usr/bin/env python3
"""
Tính 3 phương án giảm chi phí và kiểm chứng số liệu, dùng số ĐÃ TÍNH SẴN
trong monthly-metrics.json + model-metrics.json.

Script này không đọc lại CSV. Nó chỉ tổ hợp các số đã kiểm chứng, để mọi con số
trong báo cáo đều truy được về một file JSON cụ thể.

Ba phương án:
  A. Thu hồi seat của user không dùng / dùng rất ít
  B. Right-size tier theo mức dùng thật của tháng căn cứ
  C. Đổi model: chuyển một phần message từ Opus sang auto/sonnet

VÍ DỤ:
    python3 scripts/scenarios.py --monthly data/monthly-metrics.json \
        --model data/model-metrics.json --json data/scenarios.json
"""
from __future__ import annotations

import argparse
import json
import os

TIERS = {"FREE": (0, 50), "PRO": (20, 1_000), "PRO_PLUS": (40, 2_000),
         "PRO_MAX": (100, 5_000), "POWER": (200, 10_000)}
TIER_ORDER = ["FREE", "PRO", "PRO_PLUS", "PRO_MAX", "POWER"]
HEADROOM = 0.90


def fit_tier(cred: float) -> tuple[str, int]:
    for t in TIER_ORDER:
        usd, lim = TIERS[t]
        if cred <= lim * HEADROOM:
            return t, usd
    return "POWER", TIERS["POWER"][0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--monthly", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--min-active-days", type=int, default=10,
                    help="Không hạ gói user có ít ngày hoạt động hơn mức này trong tháng căn cứ "
                         "(mặc định 10). Người mới vào giữa tháng có số liệu bị hạ thấp — đã từng hạ nhầm "
                         "một người về sau bị chặn.")
    ap.add_argument("--crosscheck",
                    help="Đối chiếu với nguồn độc lập. Định dạng: "
                         "'nhãn=chỉ_số:giá_trị,chỉ_số:giá_trị'. Ví dụ: "
                         "'reports/shared/x.html=user active:72,credit:195831,ngày báo cáo:29'")
    ap.add_argument("--json")
    args = ap.parse_args()

    M = json.load(open(args.monthly, encoding="utf-8"))
    D = json.load(open(args.model, encoding="utf-8"))

    basis = M["basisMonth"]
    bm = M["months"][basis]
    cov = M["coverage"][basis]
    censored_keys = {(c["month"], c["userId"]) for c in D["censored"]}
    coef = D["nnls"]["creditPerMessageByModel"]

    print("=" * 100)
    print(f"CĂN CỨ: tháng {basis} — {cov['daysWithData']}/{cov['daysInMonth']} ngày dữ liệu "
          f"({cov['first']} → {cov['last']})")
    print(f"  {bm['totals']['activeUsers']} user active · "
          f"{bm['totals']['credits']:,.0f} credit · {bm['totals']['messages']:,} message · "
          f"{bm['totals']['creditPerMessage']:.4f} credit/msg")
    print(f"  Tổng subscription: {sum(M['tierDistribution'].values())} user = "
          f"${M['rightSize']['currentMonthlyUsd']}/tháng = "
          f"${M['rightSize']['currentMonthlyUsd']*12}/năm")
    print("=" * 100)
    print()

    users = {u["userId"]: u for u in bm["users"]}
    all_rows = M["rightSize"]["rows"]
    cur_total = M["rightSize"]["currentMonthlyUsd"]

    # ---------------- PHƯƠNG ÁN A: thu hồi seat ----------------
    print("PHƯƠNG ÁN A — THU HỒI SEAT")
    print("-" * 100)
    bands = [(0.0, "không dùng (0 credit)"),
             (0.5, "< 0,5% hạn mức"),
             (5.0, "< 5% hạn mức"),
             (10.0, "< 10% hạn mức")]
    a_out = []
    for thr, label in bands:
        pick = []
        for r in all_rows:
            u = users.get(r["userId"])
            pct = u["pctOfLimit"] if u else 0.0
            if (pct == 0.0 and thr == 0.0) or (thr > 0 and pct < thr):
                pick.append(r)
        usd = sum(p["currentUsd"] for p in pick)
        print(f"  {label:26} {len(pick):3d} seat  →  tiết kiệm ${usd:>5}/tháng = "
              f"${usd*12:>6}/năm")
        a_out.append({"threshold_pct": thr, "label": label, "seats": len(pick),
                      "monthlyUsd": usd, "yearlyUsd": usd * 12,
                      "emails": [p["email"] or p["userId"] for p in pick]})
    print("  Lưu ý: user 0 credit trong tháng căn cứ vẫn có thể đang onboarding.")
    print("         Xác nhận với quản lý trực tiếp trước khi thu hồi.")
    print()

    # ---------------- PHƯƠNG ÁN B: right-size ----------------
    print("PHƯƠNG ÁN B — RIGHT-SIZE TIER")
    print("-" * 100)
    b_rows, b_new, kept_censored, kept_thin = [], 0, [], []
    active = {u["userId"]: u.get("activeDays", 0)
              for u in (M["months"].get(basis, {}).get("users") or [])}
    for r in all_rows:
        uid = r["userId"]
        is_cens = (basis, uid) in censored_keys
        if is_cens:
            # dữ liệu bị cắt → giữ nguyên tier, không hạ
            rec, usd = r["currentTier"], r["currentUsd"]
            kept_censored.append(r["email"] or uid)
        else:
            rec, usd = fit_tier(r["basisCredits"])
            if usd < r["currentUsd"] and active.get(uid, 0) < args.min_active_days:
                # quá ít ngày dữ liệu → chưa đủ căn cứ hạ gói
                rec, usd = r["currentTier"], r["currentUsd"]
                kept_thin.append(f'{r["email"] or uid} ({active.get(uid, 0)}d)')
        b_new += usd
        b_rows.append({**r, "safeRecommendedTier": rec, "safeRecommendedUsd": usd,
                       "censored": is_cens, "safeDeltaUsd": usd - r["currentUsd"]})
    naive = M["rightSize"]["rightSizedMonthlyUsd"]
    print(f"  Right-size thô (script template)      : ${naive}/tháng  "
          f"(tiết kiệm ${cur_total-naive}/tháng)")
    print(f"  Right-size AN TOÀN (giữ user bị chặn) : ${b_new}/tháng  "
          f"(tiết kiệm ${cur_total-b_new}/tháng = ${(cur_total-b_new)*12}/năm)")
    print(f"  → {len(kept_censored)} user bị chặn trong tháng {basis} được GIỮ NGUYÊN tier: "
          f"{', '.join(kept_censored)}")
    print("    Lý do: mức dùng của họ bị hạn mức cắt ngang, nhu cầu thật cao hơn số quan sát.")
    if kept_thin:
        print(f"  → {len(kept_thin)} user CHƯA ĐỦ {args.min_active_days} ngày hoạt động — giữ nguyên, xem lại kỳ sau: "
              f"{', '.join(kept_thin)}")
    down = [r for r in b_rows if r["safeDeltaUsd"] < 0]
    print(f"  → {len(down)} user hạ tier được")
    print()

    # ---------------- PHƯƠNG ÁN C: đổi model ----------------
    print("PHƯƠNG ÁN C — ĐỔI MODEL (không đổi tier, không thu hồi seat)")
    print("-" * 100)
    msgs = D["messagesByModel"]
    c_auto = coef.get("auto", 0)
    c_son = coef.get("claude_sonnet_5", 0)
    opus_models = {k: v for k, v in msgs.items() if "opus" in k}
    opus_msgs = sum(opus_models.values())
    opus_credits = sum(coef.get(k, 0) * v for k, v in opus_models.items())
    opus_cpm = opus_credits / opus_msgs if opus_msgs else 0

    print(f"  Toàn bộ {len(msgs)} model · {D['totalMessages']:,} message "
          f"(cả kỳ {len(M['days'])} ngày)")
    print(f"  Nhóm Opus     : {opus_msgs:>9,} msg ({opus_msgs/D['totalMessages']*100:5.2f}%) "
          f"· {opus_credits:>9,.0f} credit · {opus_cpm:.4f} cr/msg")
    print(f"  auto          : {msgs.get('auto',0):>9,} msg "
          f"({msgs.get('auto',0)/D['totalMessages']*100:5.2f}%) "
          f"· {c_auto*msgs.get('auto',0):>9,.0f} credit · {c_auto:.4f} cr/msg")
    print(f"  claude_sonnet_5:{msgs.get('claude_sonnet_5',0):>9,} msg "
          f"({msgs.get('claude_sonnet_5',0)/D['totalMessages']*100:5.2f}%) "
          f"· {c_son*msgs.get('claude_sonnet_5',0):>9,.0f} credit · {c_son:.4f} cr/msg")
    if c_auto:
        print(f"  → Opus đắt hơn auto {opus_cpm/c_auto:.1f}× mỗi message")
    print()
    print(f"  {'Chuyển % Opus':>14} {'→ auto: credit tiết kiệm':>28} "
          f"{'→ sonnet_5: credit tiết kiệm':>30}")
    c_out = []
    for pct in (10, 25, 50):
        n = opus_msgs * pct / 100
        s_auto = n * (opus_cpm - c_auto)
        s_son = n * (opus_cpm - c_son)
        # quy về 1 tháng theo tỷ lệ ngày
        f = cov["daysWithData"] / len(M["days"])
        print(f"  {pct:>13}% {s_auto:>19,.0f} credit/kỳ "
              f"{s_son:>21,.0f} credit/kỳ")
        c_out.append({"shiftPct": pct, "opusMessagesShifted": round(n),
                      "creditsSavedToAuto": round(s_auto), "creditsSavedToSonnet5": round(s_son),
                      "creditsSavedToAutoPerBasisMonth": round(s_auto * f),
                      "creditsSavedToSonnet5PerBasisMonth": round(s_son * f)})
    print()
    print(f"  Quy đổi về 1 tháng cỡ tháng {basis} "
          f"({cov['daysWithData']}/{len(M['days'])} ngày của kỳ quan sát):")
    for c in c_out:
        print(f"    chuyển {c['shiftPct']:>2}% Opus → auto: "
              f"~{c['creditsSavedToAutoPerBasisMonth']:>7,} credit/tháng tiết kiệm "
              f"(≈ {c['creditsSavedToAutoPerBasisMonth']/10000:.1f} seat POWER)")
    print()
    print("  Vì sao phương án này đáng làm trước: nó KHÔNG cắt quyền của ai, KHÔNG cần")
    print("  phê duyệt ngân sách, và giải quyết đúng nguyên nhân user bị chặn giữa tháng.")
    print()

    # ---------------- kiểm chứng ----------------
    print("KIỂM CHỨNG SỐ LIỆU")
    print("-" * 100)
    at_risk = [u for u in bm["users"] if u["pctOfLimit"] >= 75]
    checks = [
        (f"user active tháng {basis}", bm["totals"]["activeUsers"]),
        (f"credit tháng {basis}", round(bm["totals"]["credits"])),
        (f"message tháng {basis}", bm["totals"]["messages"]),
        (f"ngày báo cáo tháng {basis}", cov["daysWithData"]),
        (f"user ≥75% hạn mức tháng {basis}", len(at_risk)),
        ("tổng overage credit đã dùng",
         round(sum(u["overageCreditsUsed"] for mo in M["months"]
                   for u in M["months"][mo]["users"]), 4)),
        ("tổng subscription", sum(M["tierDistribution"].values())),
        ("R² của hồi quy model", D["nnls"]["r2"]),
    ]
    for k, v in checks:
        print(f"  {k:42} {v}")
    print()

    # Đối chiếu với nguồn ĐỘC LẬP nếu có. Nhiều tổ chức tự lập báo cáo Kiro rồi để trong
    # cùng bucket (prefix reports/) hoặc gửi qua email — đó là cơ hội kiểm chứng tốt nhất.
    # Truyền qua --crosscheck 'nhãn=nguồn:chỉ_số=giá_trị,chỉ_số=giá_trị'
    cross = None
    if args.crosscheck:
        try:
            label, rest = args.crosscheck.split("=", 1)
            pairs = dict(p.split(":") for p in rest.split(","))
            cross = {"source": label, "values": pairs, "match": {}}
            print("ĐỐI CHIẾU VỚI NGUỒN ĐỘC LẬP")
            print("-" * 100)
            print(f"  Nguồn: {label}")
            ours = {k: v for k, v in checks}
            for ck, cv in pairs.items():
                mine = next((v for k, v in checks if ck.lower() in k.lower()), None)
                same = mine is not None and abs(float(mine) - float(cv)) < 1
                cross["match"][ck] = {"theirs": cv, "ours": mine, "match": same}
                print(f"  {'KHỚP  ' if same else 'LỆCH  '} {ck:32} "
                      f"họ={cv:>12}  ta={mine}")
            allm = all(x["match"] for x in cross["match"].values())
            cross["allMatch"] = allm
            print(f"  → {'KHỚP TOÀN BỘ' if allm else 'CÓ CHỈ SỐ LỆCH — điều tra trước khi dùng số'}")
        except (ValueError, TypeError) as e:
            print(f"  [!] Không đọc được --crosscheck ({e}). Định dạng: "
                  f"'nhãn=chỉ_số:giá_trị,chỉ_số:giá_trị'")
            cross = None
    else:
        print("  [i] Không có nguồn đối chiếu độc lập (--crosscheck).")
        print("      Kiểm tra prefix reports/ trong bucket — nhiều tổ chức tự lập báo cáo Kiro")
        print("      và để ở đó. Đối chiếu được là bằng chứng mạnh nhất cho độ tin cậy.")
    print()

    if args.json:
        out = {
            "basisMonth": basis, "basisCoverage": cov,
            "basisTotals": bm["totals"],
            "currentMonthlyUsd": cur_total, "currentYearlyUsd": cur_total * 12,
            "optionA_reclaimSeats": a_out,
            "optionB_rightSize": {
                "naiveMonthlyUsd": naive,
                "safeMonthlyUsd": b_new,
                "safeSavingMonthlyUsd": cur_total - b_new,
                "safeSavingYearlyUsd": (cur_total - b_new) * 12,
                "censoredUsersKept": kept_censored,
                "thinDataUsersKept": kept_thin,
                "minActiveDays": args.min_active_days,
                "downgradeCount": len(down),
                "rows": b_rows,
            },
            "optionC_modelShift": {
                "opusMessages": opus_msgs, "opusCredits": round(opus_credits),
                "opusCreditPerMsg": round(opus_cpm, 4),
                "autoCreditPerMsg": c_auto, "sonnet5CreditPerMsg": c_son,
                "opusVsAutoRatio": round(opus_cpm / c_auto, 2) if c_auto else None,
                "scenarios": c_out,
            },
            "verification": {k: v for k, v in checks},
            "crossCheck": cross,
        }
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        print(f"\n[+] Ghi {args.json}")


if __name__ == "__main__":
    main()
