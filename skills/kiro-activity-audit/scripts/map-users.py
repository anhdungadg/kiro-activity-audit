#!/usr/bin/env python3
"""
Map Kiro userId (IAM Identity Center UUID) -> người thật (email, tên, username).

Kiro prompt-logs chỉ chứa userId dạng "{identityStoreId}.{userId}", không có email.
Script này:
  1. Quét prompt-logs đã tải về, thu userId riêng biệt
  2. Gọi identitystore:DescribeUser cho từng UUID
  3. Xuất kết quả ra CSV + JSON, có cache để không gọi lại API

YÊU CẦU QUYỀN: phải chạy bằng credentials của account sở hữu IAM Identity Center
(thường là Organizations management account hoặc delegated admin), với quyền:
    identitystore:DescribeUser
    identitystore:ListUsers        (chỉ cần cho --list-all)
    sso-admin:ListInstances        (chỉ cần cho --discover)

VÍ DỤ:
    # 0. Kiểm tra bạn đang ở đúng account có Identity Center
    python3 map-users.py --discover --profile <profile>

    # 1. Map từ log đã sync về
    sync-snapshot.sh   # (BUCKET=… PROFILE=…) - đừng sync cả bucket, sẽ timeout
    python3 map-users.py --logs ./kiro-audit-<account>/data/snapshot-<ts> --profile <idc-profile>

    # 2. Map một userId đơn lẻ
    python3 map-users.py --user-id d-0123456789.00000000-1111-2222-3333-444444444444 --profile <idc-profile>

    # 3. Dump toàn bộ directory (để join offline, tránh gọi API nhiều lần)
    python3 map-users.py --list-all --identity-store-id d-0123456789 --profile <idc-profile>
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import os
import sys
from collections import Counter

CACHE_FILE = "kiro-user-map-cache.json"


# --------------------------------------------------------------------------- #
# 1. Thu userId từ prompt-logs
# --------------------------------------------------------------------------- #
def collect_user_ids(logs_dir: str) -> Counter:
    """Quét mọi *.json.gz trong logs_dir, trả về Counter{userId: số request}."""
    counts: Counter = Counter()
    pattern = os.path.join(logs_dir, "**", "*.json.gz")
    files = glob.glob(pattern, recursive=True)
    if not files:
        print(f"[!] Không tìm thấy *.json.gz nào trong {logs_dir}", file=sys.stderr)
        return counts

    for path in files:
        try:
            with gzip.open(path) as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[!] Bỏ qua {path}: {exc}", file=sys.stderr)
            continue

        for record in payload.get("records", []):
            for key in ("generateAssistantResponseEventRequest",
                        "generateCompletionsEventRequest"):
                req = record.get(key)
                if req and req.get("userId"):
                    counts[req["userId"]] += 1

    print(f"[+] Đọc {len(files)} file, thấy {len(counts)} userId riêng biệt, "
          f"{sum(counts.values())} request")
    return counts


def split_user_id(raw: str) -> tuple[str, str]:
    """'d-0123456789.290af54c-...' -> ('d-0123456789', '290af54c-...')"""
    if "." not in raw:
        raise ValueError(f"userId không đúng định dạng '{{store}}.{{uuid}}': {raw}")
    store_id, user_uuid = raw.split(".", 1)
    return store_id, user_uuid


# --------------------------------------------------------------------------- #
# 2. Gọi Identity Center
# --------------------------------------------------------------------------- #
def get_client(service: str, profile: str | None, region: str):
    try:
        import boto3
    except ImportError:
        sys.exit("[x] Cần boto3: pip install boto3")
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client(service, region_name=region)


def discover_instances(profile: str | None, region: str) -> None:
    """In ra các Identity Center instance mà credentials hiện tại thấy được."""
    client = get_client("sso-admin", profile, region)
    instances = client.list_instances().get("Instances", [])
    if not instances:
        print("[x] Không thấy Identity Center instance nào từ credentials này.")
        print("    Bạn đang ở sai account. Cần Organizations management account")
        print("    hoặc delegated admin của IAM Identity Center.")
        return
    for inst in instances:
        print(f"[+] InstanceArn     : {inst.get('InstanceArn')}")
        print(f"    IdentityStoreId : {inst.get('IdentityStoreId')}")
        print(f"    OwnerAccountId  : {inst.get('OwnerAccountId')}")
        print(f"    Status          : {inst.get('Status')}")


def extract_person(user: dict) -> dict:
    """Rút các field hữu ích từ response DescribeUser."""
    emails = user.get("Emails") or []
    primary = next((e for e in emails if e.get("Primary")), emails[0] if emails else {})
    name = user.get("Name") or {}
    return {
        "userName": user.get("UserName", ""),
        "displayName": user.get("DisplayName", ""),
        "email": primary.get("Value", ""),
        "givenName": name.get("GivenName", ""),
        "familyName": name.get("FamilyName", ""),
        "title": user.get("Title", ""),
        "externalIds": ";".join(
            f"{x.get('Issuer', '')}:{x.get('Id', '')}" for x in (user.get("ExternalIds") or [])
        ),
    }


def describe_users(user_ids: list[str], profile: str | None, region: str,
                   cache: dict) -> dict:
    """Map từng userId đầy đủ -> thông tin người thật, dùng cache khi có."""
    resolved: dict = {}
    client = None

    for raw in user_ids:
        if raw in cache:
            resolved[raw] = cache[raw]
            print(f"[cache] {raw} -> {cache[raw].get('email') or '(không email)'}")
            continue

        try:
            store_id, user_uuid = split_user_id(raw)
        except ValueError as exc:
            print(f"[!] {exc}", file=sys.stderr)
            resolved[raw] = {"error": str(exc)}
            continue

        if client is None:
            client = get_client("identitystore", profile, region)

        try:
            resp = client.describe_user(IdentityStoreId=store_id, UserId=user_uuid)
            person = extract_person(resp)
            person["identityStoreId"] = store_id
            person["userUuid"] = user_uuid
            resolved[raw] = person
            cache[raw] = person
            print(f"[ok]    {raw} -> {person.get('email') or person.get('userName')}")
        except Exception as exc:  # noqa: BLE001 - muốn bắt mọi lỗi API để không dừng vòng lặp
            name = type(exc).__name__
            hint = ""
            if "ResourceNotFound" in name or "ResourceNotFound" in str(exc):
                hint = (" | Sai account: identity store này không thuộc credentials hiện tại. "
                        "Chạy --discover để kiểm tra.")
            elif "AccessDenied" in name or "AccessDenied" in str(exc):
                hint = " | Thiếu quyền identitystore:DescribeUser."
            print(f"[x]     {raw} -> {name}{hint}", file=sys.stderr)
            resolved[raw] = {"error": f"{name}: {exc}"}

    return resolved


def list_all_users(store_id: str, profile: str | None, region: str) -> list[dict]:
    """Dump toàn bộ user trong identity store - dùng để join offline."""
    client = get_client("identitystore", profile, region)
    users: list[dict] = []
    token = None
    while True:
        kwargs = {"IdentityStoreId": store_id, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        resp = client.list_users(**kwargs)
        for user in resp.get("Users", []):
            person = extract_person(user)
            person["identityStoreId"] = store_id
            person["userUuid"] = user.get("UserId", "")
            person["kiroUserId"] = f"{store_id}.{user.get('UserId', '')}"
            users.append(person)
        token = resp.get("NextToken")
        if not token:
            break
    print(f"[+] Lấy được {len(users)} user từ identity store {store_id}")
    return users


# --------------------------------------------------------------------------- #
# 3. Xuất kết quả
# --------------------------------------------------------------------------- #
def write_outputs(rows: list[dict], out_prefix: str) -> None:
    if not rows:
        print("[!] Không có dòng nào để ghi.")
        return

    json_path = f"{out_prefix}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)

    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)

    csv_path = f"{out_prefix}.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"[+] Đã ghi {json_path} và {csv_path} ({len(rows)} dòng)")


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError:
            print(f"[!] Cache {CACHE_FILE} lỗi, bỏ qua", file=sys.stderr)
    return {}


def save_cache(cache: dict) -> None:
    if cache:
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, ensure_ascii=False)
        print(f"[+] Cache {len(cache)} user vào {CACHE_FILE}")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Map Kiro userId (Identity Center UUID) sang người thật.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--logs", help="Thư mục prompt-logs đã sync về (quét *.json.gz)")
    ap.add_argument("--user-id", action="append", dest="user_ids",
                    help="Map một userId cụ thể (lặp lại được)")
    ap.add_argument("--list-all", action="store_true",
                    help="Dump toàn bộ user trong identity store")
    ap.add_argument("--identity-store-id", help="Bắt buộc khi dùng --list-all")
    ap.add_argument("--discover", action="store_true",
                    help="In các Identity Center instance thấy được rồi thoát")
    ap.add_argument("--profile", help="AWS profile (của account có Identity Center)")
    ap.add_argument("--region", default="us-east-1", help="Region (mặc định us-east-1)")
    ap.add_argument("--out", default="kiro-user-map", help="Tiền tố file xuất")
    ap.add_argument("--no-cache", action="store_true", help="Không dùng/ghi cache")
    args = ap.parse_args()

    if args.discover:
        discover_instances(args.profile, args.region)
        return

    if args.list_all:
        if not args.identity_store_id:
            sys.exit("[x] --list-all cần --identity-store-id (vd: d-0123456789)")
        write_outputs(list_all_users(args.identity_store_id, args.profile, args.region),
                      args.out)
        return

    # Thu userId cần map
    counts: Counter = Counter()
    if args.logs:
        counts = collect_user_ids(args.logs)
    for uid in (args.user_ids or []):
        counts[uid] += 0

    if not counts:
        sys.exit("[x] Không có userId nào. Dùng --logs, --user-id, hoặc --list-all.")

    cache = {} if args.no_cache else load_cache()
    resolved = describe_users(list(counts), args.profile, args.region, cache)
    if not args.no_cache:
        save_cache(cache)

    rows = []
    for uid, requests in counts.most_common():
        row = {"kiroUserId": uid, "requestCount": requests}
        row.update(resolved.get(uid, {}))
        rows.append(row)

    write_outputs(rows, args.out)

    failed = sum(1 for r in rows if r.get("error"))
    if failed:
        print(f"\n[!] {failed}/{len(rows)} userId không map được.")
        print("    Kiểm tra: đúng account chưa (--discover), và có quyền")
        print("    identitystore:DescribeUser chưa.")


if __name__ == "__main__":
    main()
