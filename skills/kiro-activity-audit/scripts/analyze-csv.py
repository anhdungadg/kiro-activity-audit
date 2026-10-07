#!/usr/bin/env python3
"""
Đọc báo cáo CSV user activity của Kiro → bảng chi phí + gợi ý right-size tier.

CSV là nguồn CÓ THẨM QUYỀN cho usage và chi phí (prompt-logs có thể bỏ sót message).
Kiro ghi CSV lúc 02:00 UTC hằng ngày vào:
    <prefix>/AWSLogs/<account>/KiroLogs/user_report/<region>/<yyyy>/<mm>/<dd>/*.csv

LƯU Ý VỀ MÚI GIỜ: cột Date trong CSV theo lịch UTC. Với múi giờ +N, một "ngày UTC"
tương ứng giờ địa phương từ N:00 hôm đó đến N:00 hôm sau. Nghĩa là công việc làm
trong khoảng 00:00–N:00 giờ địa phương sẽ được tính vào CSV của NGÀY HÔM TRƯỚC.
Đổi múi giờ bằng: export KIRO_AUDIT_TZ=8

Script tự tìm mọi *.csv trong thư mục snapshot, gộp theo user và theo ngày, rồi tính:
  - credit/message (chỉ số hiệu quả chi phí, phụ thuộc model)
  - % hạn mức tier, ngoại suy theo số ngày làm việc
  - tier phù hợp → tiết kiệm/phát sinh
  - cảnh báo user sẽ vượt hạn mức

VÍ DỤ:
    python3 scripts/analyze-csv.py --snapshot data/snapshot-20260901-1526Z
    python3 scripts/analyze-csv.py --snapshot data/... --workdays 22 --json data/csv-metrics.json
    python3 scripts/analyze-csv.py --snapshot data/... --by-day
"""
from __future__ import annotations

import argparse
import csv
import glob
from datetime import date, timedelta
import json
import os
import sys
from collections import defaultdict

# Bảng giá Kiro - kiểm tra lại tại kiro.dev/pricing nếu đã lâu
TIERS: dict[str, tuple[int, int]] = {   # tên: (USD/tháng, credits/tháng)
    "FREE": (0, 50),
    "PRO": (20, 1_000),
    "PRO_PLUS": (40, 2_000),
    "PRO_MAX": (100, 5_000),
    "POWER": (200, 10_000),
}
OVERAGE_USD_PER_CREDIT = 0.04
AT_RISK_PCT = 75          # ngưỡng at-risk theo AWS Cloud Intelligence Dashboards

# Múi giờ hiển thị - đổi bằng KIRO_AUDIT_TZ (số giờ lệch UTC), mặc định 7 (Việt Nam)
TZ_OFFSET = float(os.environ.get("KIRO_AUDIT_TZ", "7"))
TZ_LABEL = f"GMT{'+' if TZ_OFFSET >= 0 else '-'}{abs(TZ_OFFSET):g}"
HEADROOM = 0.90           # chỉ đề xuất tier nếu dùng <= 90% hạn mức tier đó

MODEL_COLS_SUFFIX = "_messages"


# Chỉ prefix user_report/ chứa dữ liệu chi phí. Kiro còn ghi các loại CSV khác
# (ví dụ by_user_analytic/ - số dòng code AI sinh, tỷ lệ chấp nhận) với SCHEMA KHÁC HẲN:
# không có Credits_Used / Subscription_Tier / User_Email, và Date theo dạng MM-DD-YYYY
# thay vì YYYY-MM-DD. Gộp chúng vào sẽ sinh "ngày ảo" và làm sai số liệu.
COST_PREFIX = "user_report"
REQUIRED_COLS = {"UserId", "Credits_Used", "Subscription_Tier"}


def find_csvs(root: str) -> tuple[list[str], list[str]]:
    """Trả về (csv chi phí, csv loại khác bị bỏ qua)."""
    allc = sorted(glob.glob(os.path.join(root, "**", "*.csv"), recursive=True))
    cost, other = [], []
    for p in allc:
        if f"/{COST_PREFIX}/" not in p.replace(os.sep, "/"):
            other.append(p)
            continue
        # kiểm tra header thật, đừng chỉ tin đường dẫn
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                hdr = set(next(csv.reader(fh), []))
        except (OSError, StopIteration):
            other.append(p)
            continue
        (cost if REQUIRED_COLS <= hdr else other).append(p)
    return cost, other


def workdays_between(d0: str, d1: str) -> int:
    """Số ngày Thứ 2–Thứ 6 trong khoảng [d0, d1] (chuỗi YYYY-MM-DD, bao gồm cả hai đầu).

    LÝ DO TỒN TẠI: bản trước chia credit cho SỐ NGÀY LỊCH rồi nhân SỐ NGÀY LÀM VIỆC -
    trộn hai đơn vị nên hạ thấp kết quả. Đo thực tế trên account này: ngày làm việc
    ~1.827 credit/ngày, cuối tuần ~31 - chênh 58 lần. Gộp cuối tuần vào mẫu làm
    loãng tỷ lệ và khiến ngoại suy thấp hơn thực tế tới ~29%.
    """
    a = date.fromisoformat(d0)
    b = date.fromisoformat(d1)
    if b < a:
        a, b = b, a
    n = 0
    while a <= b:
        if a.weekday() < 5:
            n += 1
        a += timedelta(days=1)
    return n


def fnum(v: str | None, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load(paths: list[str]) -> tuple[dict, dict, set, dict]:
    """Trả về (per_user, per_day, model_cols, meta)."""
    per_user: dict[str, dict] = defaultdict(lambda: {
        "email": "", "tier": "", "limit": 0.0, "credits": 0.0, "msgs": 0,
        "convs": 0, "overage": 0.0, "overageEnabled": None, "isNew": False,
        "days": set(), "models": defaultdict(int), "client": "",
    })
    per_day: dict[str, dict] = defaultdict(lambda: {"credits": 0.0, "msgs": 0, "users": set()})
    model_cols: set[str] = set()
    meta: dict = {"profileIds": set(), "files": [], "clients": set()}

    for path in paths:
        meta["files"].append(os.path.relpath(path))
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                uid = (row.get("UserId") or "").strip('"')
                if not uid:
                    continue
                date = (row.get("Date") or "").strip()
                cred = fnum(row.get("Credits_Used"))
                msgs = int(fnum(row.get("Total_Messages")))

                u = per_user[uid]
                u["email"] = (row.get("User_Email") or "").strip('"') or u["email"]
                u["tier"] = (row.get("Subscription_Tier") or "").strip() or u["tier"]
                u["limit"] = fnum(row.get("Usage_Limit"), u["limit"])
                u["client"] = (row.get("Client_Type") or "").strip() or u["client"]
                u["credits"] += cred
                u["msgs"] += msgs
                u["convs"] += int(fnum(row.get("Chat_Conversations")))
                u["overage"] += fnum(row.get("Overage_Credits_Used"))
                oe = (row.get("Overage_Enabled") or "").strip().lower()
                if oe in ("true", "false"):
                    u["overageEnabled"] = (oe == "true")
                if (row.get("New_User") or "").strip().lower() == "true":
                    u["isNew"] = True
                u["days"].add(date)

                for col, val in row.items():
                    if col and col.endswith(MODEL_COLS_SUFFIX):
                        name = col[: -len(MODEL_COLS_SUFFIX)]
                        n = int(fnum(val))
                        if n:
                            u["models"][name] += n
                            model_cols.add(name)

                d = per_day[date]
                d["credits"] += cred
                d["msgs"] += msgs
                d["users"].add(uid)
                if row.get("ProfileId"):
                    meta["profileIds"].add(row["ProfileId"].strip('"'))
                if row.get("Client_Type"):
                    meta["clients"].add(row["Client_Type"].strip('"'))

    return per_user, per_day, model_cols, meta


def fit_tier(monthly_credits: float) -> tuple[str, int]:
    """Tier nhỏ nhất chứa được mức dùng (giữ 10% dư địa)."""
    for name, (usd, cred) in sorted(TIERS.items(), key=lambda x: x[1][0]):
        if monthly_credits <= cred * HEADROOM:
            return name, usd
    return "POWER+OVERAGE", TIERS["POWER"][0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", required=True, help="Thư mục snapshot chứa CSV")
    ap.add_argument("--workdays", type=int, default=22, help="Số ngày làm việc/tháng để ngoại suy (mặc định 22)")
    ap.add_argument("--json", help="Ghi kết quả ra file JSON")
    ap.add_argument("--by-day", action="store_true", help="In thêm bảng theo ngày")
    args = ap.parse_args()

    paths, other = find_csvs(args.snapshot)
    if not paths:
        print(f"[x] Không tìm thấy CSV chi phí (prefix {COST_PREFIX}/) nào trong {args.snapshot}",
              file=sys.stderr)
        print("    Kiro ghi CSV lúc 02:00 UTC hằng ngày. Nếu profile mới tạo thì chu kỳ chưa chạy.",
              file=sys.stderr)
        print("    Kiểm tra: aws s3 ls s3://<BUCKET>/ --recursive --profile <P> | grep user_report",
              file=sys.stderr)
        sys.exit(1)

    per_user, per_day, model_cols, meta = load(paths)
    days = sorted(per_day)
    ndays = len(days)

    print(f"[+] Đọc {len(paths)} file CSV · {ndays} ngày · {len(per_user)} user")
    off = int(TZ_OFFSET)
    print(f"    Ngày trong CSV theo lịch UTC. Mỗi ngày UTC = {off:02d}:00 {TZ_LABEL} hôm đó "
          f"→ {off:02d}:00 hôm sau.")
    print(f"    File: {', '.join(os.path.basename(f) for f in meta['files'])}")
    if meta["profileIds"]:
        print(f"    ProfileId: {', '.join(sorted(meta['profileIds']))}")
    if meta.get("clients"):
        print(f"    Client_Type: {', '.join(sorted(meta['clients']))}")
    if other:
        print(f"\n    [i] Bỏ qua {len(other)} CSV không phải báo cáo chi phí "
              f"(schema khác, không có Credits_Used):")
        for p_ in other:
            print(f"        {os.path.basename(p_)}")
        print("        → Xem riêng nếu cần: đây là báo cáo năng suất, không phải chi phí.")
    print()

    # ---------- bảng chi phí ----------
    rows = sorted(per_user.items(), key=lambda x: -x[1]["credits"])
    print("=" * 118)
    print(f"{'Email':26} {'Tier':9} {'Credits':>9} {'Msgs':>6} {'Cr/msg':>7} "
          f"{'Hạn mức':>8} {'%dùng':>7} {'Ngoại suy':>10} {'%hạn mức':>9} {'Trạng thái':>12}")
    print("=" * 118)

    tot_cred = tot_msg = 0.0
    cur_cost = new_cost = 0
    at_risk: list[tuple[str, float, float]] = []
    result_users: list[dict] = []

    for uid, u in rows:
        cred, msgs = u["credits"], u["msgs"]
        tot_cred += cred
        tot_msg += msgs
        cpm = cred / msgs if msgs else 0.0
        limit = u["limit"] or TIERS.get(u["tier"], (0, 0))[1]
        pct_used = cred / limit * 100 if limit else 0.0
        # Ngoại suy theo NGÀY LÀM VIỆC, không phải ngày lịch.
        # Mốc tính: từ ngày user xuất hiện lần đầu đến ngày cuối của bộ dữ liệu -
        # để user mới được cấp license giữa kỳ không bị hạ thấp tỷ lệ.
        first_day = min(u["days"]) if u["days"] else (days[0] if days else "")
        obs_wd = workdays_between(first_day, days[-1]) if days and first_day else 0
        obs_wd = max(obs_wd, 1)
        per_day_avg = cred / obs_wd
        monthly = per_day_avg * args.workdays
        pct_month = monthly / limit * 100 if limit else 0.0

        cur_price = TIERS.get(u["tier"], (0, 0))[0]
        fit, fit_price = fit_tier(monthly)
        cur_cost += cur_price
        new_cost += fit_price

        if pct_month >= 100:
            status = "SẼ BỊ CHẶN" if u["overageEnabled"] is False else "SẼ VƯỢT"
            at_risk.append((u["email"] or uid, monthly, pct_month))
        elif pct_month >= AT_RISK_PCT:
            status = "at-risk"
            at_risk.append((u["email"] or uid, monthly, pct_month))
        else:
            status = "an toàn"

        print(f"{(u['email'] or uid)[:26]:26} {u['tier']:9} {cred:9.2f} {msgs:6d} {cpm:7.3f} "
              f"{limit:8.0f} {pct_used:6.1f}% {monthly:10.0f} {pct_month:8.0f}% {status:>12}")

        result_users.append({
            "userId": uid, "email": u["email"], "tier": u["tier"],
            "credits": round(cred, 4), "messages": msgs,
            "creditPerMessage": round(cpm, 4), "usageLimit": limit,
            "chatConversations": u["convs"], "overageCreditsUsed": round(u["overage"], 4),
            "overageEnabled": u["overageEnabled"], "isNewUser": u["isNew"],
            "daysObserved": sorted(u["days"]), "models": dict(u["models"]),
            "client": u["client"],
            "avgPerWorkday": round(per_day_avg, 4),
            "workdaysObserved": obs_wd,
            "firstDay": first_day,
            "projectedMonthly": round(monthly, 1),
            "projectedPctOfLimit": round(pct_month, 1),
            "currentTierUsd": cur_price,
            "recommendedTier": fit, "recommendedTierUsd": fit_price,
            "deltaUsd": fit_price - cur_price,
            "status": status,
        })

    print("-" * 118)
    print(f"{'TỔNG':26} {'':9} {tot_cred:9.2f} {int(tot_msg):6d} "
          f"{(tot_cred/tot_msg if tot_msg else 0):7.3f}")
    print()

    # ---------- model mix ----------
    if model_cols:
        cols = sorted(model_cols)
        print("=== Model mix theo user (số message) ===")
        print(f"{'Email':26} " + " ".join(f"{c[:14]:>14}" for c in cols) + f" {'%Opus':>7}")
        for uid, u in rows:
            opus = sum(n for m, n in u["models"].items() if "opus" in m.lower())
            pct = opus / u["msgs"] * 100 if u["msgs"] else 0
            print(f"{(u['email'] or uid)[:26]:26} "
                  + " ".join(f"{u['models'].get(c, 0):>14}" for c in cols)
                  + f" {pct:6.1f}%")
        print()
        print("  Ghi chú: tỷ lệ Opus cao → credit/message cao. Chuyển sang auto/sonnet")
        print("           cho việc thường thường rẻ hơn nâng tier.")
        print()

    # ---------- right-size ----------
    print("=== Right-size tier ===")
    print(f"{'Email':26} {'Hiện tại':>12} {'Đề xuất':>16} {'Chênh lệch':>12}")
    for r in result_users:
        if r["deltaUsd"] == 0 and r["recommendedTier"] == r["tier"]:
            note = "giữ nguyên"
        else:
            note = f"{r['deltaUsd']:+d}$"
        print(f"{(r['email'] or r['userId'])[:26]:26} "
              f"{r['tier']:>9} {r['currentTierUsd']:>3}$ "
              f"{r['recommendedTier']:>12} {r['recommendedTierUsd']:>3}$ {note:>12}")
    print("-" * 70)
    print(f"  Chi phí hiện tại : ${cur_cost}/tháng  (${cur_cost*12}/năm)")
    print(f"  Sau right-size   : ${new_cost}/tháng  (${new_cost*12}/năm)")
    delta = cur_cost - new_cost
    if delta > 0:
        print(f"  → TIẾT KIỆM      : ${delta}/tháng = ${delta*12}/năm")
    elif delta < 0:
        print(f"  → PHÁT SINH THÊM : ${-delta}/tháng = ${-delta*12}/năm")
    else:
        print("  → Không thay đổi")
    print()

    # ---------- cảnh báo ----------
    if at_risk:
        print("=== ⚠️  CẢNH BÁO ===")
        for email, monthly, pct in at_risk:
            print(f"  {email}: ngoại suy {monthly:.0f} credit/tháng = {pct:.0f}% hạn mức")
        oe = {r["overageEnabled"] for r in result_users}
        if False in oe:
            print("  Overage_Enabled = false → user hết credit sẽ BỊ CHẶN, không phát sinh phí.")
            print("  Đây là rủi ro GIÁN ĐOẠN CÔNG VIỆC, không phải rủi ro chi phí.")
        excess = sum(max(0, r["projectedMonthly"] - r["usageLimit"]) for r in result_users)
        if excess:
            print(f"  Nếu bật overage: {excess:.0f} credit vượt × ${OVERAGE_USD_PER_CREDIT}"
                  f" = ${excess*OVERAGE_USD_PER_CREDIT:.0f}/tháng")
        print()

    global_wd = workdays_between(days[0], days[-1]) if days else 0
    if global_wd < 3:
        print("=== ⚠️  ĐỘ TIN CẬY ===")
        print(f"  Chỉ có {global_wd} ngày LÀM VIỆC trong dữ liệu → ngoại suy KHÔNG đáng tin.")
        print("  Cần 3–5 ngày CSV mới kết luận được xu hướng.")
        newbies = [r["email"] or r["userId"] for r in result_users if r["isNewUser"]]
        if newbies:
            print(f"  User có New_User=true (ngày đầu thường dùng nhiều hơn mức ổn định): "
                  f"{', '.join(newbies)}")
        print()

    # ---------- theo ngày ----------
    if args.by_day or ndays > 1:
        print("=== Theo ngày (lịch UTC - xem lưu ý múi giờ ở trên) ===")
        print(f"{'Ngày UTC':12} {'Tương ứng ' + TZ_LABEL:>26} {'Credits':>10} {'Msgs':>7} {'User':>6}")
        from datetime import datetime as _dt, timedelta as _td
        for d in days:
            x = per_day[d]
            try:
                d0 = _dt.strptime(d, "%Y-%m-%d")
                span = (f"{(d0 + _td(hours=TZ_OFFSET)).strftime('%d/%m %H:%M')}"
                        f"→{(d0 + _td(days=1, hours=TZ_OFFSET)).strftime('%d/%m %H:%M')}")
            except ValueError:
                span = ""
            print(f"{d:12} {span:>26} {x['credits']:10.2f} {x['msgs']:7d} {len(x['users']):6d}")
        print()

    if args.json:
        out = {
            "snapshot": args.snapshot,
            "csvFiles": meta["files"],
            "csvSkipped": [os.path.relpath(p_) for p_ in other],
            "clientTypes": sorted(meta["clients"]),
            "profileIds": sorted(meta["profileIds"]),
            "days": days, "workdaysAssumed": args.workdays,
            "totals": {"credits": round(tot_cred, 4), "messages": int(tot_msg),
                       "creditPerMessage": round(tot_cred/tot_msg, 4) if tot_msg else 0,
                       "userCount": len(per_user)},
            "cost": {"currentMonthlyUsd": cur_cost, "rightSizedMonthlyUsd": new_cost,
                     "savingMonthlyUsd": delta, "savingYearlyUsd": delta*12},
            "users": result_users,
            "perDay": {d: {"credits": round(per_day[d]["credits"], 4),
                           "messages": per_day[d]["msgs"],
                           "users": len(per_day[d]["users"])} for d in days},
        }
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        print(f"[+] Ghi {args.json}")


if __name__ == "__main__":
    main()
