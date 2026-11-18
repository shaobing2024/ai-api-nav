#!/usr/bin/env python3
"""Daily recheck for the GLOBAL DEAL HUNTER board (data-model version).

Rules (user-set 2026-11-18, replacing the old confirm-each-item rule):
- Expired deals (expiry date before today, Beijing) are removed from
  data/deals.json at the start of the run and logged in NEEDS_REVIEW.md
  under 「已自动下架」.
- Existing deals are verified against their check pages: config comes from
  the deal's own embedded "check" field first, then tools/check_config.json.
  - All pattern groups match -> verified = today. Values are NEVER changed
    by this script: a changed number only ever produces a flag.
  - A pattern group misses -> NEEDS_REVIEW.md entry, data untouched.
  - Fetch fails -> keep last-good data, record the reason, no date bump.
- New offers are AUTO-PUBLISHED (no per-item user confirmation): source
  listings are diffed against the stored baseline; each new 懒猫 / 赛博鸡蛋
  detail link is fetched and a card is built from the source content
  (fields the page does not state are written as 「未公布」; claims with no
  official basis are marked 「未核实」). Only a parse failure (no title or
  no quota extractable) holds an item back, logged in NEEDS_REVIEW.md.
  Parse-failed links stay out of the baseline so they retry next run.
- 白嫖计 limits.json is tracked by tool slug; its entries carry no official
  entry URL, so new tools are reported only, never auto-published.
- AIHot is not auto-scanned: its free-tokens page loads details on click
  and exposes no data API; it remains a manual port (noted every run).
- Expiry within 7 days -> flagged in NEEDS_REVIEW.md.
- Re-renders index.html from the data afterwards (board dates follow the
  newest verified date automatically).

Usage:  python3 tools/check.py [--dry-run]
Writes: data/deals.json (when anything changed), tools/page_state.json,
        NEEDS_REVIEW.md, index.html
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
UA = {"User-Agent": "Mozilla/5.0 (compatible; deal-hunter-recheck/2.0)"}

PALETTE = ["#0b7285", "#5f3dc4", "#a61e4d", "#1864ab",
           "#087f5b", "#b45309", "#6d28d9", "#0e7490"]
# Circuit breaker: a daily diff never legitimately produces this many new
# links for one source; more means the baseline is inconsistent, so skip
# auto-publishing for that source and flag it instead of flooding the board.
MAX_AUTO_ADD = 30
SOCIAL_HOSTS = ("t.me", "telegram.me", "twitter.com", "x.com", "weibo.com",
                "zhihu.com", "xiaohongshu.com", "bilibili.com", "youtube.com",
                "facebook.com", "linkedin.com")


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


def host_of(u):
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""


def known_urls(deals):
    urls = set()
    for d in deals:
        urls.add(norm_url(d["go_url"]))
        for f in d["fields"]:
            for href in re.findall(r'href="([^"]+)"', f["dd_html"]):
                urls.add(norm_url(href))
    return urls


# ---------------------------------------------------------------- extraction

def meta_content(raw, name):
    m = re.search(r'<meta[^>]+name="%s"[^>]+content="([^"]*)"' % name, raw)
    if not m:
        m = re.search(r'<meta[^>]+content="([^"]*)"[^>]+name="%s"' % name, raw)
    return htmlmod.unescape(m.group(1)).strip() if m else ""


def page_title(raw):
    m = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.S)
    if m:
        t = re.sub(r"<[^>]+>", "", m.group(1))
        return re.sub(r"\s+", " ", htmlmod.unescape(t)).strip()
    m = re.search(r"<title>(.*?)</title>", raw, re.S)
    if m:
        t = htmlmod.unescape(m.group(1)).strip()
        return re.split(r"\s*[-|｜]\s*", t)[0].strip()
    return ""


def first_paragraph(raw):
    body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", raw)
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", body, re.S):
        t = re.sub(r"<[^>]+>", "", m.group(1))
        t = re.sub(r"\s+", " ", htmlmod.unescape(t)).strip()
        if len(t) >= 40:
            return t
    return ""


def is_dup_official(official, known):
    """Whether an extracted official URL proves the offer is already listed.
    Root-path portal URLs (e.g. free.aliyun.com/) are shared by many distinct
    offers, so they never count as a duplicate."""
    if not official:
        return False
    if urlparse(official).path not in ("", "/"):
        return norm_url(official) in known
    return False


def outbound_links(raw, own_hosts):
    out = []
    for href, anchor in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', raw, re.S):
        h = host_of(href)
        if not h or any(h == o or h.endswith("." + o) for o in own_hosts):
            continue
        if any(h == s or h.endswith("." + s) for s in SOCIAL_HOSTS):
            continue
        a = re.sub(r"<[^>]+>", "", anchor)
        out.append((href, re.sub(r"\s+", " ", htmlmod.unescape(a)).strip()))
    return out


def pick_official(links):
    """Prefer links whose anchor sounds like a claim entry; else first link."""
    for href, anchor in links:
        if any(k in anchor for k in ("领取", "直达", "官网", "入口", "打开", "访问")):
            return href
    return links[0][0] if links else ""


DATE_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
EXPIRY_HINT = ("截止", "到期", "限免至", "免费至", "有效期", "结束", "延至", "之前")


def extract_expiry(text, day):
    """Latest hinted future date in text -> (iso_datetime, date) or ('','')."""
    best = None
    for m in DATE_RE.finditer(text):
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        ctx = text[max(0, m.start() - 14):m.start()]
        if not any(k in ctx for k in EXPIRY_HINT):
            continue
        if d < day:
            continue
        if best is None or d > best:
            best = d
    if best is None:
        return "", None
    tm = "23:59:59"
    return f"{best.isoformat()}T{tm}+08:00", best


def quota_short(quota):
    """Pick the most quota-like clause for the card's big quota line."""
    clauses = [c.strip() for c in re.split(r"[。；]", quota) if c.strip()]
    subs = []
    for c in clauses:
        subs += [s.strip() for s in c.split("，") if s.strip()]
    strong = re.compile(r"((\$|¥|￥)\s*\d[\d,\.]*|\d[\d,\.]*\s*(亿|万|千)?\s*(Token|token|TOKEN|元|美元|积分|次|GB|TB|MB|credits|Credits|USD|刀)|不限量|不限次|完全免费|免费使用)")
    for s in subs:
        if strong.search(s) and len(s) >= 4:
            return clean_quota(s[:46])
    pat = re.compile(r"(\d[\d,\.]*\s*(亿|万|千)?\s*(Token|token|TOKEN|元|美元|积分|次|GB|TB|MB|小时|天|个月|年|credits|Credits|USD|\$|¥|刀)|不限量|不限次|完全免费|免费使用)")
    for s in subs:
        if pat.search(s) and len(s) >= 6:
            return clean_quota(s[:46])
    return clean_quota(subs[0][:46] if subs else quota[:46])


def clean_quota(q):
    q = q.strip().strip("，。；：、")
    while q.count("）") > q.count("（") and q.endswith("）"):
        q = q[:-1].rstrip()
    while q.count(")") > q.count("(") and q.endswith(")"):
        q = q[:-1].rstrip()
    return q


def classify_cat(title, quota, intro):
    text = (title + " " + quota + " " + intro).lower()
    scores = {
        "免费算力": ["gpu", "tpu", "算力", "云电脑", "显卡", "a100", "h100", "训练平台", "notebook"],
        "云服务": ["云服务器", "vps", "云主机", "服务器", "云存储", "对象存储", "数据库", "云盘"],
        "开发工具": ["ide", "编程", "代码助手", "开发环境", "编辑器", "低代码", "code "],
        "AI 应用试用": ["绘画", "生图", "视频生成", "音乐生成", "写作", "ppt", "会议纪要", "翻译", "数字人", "配音", "3d", "大赛", "创作", "生成量"],
        "大模型 API": ["api", "token", "模型", "大模型", "llm", "额度"],
    }
    best, best_score = "大模型 API", 0
    for cat, kws in scores.items():
        s = sum(1 for k in kws if k in text)
        if s > best_score:
            best, best_score = cat, s
    return best


def vendor_of(title):
    t = re.split(r"[（(]", title)[0].strip()
    parts = t.split()
    if parts and len(parts[0]) <= 10:
        return parts[0]
    return t[:12]


def unique_slug(base, deals):
    slug = re.sub(r"[^a-z0-9\-]+", "-", base.lower()).strip("-") or "deal"
    taken = {d["slug"] for d in deals}
    s, n = slug, 2
    while s in taken:
        s = f"{slug}-{n}"
        n += 1
    return s


def build_card(*, slug, source_name, source_url, title, quota, gate, region,
               intro, official_url, expiry_iso, expiry_date, day):
    cat = classify_cat(title, quota, intro)
    vendor = vendor_of(title)
    day_s = day.isoformat()
    region = region.strip() if region else ""
    region_pill = region if region in ("国内直连", "全球", "海外", "国内") else ("全球" if region else "全球")
    scope = ""
    if expiry_date and (expiry_date - day).days <= 7:
        scope = "soon"
    badges = []
    if expiry_iso:
        badges.append({"cls": "badge-amber", "text": f"{expiry_iso[:10]} 到期"})
    q_short = quota_short(quota)
    color = PALETTE[sum(slug.encode("utf-8")) % len(PALETTE)]
    check_urls = [source_url] + ([official_url] if official_url and official_url != source_url else [])
    tok = vendor if len(vendor) >= 2 else title[:6]
    groups = [[tok]]
    m = re.search(r"(\$|¥)?\s*\d[\d,\.]*\s*(亿|万)?\s*(Token|token|元|积分|次|GB|credits|Credits)", quota)
    if m:
        raw = re.sub(r"\s+", " ", m.group(0)).strip()
        variants = sorted({raw, raw.replace(" ", "")})
        if variants[0] not in tok:
            groups.append(variants)
    fields = [
        {"dt": "领取条件", "dd_html": gate if gate else "见官方入口页说明（未核实）", "dd_class": None},
        {"dt": "适用地区", "dd_html": region if region else "未核实", "dd_class": None},
        {"dt": "成本", "dd_html": "¥0（免费额度内，来源站口径）", "dd_class": None},
        {"dt": "风险", "dd_html": "本条由每日复查按来源站内容自动收录，未逐条官方核实；额度、条件与到期以官方页为准，领取前请再确认。"
         + ("到期后可能自动转付费，账户有余额的留意扣费。" if expiry_iso else ""), "dd_class": "risk"},
        {"dt": "到期时间", "dd_html": (f"{expiry_iso[:10]}（北京时间）" if expiry_iso else "未公布"), "dd_class": "expiry"},
        {"dt": "来源", "dd_html": f'<a href="{source_url}" target="_blank" rel="noopener">{source_name}详情页</a>', "dd_class": None},
        {"dt": "官方入口", "dd_html": (f'<a href="{official_url}" target="_blank" rel="noopener">{host_of(official_url)}</a>'
                                        if official_url else "未核实"), "dd_class": None},
    ]
    return {
        "slug": slug, "article_class": "card", "cat": cat, "scope": scope,
        "expiry": expiry_iso, "verified": day_s, "added": day_s,
        "search": f"{title} {vendor} {cat} 免费 额度 {source_name}",
        "icon_color": color, "icon_char": (vendor[:1] or title[:1] or "?"),
        "title": title[:60], "vendor": f"{vendor} · {cat}", "badges": badges,
        "quota": q_short,
        "intro": (intro or quota)[:300],
        "pills": [cat, "真实免费", region_pill],
        "gate_html": f"<b>门槛：</b>{gate}" if gate else "<b>门槛：</b>见官方入口页说明（未核实）",
        "detail_intro": (intro or quota)[:500],
        "detail_quota_html": f'<span class="d-label">免费额度</span>{htmlmod.escape(quota[:500])}（来源：{source_name}，未逐条官方核实）',
        "fields": fields,
        "foot_exp_html": (f"⏳ {expiry_iso[:10]} 截止" if expiry_iso else "到期未公布"),
        "go_url": official_url if official_url else source_url,
        "check": {"check_urls": check_urls, "groups": groups,
                  "note": "自动收录条目：以来源站详情页核对标题与额度关键串"},
    }


# ------------------------------------------------------------- source scans

def parse_lanmao_rows(md):
    """offers.md rows: 名字 | 免费额度 | 门槛 | 地区 | 核实日期 | 详情URL"""
    rows = {}
    for line in md.splitlines():
        if "lanmao.ai/zh/o/" not in line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p != ""]
        if len(parts) < 6:
            continue
        url = parts[-1]
        if "/zh/o/" not in url:
            continue
        rows[url] = {"name": parts[0], "quota": parts[1], "gate": parts[2],
                     "region": parts[3]}
    return rows


def extract_lanmao(url, row, day):
    title = row["name"]
    quota = row["quota"]
    intro, official, expiry_iso, expiry_date = "", "", "", None
    try:
        raw = fetch(url)
        intro = meta_content(raw, "description") or first_paragraph(raw)
        official = pick_official(outbound_links(raw, ("lanmao.ai", "yunmao.cloud")))
        expiry_iso, expiry_date = extract_expiry(visible_text(raw), day)
    except Exception:
        pass  # the offers.md row alone already carries title + quota
    return {"title": title, "quota": quota, "gate": row["gate"],
            "region": row["region"], "intro": intro, "official": official,
            "expiry_iso": expiry_iso, "expiry_date": expiry_date}


def extract_jidan(url, day):
    raw = fetch(url)
    title = page_title(raw)
    text = visible_text(raw)
    intro = meta_content(raw, "description") or first_paragraph(raw)
    quota = ""
    m = re.search(r"额度[:：]\s*(.+?)\s*方式[:：]", text)
    if m:
        quota = m.group(1).strip()
    if not quota:
        cands = []
        for sent in re.split(r"[。；\n]", text):
            s = sent.strip()
            if not (8 <= len(s) <= 90):
                continue
            if not any(k in s for k in ("免费", "额度", "赠送", "领取")):
                continue
            score = 0
            if re.search(r"(\$|¥|￥)\s*\d", s):
                score += 3
            if re.search(r"\d[\d,\.]*\s*(亿|万)?\s*(元|积分|Token|token|次|GB|TB|小时|credits|Credits)", s):
                score += 3
            if "额度" in s:
                score += 1
            if "免费" in s:
                score += 1
            if s.startswith(("注册截止", "截止", "活动截止")):
                score -= 3
            if score > 0:
                cands.append((score, s))
        if cands:
            quota = max(cands, key=lambda t: t[0])[1]
    gate = ""
    m = re.search(r"方式[:：]\s*(.+?)\s*地区[:：]", text)
    if m:
        gate = m.group(1).strip()
    region = ""
    m = re.search(r"地区[:：]\s*(\S{2,6})", text)
    if m:
        region = m.group(1)
        if region.startswith("国内"):
            region = "国内直连"
    official = pick_official(outbound_links(raw, ("jidan.si", "saibodan.com")))
    expiry_iso, expiry_date = extract_expiry(text, day)
    return {"title": title, "quota": quota, "gate": gate, "region": region,
            "intro": intro, "official": official,
            "expiry_iso": expiry_iso, "expiry_date": expiry_date}


def scan_and_build(cfg, deals, old_baseline, day):
    """Diff source listings against the baseline; auto-build cards for new
    items. Returns (added, failed, notes, new_baseline) where failed entries
    are (source, link, reason) and stay out of the baseline for a retry."""
    added, failed, notes = [], [], []
    baseline = {}
    known = known_urls(deals)
    src_by_name = {s["name"]: s["url"] for s in cfg.get("sources", [])}

    # --- 懒猫 AI: offers.md table rows -------------------------------------
    name = "懒猫 AI"
    try:
        rows = parse_lanmao_rows(fetch(src_by_name[name]))
        links = sorted(rows.keys())
        prev = set(old_baseline.get(name, []))
        prev_norm = {norm_url(p) for p in prev}
        baseline[name] = links
        if not prev:
            notes.append(f"{name} 来源基线已建立（{len(links)} 条），本次不报告候选")
        else:
            new_links = [l for l in links
                         if norm_url(l) not in prev_norm and norm_url(l) not in known]
            if len(new_links) > MAX_AUTO_ADD:
                notes.append(f"{name} 新增链接异常多（{len(new_links)} 条），疑似基线不一致，本次跳过自动上架")
                baseline[name] = old_baseline.get(name, [])
            else:
                failed_links = set()
                for link in new_links:
                    ext = extract_lanmao(link, rows[link], day)
                    if not ext["title"] or not ext["quota"]:
                        failed.append((name, link, "解析失败：标题或额度为空"))
                        failed_links.add(link)
                        continue
                    if is_dup_official(ext["official"], known):
                        continue  # same offer already listed via another source
                    slug = unique_slug(link.rstrip("/").split("/o/")[-1], deals + added)
                    card = build_card(slug=slug, source_name=name, source_url=link,
                                      title=ext["title"], quota=ext["quota"],
                                      gate=ext["gate"], region=ext["region"],
                                      intro=ext["intro"], official_url=ext["official"],
                                      expiry_iso=ext["expiry_iso"],
                                      expiry_date=ext["expiry_date"], day=day)
                    added.append(card)
                    known.add(norm_url(card["go_url"]))
                baseline[name] = [l for l in links if l not in failed_links]
    except Exception as e:
        notes.append(f"{name} 读取失败：{type(e).__name__}")
        baseline[name] = old_baseline.get(name, [])

    # --- 赛博鸡蛋: /free/ article links ------------------------------------
    name = "赛博鸡蛋"
    try:
        raw = fetch(src_by_name[name])
        links = sorted(set("https://" + u for u in
                           re.findall(r"https?://(jidan\.si/free/[^\"\s\)\]]+)", raw)))
        prev = set(old_baseline.get(name, []))
        prev_norm = {norm_url(p) for p in prev}
        baseline[name] = links
        if not prev:
            notes.append(f"{name} 来源基线已建立（{len(links)} 条），本次不报告候选")
        else:
            new_links = [l for l in links
                         if norm_url(l) not in prev_norm and norm_url(l) not in known]
            if len(new_links) > MAX_AUTO_ADD:
                notes.append(f"{name} 新增链接异常多（{len(new_links)} 条），疑似基线不一致，本次跳过自动上架")
                baseline[name] = old_baseline.get(name, [])
            else:
                failed_links = set()
                for link in new_links:
                    try:
                        ext = extract_jidan(link, day)
                    except Exception as e:
                        failed.append((name, link, f"解析失败：详情页抓取失败（{type(e).__name__}）"))
                        failed_links.add(link)
                        continue
                    if not ext["title"] or not ext["quota"]:
                        failed.append((name, link, "解析失败：标题或额度提取不到"))
                        failed_links.add(link)
                        continue
                    if is_dup_official(ext["official"], known):
                        continue
                    base = re.sub(r"^\d+-", "", link.rstrip("/").split("/free/")[-1])
                    slug = unique_slug(base, deals + added)
                    card = build_card(slug=slug, source_name=name, source_url=link,
                                      title=ext["title"], quota=ext["quota"],
                                      gate=ext["gate"], region=ext["region"],
                                      intro=ext["intro"], official_url=ext["official"],
                                      expiry_iso=ext["expiry_iso"],
                                      expiry_date=ext["expiry_date"], day=day)
                    added.append(card)
                    known.add(norm_url(card["go_url"]))
                baseline[name] = [l for l in links if l not in failed_links]
    except Exception as e:
        notes.append(f"{name} 读取失败：{type(e).__name__}")
        baseline[name] = old_baseline.get(name, [])

    # --- 白嫖计: limits.json tool slugs (report only: no official URL) -----
    name = "白嫖计"
    try:
        obj = json.loads(fetch(src_by_name[name]))
        tools = obj.get("tools", [])
        slugs = sorted({t.get("slug") for t in tools if t.get("slug")})
        prev = set(old_baseline.get(name, []))
        baseline[name] = slugs
        if len(prev & set(slugs)) < 3:
            notes.append(f"{name} 基线已按 tool slug 重建（{len(slugs)} 条），本次不报告")
        else:
            by_slug = {t.get("slug"): t for t in tools}
            for s in slugs:
                if s in prev:
                    continue
                t = by_slug.get(s, {})
                failed.append((name, t.get("page", s),
                               f"新工具 {t.get('name', s)}：limits.json 未提供官方入口链接，仅报告不建卡"))
    except Exception as e:
        notes.append(f"{name} 读取失败：{type(e).__name__}")
        baseline[name] = old_baseline.get(name, [])

    notes.append("AIHot 未纳入自动扫描（页面详情靠点击加载、无公开数据接口，仍需人工浏览）")
    return added, failed, notes, baseline


# --------------------------------------------------------------------- main

def main():
    dry = "--dry-run" in sys.argv
    day = today()
    day_s = day.isoformat()
    deals_data = json.loads((ROOT / "data" / "deals.json").read_text(encoding="utf-8"))
    deals = deals_data["deals"]
    cfg = json.loads((ROOT / "tools" / "check_config.json").read_text(encoding="utf-8"))
    state_path = ROOT / "tools" / "page_state.json"
    old_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"deals": {}}
    new_state = {"date": day_s, "deals": dict(old_state.get("deals", {}))}

    # 1) auto-delist expired deals (expiry date before today)
    delisted, alive = [], []
    for d in deals:
        exp = d.get("expiry") or ""
        if exp and dt.date.fromisoformat(exp[:10]) < day:
            delisted.append(d)
            new_state["deals"].pop(d["slug"], None)
        else:
            alive.append(d)
    deals = alive

    # 2) verify existing deals (embedded "check" config wins over the file)
    results, review, failures = [], [], []
    bumped = 0
    for d in deals:
        slug = d["slug"]
        entry = d.get("check") or cfg.get(slug)
        if not entry:
            results.append((slug, "needs_review", "缺少复查配置（内嵌与 check_config.json 均无）"))
            review.append((slug, "缺少复查配置"))
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

    # 3) auto-publish new offers from the source listings
    added, add_failed, src_notes, new_baseline = scan_and_build(
        cfg, deals, old_state.get("source_baseline", {}), day)
    for card in added:
        deals.append(card)
        new_state["deals"][card["slug"]] = {"status": "ok", "detail": "自动收录", "hash": None}
    deals_data["deals"] = deals

    # 4) expiry watch for deals still listed (within 7 days)
    expiring = []
    for d in deals:
        if d["expiry"]:
            delta = (dt.date.fromisoformat(d["expiry"][:10]) - day).days
            if 0 <= delta <= 7:
                expiring.append((d["slug"], f"{delta} 天后到期（{d['expiry'][:10]}）"))

    print(f"== recheck {day_s} ==")
    for slug, status, detail in results:
        print(f"{status:12} {slug} {detail}")
    print(f"bumped={bumped} flags={len(review)} fetch_failed={len(failures)} "
          f"expiring={len(expiring)} delisted={len(delisted)} added={len(added)} "
          f"add_failed={len(add_failed)}")
    for d in delisted:
        print(f"delisted     {d['slug']} (expiry {d['expiry'][:10]})")
    for c in added:
        print(f"added        {c['slug']} <- {c['fields'][5]['dd_html'][:80]}")

    if dry:
        print("(dry-run: no files written)")
        return

    if bumped or delisted or added:
        (ROOT / "data" / "deals.json").write_text(
            json.dumps(deals_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    new_state["source_baseline"] = new_baseline
    state_path.write_text(json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [f"# 待人工复核（{day_s} 自动复查）", ""]
    if delisted:
        lines += ["## 已自动下架（过期）", ""]
        lines += [f"- **{d['slug']}**：{d['title']}（到期 {d['expiry'][:10]}）" for d in delisted] + [""]
    if added:
        lines += ["## 已自动上架", ""]
        lines += [f"- **{c['slug']}**：{c['title']}（来源：{c['go_url']}）" for c in added] + [""]
    if add_failed:
        lines += ["## 自动上架未成功（解析失败或缺入口，仅报告）", ""]
        lines += [f"- {name}：{link} —— {reason}" for name, link, reason in add_failed] + [""]
    if review:
        lines += ["## 模式未命中（数据未改动）", ""]
        lines += [f"- **{s}**：{r}" for s, r in review] + [""]
    if failures:
        lines += ["## 抓取失败（保留旧数据）", ""]
        lines += [f"- **{s}**：{r}" for s, r in failures] + [""]
    if expiring:
        lines += ["## 即将到期", ""]
        lines += [f"- **{s}**：{r}" for s, r in expiring] + [""]
    if src_notes:
        lines += ["## 来源站读取说明", ""]
        lines += [f"- {n}" for n in src_notes] + [""]
    if not (delisted or added or add_failed or review or failures or expiring):
        lines += ["本次复查：全部通过，无待复核项。", ""]
    (ROOT / "NEEDS_REVIEW.md").write_text("\n".join(lines), encoding="utf-8")

    subprocess.run([sys.executable, str(ROOT / "tools" / "render.py")], check=True)
    print("state + NEEDS_REVIEW.md written; index.html re-rendered")


if __name__ == "__main__":
    main()
