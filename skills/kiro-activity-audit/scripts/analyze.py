#!/usr/bin/env python3
"""Aggregate Kiro activity logs from the S3 export into report facts."""
import gzip, json, glob, re, os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Múi giờ hiển thị - đổi bằng biến môi trường KIRO_AUDIT_TZ (số giờ lệch UTC).
#   export KIRO_AUDIT_TZ=7    → GMT+7 (Việt Nam, mặc định)
#   export KIRO_AUDIT_TZ=8    → GMT+8 (Singapore, Malaysia)
#   export KIRO_AUDIT_TZ=0    → UTC
# Người dùng và người đọc report thường cùng múi giờ → hiển thị giờ địa phương.
# ---------------------------------------------------------------------------
TZ_OFFSET = float(os.environ.get("KIRO_AUDIT_TZ", "7"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))
TZ_LABEL = f"GMT{'+' if TZ_OFFSET >= 0 else '-'}{abs(TZ_OFFSET):g}"


def vn(iso: str) -> datetime:
    """ISO8601 UTC -> datetime giờ địa phương (TZ_LABEL)."""
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(LOCAL_TZ)

AR, GC, VAL = [], [], []

for f in sorted(glob.glob('prompt-logs/**/*', recursive=True)) + sorted(glob.glob('user-activity-reports/**/*', recursive=True)):
    if not os.path.isfile(f):
        continue
    if f.endswith('.json.gz'):
        for r in json.load(gzip.open(f))['records']:
            if 'generateAssistantResponseEventRequest' in r:
                AR.append((f, r))
            elif 'generateCompletionsEventRequest' in r:
                GC.append((f, r))
    else:
        VAL.append((f, open(f, encoding='utf-8', errors='replace').read()))

print(f"records: chat(GenerateAssistantResponse)={len(AR)}  inline(GenerateCompletions)={len(GC)}  validation={len(VAL)}")

# ---- users / models / time range ----
users, models, ts = Counter(), Counter(), []
for _, r in AR:
    q = r['generateAssistantResponseEventRequest']
    users[q['userId']] += 1
    models[q.get('modelId')] += 1
    ts.append(q['timeStamp'])
for _, r in GC:
    q = r['generateCompletionsEventRequest']
    users[q['userId']] += 1
    ts.append(q['timeStamp'])
for _, t in VAL:
    m = re.search(r'Timestamp: (\S+)', t)
    if m: ts.append(m.group(1))

print("\nusers:", dict(users))
print("models (chat):", dict(models))
print(f"time range ({TZ_LABEL}):", vn(min(ts)).strftime('%d/%m %H:%M:%S'),
      "->", vn(max(ts)).strftime('%d/%m %H:%M:%S'), " | UTC:", min(ts)[:19], "->", max(ts)[:19])

# ---- chat: trigger type, prompt sizes, empty responses ----
trig = Counter(r['generateAssistantResponseEventRequest']['chatTriggerType'] for _, r in AR)
print("\nchatTriggerType:", dict(trig))
def _resp(r):
    return r.get('generateAssistantResponseEventResponse') or {}

empty_resp = sum(1 for _, r in AR if not _resp(r).get('assistantResponse'))
print(f"chat records with EMPTY assistantResponse: {empty_resp}/{len(AR)}")
no_meta = sum(1 for _, r in AR if 'messageMetadata' not in _resp(r))
if no_meta:
    print(f"chat records KHÔNG có messageMetadata: {no_meta}/{len(AR)}  (biến thể schema)")
nullconv = sum(1 for _, r in AR
               if (_resp(r).get('messageMetadata') or {}).get('conversationId') is None)
print(f"chat records with null conversationId: {nullconv}/{len(AR)}")
coderef = sum(len(_resp(r).get('codeReferenceEvents') or []) for _, r in AR)
weblinks = sum(len(_resp(r).get('supplementaryWebLinksEvent') or []) for _, r in AR)
print(f"codeReferenceEvents total: {coderef}   supplementaryWebLinks total: {weblinks}")

plens = [len(r['generateAssistantResponseEventRequest']['prompt'] or '') for _, r in AR]
nonempty = [n for n in plens if n]
print(f"prompt length: empty={plens.count(0)} nonempty={len(nonempty)} min={min(nonempty)} max={max(nonempty)} avg={sum(nonempty)//len(nonempty)}")

# ---- inline completions: files, langs, context sizes, suggestions ----
files = Counter()
for _, r in GC:
    files[r['generateCompletionsEventRequest'].get('fileName')] += 1
print("\ninline completion by fileName:")
for k, v in files.most_common():
    print(f"   {v:3d}  {k}")

ext = Counter((os.path.splitext(k or '')[1] or '(none)') for k, v in files.items() for _ in range(v))
print("by extension:", dict(ext))

lc = [len(r['generateCompletionsEventRequest'].get('leftContext') or '') for _, r in GC]
rc = [len(r['generateCompletionsEventRequest'].get('rightContext') or '') for _, r in GC]
print(f"leftContext chars: min={min(lc)} max={max(lc)} avg={sum(lc)//len(lc)} total={sum(lc)}")
print(f"rightContext chars: min={min(rc)} max={max(rc)} avg={sum(rc)//len(rc)}")
ncomp = Counter(len(r.get('generateCompletionsEventResponse', {}).get('completions') or []) for _, r in GC)
print("completions returned per request:", dict(ncomp))

# ---- per-minute activity ----
buckets = Counter(vn(t).strftime('%d/%m %H:%M') for t in ts)
print(f"\nactivity per minute ({TZ_LABEL}):")
for k in sorted(buckets, key=lambda x: (x[3:5], x[:2], x[6:])):
    print(f"   {k}  {'#'*buckets[k]} ({buckets[k]})")

hours = Counter(vn(t).strftime('%d/%m %Hh') for t in ts)
print(f"\nactivity per hour ({TZ_LABEL}):")
for k in sorted(hours, key=lambda x: (x[3:5], x[:2], x[6:])):
    print(f"   {k}  {'#'*min(60, hours[k]//5)} ({hours[k]})")

# ---- what the prompts / code are about ----
print("\n--- chat prompts (non-empty), truncated ---")
for f, r in AR:
    q = r['generateAssistantResponseEventRequest']
    p = (q['prompt'] or '').split('<EnvironmentContext>')[0].strip()
    if p:
        print(f"[{vn(q['timeStamp']).strftime('%d/%m %H:%M:%S')}] {p[:220]!r}")

# ---- environment context: open/active editor files ----
openf, activef = Counter(), Counter()
for _, r in AR:
    p = r['generateAssistantResponseEventRequest']['prompt'] or ''
    for m in re.findall(r'<OPEN-EDITOR-FILES>(.*?)</OPEN-EDITOR-FILES>', p, re.S):
        for fn in re.findall(r'<file name="([^"]+)"', m):
            openf[fn] += 1
    for m in re.findall(r'<ACTIVE-EDITOR-FILE>(.*?)</ACTIVE-EDITOR-FILE>', p, re.S):
        for fn in re.findall(r'<file name="([^"]+)"', m):
            activef[fn] += 1
print("\nopen editor files (from EnvironmentContext):")
for k, v in openf.most_common():
    print(f"   {v:3d}  {k}")
print("active editor file:")
for k, v in activef.most_common():
    print(f"   {v:3d}  {k}")

# ---- tech signals across all captured content ----
blob = "\n".join((r['generateAssistantResponseEventRequest']['prompt'] or '') for _, r in AR)
blob += "\n".join((r['generateCompletionsEventRequest'].get('leftContext') or '') + (r['generateCompletionsEventRequest'].get('rightContext') or '') for _, r in GC)
terms = ['cassandra', 'medusa', 'karpenter', 'helm', 'kubernetes', 'kubectl', 'namespace', 'CronJob',
         'Taskfile', 'ECR', 'registry', 'backup', 'purge', 'sidecar', 'values.yaml', 'vnmt02', 'vnmt03',
         'EKS', 'terraform', 'argocd', 'nodepool', 'StatefulSet', 'PersistentVolume', 's3', 'IAM']
hits = {t: len(re.findall(re.escape(t), blob, re.I)) for t in terms}
print("\nkeyword frequency across prompts + code context:")
for k, v in sorted(hits.items(), key=lambda x: -x[1]):
    if v: print(f"   {v:4d}  {k}")

print(f"\ntotal captured prompt/code content: {len(blob)} chars")
