#!/usr/bin/env python3
"""Render index.html from data/deals.json + tools/template.html.

Board dates （页头「数据 · <日期>」、页脚「本版核实日期 <日期>」) are
generated, never hard-coded: board_date = the newest verified date across
deals (i.e. the latest successful recheck, Beijing date).
Hero card body is fixed copy set by the user 2026-11-18:
「官方每条严格核实，放心领取，数据实时更新。」— it carries no date slot.
The stat caption「今日新上 2026-11-12 核实上架」is ZCode's listing date and
stays literal in the template -- it is NOT a board date.

Usage:  python3 tools/render.py [--board-date YYYY-MM-DD] [--out path]
Defaults: out = repo root index.html.
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CARD_TEMPLATE = """<article class="{article_class}" id="deal-{slug}" data-deal="{slug}" data-cat="{cat}" data-scope="{scope}" data-expiry="{expiry}" data-verified="{verified}" data-search="{search}">
  <div class="card-top">
    <span class="icon" style="background:{icon_color}" aria-hidden="true">{icon_char}</span>
    <div class="card-head">
      <h3>{title}</h3>
      <p class="card-cat">{vendor}</p>
    </div>{badges_line}
  </div>
  <p class="quota-big">{quota}</p>
  <p class="intro">{intro}</p>
  <div class="pills">{pills}</div>
  <p class="gate">{gate_html}</p>
  <button class="toggle" type="button" aria-expanded="false">详情 <span class="chev" aria-hidden="true">›</span></button>
  <div class="detail" hidden><p class="d-intro">{detail_intro}</p><div class="d-quota">{detail_quota_html}</div><dl class="d-fields">{fields}</dl><p class="d-verified">最后核实 {verified}</p></div>
  <div class="card-foot">
    <div class="foot-meta"><span class="exp">{foot_exp_html}</span><span>最后核实 {verified}</span></div>
    <a class="go" href="{go_url}" target="_blank" rel="noopener">去领取 <span aria-hidden="true">→</span></a>
  </div>
</article>
"""

def render_card(d, board_date):
    # 「今日新上」is computed from the deal's "added" date, never stored:
    # only a deal added on the board date itself renders as new; a stored
    # scope/badge saying "new" on any older deal (e.g. ZCode, listed
    # 2026-11-12) is dropped so it cannot go stale again.
    is_new = bool(d.get("added")) and d["added"] == board_date
    scope = d["scope"]
    if is_new:
        scope = "new"
    elif scope == "new":
        scope = ""
    card_badges = [b for b in d["badges"] if b["text"] != "今日新上"]
    if is_new:
        card_badges = [{"cls": "", "text": "今日新上"}] + card_badges
    badges = "".join(
        '<span class="badge{extra}">{text}</span>'.format(
            extra=f' {b["cls"]}' if b["cls"] else "", text=b["text"])
        for b in card_badges)
    badges_line = f'\n    <span class="badges">{badges}</span>' if card_badges else ""
    pills = "".join(f'<span class="pill">{p}</span>' for p in d["pills"])
    fields = "".join(
        '<div><dt>{dt}</dt><dd{cls}>{dd}</dd></div>'.format(
            dt=f["dt"],
            cls=f' class="{f["dd_class"]}"' if f.get("dd_class") else "",
            dd=f["dd_html"]) for f in d["fields"])
    return CARD_TEMPLATE.format(
        article_class=d["article_class"], slug=d["slug"], cat=d["cat"],
        scope=scope, expiry=d["expiry"], verified=d["verified"],
        search=d["search"], icon_color=d["icon_color"], icon_char=d["icon_char"],
        title=d["title"], vendor=d["vendor"], badges_line=badges_line, quota=d["quota"],
        intro=d["intro"], pills=pills, gate_html=d["gate_html"],
        detail_intro=d["detail_intro"], detail_quota_html=d["detail_quota_html"],
        fields=fields, foot_exp_html=d["foot_exp_html"], go_url=d["go_url"])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board-date", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    data = json.loads((ROOT / "data" / "deals.json").read_text(encoding="utf-8"))
    deals = data["deals"]
    board_date = args.board_date or max(d["verified"] for d in deals)
    tpl = (ROOT / "tools" / "template.html").read_text(encoding="utf-8")
    assert tpl.count("{{BOARD_DATE}}") == 2, "template must carry exactly 2 board-date slots"
    cards_html = "".join(render_card(d, board_date) for d in deals).rstrip("\n")
    out = tpl.replace("{{BOARD_DATE}}", board_date).replace("<!--CARDS-->\n", cards_html)
    # 「最近截止」stat: the template's static date + vendor label go stale the
    # moment the soonest deal changes, so both are generated from the data.
    expiring = [d for d in deals if d.get("expiry")]
    if expiring:
        soonest = min(expiring, key=lambda d: d["expiry"])
        sname = soonest["title"].split("限时")[0].split("（")[0].strip()
        if not sname or len(sname) > 18:
            sname = soonest["vendor"].split(" · ")[0]
        out = re.sub(
            r'<div class="num date" id="statSoon">[^<]*</div><div class="lbl">最近截止 · [^<]*<br />限时免费到期日</div>',
            f'<div class="num date" id="statSoon">{soonest["expiry"][:10]}</div>'
            f'<div class="lbl">最近截止 · {sname}<br />限时免费到期日</div>', out)
    # 「今日新上」stat caption: show the board date when something was added
    # that day; otherwise say so instead of keeping a stale listing date.
    if any(d.get("added") == board_date for d in deals):
        out = re.sub(r'(今日新上<br />)\d{4}-\d{2}-\d{2}( 核实上架)',
                     rf'\g<1>{board_date}\g<2>', out)
    else:
        out = re.sub(r'今日新上<br />\d{4}-\d{2}-\d{2} 核实上架',
                     '今日新上<br />今日暂无新上', out)
    out_path = Path(args.out) if args.out else ROOT / "index.html"
    out_path.write_text(out, encoding="utf-8")
    print(f"rendered {len(deals)} deals, board date {board_date} -> {out_path}")

if __name__ == "__main__":
    main()
