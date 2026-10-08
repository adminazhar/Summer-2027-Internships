"""Rebuild the job tables in README.md and the other lists from TalentdPro's public jobs API.

Run daily by .github/workflows/update-lists.yml. Standard library only.

    python .github/scripts/update_lists.py            # write the files
    python .github/scripts/update_lists.py --check    # print counts, write nothing
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://brain.talentd.in/api/v1/jobs/postings"
SITE = "https://www.talentdpro.com"
ROOT = Path(__file__).resolve().parents[2]
PER_PAGE = 200
USER_AGENT = "us-internships-list/1.0 (+https://github.com/adminazhar)"

# Rows are grouped like the big SWE lists: FAANG+, then quant and trading firms, then everyone else.
# Matched on the start of the company name ("Amazon Web Services" -> Amazon).
FAANG_PLUS = [
    "Google", "Alphabet", "Apple", "Meta", "Amazon", "AWS", "Microsoft", "Netflix", "NVIDIA", "Tesla",
    "SpaceX", "Uber", "Airbnb", "Stripe", "Salesforce", "Adobe", "Oracle", "LinkedIn", "Snap", "Pinterest",
    "Databricks", "OpenAI", "Anthropic", "Scale AI", "Palantir", "Spotify", "Intel", "AMD", "Qualcomm", "IBM",
    "Cisco", "Bloomberg", "Coinbase", "Robinhood", "DoorDash", "Lyft", "Instacart", "Shopify", "Atlassian",
    "Dropbox", "Reddit", "Roblox", "ServiceNow", "Snowflake", "Workday", "Intuit", "PayPal", "TikTok",
    "ByteDance", "Datadog", "Cloudflare", "MongoDB", "Figma", "Waymo", "Twilio", "HubSpot", "Broadcom",
    "Samsung", "Sony", "Rivian", "Lucid", "Zillow", "Expedia", "eBay",
]
QUANT = [
    "Jane Street", "Citadel", "Two Sigma", "Hudson River Trading", "HRT", "Jump Trading", "D. E. Shaw",
    "D.E. Shaw", "DE Shaw", "Susquehanna", "SIG", "Optiver", "IMC", "Akuna", "Five Rings", "Tower Research",
    "Virtu", "DRW", "Old Mission", "Point72", "Millennium", "Bridgewater", "Renaissance", "Radix", "Belvedere",
    "Wolverine", "Peak6", "XTX", "Squarepoint", "AQR", "Balyasny", "Arrowstreet", "Voleon", "Headlands",
    "Flow Traders", "Chicago Trading Company", "Geneva Trading", "Vatic", "Hap Capital", "Maven Securities",
]


def _names_re(names):
    return re.compile(r"^(?:" + "|".join(re.escape(n) for n in names) + r")\b", re.IGNORECASE)


FAANG_RE, QUANT_RE = _names_re(FAANG_PLUS), _names_re(QUANT)
# Source data sometimes has "Spacex" or "Hrt"; show the usual spelling when the whole name matches.
KNOWN_SPELLING = {n.lower(): n for n in FAANG_PLUS + QUANT}
GROUPS = [  # (heading, anchor) — GitHub anchors drop "+" and turn "&" into "--"
    ("FAANG+", "faang"),
    ("Quant & Trading", "quant--trading"),
    ("Other", "other"),
]


def group_of(job):
    name = company_name(job)
    if QUANT_RE.match(name):
        return "Quant & Trading"
    if FAANG_RE.match(name):
        return "FAANG+"
    return "Other"


STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}

# One entry per output file. `queries` are API filter sets whose results are merged.
LISTS = [
    {
        "file": "README.md",
        "title": "Software & Data Internships",
        "queries": [{"job_type": "Internship", "role_category": "IT/Software"},
                    {"job_type": "Internship", "role_category": "Research & Science"}],
    },
    {
        "file": "NEW_GRAD.md",
        "title": "New Grad & Entry-Level Jobs",
        "intern": False,
        "queries": [{"job_type": "Fresher"}],
    },
    {
        "file": "ENGINEERING_INTERNSHIPS.md",
        "title": "Hardware & Engineering Internships",
        "queries": [{"job_type": "Internship", "role_category": "Core Engineering"},
                    {"job_type": "Internship", "role_category": "Manufacturing & Operations"}],
    },
    {
        "file": "BUSINESS_INTERNSHIPS.md",
        "title": "Business, Finance & Other Internships",
        "queries": [{"job_type": "Internship", "role_category": c} for c in (
            "Banking & Finance", "Sales & Marketing", "Design", "HR & Admin", "Healthcare & Pharma", "Other")],
    },
]

START = "<!-- JOBS_TABLE_START -->"
END = "<!-- JOBS_TABLE_END -->"


def fetch_all(filters):
    jobs, page = [], 1
    while True:
        params = {"market": "us", "per_page": PER_PAGE, "page": page, **filters}
        url = f"{API}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.load(resp)
                break
            except Exception:  # noqa: BLE001 — retry transient errors, then give up loudly
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        jobs.extend(data.get("jobs") or [])
        if page >= int(data.get("meta", {}).get("total_pages") or 1):
            return jobs
        page += 1


def company_name(job):
    c = job.get("company")
    return (c.get("name") or c.get("company_name") or "").strip() if isinstance(c, dict) else str(c or "").strip()


def position(job):
    return clean(job.get("role") or job.get("title") or "")


def clean(text):
    """Table-safe: no pipes, no line breaks, no HTML."""
    text = re.sub(r"<[^>]+>", "", str(text))
    return re.sub(r"\s+", " ", text.replace("|", "/")).strip()


def location(job):
    places = []
    for loc in job.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        city = (loc.get("city") or "").strip()
        state = loc.get("state") or {}
        label = state.get("label") if isinstance(state, dict) else str(state or "")
        abbr = STATE_ABBR.get((label or "").strip().lower(), (label or "").strip())
        place = ", ".join(p for p in (city, abbr) if p)
        if place and place not in places:
            places.append(place)
    if (job.get("workplace_type") or "").lower() == "remote":
        places.insert(0, "Remote")
    if not places:
        return "USA"
    return places[0] + (f" +{len(places) - 1}" if len(places) > 1 else "")


HOURS_PER_YEAR = 2080
INTERN_HOURLY = (12, 150)        # plausible intern pay, $/hr
YEARLY = (25_000, 450_000)       # plausible full-time pay, $/yr
WIDE_RANGE = 2.5                 # max/min beyond this is shown as "from $min"


def pay(job, intern):
    """Interns in $/hr (employers often post yearly equivalents), full-time in $k/yr.
    Values outside a plausible band are left blank rather than shown wrong."""
    rng = job.get("salary_range") or {}
    try:
        low = float(rng.get("min") or 0)
        high = float(rng.get("max") or 0)
    except (TypeError, ValueError):
        return ""
    low, high = (low or high), (high or low)
    if not low:
        return ""
    if intern:
        if low >= 500:  # yearly figure
            low, high = low / HOURS_PER_YEAR, high / HOURS_PER_YEAR
        lo_ok, hi_ok = INTERN_HOURLY
        unit, fmt = "/hr", (lambda v: f"${v:.0f}")
    else:
        if low < 500:  # hourly figure
            low, high = low * HOURS_PER_YEAR, high * HOURS_PER_YEAR
        lo_ok, hi_ok = YEARLY
        unit, fmt = "", (lambda v: f"${v / 1000:.0f}k")
    if not (lo_ok <= low <= hi_ok):
        return ""
    if high > hi_ok or high < low:
        high = low
    if high > WIDE_RANGE * low:
        return f"from {fmt(low)}{unit}"
    if fmt(high) == fmt(low):
        return f"{fmt(low)}{unit}"
    return f"{fmt(low)}–{fmt(high)[1:]}{unit}"


def posted_at(job):
    date = (job.get("date") or {}).get("published")
    try:
        return datetime.fromisoformat(date)
    except (TypeError, ValueError):
        return None


def age(job, now):
    published = posted_at(job)
    if not published:
        return ""
    return f"{max(0, (now - published).days)}d"


def apply_link(job):
    link = (job.get("apply_link") or "").strip()
    return link if link.startswith(("http://", "https://")) else ""


TRACKING = "utm_source=github&utm_medium=list&utm_campaign=us-internships"


def details_link(job):
    slug = (job.get("slug") or "").strip()
    return f"{SITE}/us/jobs/{urllib.parse.quote(slug)}?{TRACKING}" if slug else ""


def buttons(job):
    """Apply goes straight to the employer; Details opens the role on TalentdPro."""
    parts = []
    link = apply_link(job)
    if link:
        parts.append(f'<a href="{link}"><img src="assets/apply.svg" alt="Apply" width="64"></a>')
    details = details_link(job)
    if details:
        parts.append(f'<a href="{details}"><img src="assets/details.svg" alt="Details" width="64"></a>')
    return " ".join(parts)


def row(job, now, intern):
    name = company_name(job)
    name = KNOWN_SPELLING.get(name.lower(), name)
    return (f"| **{clean(name)}** | {position(job)} | {location(job)} | {pay(job, intern)} | "
            f"{age(job, now)} | {buttons(job)} |")


HEADER = "| Company | Role | Location | Pay | Posted | Apply |\n|---|---|---|---|---|---|"


def grouped(jobs):
    out = {heading: [] for heading, _ in GROUPS}
    for job in jobs:
        out[group_of(job)].append(job)
    return out


def table(jobs, now, intern):
    parts = []
    for heading, anchor in GROUPS:
        rows = grouped(jobs)[heading]
        if not rows:
            continue
        parts += [f"### {heading}", "", HEADER, *(row(j, now, intern) for j in rows), "",
                  "[Back to top](#)", ""]
    return "\n".join(parts)


def collect(spec):
    seen, jobs = set(), []
    for filters in spec["queries"]:
        for job in fetch_all(filters):
            if job.get("id") in seen or not apply_link(job):
                continue
            seen.add(job.get("id"))
            jobs.append(job)
    jobs.sort(key=lambda j: posted_at(j) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return jobs


def replace_table(path, body):
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(f"{path.name}: table markers missing")
    return pattern.sub(lambda _: f"{START}\n{body}\n{END}", text)


SUMMARY_START, SUMMARY_END = "<!-- SUMMARY_START -->", "<!-- SUMMARY_END -->"
REPO = "adminazhar/Summer-2027-Internships"


def badge(label, message, color):
    q = urllib.parse.quote
    return f"![{label}](https://img.shields.io/badge/{q(label)}-{q(message.replace('-', '--'))}-{color})"


def summary(counts, groups, now):
    total = sum(counts.values())
    lines = [
        " ".join([
            badge("open roles", f"{total:,}", "2ea44f"),
            badge("refreshed", "every 4 hours", "0969da"),
            badge("updated", f"{now:%b %-d, %Y}", "6e7781"),
            f"![GitHub stars](https://img.shields.io/github/stars/{REPO}?style=social)",
        ]),
        "",
    ]
    for spec in LISTS:
        file = spec["file"]
        link = "README.md" if file == "README.md" else file
        g = groups[file]
        jumps = " · ".join(f"[{h}]({link}#{a}) ({len(g[h])})" for h, a in GROUPS if g[h])
        lines.append(f"- **[{spec['title']}]({link})**: **{counts[file]:,}** open ({jumps})")
    lines += ["", "⭐ **Star the repo** to keep it one click away. New roles land every 4 hours."]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="print counts, write nothing")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    counts, bodies, groups = {}, {}, {}
    for spec in LISTS:
        jobs = collect(spec)
        counts[spec["file"]] = len(jobs)
        groups[spec["file"]] = grouped(jobs)
        bodies[spec["file"]] = table(jobs, now, spec.get("intern", True))
        print(f"{spec['file']}: {len(jobs)} jobs")
    if args.check:
        return 0
    if min(counts.values()) == 0:
        # An empty list means the API failed quietly; keep yesterday's tables rather than wipe them.
        raise SystemExit("a list came back empty; not updating")
    for spec in LISTS:
        path = ROOT / spec["file"]
        text = replace_table(path, bodies[spec["file"]])
        block = summary(counts, groups, now)
        text = re.sub(re.escape(SUMMARY_START) + r".*?" + re.escape(SUMMARY_END),
                      lambda _: f"{SUMMARY_START}\n{block}\n{SUMMARY_END}", text, flags=re.DOTALL)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
