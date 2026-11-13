#!/usr/bin/env python3
"""Daily recheck for the GLOBAL DEAL HUNTER board (data-model version).

Rules (user-set, mechanical -- no semantic judgements):
- For each deal in data/deals.json, fetch its official page(s) from
  tools/check_config.json and check the pattern groups there.
  - All groups match  -> deal["verified"] = today (Beijing). Values are NOT
    changed by this script: a changed number only ever produces a flag.
  - A pattern group misses -> NEEDS_REVIEW.md entry, data untouched.
  - Fetch fails -> keep last-good data, record the reason, no date bump.
- Expiry within 7 days (or past) -> flagged in NEEDS_REVIEW.md too.
- New candidates: the 4 source listings are scanned for links that match no
  known source/official URL; matches are REPORTED ONLY in NEEDS_REVIEW.md,
  never auto-added.
- Re-renders index.html from the data afterwards (board dates follow the
  newest verified date automatically).

Usage:  python3 tools/check.py [--dry-run]
Writes: data/deals.json (only when a verified date changes),
        tools/page_state.json, NEEDS_REVIEW.md, index.html
"""
import datetime as dt
import hashlib
import html as htmlmod
import json
import re
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
BEIJING = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (compatible; deal-hunter-recheck/1.0)"}


def today():
    return dt.datetime.now(BEIJING).date()


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def visible_text(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", htmlmod.unescape(html))


def norm_url(u):
    try:
        p = urlparse(u)
        return (p.netloc + p.path).rstrip("/").lower()
    except Exception:
        return u.strip().lower()


def known_urls(deals):
    urls = set()
    for d in deals:
        urls.add(norm_url(d["go_url"]))
        for f in d["fields"]:
            for href in re.findall(r'href="([^"]+)"', f["dd_html"]):
                urls.add(norm_url(href))
    return urls


def scan_sources(cfg, known, old_baseline):
    """Candidate tracking by baseline diff: a link counts as a candidate only
    if it was NOT in the stored baseline from previous runs (first run seeds
    the baseline and reports nothing). AIHot is skipped: its deal list is
    JS-rendered and its raw HTML is mostly hashed asset URLs.
    Returns (candidates, notes, new_baseline)."""
    candidates, notes, baseline = [], [], {}
    patterns = {
        "懒猫 AI": (r'https?://lanmao\.ai/zh/o/[^)\s"\]]+',),
        "赛博鸡蛋": (r'https?://jidan\.si/free/[^"\s\)\]]+',),
    }
    for src in cfg.get("sources", []):
        name = src["name"]
        if name not in patterns:
            if name != "白嫖计":
                notes.append(f"{name} 未纳入自动候选扫描（需人工浏览）")
            continue
        try:
            raw = fetch(src["url"])
        except Exception as e:
            notes.append(f"{name} 读取失败：{type(e).__name__}")
            continue
        links = sorted(set(re.findall(patterns[name][0], raw)))
        baseline[name] = links
        prev = set(old_baseline.get(name, []))
        if not prev:
            notes.append(f"{name} 来源基线已建立（{len(links)} 条），本次不报告候选")
            continue
        for link in links:
            if link not in prev and norm_url(link) not in known:
                candidates.append((name, link))
    # baipiaoji limits.json: track vendor/model keys (best-effort, guarded)
    for src in cfg.get("sources", []):
        if src["name"] != "白嫖计":
            continue
        try:
            raw = fetch(src["url"])
            obj = json.loads(raw)
            keys = sorted(collect_keys(obj))
        except Exception as e:
            notes.append(f"白嫖计 读取失败：{type(e).__name__}")
            continue
        baseline["白嫖计"] = keys
        prev = set(old_baseline.get("白嫖计", []))
        if not prev:
            notes.append(f"白嫖计 来源基线已建立（{len(keys)} 项），本次不报告候选")
            continue
        for k in keys:
            if k not in prev:
                candidates.append(("白嫖计", k))
    return candidates, notes, baseline


def collect_keys(obj, out=None):
    out = out if out is not None else set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            collect_keys(v, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_keys(item, out)
    return out


def main():
    dry = "--dry-run" in sys.argv
    day = today()
    day_s = day.isoformat()
    deals_data = json.loads((ROOT / "data" / "deals.json").read_text(encoding="utf-8"))
    deals = deals_data["deals"]
    cfg = json.loads((ROOT / "tools" / "check_config.json").read_text(encoding="utf-8"))
    state_path = ROOT / "tools" / "page_state.json"
    old_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"deals": {}}
    new_state = {"date": day_s, "deals": old_state.get("deals", {})}

    results = []   # (slug, status, detail)
    review = []    # (slug, reason)
    failures = []  # (slug, reason)
    bumped = 0

    for d in deals:
        slug = d["slug"]
        entry = cfg.get(slug)
        if not entry:
            results.append((slug, "needs_review", "check_config.json 缺少该条配置"))
            review.append((slug, "check_config.json 缺少该条配置"))
            continue
        text, err, page_hash = None, None, None
        for url in entry["check_urls"]:
            try:
                raw = fetch(url)
                text = visible_text(raw)
                page_hash = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        if text is None:
            results.append((slug, "fetch_failed", err or "全部核对页抓取失败"))
            failures.append((slug, err or "全部核对页抓取失败"))
            new_state["deals"][slug] = {"status": "fetch_failed", "detail": err, "hash": None}
            continue
        missed = [g for g in entry["groups"] if not any(alt in text for alt in g)]
        if missed:
            detail = "未命中：" + " / ".join(" 或 ".join(g) for g in missed)
            results.append((slug, "needs_review", detail))
            review.append((slug, detail))
            new_state["deals"][slug] = {"status": "needs_review", "detail": detail, "hash": page_hash}
            continue
        results.append((slug, "ok", ""))
        new_state["deals"][slug] = {"status": "ok", "detail": "", "hash": page_hash}
        if d["verified"] != day_s:
            d["verified"] = day_s
            bumped += 1

    # expiry watch: within 7 days or past
    expiring = []
    for d in deals:
        if d["expiry"]:
            exp_date = dt.date.fromisoformat(d["expiry"][:10])
            delta = (exp_date - day).days
            if delta < 0:
                expiring.append((d["slug"], f"已过期 {d['expiry'][:10]}"))
            elif delta <= 7:
                expiring.append((d["slug"], f"{delta} 天后到期（{d['expiry'][:10]}）"))

    candidates, src_notes, new_baseline = scan_sources(
        cfg, known_urls(deals), old_state.get("source_baseline", {}))

    print(f"== recheck {day_s} ==")
    for slug, status, detail in results:
        print(f"{status:12} {slug} {detail}")
    print(f"bumped={bumped} flags={len(review)} fetch_failed={len(failures)} "
          f"expiring={len(expiring)} candidates={len(candidates)}")

    if dry:
        print("(dry-run: no files written)")
        return

    if bumped:
        (ROOT / "data" / "deals.json").write_text(
            json.dumps(deals_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    new_state["date"] = day_s
    new_state["source_baseline"] = new_baseline
    state_path.write_text(json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [f"# 待人工复核（{day_s} 自动复查）", ""]
    if review:
        lines += ["## 模式未命中（数据未改动）", ""]
        lines += [f"- **{s}**：{r}" for s, r in review] + [""]
    if failures:
        lines += ["## 抓取失败（保留旧数据）", ""]
        lines += [f"- **{s}**：{r}" for s, r in failures] + [""]
    if expiring:
        lines += ["## 即将到期 / 已过期", ""]
        lines += [f"- **{s}**：{r}" for s, r in expiring] + [""]
    if candidates:
        lines += ["## 新候选线索（仅报告，未收录，等用户点头）", ""]
        lines += [f"- {name}：{link}" for name, link in candidates[:40]] + [""]
    if src_notes:
        lines += ["## 来源站读取说明", ""]
        lines += [f"- {n}" for n in src_notes] + [""]
    if not (review or failures or expiring or candidates):
        lines += ["本次复查：全部通过，无待复核项。", ""]
    (ROOT / "NEEDS_REVIEW.md").write_text("\n".join(lines), encoding="utf-8")

    subprocess.run([sys.executable, str(ROOT / "tools" / "render.py")], check=True)
    print("state + NEEDS_REVIEW.md written; index.html re-rendered")


if __name__ == "__main__":
    main()
