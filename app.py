#!/usr/bin/env python3
"""
Flight search web app: Flask + HTMX + Plotly.js + fast-flights.
Uses the monospace-web design system.
"""

import re, time, uuid, json, traceback, html
from datetime import date as date_cls, datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, render_template_string, Response, stream_with_context

from fast_flights import FlightData, Passengers, get_flights
import airportsdata

app = Flask(__name__)

IATA_DB = airportsdata.load('IATA')

# ---------------------------------------------------------------------------
# Airport lookup (via airportsdata)
# ---------------------------------------------------------------------------
def get_airport(code):
    """Return airport dict or None if not a valid IATA code."""
    return IATA_DB.get(code.upper())

def get_tz(code):
    ap = get_airport(code)
    return ZoneInfo(ap['tz']) if ap else None

def airport_name(code):
    ap = get_airport(code)
    if not ap:
        return None
    return ap.get('city', ap['name'])

SEARCHES = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_hhmm(s):
    if not s:
        return None, None
    m = re.match(r'(\d{1,2}):(\d{2})\s*(AM|PM)', s.strip(), re.I)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if ap == 'PM' and h != 12: h += 12
        elif ap == 'AM' and h == 12: h = 0
        return h, mi
    m2 = re.match(r'(\d{1,2}):(\d{2})', s.strip())
    if m2:
        return int(m2.group(1)), int(m2.group(2))
    return None, None


def extract_td(s):
    m = re.match(r'(\d{1,2}:\d{2}\s*[AP]M)\s+on\s+(.*)', s.strip(), re.I)
    if not m:
        return None, None, None, None
    ts, ds = m.group(1).strip(), m.group(2).strip()
    h, mi = parse_hhmm(ts)
    return ts, ds, h, mi


def price_int(s):
    c = re.sub(r'[^\d]', '', s)
    return int(c) if c else 999999


def date_day(ds):
    m = re.search(r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d+)', ds)
    return int(m.group(1)) if m else None


def date_month(ds):
    months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
              'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
    m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', ds)
    return months.get(m.group(1)) if m else None


def to_dt(date_str, h, mi, year=2026, tz=None):
    mon = date_month(date_str)
    day = date_day(date_str)
    if mon is None or day is None or h is None:
        return None
    dt = datetime(year, mon, day, h, mi)
    if tz:
        dt = dt.replace(tzinfo=tz)
    return dt


def search_retry(fd, pax, retries=4):
    for i in range(retries):
        try:
            return get_flights(flight_data=[fd], trip="one-way",
                               passengers=pax, seat="economy")
        except RuntimeError:
            if i < retries - 1:
                wait = 2 ** i  # 1s, 2s, 4s
                time.sleep(wait)
            else:
                raise


def dedup(flights):
    seen, out = set(), []
    for f in flights:
        key = (f['airline'], f['dep_time'],
               f.get('arr_str', f.get('arr_time', '')), f['price'])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def time_minutes(h, m):
    return h * 60 + m if h is not None else None


def progress(msg):
    safe = msg.replace("'", "\\'")
    return (f'<script>document.getElementById("log").innerHTML += '
            f"'<p>{safe}</p>';window.scrollTo(0,document.body.scrollHeight);"
            f'</script>\n')


def esc(s):
    return html.escape(str(s)) if s else ''


def make_dt(date_str, time_str, tz):
    """Combine a date string (ISO) and optional time string (HH:MM) into a datetime."""
    if not date_str:
        return None
    d = date_cls.fromisoformat(date_str)
    h, mi = parse_hhmm(time_str) if time_str else (None, None)
    dt = datetime(d.year, d.month, d.day, h or 0, mi or 0)
    if tz:
        dt = dt.replace(tzinfo=tz)
    return dt


def compute_search_dates(after_dt, before_dt):
    """Return list of ISO date strings to search (departure dates).

    Both after_dt and before_dt should be set (via symmetric defaults).
    Covers every day from after_dt.date() through before_dt.date().
    """
    if not after_dt or not before_dt:
        return []
    d0 = after_dt.date() if isinstance(after_dt, datetime) else after_dt
    d1 = before_dt.date() if isinstance(before_dt, datetime) else before_dt
    dates = []
    d = d0
    while d <= d1:
        dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


def short_t(dt):
    """Format a datetime as compact time like '10p' or '9:30a'."""
    h = dt.hour
    m = dt.minute
    ap = 'a' if h < 12 else 'p'
    h12 = h % 12 or 12
    if m:
        return f'{h12}:{m:02d}{ap}'
    return f'{h12}{ap}'


def build_html_timeline(pareto_indices, kept, sid=''):
    """Build an HTML/CSS Gantt timeline for Pareto flights.

    Layout per row:
      [price 7ch] [outbound flex:1] [dest-hrs 7ch] [return flex:1]

    Each flight zone spans from earliest departure to latest arrival
    across all entries.  Within that 0-1 range every flight is positioned
    proportionally: legs are black, layover gaps are white (page bg),
    and the margin after outbound / before return is gray to blend
    seamlessly into the destination label.
    """
    entries = []
    for i in pareto_indices:
        p = kept[i]
        o, r = p['out'], p['ret']
        od, oa = o.get('dep_dt'), o.get('arr_dt')
        rd, ra = r.get('dep_dt'), r.get('arr_dt')
        if not all([od, oa, rd, ra]):
            continue
        entries.append({
            'price': p['total'],
            'od': od, 'oa': oa, 'rd': rd, 'ra': ra,
            'dest_hrs': p['dest_hrs'],
            'out_stops': o['stops'], 'ret_stops': r['stops'],
            'out_airline': o['airline'], 'ret_airline': r['airline'],
            'idx': i,
        })

    if not entries:
        return ''

    # --- Normalize to UTC for positioning (handles cross-tz flights) ---
    _utc = ZoneInfo('UTC')
    def to_utc(dt):
        if dt is None:
            return None
        if dt.tzinfo is not None:
            return dt.astimezone(_utc)
        return dt.replace(tzinfo=_utc)

    for e in entries:
        e['od_u'] = to_utc(e['od'])
        e['oa_u'] = to_utc(e['oa'])
        e['rd_u'] = to_utc(e['rd'])
        e['ra_u'] = to_utc(e['ra'])

    # --- Zone boundaries (earliest dep → latest arr for each direction) ---
    out_t0 = min(e['od_u'] for e in entries)
    out_t1 = max(e['oa_u'] for e in entries)
    ret_t0 = min(e['rd_u'] for e in entries)
    ret_t1 = max(e['ra_u'] for e in entries)

    out_span = (out_t1 - out_t0).total_seconds() or 1
    ret_span = (ret_t1 - ret_t0).total_seconds() or 1

    # Proportional zone widths
    total_span = out_span + ret_span
    out_flex = out_span / total_span * 100
    ret_flex = ret_span / total_span * 100

    def stop_cls(n):
        if n == 0: return 'tl-s0'
        if n == 1: return 'tl-s1'
        if n == 2: return 'tl-s2'
        return 'tl-s3'

    def legs_html(n):
        return ''.join('<div class="tl-leg"></div>' for _ in range(n))

    axis = ''  # no top tick axis

    # --- Data rows ---
    rows = []
    for e in entries:
        # Outbound: position within zone (using UTC)
        o_pre  = (e['od_u'] - out_t0).total_seconds() / out_span * 100
        o_bar  = (e['oa_u'] - e['od_u']).total_seconds() / out_span * 100
        o_post = (out_t1 - e['oa_u']).total_seconds() / out_span * 100

        # Return: position within zone (using UTC)
        r_pre  = (e['rd_u'] - ret_t0).total_seconds() / ret_span * 100
        r_bar  = (e['ra_u'] - e['rd_u']).total_seconds() / ret_span * 100
        r_post = (ret_t1 - e['ra_u']).total_seconds() / ret_span * 100

        out_time = f'{short_t(e["od"])}\u2013{short_t(e["oa"])}'
        ret_time = f'{short_t(e["rd"])}\u2013{short_t(e["ra"])}'

        out_stops_s = 'Nonstop' if e['out_stops'] == 0 else f'{e["out_stops"]} stop'
        ret_stops_s = 'Nonstop' if e['ret_stops'] == 0 else f'{e["ret_stops"]} stop'
        out_title = esc(f'{e["out_airline"]} \u00b7 {out_time} \u00b7 {out_stops_s}')
        ret_title = esc(f'{e["ret_airline"]} \u00b7 {ret_time} \u00b7 {ret_stops_s}')

        o_dep = short_t(e['od'])
        o_arr = short_t(e['oa'])
        r_dep = short_t(e['rd'])
        r_arr = short_t(e['ra'])

        rows.append(
            f'<div class="tl-row" hx-get="/flight/{sid}/{e["idx"]}" hx-target="#detail" hx-swap="innerHTML">'
            f'<div class="tl-price">${e["price"]}</div>'
            # ── outbound zone ──
            f'<div class="tl-zone" style="flex:{out_flex:.1f} 1 0">'
            f'<div class="tl-space" style="flex:0 0 {o_pre:.2f}%"></div>'
            f'<div class="tl-flight {stop_cls(e["out_stops"])}" style="flex:0 0 {o_bar:.2f}%" title="{out_title}">'
            f'{legs_html(e["out_stops"] + 1)}'
            f'<span class="tl-ftime">{o_dep}</span></div>'
            f'<div class="tl-space tl-gray" style="flex:0 0 {o_post:.2f}%"></div>'
            f'<span class="tl-gtime" style="left:{o_pre + o_bar:.2f}%;padding-left:0.5ch">{o_arr}</span>'
            f'</div>'
            # ── destination label ──
            f'<div class="tl-mid">{e["dest_hrs"]}h</div>'
            # ── return zone ──
            f'<div class="tl-zone" style="flex:{ret_flex:.1f} 1 0">'
            f'<div class="tl-space tl-gray" style="flex:0 0 {r_pre:.2f}%"></div>'
            f'<div class="tl-flight {stop_cls(e["ret_stops"])}" style="flex:0 0 {r_bar:.2f}%" title="{ret_title}">'
            f'{legs_html(e["ret_stops"] + 1)}'
            f'<span class="tl-ftime">{r_dep}</span></div>'
            f'<div class="tl-space" style="flex:0 0 {r_post:.2f}%"></div>'
            f'<span class="tl-gtime" style="left:{r_pre + r_bar:.2f}%;padding-left:0.5ch">{r_arr}</span>'
            f'</div>'
            f'</div>'
        )

    return f'<div class="tl-wrap">{axis}\n{"".join(rows)}\n</div>'


# ---------------------------------------------------------------------------
# CSS / HTML constants
# ---------------------------------------------------------------------------

HEAD = '''
<link rel="stylesheet" href="/static/reset.css">
<link rel="stylesheet" href="/static/monospace.css">
'''

APP_STYLE = '''
<style>
  /* App-specific overrides */
  body { max-width: calc(min(90ch, round(down, 100%, 1ch))); }
  .row { display: flex !important; flex-direction: row !important; gap: 1ch; flex-wrap: wrap; margin-bottom: var(--line-height); }
  .row > * + * { margin-top: 0; }
  .field { display: flex !important; flex-direction: column !important; flex: 1 1 0; min-width: 10ch; max-width: none !important; }
  .field label { margin-bottom: 0; }
  .field label + input { margin-top: 0; }
  #chart { width: 100%; height: 520px; background: var(--background-color-alt); margin: var(--line-height) 0; }
  #detail { border: var(--border-thickness) solid var(--text-color); padding: calc(var(--line-height) - var(--border-thickness)) 1ch; margin: var(--line-height) 0; }
  tr.clickable:hover { background: var(--background-color-alt); cursor: pointer; }
  .back { margin-bottom: var(--line-height); display: inline-block; }
  .btn-search { background: #3498db; color: #fff; border-color: #2980b9; }
  .btn-search:hover { background: #2980b9; }
  nav { display: flex; justify-content: flex-end; gap: 1ch; }
  nav > * + * { margin-top: 0; }
  .btn-clear { }
  .airport-hint { color: var(--text-color-alt); margin: 0; }
  .airport-hint.invalid { color: #e74c3c; }
  #log p { margin: 0; }
  .spinner { display: inline-block; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  /* Fix Plotly — monospace CSS * + * margin and line-height breaks SVG */
  .js-plotly-plot, .js-plotly-plot * { line-height: normal !important; }
  .js-plotly-plot * + * { margin-top: 0 !important; }

  /* Timeline — [price] [outbound flex:1] [dest-hrs] [return flex:1] */
  .tl-wrap { margin: var(--line-height) 0; }
  .tl-wrap * + * { margin-top: 0; }
  /* rows */
  .tl-row { display: flex; align-items: stretch; height: calc(var(--line-height) * 2); cursor: pointer; }
  .tl-row:hover { background: var(--background-color-alt); }
  .tl-price { flex: 0 0 7ch; text-align: right; padding-right: 1ch;
              line-height: calc(var(--line-height) * 2); white-space: nowrap; overflow: hidden; }
  .tl-zone { flex: 1 1 0; display: flex; align-items: stretch; min-width: 0; overflow: hidden; position: relative; }
  .tl-mid { flex: 0 0 7ch; display: flex; align-items: center; justify-content: center;
            background: var(--background-color-alt); white-space: nowrap; }
  .tl-space { }
  .tl-gray { background: var(--background-color-alt); }
  .tl-flight { position: relative; display: flex; align-items: stretch;
               gap: 3px; overflow: hidden; cursor: default; }
  .tl-leg { flex: 1; }
  .tl-s0 .tl-leg { background: #2ecc71; }
  .tl-s1 .tl-leg { background: #3498db; }
  .tl-s2 .tl-leg { background: #f39c12; }
  .tl-s3 .tl-leg { background: #e74c3c; }
  .tl-ftime { position: absolute; inset: 0; display: flex; align-items: center;
              z-index: 1; white-space: nowrap; color: #fff;
              padding: 0 0.5ch; overflow: hidden; pointer-events: none; }
  .tl-gtime { position: absolute; top: 0; bottom: 0; display: flex; align-items: center;
              white-space: nowrap; color: var(--text-color-alt);
              pointer-events: none; z-index: 2; }
  /* legend */
  .tl-legend { line-height: var(--line-height); }
  .tl-swatch { display: inline-block; width: 2ch; height: calc(var(--line-height) * 0.6);
               vertical-align: middle; margin: 0 0.5ch 0 1ch; }
  .tl-swatch:first-child { margin-left: 0; }
  .tl-sw-s0 { background: #2ecc71; }
  .tl-sw-s1 { background: #3498db; }
  .tl-sw-s2 { background: #f39c12; }
  .tl-sw-dest { background: var(--background-color-alt); }
</style>
'''

# Scatter chart JS — plain string, data injected via global vars
SCATTER_CHART_JS = '''
try {
  var D = CHART_DATA;
  var mono = '"JetBrains Mono", monospace';
  var trace = {
    x: D.xs, y: D.ys,
    mode: "markers", type: "scatter",
    marker: { size: 10, color: D.colors, line: { width: 1, color: "#333" } },
    text: D.texts, customdata: D.idxs,
    hovertemplate: "%{text}<br>%{x:.1f}h at dest<extra></extra>"
  };
  var pareto = {
    x: D.pareto_x, y: D.pareto_y,
    mode: "lines", type: "scatter",
    line: { dash: "dash", color: "#333", width: 1.5 },
    name: "Pareto frontier", hoverinfo: "skip"
  };
  var layout = {
    xaxis: { title: { text: "Time at " + DEST + " (hours)", font: { family: mono, size: 13 } },
             tickfont: { family: mono, size: 11 } },
    yaxis: { title: { text: "Total Price ($)", font: { family: mono, size: 13 } },
             tickfont: { family: mono, size: 11 }, autorange: "reversed" },
    hoverlabel: { font: { family: mono, size: 12 } },
    hovermode: "closest", showlegend: false,
    margin: { t: 10, l: 60, r: 20, b: 50 },
    plot_bgcolor: "transparent", paper_bgcolor: "transparent"
  };
  var el = document.getElementById("chart");
  Plotly.newPlot(el, [trace, pareto], layout, { responsive: true, displayModeBar: false });
  el.on("plotly_click", function(data) {
    var idx = data.points[0].customdata;
    if (idx !== undefined) htmx.ajax("GET", "/flight/" + SEARCH_ID + "/" + idx, "#detail");
  });
} catch(e) {
  console.error("Chart error:", e);
  document.getElementById("chart").innerHTML = "<p>Chart error: " + e.message + "</p>";
}
'''

DEBUG_JS = '''
var debugToggle = document.querySelector(".debug-toggle");
if (debugToggle) {
  function onDebugToggle() {
    document.body.classList.toggle("debug", debugToggle.checked);
  }
  debugToggle.addEventListener("change", onDebugToggle);
  onDebugToggle();
}
'''

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)


@app.route('/airport/<code>')
def airport_lookup(code):
    ap = get_airport(code.upper())
    if ap:
        return json.dumps({'code': code.upper(), 'name': ap.get('city', ap['name']),
                           'full': ap['name']})
    return json.dumps({'code': code.upper(), 'name': None}), 404


@app.route('/search', methods=['POST'])
def search():
    origin = request.form.get('origin', 'SFO').upper().strip()
    dest = request.form.get('dest', 'BOS').upper().strip()

    # Outbound: depart origin after X, arrive dest before Y
    out_after_date = request.form.get('out_after_date', '').strip()
    out_after_time = request.form.get('out_after_time', '').strip()
    out_before_date = request.form.get('out_before_date', '').strip()
    out_before_time = request.form.get('out_before_time', '').strip()

    # Return: depart dest after X, arrive origin before Y
    ret_after_date = request.form.get('ret_after_date', '').strip()
    ret_after_time = request.form.get('ret_after_time', '').strip()
    ret_before_date = request.form.get('ret_before_date', '').strip()
    ret_before_time = request.form.get('ret_before_time', '').strip()

    origin_tz = get_tz(origin)
    dest_tz = get_tz(dest)

    # Build constraint datetimes
    # Outbound: after = departure at origin_tz, before = arrival at dest_tz
    out_after_dt = make_dt(out_after_date, out_after_time, origin_tz)
    out_before_dt = make_dt(out_before_date, out_before_time, dest_tz)

    # Symmetric defaults: forward +1 day end-of-day, back -1 day start-of-day
    if out_after_dt and not out_before_dt:
        d = out_after_dt.date() + timedelta(days=1)
        out_before_dt = datetime(d.year, d.month, d.day, 23, 59, tzinfo=dest_tz)
    elif out_before_dt and not out_after_dt:
        d = out_before_dt.date() - timedelta(days=1)
        out_after_dt = datetime(d.year, d.month, d.day, 0, 0, tzinfo=origin_tz)
    elif out_before_dt and not out_before_time:
        out_before_dt = out_before_dt.replace(hour=23, minute=59)

    # Return: after = departure at dest_tz, before = arrival at origin_tz
    ret_after_dt = make_dt(ret_after_date, ret_after_time, dest_tz)
    ret_before_dt = make_dt(ret_before_date, ret_before_time, origin_tz)

    if ret_after_dt and not ret_before_dt:
        d = ret_after_dt.date() + timedelta(days=1)
        ret_before_dt = datetime(d.year, d.month, d.day, 23, 59, tzinfo=origin_tz)
    elif ret_before_dt and not ret_after_dt:
        d = ret_before_dt.date() - timedelta(days=1)
        ret_after_dt = datetime(d.year, d.month, d.day, 0, 0, tzinfo=dest_tz)
    elif ret_before_dt and not ret_before_time:
        ret_before_dt = ret_before_dt.replace(hour=23, minute=59)

    # For display / Google Flights link
    dep_date = out_after_date or out_before_date or ''
    ret_date = ret_before_date or ret_after_date or ''

    def generate():
        yield ('<!DOCTYPE html><html><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
               f'<title>Searching {esc(origin)} - {esc(dest)}...</title>'
               + HEAD + APP_STYLE +
               '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'
               '<script src="https://unpkg.com/htmx.org@2.0.4"></script>'
               '</head><body>'
               '<label class="debug-toggle-label">'
               '<input type="checkbox" class="debug-toggle" /> Debug mode</label>'
               f'<a class="back" href="/">&#8592; New search</a>'
               f'<h1>{esc(origin)} &#8596; {esc(dest)}</h1>'
               '<div id="loading">'
               '<h2><span class="spinner">&#9992;</span> Searching flights...</h2>'
               '<div id="log"></div>'
               '</div>\n')

        try:
            pax = Passengers(adults=1)

            # --- Outbound ---
            out_dates = compute_search_dates(out_after_dt, out_before_dt)
            if not out_dates:
                raise ValueError("No outbound dates — set at least one outbound date.")
            outbound = []
            for di, odate in enumerate(out_dates):
                if di > 0:
                    yield progress("Waiting 1s (rate limit)...")
                    time.sleep(1)
                yield progress(f"Searching outbound {esc(origin)} &#8594; {esc(dest)} on {esc(odate)}...")
                fd_out = FlightData(date=odate, from_airport=origin, to_airport=dest)
                r_out = search_retry(fd_out, pax)
                yield progress(f"Found {len(r_out.flights)} flights on {esc(odate)}")
                for f in r_out.flights:
                    dts, dds, dh, dm = extract_td(f.departure)
                    if dh is None: continue
                    dep_dt = to_dt(dds, dh, dm, tz=origin_tz) if dds else None
                    if out_after_dt and dep_dt and dep_dt < out_after_dt:
                        continue
                    ats, ads, ah, am = extract_td(f.arrival)
                    if ah is None: continue
                    arr_dt = to_dt(ads, ah, am, tz=dest_tz) if ads else None
                    if out_before_dt and arr_dt and arr_dt > out_before_dt:
                        continue
                    outbound.append({
                        'airline': f.name, 'dep_time': dts, 'dep_date': dds,
                        'dep_h': dh, 'dep_m': dm, 'dep_dt': dep_dt,
                        'arr_str': f.arrival, 'arr_date': ads, 'arr_h': ah, 'arr_m': am,
                        'arr_dt': arr_dt,
                        'duration': f.duration, 'stops': f.stops,
                        'price': f.price, 'price_int': price_int(f.price),
                    })
            outbound = dedup(outbound)
            n_ns_out = sum(1 for o in outbound if o['stops'] == 0)
            yield progress(f"&#10003; {len(outbound)} outbound match filters ({n_ns_out} nonstop)")

            yield progress("Waiting 1s (rate limit)...")
            time.sleep(1)

            # --- Return ---
            ret_dates = compute_search_dates(ret_after_dt, ret_before_dt)
            if not ret_dates:
                raise ValueError("No return dates — set at least one return date.")
            returns = []
            for di, rdate in enumerate(ret_dates):
                if di > 0:
                    yield progress("Waiting 1s (rate limit)...")
                    time.sleep(1)
                yield progress(f"Searching return {esc(dest)} &#8594; {esc(origin)} on {esc(rdate)}...")
                fd_ret = FlightData(date=rdate, from_airport=dest, to_airport=origin)
                r_ret = search_retry(fd_ret, pax)
                yield progress(f"Found {len(r_ret.flights)} flights on {esc(rdate)}")
                for f in r_ret.flights:
                    dts, dds, dh, dm = extract_td(f.departure)
                    if dh is None: continue
                    dep_dt = to_dt(dds, dh, dm, tz=dest_tz) if dds else None
                    if ret_after_dt and dep_dt and dep_dt < ret_after_dt:
                        continue
                    ats, ads, ah, am = extract_td(f.arrival)
                    if ah is None: continue
                    arr_dt = to_dt(ads, ah, am, tz=origin_tz) if ads else None
                    if ret_before_dt and arr_dt and arr_dt > ret_before_dt:
                        continue
                    returns.append({
                        'airline': f.name, 'dep_time': dts, 'dep_date': dds,
                        'dep_h': dh, 'dep_m': dm, 'dep_dt': dep_dt,
                        'arr_time': ats, 'arr_date': ads, 'arr_h': ah, 'arr_m': am,
                        'arr_dt': arr_dt,
                        'duration': f.duration, 'stops': f.stops,
                        'price': f.price, 'price_int': price_int(f.price),
                    })
            returns = dedup(returns)
            n_ns_ret = sum(1 for r in returns if r['stops'] == 0)
            yield progress(f"&#10003; {len(returns)} return match filters ({n_ns_ret} nonstop)")

            # --- Combine ---
            yield progress("Combining pairs...")
            pairs = []
            for o in outbound:
                if o['arr_dt'] is None: continue
                for r in returns:
                    if r['dep_dt'] is None: continue
                    dest_hrs = (r['dep_dt'] - o['arr_dt']).total_seconds() / 3600
                    if dest_hrs <= 0: continue
                    pairs.append({
                        'total': o['price_int'] + r['price_int'],
                        'dest_hrs': round(dest_hrs, 1),
                        'out': o, 'ret': r,
                    })
            pairs.sort(key=lambda x: x['total'])

            nonstop = [p for p in pairs if p['out']['stops'] == 0 and p['ret']['stops'] == 0]
            with_stops = [p for p in pairs if not (p['out']['stops'] == 0 and p['ret']['stops'] == 0)]
            kept = with_stops[:50]
            ns_ids = {id(p) for p in kept}
            for p in nonstop:
                if id(p) not in ns_ids:
                    kept.append(p)
            kept.sort(key=lambda x: x['total'])

            yield progress(f"&#10003; {len(kept)} combos ({len(nonstop)} both-nonstop)")

            if not kept:
                yield '<script>document.getElementById("loading").style.display="none";</script>\n'
                yield (f'<h2>No results</h2>'
                       f'<p>{len(outbound)} outbound and {len(returns)} return flights matched filters, '
                       f'but produced 0 valid round-trip combos.</p>'
                       f'<p><a href="/">&#8592; Try again</a> with different dates or looser time filters.</p>')
                yield '<div class="debug-grid"></div>'
                yield '<script src="/static/monospace.js"></script>'
                yield '<script>' + DEBUG_JS + '</script>'
                yield '</body></html>'
                return

            # Store
            sid = uuid.uuid4().hex[:8]
            gf_link = (f"https://www.google.com/travel/flights"
                       f"?q=flights+from+{origin}+to+{dest}+on+{dep_date}+returning+{ret_date}")
            SEARCHES[sid] = {'pairs': kept, 'origin': origin, 'dest': dest,
                             'dep_date': dep_date, 'ret_date': ret_date, 'gf_link': gf_link}

            # --- Chart data ---
            xs, ys, colors, texts, idxs = [], [], [], [], []
            for i, p in enumerate(kept):
                xs.append(p['dest_hrs'])
                ys.append(p['total'])
                s = p['out']['stops'] + p['ret']['stops']
                if s == 0:   colors.append('#2ecc71')
                elif s <= 1: colors.append('#3498db')
                elif s <= 2: colors.append('#f39c12')
                else:        colors.append('#e74c3c')
                texts.append(f"${p['total']} {p['out']['airline']}/{p['ret']['airline']}")
                idxs.append(i)

            # --- Pareto frontier ---
            pareto_indices = []
            indexed = sorted(range(len(kept)), key=lambda i: (kept[i]['total'], -kept[i]['dest_hrs']))
            pareto_x, pareto_y = [], []
            max_hrs = -1
            for i in indexed:
                if kept[i]['dest_hrs'] > max_hrs:
                    pareto_indices.append(i)
                    pareto_x.append(kept[i]['dest_hrs'])
                    pareto_y.append(kept[i]['total'])
                    max_hrs = kept[i]['dest_hrs']
            pf = sorted(zip(pareto_x, pareto_y, pareto_indices))
            pareto_x = [a for a, _, _ in pf]
            pareto_y = [a for _, a, _ in pf]
            pareto_indices = [a for _, _, a in pf]

            chart_data = json.dumps({
                'xs': xs, 'ys': ys, 'colors': colors, 'texts': texts, 'idxs': idxs,
                'pareto_x': pareto_x, 'pareto_y': pareto_y,
            })

            # --- HTML timeline ---
            timeline_html = build_html_timeline(pareto_indices, kept, sid)

            # --- Render results ---
            yield progress("Drawing charts...")
            yield '<script>document.getElementById("loading").style.display="none";</script>\n'

            out_desc = esc(out_after_dt.strftime('%b %-d')) + '&ndash;' + esc(out_before_dt.strftime('%b %-d'))
            ret_desc = esc(ret_after_dt.strftime('%b %-d')) + '&ndash;' + esc(ret_before_dt.strftime('%b %-d'))
            yield (f'<h2>{esc(origin)} &#8596; {esc(dest)}: {len(kept)} combos</h2>'
                   f'<p>Out {out_desc} &#8594; Ret {ret_desc} &middot; '
                   f'{len(outbound)} outbound &middot; {len(returns)} return &middot; '
                   f'<a href="{esc(gf_link)}" target="_blank">Google Flights</a></p>')

            # Scatter chart
            yield (f'<h2>Price vs. Time at {esc(dest)}</h2>'
                   '<p>Click a dot or table row to see details.</p>'
                   '<div id="chart"></div>')

            # Legend + HTML timeline
            yield ('<h2>Pareto Timeline</h2>'
                   '<div class="tl-legend">'
                   '<span class="tl-swatch tl-sw-s0"></span> nonstop '
                   '<span class="tl-swatch tl-sw-s1"></span> 1 stop '
                   '<span class="tl-swatch tl-sw-s2"></span> 2 stop '
                   '<span class="tl-swatch tl-sw-dest"></span> at dest'
                   '</div>')
            if timeline_html:
                yield timeline_html

            # Detail panel
            yield '<h2>Flight Details</h2>'
            yield '<div id="detail"><em>Click a flight on the chart or table.</em></div>'

            # Table
            nb = lambda s: esc(s).replace(' ', '&nbsp;') if s else ''
            rows = []
            for i, p in enumerate(kept):
                o, r = p['out'], p['ret']
                total_stops = o['stops'] + r['stops']
                rows.append(
                    f'<tr class="clickable" hx-get="/flight/{sid}/{i}" '
                    f'hx-target="#detail" hx-swap="innerHTML">'
                    f'<td>{i+1}</td>'
                    f'<td>${p["total"]}</td>'
                    f'<td>{p["dest_hrs"]}</td>'
                    f'<td>{total_stops}</td>'
                    f'<td>{esc(o["airline"])}</td>'
                    f'<td>{nb(o["dep_time"])}</td>'
                    f'<td>{nb(o.get("arr_str","")[:30])}</td>'
                    f'<td>{esc(r["airline"])}</td>'
                    f'<td>{nb(r["dep_time"])}</td>'
                    f'<td>{nb(r.get("arr_time",""))}</td>'
                    f'</tr>')

            yield ('<h2>All Results</h2>'
                   '<table><thead><tr>'
                   '<th class="width-min">#</th>'
                   '<th class="width-min">Price</th>'
                   '<th class="width-min">Hrs</th>'
                   '<th class="width-min">Stops</th>'
                   '<th>Out</th>'
                   '<th class="width-min">Depart</th>'
                   '<th class="width-auto">Arrive</th>'
                   '<th>Ret</th>'
                   '<th class="width-min">Depart</th>'
                   '<th class="width-min">Arrive</th>'
                   '</tr></thead><tbody>'
                   + ''.join(rows) +
                   '</tbody></table>')

            # Chart script
            yield ('<script>\n'
                   'var CHART_DATA = ' + chart_data + ';\n'
                   'var SEARCH_ID = "' + sid + '";\n'
                   'var DEST = "' + dest + '";\n'
                   + SCATTER_CHART_JS +
                   '\n</script>\n')

        except Exception as e:
            yield progress(f"<strong style='color:red'>Error: {esc(str(e))}</strong>")
            yield f'<pre style="color:red">{esc(traceback.format_exc())}</pre>'

        # Debug grid + JS (at end of body)
        yield '<div class="debug-grid"></div>'
        yield '<script src="/static/monospace.js"></script>'
        yield '<script>' + DEBUG_JS + '</script>'
        yield '</body></html>'

    return Response(stream_with_context(generate()), mimetype='text/html')


@app.route('/flight/<sid>/<int:idx>')
def flight_detail(sid, idx):
    data = SEARCHES.get(sid)
    if not data or idx >= len(data['pairs']):
        return '<p>Flight not found.</p>'
    p = data['pairs'][idx]
    o, r = p['out'], p['ret']
    return render_template_string(DETAIL_HTML,
        p=p, o=o, r=r,
        origin=data['origin'], dest=data['dest'],
        gf_link=data['gf_link'])


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

INDEX_HTML = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Flight Search</title>
  ''' + HEAD + APP_STYLE + '''
</head>
<body>
  <label class="debug-toggle-label">
    <input type="checkbox" class="debug-toggle" /> Debug mode</label>

  <h1>Flight Search</h1>

  <form action="/search" method="POST">
    <div class="row">
      <div class="field">
        <label>Origin <input name="origin" type="text" required placeholder="SFO"></label>
        <span class="airport-hint" id="hint-origin"></span>
      </div>
      <div class="field">
        <label>Destination <input name="dest" type="text" required placeholder="LHR"></label>
        <span class="airport-hint" id="hint-dest"></span>
      </div>
    </div>

    <h2>Outbound</h2>
    <div class="row">
      <div class="field">
        <label>Depart after <input name="out_after_date" type="date"></label>
      </div>
      <div class="field">
        <label>&nbsp;<input name="out_after_time" type="time"></label>
      </div>
      <div class="field">
        <label>Arrive before <input name="out_before_date" type="date"></label>
      </div>
      <div class="field">
        <label>&nbsp;<input name="out_before_time" type="time"></label>
      </div>
    </div>

    <h2>Return</h2>
    <div class="row">
      <div class="field">
        <label>Depart after <input name="ret_after_date" type="date"></label>
      </div>
      <div class="field">
        <label>&nbsp;<input name="ret_after_time" type="time"></label>
      </div>
      <div class="field">
        <label>Arrive before <input name="ret_before_date" type="date"></label>
      </div>
      <div class="field">
        <label>&nbsp;<input name="ret_before_time" type="time"></label>
      </div>
    </div>

    <nav>
      <button type="button" class="btn-clear" onclick="if(confirm('Clear all fields?')){localStorage.removeItem('flightForm');this.form.reset();}">Clear</button>
      <button type="submit" class="btn-search">Search Flights</button>
    </nav>
  </form>

  <div class="debug-grid"></div>
  <script src="/static/monospace.js"></script>
  <script>
    (function() {
      var form = document.querySelector("form");
      var saved = localStorage.getItem("flightForm");
      if (saved) {
        try {
          var data = JSON.parse(saved);
          for (var key in data) {
            var el = form.elements[key];
            if (el) el.value = data[key];
          }
        } catch(e) {}
      }
      function saveForm() {
        var data = {};
        for (var i = 0; i < form.elements.length; i++) {
          var el = form.elements[i];
          if (el.name) data[el.name] = el.value;
        }
        localStorage.setItem("flightForm", JSON.stringify(data));
      }
      form.addEventListener("input", saveForm);
      form.addEventListener("change", saveForm);

      // Airport code validation
      function checkAirport(input, hintId) {
        var code = input.value.trim().toUpperCase();
        var hint = document.getElementById(hintId);
        if (!code) { hint.textContent = ''; hint.className = 'airport-hint'; return; }
        fetch('/airport/' + encodeURIComponent(code))
          .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
          .then(function(res) {
            if (res.ok) {
              hint.textContent = res.data.name;
              hint.className = 'airport-hint';
              input.value = res.data.code;
            } else {
              hint.textContent = 'Unknown airport code';
              hint.className = 'airport-hint invalid';
            }
          });
      }
      var originEl = form.elements['origin'];
      var destEl = form.elements['dest'];
      originEl.addEventListener('blur', function() { checkAirport(originEl, 'hint-origin'); });
      destEl.addEventListener('blur', function() { checkAirport(destEl, 'hint-dest'); });
      // Check on load if values restored
      if (originEl.value) checkAirport(originEl, 'hint-origin');
      if (destEl.value) checkAirport(destEl, 'hint-dest');
    })();
  </script>
</body>
</html>
'''

DETAIL_HTML = '''
<h3>${{ p.total }} &mdash; {{ p.dest_hrs }}h at {{ dest }}</h3>

<table>
  <tr>
    <th>Leg</th><th>Airline</th><th>Stops</th><th>Depart</th><th>Arrive</th><th>Duration</th><th class="width-min">Price</th>
  </tr>
  <tr>
    <td><strong>Out</strong> {{ origin }}&#8594;{{ dest }}</td>
    <td>{{ o.airline }}</td>
    <td>{% if o.stops == 0 %}Nonstop{% else %}{{ o.stops }} stop{% endif %}</td>
    <td>{{ o.dep_time }} {{ o.dep_date }}</td>
    <td>{{ o.arr_str }}</td>
    <td>{{ o.duration }}</td>
    <td>{{ o.price }}</td>
  </tr>
  <tr>
    <td><strong>Ret</strong> {{ dest }}&#8594;{{ origin }}</td>
    <td>{{ r.airline }}</td>
    <td>{% if r.stops == 0 %}Nonstop{% else %}{{ r.stops }} stop{% endif %}</td>
    <td>{{ r.dep_time }} {{ r.dep_date }}</td>
    <td>{{ r.arr_time }} {{ r.arr_date }}</td>
    <td>{{ r.duration }}</td>
    <td>{{ r.price }}</td>
  </tr>
</table>

<p>
  <a href="{{ gf_link }}" target="_blank">Book on Google Flights &#8594;</a>
</p>
'''

if __name__ == '__main__':
    app.run(debug=True, port=5001)
