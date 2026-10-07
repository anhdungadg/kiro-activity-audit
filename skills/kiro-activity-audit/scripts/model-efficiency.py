#!/usr/bin/env python3
"""
Phân tích hiệu quả chi phí theo MODEL và phát hiện dữ liệu BỊ CHẶN (censored).

Hai câu hỏi script này trả lời, mà analyze-csv.py của template không trả lời được:

1. MODEL nào đang đốt credit?
   Credit tính theo độ phức tạp prompt × hệ số model. CSV cho số message theo từng
   model nên có thể suy ra credit/message trung bình của mỗi model bằng hồi quy
   không âm (NNLS) trên toàn bộ dòng user-ngày. Đây là cách duy nhất lấy được hệ số
   model từ dữ liệu, vì AWS không công bố hệ số Opus.

2. User nào bị CHẶN vì hết credit?
   Khi Credits_Used chạm Usage_Limit và Overage_Enabled=false, Kiro chặn user.
   Dữ liệu của những user đó là DỮ LIỆU BỊ CẮT (right-censored): nhu cầu thật CAO HƠN
   con số quan sát được. KHÔNG được hạ tier cho họ, và KHÔNG được coi con số của họ
   là mức dùng thật. analyze-csv.py không phân biệt điều này.

Chỉ đọc file, không gọi AWS.

VÍ DỤ:
    python3 scripts/model-efficiency.py --snapshot data/snapshot-20260909-1056Z
    python3 scripts/model-efficiency.py --snapshot data/... --json data/model-metrics.json
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from collections import defaultdict

COST_PREFIX = "user_report"
REQUIRED = {"UserId", "Credits_Used", "Subscription_Tier"}
MODEL_SUFFIX = "_messages"
CENSOR_PCT = 99.0        # >= 99% hạn mức coi như đã bị chặn
TIER_CREDITS = {"FREE": 50, "PRO": 1000, "PRO_PLUS": 2000, "PRO_MAX": 5000, "POWER": 10000}


def fnum(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def nnls(A: list[list[float]], b: list[float], iters: int = 8000,
         lr: float = 1e-9) -> list[float]:
    """Hồi quy bình phương nhỏ nhất có ràng buộc không âm, bằng gradient projection.

    Dùng thuần Python để không phụ thuộc numpy/scipy (máy audit có thể không có).
    Bài toán nhỏ (n cột = số model, thường < 10) nên tốc độ không phải vấn đề.
    """
    n = len(A[0])
    x = [0.5] * n
    for _ in range(iters):
        # gradient = 2 * A^T (A x - b)
        grad = [0.0] * n
        for row, bi in zip(A, b):
            r = sum(row[j] * x[j] for j in range(n)) - bi
            for j in range(n):
                grad[j] += 2 * row[j] * r
        for j in range(n):
            x[j] = max(0.0, x[j] - lr * grad[j])
    return x


def r2(A, b, x) -> float:
    pred = [sum(r[j] * x[j] for j in range(len(x))) for r in A]
    mb = sum(b) / len(b)
    ss_res = sum((bi - pi) ** 2 for bi, pi in zip(b, pred))
    ss_tot = sum((bi - mb) ** 2 for bi in b)
    return 1 - ss_res / ss_tot if ss_tot else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--json")
    args = ap.parse_args()

    paths = [p for p in sorted(glob.glob(os.path.join(args.snapshot, "**", "*.csv"),
                                         recursive=True))
             if f"/{COST_PREFIX}/" in p.replace(os.sep, "/")]
    paths = [p for p in paths if REQUIRED <= set(
        next(csv.reader(open(p, encoding="utf-8-sig", newline="")), []))]
    if not paths:
        print("[x] Không thấy CSV chi phí", file=sys.stderr)
        sys.exit(1)

    recs: list[dict] = []
    model_names: set[str] = set()
    users: dict[str, dict] = defaultdict(
        lambda: {"email": "", "tier": "", "limit": 0.0, "oe": None})

    for p in paths:
        with open(p, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                uid = (row.get("UserId") or "").strip().strip('"')
                if not uid:
                    continue
                models = {}
                for col, val in row.items():
                    if col and col.endswith(MODEL_SUFFIX):
                        n = int(fnum(val))
                        name = col[: -len(MODEL_SUFFIX)]
                        model_names.add(name)
                        if n:
                            models[name] = n
                u = users[uid]
                u["email"] = (row.get("User_Email") or "").strip('"') or u["email"]
                t = (row.get("Subscription_Tier") or "").strip()
                if t:
                    u["tier"] = t
                lim = fnum(row.get("Usage_Limit"))
                if lim:
                    u["limit"] = lim
                oe = (row.get("Overage_Enabled") or "").strip().lower()
                if oe in ("true", "false"):
                    u["oe"] = (oe == "true")
                recs.append({
                    "uid": uid, "date": (row.get("Date") or "").strip(),
                    "credits": fnum(row.get("Credits_Used")),
                    "msgs": int(fnum(row.get("Total_Messages"))),
                    "client": (row.get("Client_Type") or "").strip().strip('"'),
                    "models": models,
                })

    cols = sorted(model_names)
    print(f"[+] {len(recs)} dòng user-ngày-client · {len(users)} user · "
          f"{len(cols)} model xuất hiện trong schema")
    print()

    # ---------- 1. tổng message + credit theo model (phân bổ tuyến tính) ----------
    print("=" * 96)
    print("MODEL MIX - tổng message theo model (số đếm trực tiếp từ CSV)")
    print("=" * 96)
    tot_by_model = defaultdict(int)
    clients_by_model = defaultdict(set)
    for r in recs:
        for m, n in r["models"].items():
            tot_by_model[m] += n
            clients_by_model[m].add(r["client"])
    grand = sum(tot_by_model.values())
    print(f"{'Model':28} {'Messages':>12} {'%':>7}   Client")
    print("-" * 96)
    for m, n in sorted(tot_by_model.items(), key=lambda x: -x[1]):
        print(f"{m:28} {n:>12,} {n/grand*100:6.2f}%   "
              f"{', '.join(sorted(clients_by_model[m]))}")
    print("-" * 96)
    print(f"{'TỔNG':28} {grand:>12,}")
    print()

    # ---------- 2. hồi quy NNLS: credit/message của từng model ----------
    # chỉ dùng dòng mà tổng message theo model == Total_Messages (dòng nhất quán)
    fit_rows, fit_b, dropped = [], [], 0
    for r in recs:
        s = sum(r["models"].values())
        if s == 0 or abs(s - r["msgs"]) > 0:
            dropped += 1
            continue
        fit_rows.append([float(r["models"].get(c, 0)) for c in cols])
        fit_b.append(r["credits"])

    print("=" * 96)
    print("CREDIT/MESSAGE THEO MODEL - hồi quy NNLS trên dòng user-ngày")
    print("=" * 96)
    print(f"  Dùng {len(fit_rows)}/{len(recs)} dòng (bỏ {dropped} dòng có tổng model "
          f"≠ Total_Messages hoặc = 0)")
    if len(fit_rows) < len(cols) * 3:
        print("  [!] Quá ít dòng để hồi quy đáng tin. Bỏ qua.")
        coef = {}
    else:
        x = nnls(fit_rows, fit_b)
        score = r2(fit_rows, fit_b, x)
        coef = {c: x[i] for i, c in enumerate(cols)}
        print(f"  R² = {score:.4f}")
        print()
        print(f"{'Model':28} {'credit/msg':>12} {'Messages':>12} {'Credit suy ra':>16} {'%credit':>9}")
        print("-" * 96)
        est_tot = sum(coef[c] * tot_by_model.get(c, 0) for c in cols)
        for c in sorted(cols, key=lambda k: -coef[k]):
            n = tot_by_model.get(c, 0)
            est = coef[c] * n
            print(f"{c:28} {coef[c]:12.4f} {n:>12,} {est:>16,.0f} "
                  f"{(est/est_tot*100 if est_tot else 0):8.2f}%")
        print("-" * 96)
        print(f"  Tổng credit suy ra: {est_tot:,.0f} · thực tế: {sum(fit_b):,.0f}")
        print()
        print("  Đọc bảng này thế nào: hệ số là credit trung bình MỖI MESSAGE của model đó.")
        print("  Hệ số cao = model đắt. Chuyển việc thường sang model hệ số thấp rẻ hơn nâng tier.")
        print("  Lưu ý: đây là ƯỚC LƯỢNG THỐNG KÊ, không phải bảng giá AWS công bố.")
    print()

    # ---------- 3. dữ liệu bị chặn ----------
    per_month: dict[tuple, dict] = defaultdict(lambda: {"credits": 0.0, "msgs": 0, "days": set()})
    for r in recs:
        if len(r["date"]) >= 7:
            k = (r["date"][:7], r["uid"])
            per_month[k]["credits"] += r["credits"]
            per_month[k]["msgs"] += r["msgs"]
            per_month[k]["days"].add(r["date"])

    print("=" * 96)
    print("DỮ LIỆU BỊ CHẶN (right-censored) - user hết credit nên bị Kiro khoá")
    print("=" * 96)
    censored = []
    for (mo, uid), v in sorted(per_month.items(), key=lambda x: -x[1]["credits"]):
        u = users[uid]
        limit = u["limit"] or TIER_CREDITS.get(u["tier"], 0)
        if not limit:
            continue
        pct = v["credits"] / limit * 100
        if pct >= CENSOR_PCT:
            censored.append({
                "month": mo, "userId": uid, "email": u["email"], "tier": u["tier"],
                "credits": round(v["credits"], 2), "limit": limit,
                "pctOfLimit": round(pct, 2), "activeDays": len(v["days"]),
                "messages": v["msgs"],
                "overageEnabled": u["oe"],
            })
    if censored:
        print(f"{'Tháng':9} {'Email':28} {'Tier':9} {'Credits':>10} {'%':>7} {'Ngày':>5} {'Msgs':>8}")
        print("-" * 96)
        for c in censored:
            print(f"{c['month']:9} {(c['email'] or c['userId'])[:28]:28} {c['tier']:9} "
                  f"{c['credits']:10.2f} {c['pctOfLimit']:6.1f}% {c['activeDays']:5d} "
                  f"{c['messages']:8,}")
        print("-" * 96)
        print(f"  {len(censored)} lượt user-tháng bị chặn.")
        print("  HỆ QUẢ PHƯƠNG PHÁP: nhu cầu thật của các user này CAO HƠN số quan sát được.")
        print("  → KHÔNG hạ tier cho họ. KHÔNG dùng số của họ làm mức dùng thật.")
        print("  → Số ngày active thấp mà đã chạm hạn mức = đốt credit rất nhanh, cần xem lại")
        print("    cách dùng (model đắt? spec task? context lớn?) trước khi nâng tier.")
    else:
        print("  Không có user nào chạm hạn mức.")
    print()

    # ---------- 4. đốt credit nhanh nhất theo ngày active ----------
    print("=" * 96)
    print("TỐC ĐỘ ĐỐT CREDIT - credit/ngày-active (top 15)")
    print("=" * 96)
    burn = []
    for (mo, uid), v in per_month.items():
        nd = len(v["days"])
        if nd:
            burn.append((v["credits"] / nd, mo, uid, nd, v["credits"], v["msgs"]))
    burn.sort(reverse=True)
    print(f"{'Tháng':9} {'Email':28} {'Cr/ngày':>10} {'Ngày':>5} {'Credits':>10} {'Cr/msg':>8}")
    print("-" * 96)
    for rate, mo, uid, nd, cr, ms in burn[:15]:
        u = users[uid]
        print(f"{mo:9} {(u['email'] or uid)[:28]:28} {rate:10.1f} {nd:5d} {cr:10.2f} "
              f"{(cr/ms if ms else 0):8.3f}")
    print()

    if args.json:
        out = {
            "snapshotDir": args.snapshot,
            "recordCount": len(recs),
            "modelColumns": cols,
            "messagesByModel": dict(sorted(tot_by_model.items(), key=lambda x: -x[1])),
            "clientsByModel": {k: sorted(v) for k, v in clients_by_model.items()},
            "totalMessages": grand,
            "nnls": {
                "rowsUsed": len(fit_rows), "rowsDropped": dropped,
                "r2": round(r2(fit_rows, fit_b, [coef[c] for c in cols]), 6) if coef else None,
                "creditPerMessageByModel": {k: round(v, 6) for k, v in coef.items()},
                "note": "Ước lượng thống kê từ dữ liệu, KHÔNG phải bảng giá AWS công bố.",
            },
            "censored": censored,
            "censorThresholdPct": CENSOR_PCT,
            "burnRateTop": [
                {"month": mo, "userId": uid, "email": users[uid]["email"],
                 "creditsPerActiveDay": round(rate, 2), "activeDays": nd,
                 "credits": round(cr, 2), "messages": ms}
                for rate, mo, uid, nd, cr, ms in burn[:20]],
        }
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        print(f"[+] Ghi {args.json}")


if __name__ == "__main__":
    main()
