#!/usr/bin/env python3
"""
Gộp CSV user_report của Kiro theo THÁNG DƯƠNG LỊCH (không ngoại suy).

Lý do tồn tại script này: analyze-csv.py chia tổng credit cho SỐ NGÀY CÓ DỮ LIỆU rồi nhân
với số ngày làm việc giả định. Cách đó chỉ đúng khi dataset nằm trong MỘT tháng. Khi dữ liệu
trải nhiều tháng (thường xảy ra sau vài tuần triển khai):

  - Cột "%dùng" của analyze-csv.py so credit N ngày với hạn mức THÁNG → vô nghĩa
    (200% không có nghĩa là vượt hạn mức).
  - Tháng đã gần đủ ngày thì dùng SỐ THẬT, không cần ngoại suy.

Script này KHÔNG ngoại suy. Nó chỉ cộng credit thực tế theo từng tháng và so với hạn mức
tháng của tier. Tháng chưa đủ ngày được ghi nhãn rõ ràng.

Chỉ đọc file, không gọi AWS, không ghi gì lên bucket.

VÍ DỤ:
    python3 scripts/monthly-rollup.py --snapshot data/snapshot-20260909-1056Z
    python3 scripts/monthly-rollup.py --snapshot data/... --corp-domain @acme.com.vn \
        --json data/monthly-metrics.json
"""
from __future__ import annotations

import argparse
import calendar
import csv
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date

# Bảng giá Kiro - xác minh lại tại kiro.dev/pricing trước khi trích dẫn
TIERS: dict[str, tuple[int, int]] = {   # tên: (USD/tháng, credits/tháng)
    "FREE": (0, 50),
    "PRO": (20, 1_000),
    "PRO_PLUS": (40, 2_000),
    "PRO_MAX": (100, 5_000),
    "POWER": (200, 10_000),
}
TIER_ORDER = ["FREE", "PRO", "PRO_PLUS", "PRO_MAX", "POWER"]
OVERAGE_USD_PER_CREDIT = 0.04
HEADROOM = 0.90           # chỉ hạ tier nếu mức dùng <= 90% hạn mức tier mới
AT_RISK_PCT = 75.0

COST_PREFIX = "user_report"
REQUIRED_COLS = {"UserId", "Credits_Used", "Subscription_Tier"}
MODEL_SUFFIX = "_messages"

# Domain công ty - email ngoài domain này được đánh dấu để rà soát.
# Truyền qua --corp-domain. Nếu không truyền, script TỰ SUY domain phổ biến nhất trong dữ liệu.
CORP_DOMAIN_DEFAULT = None


def fnum(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def find_cost_csvs(root: str) -> list[str]:
    out = []
    for p in sorted(glob.glob(os.path.join(root, "**", "*.csv"), recursive=True)):
        if f"/{COST_PREFIX}/" not in p.replace(os.sep, "/"):
            continue
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                hdr = set(next(csv.reader(fh), []))
        except (OSError, StopIteration):
            continue
        if REQUIRED_COLS <= hdr:
            out.append(p)
    return out


def fit_tier(monthly_credits: float) -> tuple[str, int]:
    """Tier nhỏ nhất chứa được mức dùng, giữ 10% dư địa."""
    for name in TIER_ORDER:
        usd, cred = TIERS[name]
        if monthly_credits <= cred * HEADROOM:
            return name, usd
    return "POWER+OVERAGE", TIERS["POWER"][0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--corp-domain", default=CORP_DOMAIN_DEFAULT,
                    help="Domain email công ty, ví dụ @acme.com.vn. Bỏ trống thì tự suy "
                         "domain phổ biến nhất trong dữ liệu.")
    ap.add_argument("--json")
    args = ap.parse_args()

    paths = find_cost_csvs(args.snapshot)
    if not paths:
        print(f"[x] Không thấy CSV chi phí trong {args.snapshot}", file=sys.stderr)
        sys.exit(1)

    # (month, uid) -> số liệu ; và uid -> thuộc tính user
    mu: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "credits": 0.0, "msgs": 0, "convs": 0, "days": set(),
        "models": defaultdict(int), "clients": set(), "overage": 0.0,
    })
    users: dict[str, dict] = defaultdict(lambda: {
        "email": "", "tier": "", "limit": 0.0, "overageEnabled": None,
        "overageCap": 0.0, "newUserDates": set(),
    })
    months: set[str] = set()
    all_days: set[str] = set()
    profile_ids: set[str] = set()

    for path in paths:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                uid = (row.get("UserId") or "").strip().strip('"')
                d = (row.get("Date") or "").strip()
                if not uid or len(d) < 7:
                    continue
                month = d[:7]                      # YYYY-MM (Date đã là YYYY-MM-DD)
                months.add(month)
                all_days.add(d)

                u = users[uid]
                u["email"] = (row.get("User_Email") or "").strip('"') or u["email"]
                # tier/limit: lấy giá trị của ngày MỚI NHẤT (tier có thể đổi giữa kỳ)
                tier = (row.get("Subscription_Tier") or "").strip()
                if tier:
                    u["tier"] = tier
                lim = fnum(row.get("Usage_Limit"))
                if lim:
                    u["limit"] = lim
                u["overageCap"] = fnum(row.get("Overage_Cap"), u["overageCap"])
                oe = (row.get("Overage_Enabled") or "").strip().lower()
                if oe in ("true", "false"):
                    u["overageEnabled"] = (oe == "true")
                if (row.get("New_User") or "").strip().lower() == "true":
                    u["newUserDates"].add(d)

                k = (month, uid)
                m = mu[k]
                m["credits"] += fnum(row.get("Credits_Used"))
                m["msgs"] += int(fnum(row.get("Total_Messages")))
                m["convs"] += int(fnum(row.get("Chat_Conversations")))
                m["overage"] += fnum(row.get("Overage_Credits_Used"))
                m["days"].add(d)
                ct = (row.get("Client_Type") or "").strip().strip('"')
                if ct:
                    m["clients"].add(ct)
                if row.get("ProfileId"):
                    profile_ids.add(row["ProfileId"].strip('"'))
                for col, val in row.items():
                    if col and col.endswith(MODEL_SUFFIX):
                        n = int(fnum(val))
                        if n:
                            m["models"][col[: -len(MODEL_SUFFIX)]] += n

    months_sorted = sorted(months)
    days_sorted = sorted(all_days)

    # Domain công ty: dùng giá trị truyền vào, hoặc tự suy domain phổ biến nhất.
    # Tự suy an toàn hơn hardcode vì mỗi khách hàng một domain, và một tổ chức có thể
    # dùng nhiều domain hợp lệ - khi đó phải truyền --corp-domain tường minh.
    corp = args.corp_domain
    dom_count: dict[str, int] = defaultdict(int)
    for u in users.values():
        if u["email"] and "@" in u["email"]:
            dom_count["@" + u["email"].rsplit("@", 1)[1]] += 1
    if not corp and dom_count:
        corp = max(dom_count, key=lambda d: dom_count[d])
    corp = corp or "@__khong_xac_dinh__"

    # độ phủ từng tháng: có bao nhiêu ngày trong tháng xuất hiện trong dữ liệu
    coverage: dict[str, dict] = {}
    for mo in months_sorted:
        y, mm = int(mo[:4]), int(mo[5:7])
        in_month = sorted(d for d in days_sorted if d.startswith(mo))
        coverage[mo] = {
            "daysWithData": len(in_month),
            "daysInMonth": calendar.monthrange(y, mm)[1],
            "first": in_month[0], "last": in_month[-1],
            "complete": len(in_month) == calendar.monthrange(y, mm)[1],
        }

    print(f"[+] {len(paths)} file CSV · {len(days_sorted)} ngày "
          f"({days_sorted[0]} → {days_sorted[-1]}) · {len(users)} user")
    print(f"    ProfileId: {', '.join(sorted(profile_ids))}")
    print()
    print("=== ĐỘ PHỦ DỮ LIỆU THEO THÁNG ===")
    for mo in months_sorted:
        c = coverage[mo]
        flag = "ĐỦ THÁNG" if c["complete"] else f"THIẾU {c['daysInMonth']-c['daysWithData']} ngày"
        print(f"  {mo}: {c['daysWithData']}/{c['daysInMonth']} ngày "
              f"({c['first']} → {c['last']})  [{flag}]")
    print()

    out_months: dict[str, dict] = {}

    for mo in months_sorted:
        c = coverage[mo]
        rows = sorted(((uid, v) for (m_, uid), v in mu.items() if m_ == mo),
                      key=lambda x: -x[1]["credits"])
        tot_c = sum(v["credits"] for _, v in rows)
        tot_m = sum(v["msgs"] for _, v in rows)

        print("=" * 108)
        print(f"THÁNG {mo}  -  {c['daysWithData']}/{c['daysInMonth']} ngày dữ liệu"
              f"{'' if c['complete'] else '  (CHƯA ĐỦ THÁNG - số dưới đây là SỐ THẬT tới nay, không ngoại suy)'}")
        print("=" * 108)
        print(f"{'Email':28} {'Tier':9} {'Credits':>9} {'Msgs':>7} {'Cr/msg':>7} "
              f"{'Hạn mức':>8} {'%hạn mức':>9} {'Ngày':>5} {'Trạng thái':>11}")
        print("-" * 108)

        month_users = []
        for uid, v in rows:
            u = users[uid]
            limit = u["limit"] or TIERS.get(u["tier"], (0, 0))[1]
            pct = v["credits"] / limit * 100 if limit else 0.0
            cpm = v["credits"] / v["msgs"] if v["msgs"] else 0.0
            if pct >= 100:
                st = "VƯỢT" if u["overageEnabled"] else "BỊ CHẶN"
            elif pct >= AT_RISK_PCT:
                st = "at-risk"
            else:
                st = "an toàn"
            label = u["email"] or uid
            print(f"{label[:28]:28} {u['tier']:9} {v['credits']:9.2f} {v['msgs']:7d} "
                  f"{cpm:7.3f} {limit:8.0f} {pct:8.1f}% {len(v['days']):5d} {st:>11}")
            month_users.append({
                "userId": uid, "email": u["email"], "tier": u["tier"],
                "credits": round(v["credits"], 4), "messages": v["msgs"],
                "creditPerMessage": round(cpm, 4),
                "usageLimit": limit, "pctOfLimit": round(pct, 2),
                "activeDays": len(v["days"]), "chatConversations": v["convs"],
                "overageCreditsUsed": round(v["overage"], 4),
                "clients": sorted(v["clients"]), "models": dict(v["models"]),
                "status": st,
                "isNewUser": bool(u["newUserDates"] & v["days"]),
                "nonCorpEmail": bool(u["email"]) and not u["email"].endswith(corp),
            })
        print("-" * 108)
        print(f"{'TỔNG':28} {'':9} {tot_c:9.2f} {tot_m:7d} "
              f"{(tot_c/tot_m if tot_m else 0):7.3f}")
        print()
        out_months[mo] = {
            "coverage": {k: (sorted(x) if isinstance(x, set) else x)
                         for k, x in coverage[mo].items()},
            "totals": {"credits": round(tot_c, 4), "messages": tot_m,
                       "creditPerMessage": round(tot_c/tot_m, 4) if tot_m else 0,
                       "activeUsers": len(rows)},
            "users": month_users,
        }

    # ---------- right-size dựa trên THÁNG ĐỦ NGÀY cao nhất ----------
    complete = [mo for mo in months_sorted if coverage[mo]["complete"]]
    # tháng 8 thiếu 01-02 nhưng vẫn là tháng gần đủ nhất → dùng tháng có nhiều ngày nhất
    basis = max(months_sorted, key=lambda m: coverage[m]["daysWithData"])
    basis_note = ("ĐỦ THÁNG" if coverage[basis]["complete"]
                  else f"{coverage[basis]['daysWithData']}/{coverage[basis]['daysInMonth']} ngày")

    print("=" * 108)
    print(f"RIGHT-SIZE TIER - căn cứ tháng {basis} ({basis_note}), dùng SỐ THẬT không ngoại suy")
    print("=" * 108)

    basis_by_uid = {r["userId"]: r for r in out_months[basis]["users"]}
    # user có subscription nhưng KHÔNG có dòng nào trong tháng căn cứ = không dùng
    all_uids = set(users)
    cur_total = new_total = 0
    right_rows = []
    for uid in sorted(all_uids, key=lambda x: -(basis_by_uid.get(x, {}).get("credits", 0))):
        u = users[uid]
        cred = basis_by_uid.get(uid, {}).get("credits", 0.0)
        cur_usd = TIERS.get(u["tier"], (0, 0))[0]
        rec, rec_usd = fit_tier(cred)
        cur_total += cur_usd
        new_total += rec_usd
        right_rows.append({
            "userId": uid, "email": u["email"], "currentTier": u["tier"],
            "currentUsd": cur_usd, "basisCredits": round(cred, 4),
            "recommendedTier": rec, "recommendedUsd": rec_usd,
            "deltaUsd": rec_usd - cur_usd,
            "activeInBasisMonth": uid in basis_by_uid,
            "nonCorpEmail": bool(u["email"]) and not u["email"].endswith(corp),
        })

    print(f"{'Email':28} {'Hiện tại':>16} {'Credits':>9} {'Đề xuất':>16} {'Chênh':>8}")
    print("-" * 108)
    for r in right_rows:
        print(f"{(r['email'] or r['userId'])[:28]:28} "
              f"{r['currentTier']:>9} {r['currentUsd']:>4}$ {r['basisCredits']:9.2f} "
              f"{r['recommendedTier']:>11} {r['recommendedUsd']:>3}$ "
              f"{r['deltaUsd']:>+7d}$")
    print("-" * 108)
    delta = cur_total - new_total
    print(f"  Chi phí hiện tại : ${cur_total}/tháng   (${cur_total*12}/năm)")
    print(f"  Sau right-size   : ${new_total}/tháng   (${new_total*12}/năm)")
    print(f"  → {'TIẾT KIỆM' if delta > 0 else 'PHÁT SINH'}: "
          f"${abs(delta)}/tháng = ${abs(delta)*12}/năm")
    print()

    # ---------- phân bố tier hiện tại ----------
    tier_count: dict[str, int] = defaultdict(int)
    for u in users.values():
        tier_count[u["tier"] or "(trống)"] += 1
    print("=== PHÂN BỔ TIER HIỆN TẠI ===")
    for t in TIER_ORDER + [k for k in tier_count if k not in TIER_ORDER]:
        if tier_count.get(t):
            usd, cred = TIERS.get(t, (0, 0))
            print(f"  {t:10} {tier_count[t]:3d} user × ${usd:>3}/tháng "
                  f"= ${tier_count[t]*usd:>5}/tháng")
    print(f"  {'TỔNG':10} {len(users):3d} user "
          f"{'':17}= ${cur_total:>5}/tháng = ${cur_total*12}/năm")
    print()

    # ---------- email ngoài domain ----------
    noncorp = [u for u in users.values()
               if u["email"] and not u["email"].endswith(corp)]
    if noncorp:
        print(f"=== ⚠️  EMAIL NGOÀI DOMAIN CÔNG TY ({corp}) - cần rà soát ===")
        if not args.corp_domain:
            print(f"    (domain tự suy từ dữ liệu: {dom_count.get(corp,0)}/{len(users)} user. "
                  f"Nếu tổ chức dùng nhiều domain hợp lệ, truyền --corp-domain tường minh.)")
        for u in sorted(noncorp, key=lambda x: x["email"]):
            print(f"  {u['email']:32} tier {u['tier']}  (${TIERS.get(u['tier'],(0,0))[0]}/tháng)")
        print("  → Xác minh danh tính trong IAM Identity Center. Nếu là tài khoản cá nhân")
        print("    hoặc nhà thầu đã kết thúc hợp đồng thì thu hồi subscription.")
        print()

    # ---------- user không dùng ----------
    idle = [r for r in right_rows if r["basisCredits"] == 0]
    if idle:
        print(f"=== ⚠️  {len(idle)} USER KHÔNG PHÁT SINH CREDIT trong tháng {basis} ===")
        for r in idle:
            print(f"  {(r['email'] or r['userId'])[:40]:40} tier {r['currentTier']:9} "
                  f"= ${r['currentUsd']}/tháng bị bỏ không")
        print(f"  → Tổng chi phí đứng yên: ${sum(r['currentUsd'] for r in idle)}/tháng "
              f"= ${sum(r['currentUsd'] for r in idle)*12}/năm")
        print()

    # ---------- overage ----------
    oe_vals = {u["overageEnabled"] for u in users.values()}
    print("=== OVERAGE ===")
    print(f"  Overage_Enabled quan sát được: {oe_vals}")
    if oe_vals == {False}:
        print("  Tất cả user đều TẮT overage → hết credit thì BỊ CHẶN, không phát sinh phí.")
        print("  Đây là rủi ro GIÁN ĐOẠN CÔNG VIỆC, không phải rủi ro chi phí.")
    over_users = [r for mo in months_sorted for r in out_months[mo]["users"]
                  if r["pctOfLimit"] >= 100]
    if over_users:
        print(f"  {len(over_users)} lượt user-tháng đã CHẠM/VƯỢT hạn mức:")
        for r in over_users:
            print(f"    {r['email'] or r['userId']:32} {r['pctOfLimit']:6.1f}% hạn mức")
    print()

    if args.json:
        out = {
            "snapshotDir": args.snapshot,
            "csvFileCount": len(paths),
            "days": days_sorted,
            "profileIds": sorted(profile_ids),
            "coverage": coverage,
            "basisMonth": basis,
            "basisMonthComplete": coverage[basis]["complete"],
            "months": out_months,
            "rightSize": {
                "basisMonth": basis,
                "currentMonthlyUsd": cur_total,
                "rightSizedMonthlyUsd": new_total,
                "savingMonthlyUsd": delta,
                "savingYearlyUsd": delta * 12,
                "rows": right_rows,
            },
            "tierDistribution": dict(tier_count),
            "corpDomain": corp,
            "corpDomainAutoDetected": args.corp_domain is None,
            "emailDomains": dict(sorted(dom_count.items(), key=lambda x: -x[1])),
            "nonCorpEmails": sorted(u["email"] for u in noncorp),
            "idleUsersInBasisMonth": [
                {"email": r["email"], "userId": r["userId"],
                 "tier": r["currentTier"], "usd": r["currentUsd"]} for r in idle],
            "overageEnabledValues": sorted(str(v) for v in oe_vals),
            "pricingTable": {k: {"usd": v[0], "credits": v[1]} for k, v in TIERS.items()},
            "overageUsdPerCredit": OVERAGE_USD_PER_CREDIT,
        }
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        print(f"[+] Ghi {args.json}")


if __name__ == "__main__":
    main()
