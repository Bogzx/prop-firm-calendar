"""The hosted landing page: next interruption, live countdown, one-click subscribe.

Self-contained HTML (inline CSS/JS, no external requests): it must render
offline, stay fast, and not leak subscriber traffic to CDNs. Times are served
as ISO in data attributes; JavaScript upgrades them to the visitor's local
timezone and runs the countdown. Without JS the page still shows UTC times.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from ftmo_calendar.state import State

_MAX_PAST_ROWS = 6

_SOURCE_STATES = {
    "ok": "OPERATIONAL",
    "error": "SYNC ERROR",
    "stale": "GONE QUIET",
    "anomaly": "NEEDS REVIEW",
}


def _humanize(seconds: float) -> str:
    """Compact age string: '4 min', '3 h', '12 d'."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} d"


def _row(summary: str, start: str, end: str, state_cls: str, firm_label: str = "") -> str:
    # The badge sits inside the event cell rather than in a column of its own:
    # a fourth column is unreadable on a phone, and a single-firm page must not
    # grow one at all.
    badge = f'<span class="fbadge">{html.escape(firm_label)}</span>' if firm_label else ""
    return (
        f'<tr class="{state_cls}">'
        f'<td class="ev">{badge}{html.escape(summary)}</td>'
        f'<td><time data-iso="{html.escape(start)}">{html.escape(start)}</time></td>'
        f'<td><time data-iso="{html.escape(end)}">{html.escape(end)}</time></td>'
        f"</tr>"
    )


def _firm_titles(sources: list) -> dict[str, str]:
    """Profile name -> display name, read off the per-firm health snapshot."""
    titles = {}
    for entry in sources:
        name = str(entry.get("firm") or "")
        if name:
            titles[name] = str(entry.get("display_name") or name)
    return titles


def _page_title(sources: list) -> str:
    """Name the page after what is actually in it.

    Deliberately mirrors `ics.calendar_name`: the heading a visitor reads and
    the calendar name their app shows come from the same rule, so the two can
    never disagree. A one-firm deployment keeps that firm's name, which is what
    makes this change invisible to every existing single-firm self-hoster.
    """
    if len(sources) > 1:
        return "Prop Firm Trading Calendar"
    if len(sources) == 1:
        entry = sources[0]
        return f"{entry.get('display_name') or entry.get('firm') or 'FTMO'} Trading Calendar"
    return "FTMO Trading Calendar"  # no health data yet: keep the historical name


def render_page(
    state: State, snapshot: dict, stats: dict | None = None, *, default_firm: str = ""
) -> bytes:
    now = datetime.now(UTC)
    # Per-source health drives the whole multi-firm presentation: branding,
    # chips and badges all key off how many firms are actually configured.
    sources = snapshot.get("sources") or []
    titles = _firm_titles(sources)
    multi = len(sources) > 1

    upcoming: list[tuple[datetime, str, str, str, str]] = []
    past: list[tuple[datetime, str, str, str, str]] = []
    for post in state.posts.values():
        # `default_firm` attributes state written before per-firm tracking; without
        # it every pre-upgrade event would render with a blank badge.
        firm = state.firm_of(post, default_firm) if multi else ""
        label = titles.get(firm, firm)
        for event in post.events:
            if not event.summary or not event.start:
                continue
            try:
                start_dt = datetime.fromisoformat(event.start)
                end_dt = datetime.fromisoformat(event.end)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=UTC)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=UTC)
            except ValueError:
                continue
            target = upcoming if end_dt > now else past
            target.append((start_dt, event.summary, event.start, event.end, label))
    upcoming.sort(key=lambda item: item[0])
    past.sort(key=lambda item: item[0], reverse=True)

    rows = [
        _row(summary, start, end, "live" if start_dt <= now else "soon", label)
        for start_dt, summary, start, end, label in upcoming
    ]
    rows += [
        _row(summary, start, end, "past", label)
        for _, summary, start, end, label in past[:_MAX_PAST_ROWS]
    ]
    table_body = (
        "".join(rows) or '<tr><td colspan="3" class="empty">no events tracked yet</td></tr>'
    )

    ok = bool(snapshot.get("ok"))
    state_name = str(snapshot.get("status") or ("ok" if ok else "error"))
    health_cls = "ok" if ok else "err"
    health_text = {
        "ok": "OPERATIONAL",
        "error": "SYNC ERROR",
        "stale": "SYNC STALE",
        "anomaly": "NEEDS REVIEW",
        "degraded": "SOURCE DOWN",
    }.get(state_name, "OPERATIONAL" if ok else "SYNC ERROR")
    last_error = snapshot.get("last_error") or ""
    error_line = (
        f'<p class="errline">last error: {html.escape(last_error)}</p>' if last_error else ""
    )
    anomalies = snapshot.get("anomalies") or []
    if anomalies:
        error_line += "".join(
            f'<p class="errline">anomaly: {html.escape(str(a))}</p>' for a in anomalies
        )

    # Freshness is the trust signal: a green badge next to a three-week-old
    # sync is exactly the reassurance nobody should be given.
    age_seconds = snapshot.get("last_success_age_seconds")
    if snapshot.get("last_success") is None:
        freshness = "never"  # a running process is not a working one
    elif isinstance(age_seconds, int | float):
        freshness = f"{_humanize(float(age_seconds))} ago"
    else:
        freshness = "unknown"
    source_name = snapshot.get("source") or "FTMO"
    source_line = (
        f'<span class="srcline">source: {html.escape(str(source_name))} · '
        f"last successful sync {html.escape(freshness)}</span>"
    )

    # Per-source health. A combined feed that reports one overall badge lets a
    # firm whose scraper has gone quiet hide behind the healthy ones — and its
    # subscribers are precisely the people who then get caught by an outage.
    # Rendered only when there is more than one source, so a single-firm
    # deployment's page is unchanged.
    sources_section = ""
    if multi:
        cards = []
        for entry in sources:
            healthy = bool(entry.get("ok"))
            cls = "ok" if healthy else "err"
            label = _SOURCE_STATES.get(str(entry.get("status")), "OPERATIONAL")
            age = entry.get("last_success_age_seconds")
            if entry.get("last_success") is None:
                when = "never synced"
            elif isinstance(age, int | float):
                when = f"{_humanize(float(age))} ago"
            else:
                when = "unknown"
            detail = entry.get("last_error") or "; ".join(entry.get("anomalies") or [])
            detail_html = (
                f'<span class="srcnote">{html.escape(str(detail)[:200])}</span>' if detail else ""
            )
            cards.append(
                f'<div class="src {cls}"><span class="dot"></span>'
                f"<b>{html.escape(str(entry.get('display_name') or entry.get('firm')))}</b>"
                f"<span>{label} · {html.escape(when)}</span>{detail_html}</div>"
            )
        sources_section = (
            f'<section><h2>SOURCES</h2><div class="srcgrid">{"".join(cards)}</div></section>'
        )

    # Branding is derived, never configured, so the heading a visitor reads and
    # the calendar name their app shows are always the same claim.
    firm_names = [titles[str(entry.get("firm"))] for entry in sources if entry.get("firm")]
    page_title = _page_title(sources)
    brand_sub = (
        " · ".join(html.escape(name) for name in firm_names)
        if multi
        else "maintenance &amp; market closures · auto-synced"
    )
    subject = "prop firm" if multi else (firm_names[0] if firm_names else "FTMO")
    meta_desc = (
        f"Live calendar of {html.escape(subject)} maintenance windows and market closures. "
        "Subscribe once — never get caught by a platform outage again."
    )
    if multi:
        affiliation = "not affiliated with any listed firm"
    elif firm_names:
        affiliation = f"not affiliated with {html.escape(firm_names[0])}"
    else:
        affiliation = "not affiliated with FTMO"
    # The upstream link is FTMO's only when FTMO is the only firm; with several,
    # the SOURCES panel names them all and one link would misrepresent the rest.
    feed_link = (
        '<span>feed: <a href="https://ftmo.com/en/trading-updates/" rel="noopener">ftmo.com</a></span>'
        if not sources or (len(sources) == 1 and sources[0].get("firm") == "ftmo")
        else ""
    )

    # Firm chips drive the ?firms= filter the server already supports. Offered
    # only when there is a choice to make: one firm means one checkbox, which is
    # noise rather than a filter.
    firm_filters = ""
    if multi:
        chips = "".join(
            '<label><input type="checkbox" data-firm="{firm}" checked> {name}</label>'.format(
                firm=html.escape(str(entry.get("firm"))),
                name=html.escape(titles[str(entry.get("firm"))]),
            )
            for entry in sources
            if entry.get("firm")
        )
        firm_filters = (
            '<div class="filters">'
            '<span class="hint">Only some firms? Untick — the URL updates:</span>'
            f"{chips}</div>"
        )

    def iso_or_dash(key: str) -> str:
        value = snapshot.get(key)
        return (
            f'<time data-iso="{html.escape(value)}">{html.escape(value)}</time>' if value else "—"
        )

    stats_line = ""
    if stats is not None:
        today = stats.get("today", {})
        stats_line = (
            f"<span>today: {today.get('visitors', 0)} visitors · "
            f"{today.get('feed_hits', 0)} feed pulls</span>"
            '<span class="privacy">anonymous first-party visit counting only — '
            "no third-party trackers</span>"
        )

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{meta_desc}">
<title>{html.escape(page_title)} — next interruption</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 \
viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📅</text></svg>">
<style>
:root {{
  --ink:#0a0e14; --panel:#0e141d; --line:#1c2736; --line-soft:#141d2a;
  --txt:#c9d4e3; --dim:#5d6d83; --faint:#39465a;
  --amber:#ffb02e; --amber-dim:#8a5f1a; --green:#3ddc84; --red:#ff5d5d;
  --mono:ui-monospace,"Cascadia Code","SF Mono",Consolas,Menlo,monospace;
  --serif:Georgia,"Times New Roman",serif;
}}
* {{ box-sizing:border-box; margin:0; }}
html {{ background:var(--ink); }}
body {{
  font-family:var(--mono); color:var(--txt); min-height:100vh;
  background:
    radial-gradient(1200px 500px at 70% -10%, rgba(255,176,46,.05), transparent 60%),
    radial-gradient(900px 600px at 0% 110%, rgba(61,220,132,.04), transparent 55%),
    var(--ink);
}}
body::after {{
  content:""; position:fixed; inset:0; pointer-events:none; opacity:.5;
  background:repeating-linear-gradient(0deg, transparent 0 2px, rgba(0,0,0,.16) 2px 3px);
}}
.wrap {{ max-width:880px; margin:0 auto; padding:0 20px 48px; position:relative; }}

header {{
  display:flex; align-items:center; gap:14px; padding:22px 0 18px;
  border-bottom:1px solid var(--line);
}}
.mark {{ width:30px; height:30px; flex:none; }}
.brand {{ font-size:13px; letter-spacing:.22em; }}
.brand small {{ display:block; color:var(--dim); letter-spacing:.08em; font-size:11px; margin-top:3px; }}
.health {{ margin-left:auto; font-size:11px; letter-spacing:.18em; display:flex; align-items:center; gap:8px; }}
.dot {{ width:8px; height:8px; border-radius:50%; }}
.ok .dot {{ background:var(--green); box-shadow:0 0 10px var(--green); animation:pulse 2.4s infinite; }}
.err .dot {{ background:var(--red); box-shadow:0 0 10px var(--red); }}
.ok {{ color:var(--green); }} .err {{ color:var(--red); }}
@keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.35 }} }}

.hero {{ padding:54px 0 40px; border-bottom:1px solid var(--line); animation:rise .6s ease both; }}
.label {{ font-size:11px; letter-spacing:.3em; color:var(--dim); }}
.label .accent {{ color:var(--amber); }}
#count {{
  font-size:clamp(30px,8.6vw,84px); font-weight:700; letter-spacing:.02em;
  font-variant-numeric:tabular-nums; line-height:1.05; margin:14px 0 6px;
  color:var(--amber); text-shadow:0 0 28px rgba(255,176,46,.25);
}}
#count.clear {{ color:var(--green); text-shadow:0 0 28px rgba(61,220,132,.2); }}
#count small {{ font-size:.38em; font-weight:400; color:var(--amber-dim); letter-spacing:.1em; }}
#nextname {{ font-size:16px; }}
#nextwhen {{ color:var(--dim); font-size:13px; margin-top:6px; }}
.tzline {{ font-family:var(--serif); font-style:italic; color:var(--faint); font-size:13px; margin-top:14px; }}

section {{ padding:34px 0 0; }}
h2 {{ font-size:11px; letter-spacing:.3em; color:var(--dim); font-weight:400; margin-bottom:18px; }}
h2::before {{ content:"// "; color:var(--faint); }}

.sub {{ display:flex; flex-wrap:wrap; gap:10px; align-items:stretch; animation:rise .6s .12s ease both; }}
.urlbox {{
  flex:1 1 320px; display:flex; border:1px solid var(--line); background:var(--panel);
}}
#feedurl {{
  flex:1; background:none; border:0; color:var(--amber); font:inherit; font-size:13px;
  padding:13px 14px; min-width:0;
}}
button, .btn {{
  font:inherit; font-size:12px; letter-spacing:.14em; cursor:pointer; text-decoration:none;
  color:var(--ink); background:var(--amber); border:1px solid var(--amber);
  padding:13px 20px; transition:filter .15s;
}}
button:hover, .btn:hover {{ filter:brightness(1.15); }}
.btn.ghost {{ background:none; color:var(--amber); }}
.filters {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; align-items:center; }}
.filters .hint {{ font-family:var(--serif); font-style:italic; color:var(--faint); font-size:12.5px; margin-right:6px; }}
.filters label {{ border:1px solid var(--line); background:var(--panel); padding:7px 12px;
  font-size:11px; letter-spacing:.12em; color:var(--dim); cursor:pointer;
  display:flex; gap:7px; align-items:center; user-select:none; transition:color .15s,border-color .15s; }}
.filters label:has(input:checked) {{ color:var(--amber); border-color:var(--amber-dim); }}
.filters input {{ accent-color:var(--amber); margin:0; }}
.apps {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; margin-top:14px; }}
.app {{ border:1px solid var(--line-soft); background:var(--panel); padding:14px 16px; font-size:12.5px; line-height:1.65; }}
.app b {{ color:var(--txt); display:block; margin-bottom:4px; letter-spacing:.08em; }}
.app span {{ color:var(--dim); font-family:var(--serif); font-size:13px; }}

.srcgrid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:10px; }}
.src {{ border:1px solid var(--line-soft); background:var(--panel); padding:13px 15px;
  font-size:12px; display:grid; grid-template-columns:auto 1fr; gap:3px 9px; align-items:baseline; }}
.src .dot {{ grid-row:1/3; align-self:center; }}
.src b {{ color:var(--txt); letter-spacing:.08em; }}
.src span {{ color:var(--dim); font-size:11px; letter-spacing:.1em; }}
.src.err b {{ color:var(--red); }}
.src .srcnote {{ grid-column:2; color:var(--faint); font-family:var(--serif); font-style:italic;
  letter-spacing:0; font-size:12px; }}

table {{ width:100%; border-collapse:collapse; font-size:13px; animation:rise .6s .2s ease both; }}
th {{ text-align:left; color:var(--faint); font-weight:400; font-size:11px; letter-spacing:.2em;
     padding:0 12px 10px; border-bottom:1px solid var(--line); }}
td {{ padding:13px 12px; border-bottom:1px solid var(--line-soft); vertical-align:top; }}
tr.soon td.ev {{ border-left:2px solid var(--amber); }}
tr.live td.ev {{ border-left:2px solid var(--red); }}
tr.live td.ev::after {{ content:" ● LIVE"; color:var(--red); font-size:10px; letter-spacing:.15em; }}
tr.past {{ opacity:.38; }}
.fbadge {{ display:inline-block; border:1px solid var(--line); color:var(--dim);
  font-size:9.5px; letter-spacing:.14em; text-transform:uppercase; padding:2px 6px;
  margin-right:9px; vertical-align:1px; white-space:nowrap; }}
td time {{ color:var(--dim); font-variant-numeric:tabular-nums; }}
.empty {{ color:var(--faint); font-family:var(--serif); font-style:italic; }}

footer {{ margin-top:46px; padding-top:18px; border-top:1px solid var(--line);
  font-size:11px; color:var(--faint); display:flex; flex-wrap:wrap; gap:8px 26px; letter-spacing:.06em; }}
footer time {{ color:var(--dim); }}
footer a {{ color:var(--dim); text-decoration:none; border-bottom:1px solid var(--line); }}
footer a:hover {{ color:var(--amber); }}
.errline {{ color:var(--red); font-size:11px; margin-top:10px; width:100%; }}
.srcline {{ color:var(--dim); }}
.privacy {{ font-family:var(--serif); font-style:italic; }}
@keyframes rise {{ from {{ opacity:0; transform:translateY(10px) }} to {{ opacity:1; transform:none }} }}
@media (max-width:520px) {{
  #count {{ font-size:clamp(22px,7.5vw,32px); letter-spacing:0; }}
  td {{ padding:11px 8px; }} th {{ padding:0 8px 8px; }}
}}
</style></head><body><div class="wrap">

<header>
  <svg class="mark" viewBox="0 0 32 32" fill="none" aria-hidden="true">
    <rect x="1.5" y="1.5" width="29" height="29" stroke="#ffb02e" stroke-width="1.5"/>
    <rect x="7" y="13" width="3.5" height="9" fill="#3ddc84"/>
    <path d="M8.75 9v4M8.75 22v3" stroke="#3ddc84" stroke-width="1.4"/>
    <rect x="14.25" y="9" width="3.5" height="8" fill="#ff5d5d"/>
    <path d="M16 6v3M16 17v4" stroke="#ff5d5d" stroke-width="1.4"/>
    <rect x="21.5" y="15" width="3.5" height="7" fill="#ffb02e"/>
    <path d="M23.25 11v4M23.25 22v4" stroke="#ffb02e" stroke-width="1.4"/>
  </svg>
  <div class="brand">{html.escape(page_title).upper()}
    <small>{brand_sub}</small></div>
  <div class="health {health_cls}"><span class="dot"></span>{health_text}</div>
</header>

<div class="hero">
  <div class="label"><span class="accent">▮</span> NEXT TRADING INTERRUPTION</div>
  <div id="count">—</div>
  <div id="nextname">&nbsp;</div>
  <div id="nextwhen"></div>
  <p class="tzline">All times shown in your local timezone.</p>
</div>

<section>
  <h2>SUBSCRIBE — 30 SECONDS, NO ACCOUNT</h2>
  <div class="sub">
    <div class="urlbox"><input id="feedurl" readonly value="/feed.ics" aria-label="Calendar feed URL"></div>
    <button id="copybtn" type="button">COPY URL</button>
    <a id="webcal" class="btn ghost" href="/feed.ics">OPEN IN CALENDAR APP</a>
  </div>
  <div class="filters">
    <span class="hint">Only care about some of it? Untick — the URL updates:</span>
    <label><input type="checkbox" data-type="maintenance" checked> MAINTENANCE</label>
    <label><input type="checkbox" data-type="crypto_closure" checked> CRYPTO</label>
    <label><input type="checkbox" data-type="holiday_closure" checked> HOLIDAY CLOSURES</label>
    <label><input type="checkbox" data-type="early_close" checked> EARLY CLOSES</label>
    <label><input type="checkbox" data-type="late_open" checked> LATE OPENS</label>
    <label><input type="checkbox" data-type="symbol_event" checked> SYMBOL EVENTS</label>
    <label><input type="checkbox" data-type="other" checked> OTHER</label>
  </div>
  {firm_filters}
  <div class="apps">
    <div class="app"><b>GOOGLE CALENDAR</b>
      <span>Other calendars → + → From URL → paste the feed URL. Appears on your phone automatically.</span></div>
    <div class="app"><b>APPLE CALENDAR</b>
      <span>File → New Calendar Subscription… → paste the URL (or use the button above).</span></div>
    <div class="app"><b>OUTLOOK</b>
      <span>Add calendar → Subscribe from web → paste the URL.</span></div>
  </div>
</section>

{sources_section}

<section>
  <h2>SCHEDULE</h2>
  <table>
    <thead><tr><th>EVENT</th><th>START</th><th>END</th></tr></thead>
    <tbody>{table_body}</tbody>
  </table>
</section>

<footer>
  <span>last sync {iso_or_dash("last_run")}</span>
  <span>next sync {iso_or_dash("next_run")}</span>
  <span>ok {snapshot.get("runs_ok", 0)} · failed {snapshot.get("runs_failed", 0)}</span>
  {source_line}
  {feed_link}
  <span><a href="https://github.com/Bogzx/prop-firm-calendar" rel="noopener">open source</a> · {affiliation}</span>
  {stats_line}
  {error_line}
</footer>

</div><script>
(function () {{
  var urlEl = document.getElementById("feedurl");
  var webcalEl = document.getElementById("webcal");
  // Scoped per axis: both filter rows live in .filters, so an unscoped
  // selector would read data-type off a firm checkbox and emit "?types=null".
  var typeBoxes = [].slice.call(document.querySelectorAll(".filters input[data-type]"));
  var firmBoxes = [].slice.call(document.querySelectorAll(".filters input[data-firm]"));
  var boxes = typeBoxes.concat(firmBoxes);
  function axis(group, attr, key) {{
    var checked = group.filter(function (b) {{ return b.checked; }})
                       .map(function (b) {{ return b.getAttribute(attr); }});
    // Everything ticked means no filter at all: the bare /feed.ics is served
    // straight from disk, and existing subscribers must stay on that path.
    return (checked.length && checked.length < group.length)
      ? key + "=" + checked.join(",") : "";
  }}
  function feedQuery() {{
    var parts = [axis(typeBoxes, "data-type", "types"),
                 axis(firmBoxes, "data-firm", "firms")].filter(Boolean);
    return parts.length ? "?" + parts.join("&") : "";
  }}
  function refreshUrls() {{
    var qs = feedQuery();
    urlEl.value = location.origin + "/feed.ics" + qs;
    webcalEl.href = "webcal://" + location.host + "/feed.ics" + qs;
  }}
  boxes.forEach(function (b) {{ b.addEventListener("change", refreshUrls); }});
  refreshUrls();
  document.getElementById("copybtn").addEventListener("click", function () {{
    var btn = this, value = urlEl.value;
    (navigator.clipboard ? navigator.clipboard.writeText(value)
      : Promise.reject()).catch(function () {{ urlEl.select(); document.execCommand("copy"); }})
      .then(function () {{
        btn.textContent = "COPIED ✓";
        setTimeout(function () {{ btn.textContent = "COPY URL"; }}, 1600);
      }});
  }});

  var fmt = new Intl.DateTimeFormat(undefined,
    {{ weekday:"short", day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit" }});
  document.querySelectorAll("time[data-iso]").forEach(function (el) {{
    var d = new Date(el.getAttribute("data-iso"));
    if (!isNaN(d)) el.textContent = fmt.format(d);
  }});

  var events = [];
  document.querySelectorAll("tr.soon, tr.live").forEach(function (tr) {{
    var times = tr.querySelectorAll("time[data-iso]");
    events.push({{
      name: tr.querySelector(".ev").childNodes[0].textContent.trim(),
      start: new Date(times[0].getAttribute("data-iso")),
      end: new Date(times[1].getAttribute("data-iso"))
    }});
  }});

  var countEl = document.getElementById("count");
  var nameEl = document.getElementById("nextname");
  var whenEl = document.getElementById("nextwhen");
  function pad(n) {{ return String(n).padStart(2, "0"); }}
  function tick() {{
    var now = new Date();
    var next = events.filter(function (e) {{ return e.end > now; }})
                     .sort(function (a, b) {{ return a.start - b.start; }})[0];
    if (!next) {{
      countEl.textContent = "ALL CLEAR";
      countEl.classList.add("clear");
      nameEl.textContent = "no trading interruptions scheduled";
      whenEl.textContent = "";
      return;
    }}
    countEl.classList.remove("clear");
    var inProgress = next.start <= now;
    var target = inProgress ? next.end : next.start;
    var s = Math.max(0, Math.floor((target - now) / 1000));
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
        m = Math.floor(s % 3600 / 60), sec = s % 60;
    countEl.innerHTML = (d ? d + "<small>D </small>" : "") + pad(h) + "<small>H </small>"
      + pad(m) + "<small>M </small>" + pad(sec) + "<small>S</small>";
    nameEl.textContent = next.name;
    whenEl.textContent = (inProgress ? "in progress — ends " : "begins ")
      + fmt.format(inProgress ? next.end : next.start);
  }}
  tick();
  setInterval(tick, 1000);
}})();
</script></body></html>"""
    return page.encode("utf-8")
