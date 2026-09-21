#!/usr/bin/env python3
"""
Job Watch: checks company career sites for new postings, records them
in a feed website (docs/index.html), and optionally sends alerts to
Discord, Slack, or email.

Run:  python monitor.py
"""

import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = ROOT / "companies.yaml"
SEEN_FILE = ROOT / "data" / "seen.json"
FEED_FILE = ROOT / "data" / "jobs.json"
PAGE_TEMPLATE = ROOT / "template.html"
PAGE_OUT = ROOT / "docs" / "index.html"

FEED_LIMIT = 1000          # how many jobs the website keeps
SNIPPET_LEN = 260          # length of the brief description
TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (JobWatch personal job alert script)"}


# ------------------------------------------------------------------ helpers

def clean_text(raw, limit=SNIPPET_LEN):
    """Turn HTML/escaped text into a short plain-text description."""
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = BeautifulSoup(text, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def job(company, job_id, title, url, location="", description="", posted=""):
    return {
        "key": f"{company}::{job_id}",
        "company": company,
        "title": (title or "Untitled role").strip(),
        "url": url,
        "location": (location or "").strip(),
        "description": clean_text(description),
        "posted": posted or "",
    }


def get_json(url, headers=None, **kw):
    r = requests.get(url, headers={**HEADERS, **(headers or {})}, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 JobWatch/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}
_robots_cache = {}


class BlockedByRobots(Exception):
    pass


def allowed_by_robots(url):
    """Respect a site's robots.txt for plain page reading."""
    from urllib.robotparser import RobotFileParser
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    if base not in _robots_cache:
        rp = RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", headers=BROWSER_HEADERS, timeout=15)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        _robots_cache[base] = rp
    return _robots_cache[base].can_fetch("JobWatch", url)


def get_page(url, check_robots=True, **kw):
    if check_robots and not allowed_by_robots(url):
        raise BlockedByRobots("this site doesn't allow automated checks")
    r = requests.get(url, headers={**BROWSER_HEADERS, "Accept": "text/html,*/*"},
                     timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def pick(d, *keys):
    """First non-empty value among keys (dotted paths allowed)."""
    for k in keys:
        v = d
        for part in k.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        if v not in (None, "", [], {}):
            return v
    return ""


# ------------------------------------------------------------ source readers

def fetch_greenhouse(c):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['id']}/jobs",
                    params={"content": "true"})
    return [job(c["name"], j["id"], j.get("title"), j.get("absolute_url"),
                (j.get("location") or {}).get("name", ""),
                j.get("content", ""), j.get("updated_at", ""))
            for j in data.get("jobs", [])]


def fetch_lever(c):
    data = get_json(f"https://api.lever.co/v0/postings/{c['id']}", params={"mode": "json"})
    out = []
    for j in data:
        cats = j.get("categories") or {}
        posted = ""
        if j.get("createdAt"):
            posted = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat()
        out.append(job(c["name"], j["id"], j.get("text"), j.get("hostedUrl"),
                       cats.get("location", ""), j.get("descriptionPlain", ""), posted))
    return out


def fetch_ashby(c):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['id']}")
    return [job(c["name"], j["id"], j.get("title"), j.get("jobUrl"),
                j.get("location", ""),
                j.get("descriptionPlain") or j.get("descriptionHtml", ""),
                j.get("publishedAt", ""))
            for j in data.get("jobs", []) if j.get("isListed", True)]


def fetch_smartrecruiters(c):
    out, offset = [], 0
    while True:
        data = get_json(f"https://api.smartrecruiters.com/v1/companies/{c['id']}/postings",
                        params={"limit": 100, "offset": offset})
        for j in data.get("content", []):
            loc = j.get("location") or {}
            loc_txt = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            if loc.get("remote"):
                loc_txt = (loc_txt + " (remote)").strip()
            desc = " · ".join(x for x in [(j.get("department") or {}).get("label"),
                                          (j.get("typeOfEmployment") or {}).get("label")] if x)
            out.append(job(c["name"], j["id"], j.get("name"),
                           f"https://jobs.smartrecruiters.com/{c['id']}/{j['id']}",
                           loc_txt, desc, j.get("releasedDate", "")))
        offset += 100
        if offset >= data.get("totalFound", 0) or offset > 2000:
            return out


def fetch_workday(c):
    # e.g. https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite
    u = urlparse(c["url"])
    tenant = u.netloc.split(".")[0]
    parts = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    site = parts[0]
    api = f"{u.scheme}://{u.netloc}/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    while offset < 400:  # newest few hundred is plenty for alerts
        r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                     "searchText": c.get("search", "")},
                          headers={**HEADERS, "Content-Type": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        posts = r.json().get("jobPostings", [])
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath", "")
            out.append(job(c["name"], path, j.get("title"),
                           f"{u.scheme}://{u.netloc}/{site}{path}",
                           j.get("locationsText", ""),
                           " · ".join(j.get("bulletFields") or []),
                           j.get("postedOn", "")))
        offset += 20
    return out


JOBISH = re.compile(r"(job|career|position|opening|posting|opportunit|requisition|vacanc)", re.I)
NAV_WORDS = {"careers", "jobs", "job search", "search jobs", "apply", "apply now", "home",
             "view all", "all jobs", "open positions", "current openings", "learn more",
             "see all jobs", "view jobs", "job alerts", "sign in", "login", "back to jobs"}


def fetch_page(c):
    """Generic careers page reader.

    1. Collects links that look like individual job postings.
    2. If none are found, falls back to change detection: you get one alert
       saying the page changed, with a link to check it.
    """
    r = get_page(c["url"], check_robots=not c.get("ignore_robots"))
    soup = BeautifulSoup(r.text, "html.parser")
    needle = c.get("link_contains", "")
    site = urlparse(c["url"]).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(r.url, a["href"]).split("#")[0]
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 4 or len(title) > 140 or title.lower() in NAV_WORDS:
            continue
        if href in seen or href.rstrip("/") == c["url"].rstrip("/").split("?")[0]:
            continue
        if needle:
            if needle not in href:
                continue
        else:
            path = urlparse(href).path
            # a job link: job-ish URL with something after it (an id or slug)
            if not JOBISH.search(path) or len([p for p in path.split("/") if p]) < 2:
                continue
            if urlparse(href).netloc not in (site, "") and not JOBISH.search(urlparse(href).netloc):
                continue
        seen.add(href)
        out.append(job(c["name"], href, title, href))
    if out:
        return out
    # change detection fallback
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    text = re.sub(r"\b\d{1,2}:\d{2}\b|©.*$", "", text)  # ignore clocks/footers
    import hashlib
    digest = hashlib.sha1(text.encode()).hexdigest()[:12]
    return [job(c["name"], f"change-{digest}", "Careers page updated", c["url"],
                description="This page changed since the last check, which often means "
                            "a new opening was posted. Open it to see what's new.")]


def fetch_teamwork(c):
    """TeamWork Online listing pages (sports jobs). Job links end in a numeric id."""
    out, seen = [], set()
    for page in range(1, int(c.get("pages", 2)) + 1):
        url = c["url"] + (("&" if "?" in c["url"] else "?") + f"page={page}" if page > 1 else "")
        soup = BeautifulSoup(get_page(url, check_robots=not c.get("ignore_robots")).text, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            m = re.search(r"-(\d{6,})/?$", urlparse(href).path)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            found += 1
            # the card's text lines after the title are company, then location
            card = a
            for _ in range(5):
                if card.parent is None:
                    break
                card = card.parent
                lines = [t.strip() for t in card.stripped_strings]
                if len(lines) >= 4:
                    break
            lines = [t for t in card.stripped_strings]
            title = a.get_text(" ", strip=True)
            after = lines[lines.index(title) + 1:] if title in lines else []
            company = after[0] if after else ""
            location = after[1] if len(after) > 1 else ""
            level = lines[0] if lines and lines[0] in (
                "Intern", "Entry Level", "Manager", "Director", "Senior", "Part Time") else ""
            desc = " · ".join(x for x in [company, level] if x)
            out.append(job(c["name"], m.group(1), title, href, location, desc))
        if not found:
            break
        time.sleep(1)
    return out


def fetch_ukg(c):
    """UKG / UltiPro recruiting boards (recruiting.ultipro.com, *.rec.pro.ukg.net)."""
    u = urlparse(c["url"])
    parts = [p for p in u.path.split("/") if p]
    i = [p.lower() for p in parts].index("jobboard")
    base = f"{u.scheme}://{u.netloc}/{parts[i-1]}/JobBoard/{parts[i+1]}"
    qs = parse_qs(u.query)
    filters = []
    for field in (4, 5, 6, 37):
        vals = " ".join(qs.get(f"f{field}", [])).split()
        filters.append({"t": "TermsSearchFilterDto", "fieldName": field, "extra": None, "values": vals})
    out, skip = [], 0
    while skip < 1000:
        payload = {
            "opportunitySearch": {
                "Top": 50, "Skip": skip, "QueryString": "",
                "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
                "Filters": filters,
            },
            "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
                              "Skills": [], "hasNoLicenses": False, "SkippedSkills": []},
        }
        r = requests.post(base + "/JobBoardView/LoadSearchResults", json=payload,
                          headers={**BROWSER_HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        opps = data.get("opportunities") or []
        for o in opps:
            locs = []
            for l in o.get("Locations") or []:
                a = l.get("Address") or {}
                txt = ", ".join(x for x in [a.get("City"), (a.get("State") or {}).get("Code")] if x) \
                    or l.get("LocalizedDescription") or l.get("LocalizedName") or ""
                if txt and txt not in locs:
                    locs.append(txt)
            out.append(job(c["name"], o.get("Id"), o.get("Title"),
                           f"{base}/OpportunityDetail?opportunityId={o.get('Id')}",
                           " / ".join(locs), o.get("BriefDescription", ""), o.get("PostedDate", "")))
        skip += 50
        if len(opps) < 50 or skip >= (data.get("totalCount") or 0):
            break
    return out


def fetch_dayforce(c):
    """Dayforce candidate portals (jobs.dayforcehcm.com)."""
    u = urlparse(c["url"])
    parts = [p for p in u.path.split("/") if p]
    culture = "en-US"
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0], re.I):
        culture, parts = parts[0], parts[1:]
    ns, board = parts[0], (parts[1] if len(parts) > 1 else "CANDIDATEPORTAL")
    origin = f"{u.scheme}://{u.netloc}"
    board_url = f"{origin}/{culture}/{ns}/{board}"
    sess = requests.Session()
    sess.headers.update({**BROWSER_HEADERS,
                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                         "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
                         "Upgrade-Insecure-Requests": "1"})
    payload = {"clientNamespace": ns, "jobBoardCode": board, "cultureCode": culture,
               "paginationStart": 0, "distanceUnit": 0}
    page_status = None
    try:
        page = sess.get(board_url, timeout=TIMEOUT)
        page_status = page.status_code
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page.text, re.S)
        if m:
            nd = json.loads(m.group(1))
            for q in nd.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", []):
                if (q.get("queryKey") or [""])[0] == "site-info":
                    info = (q.get("state", {}).get("data") or {})
                    info = info.get("result", info)
                    ns = info.get("clientNamespace") or ns
                    payload.update(clientNamespace=ns,
                                   jobBoardCode=info.get("careerSiteXRefCode") or board,
                                   cultureCode=info.get("cultureCode") or culture)
                    if info.get("jobBoardId"):
                        payload["jobBoardId"] = info["jobBoardId"]
    except Exception:
        pass
    api_headers = {"Accept": "application/json, text/plain, */*", "Content-Type": "application/json",
                   "Origin": origin, "Referer": board_url,
                   "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
    r = None
    for attempt in range(2):
        r = sess.post(f"{origin}/api/geo/{ns}/jobposting/search", json=payload,
                      headers=api_headers, timeout=TIMEOUT)
        if r.status_code != 403:
            break
        time.sleep(3)
    if r.status_code >= 400:
        raise RuntimeError(f"Dayforce refused the request (board page {page_status}, "
                           f"job search {r.status_code}); it may be blocking GitHub's servers")
    data = r.json()
    posts = []
    for key in ("jobPostings", "jobPostingSummaries", "searchResult.jobPostings",
                "searchResult.jobPostingSummaries", "result.jobPostings", "data.jobPostings"):
        v = pick(data, key)
        if isinstance(v, list):
            posts = v
            break
    out = []
    for p in posts:
        pid = pick(p, "jobPostingId", "postingId", "id", "jobId", "reqId")
        link = pick(p, "jobPostingUrl", "jobUrl", "url")
        link = urljoin(board_url, link) if link else \
            f"{origin}/{payload['cultureCode']}/{ns}/{payload['jobBoardCode']}/jobs/{pid}"
        loc = pick(p, "location", "locationName", "postingLocation", "displayLocation", "cityState")
        if not loc and isinstance(p.get("postingLocations"), list):
            loc = " | ".join(", ".join(str(x) for x in [l.get("locationName") or l.get("city"),
                                                         l.get("state")] if x)
                             for l in p["postingLocations"] if isinstance(l, dict))
        out.append(job(c["name"], pid or link, pick(p, "jobTitle", "title", "postingTitle"), link,
                       str(loc or ""), pick(p, "jobDescription", "shortDescription", "description"),
                       str(pick(p, "postingStartTimestampUTC", "postingDate", "postedDate"))))
    return out


def fetch_adp(c):
    """ADP Workforce Now career centers."""
    qs = parse_qs(urlparse(c["url"]).query)
    cid, cc = qs["cid"][0], qs.get("ccId", ["19000101_000001"])[0]
    board = (f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
             f"?cid={cid}&ccId={cc}&lang=en_US")
    data = get_json("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
                    "staffing/v1/job-requisitions",
                    params={"cid": cid, "ccId": cc, "lang": "en_US", "locale": "en_US"})
    out = []
    for j in data.get("jobRequisitions") or []:
        locs = []
        for l in j.get("requisitionLocations") or []:
            a = l.get("address") or {}
            txt = ", ".join(x for x in [a.get("cityName"),
                                        (a.get("countrySubdivisionLevel1") or {}).get("codeValue")] if x) \
                or (l.get("nameCode") or {}).get("shortName", "")
            if txt and txt not in locs:
                locs.append(txt)
        item = j.get("itemID", "")
        out.append(job(c["name"], item, j.get("requisitionTitle"), f"{board}&jobId={item}",
                       " / ".join(locs), pick(j, "requisitionDescription", "jobDescription"),
                       str(pick(j, "postDate", "postingDate"))))
    return out


def fetch_bamboohr(c):
    host = urlparse(c["url"]).netloc
    data = get_json(f"https://{host}/careers/list")
    out = []
    for j in data.get("result") or []:
        loc = j.get("location") or {}
        loc_txt = ", ".join(x for x in [loc.get("city"), loc.get("state")] if x) \
            or ("Remote" if j.get("isRemote") else "")
        out.append(job(c["name"], j.get("id"), j.get("jobOpeningName"),
                       f"https://{host}/careers/{j.get('id')}", loc_txt,
                       " · ".join(x for x in [j.get("departmentLabel"),
                                              j.get("employmentStatusLabel")] if x)))
    return out


def fetch_hireology(c):
    slug = [p for p in urlparse(c["url"]).path.split("/") if p][0]
    widget = requests.get(f"https://careers.hireology.com/{slug}",
                          params={"widget": "t", "ref": "career_site", "ref_m": "application"},
                          headers=BROWSER_HEADERS, timeout=TIMEOUT)
    params = {"ref": "career_site", "ref_m": "application", "widget": "t",
              "sort": "jobs.created_at", "sort_dir": "desc"}
    m = re.search(r"var\s+startingData\s*=\s*(\{.*?\})\s*;", widget.text, re.S)
    path = slug
    if m:
        try:
            sd = json.loads(m.group(1))
            path = sd.get("careersPath") or slug
            if sd.get("xdm_c"):
                params.update(xdm_c=sd["xdm_c"], xdm_e=f"https://{slug}.hireology.careers", xdm_p="1")
        except Exception:
            pass
    data = get_json(f"https://api.hireology.com/v2/public/careers/{path}", params=params)
    out = []
    for item in data.get("data") or []:
        rec = item.get("attributes") or item
        loc = (rec.get("locations") or [{}])[0] if rec.get("locations") else {}
        loc_txt = loc if isinstance(loc, str) else ", ".join(
            str(x) for x in [loc.get("city"), loc.get("state")] if x)
        link = rec.get("career_site_url") or rec.get("career-site-url") or \
            f"https://careers.hireology.com/{str(rec.get('career_site_path') or '').lstrip('/')}"
        out.append(job(c["name"], rec.get("id") or item.get("id"), rec.get("name"), link,
                       loc_txt, rec.get("job_description") or rec.get("description") or ""))
    return out


def fetch_isolved(c):
    u = urlparse(c["url"])
    page = requests.get(c["url"], headers=BROWSER_HEADERS, timeout=TIMEOUT)
    page.raise_for_status()
    m = re.search(r'"domain_id"\s*:\s*"?(\d+)"?', page.text)
    if not m:
        raise RuntimeError("couldn't find the job board id on the page")
    data = get_json(f"{u.scheme}://{u.netloc}/core/jobs/{m.group(1)}", params={"getParams": "{}"},
                    headers={**BROWSER_HEADERS, "Accept": "application/json", "Referer": c["url"]})
    return [job(c["name"], j.get("jobUrl"), j.get("title"), j.get("jobUrl"), j.get("jobLocation", ""))
            for j in (data.get("data") or {}).get("jobs") or [] if j.get("jobUrl")]


def fetch_icims(c):
    """iCIMS career sites. Tries several forms of the job list address."""
    u = urlparse(c["url"])
    base = f"{u.scheme}://{u.netloc}"
    sess = requests.Session()
    sess.headers.update({**BROWSER_HEADERS, "Accept": "text/html,*/*"})
    if not c.get("ignore_robots") and not allowed_by_robots(base + "/jobs/search"):
        raise BlockedByRobots("this site doesn't allow automated checks")
    try:
        sess.get(base + "/jobs/intro", timeout=TIMEOUT)   # pick up session cookies
    except Exception:
        pass
    variants = [base + "/jobs/search?ss=1&in_iframe=1&pr={pr}",
                base + "/jobs/search?ss=1&pr={pr}",
                c["url"] + ("&" if "?" in c["url"] else "?") + "pr={pr}"]
    last_err = None
    for pattern in variants:
        out, seen = [], set()
        try:
            for pr in range(0, 10):
                r = sess.get(pattern.format(pr=pr), timeout=TIMEOUT)
                r.raise_for_status()
                found = 0
                for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
                    m = re.search(r"/jobs/(\d+)/[^/?]+/job", a["href"])
                    if not m or m.group(1) in seen:
                        continue
                    seen.add(m.group(1))
                    found += 1
                    title = a.get("title") or a.get_text(" ", strip=True)
                    title = re.sub(r"^\s*\d+\s*-\s*", "", title)
                    out.append(job(c["name"], m.group(1), title, urljoin(r.url, a["href"]).split("?")[0]))
                if not found:
                    break
                time.sleep(1)
            if out:
                return out
        except requests.HTTPError as e:
            last_err = e
    if last_err:
        raise last_err
    return []


def fetch_paycor(c):
    """Paycor (recruitingbypaycor.com) career boards."""
    qs = parse_qs(urlparse(c["url"]).query)
    client = qs.get("clientId", [""])[0]
    board = f"https://recruitingbypaycor.com/career/CareerHome.action?clientId={client}" if client else c["url"]
    r = get_page(board, check_robots=not c.get("ignore_robots"))
    soup = BeautifulSoup(r.text, "html.parser")
    if soup.find(id="gnewtonNoActiveJobs"):
        return []
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        if "JobIntroduction.action" not in a["href"]:
            continue
        href = urljoin(r.url, a["href"])
        jid = parse_qs(urlparse(href).query).get("id", [href])[0]
        if jid in seen:
            continue
        seen.add(jid)
        title = a.get("ns-qa") or a.get_text(" ", strip=True)
        row = a.find_parent(class_="gnewtonCareerGroupRowClass") or a.parent
        loc_el = row.find(class_="gnewtonCareerGroupJobDescriptionClass") if row else None
        loc = loc_el.get_text(" ", strip=True) if loc_el else ""
        out.append(job(c["name"], jid, title, href, loc))
    return out


def fetch_rss(c):
    """RSS/Atom feeds, optionally keeping only items whose title contains title_contains."""
    import xml.etree.ElementTree as ET
    urls = c.get("urls") or [c["url"]]
    last_err = None
    for url in urls:
        try:
            r = requests.get(url, headers={**BROWSER_HEADERS,
                                           "Accept": "application/rss+xml, application/xml, text/xml"},
                             timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            last_err = e
            continue
        want = (c.get("title_contains") or "").lower()
        out = []
        for item in root.iter():
            if item.tag.split("}")[-1] not in ("item", "entry"):
                continue
            get = lambda name: next((ch for ch in item if ch.tag.split("}")[-1] == name), None)
            t, l = get("title"), get("link")
            title = (t.text or "").strip() if t is not None else ""
            link = (l.text or l.get("href") or "").strip() if l is not None else ""
            if not title or not link or (want and want not in title.lower()):
                continue
            d, p = get("description") or get("summary"), get("pubDate") or get("published")
            out.append(job(c["name"], link, re.sub(r"^Job Posting:\s*", "", title), link,
                           description=d.text if d is not None else "",
                           posted=p.text if p is not None else ""))
        return out
    raise last_err or RuntimeError("feed not available")


def fetch_paycom(c):
    page = requests.get(c["url"], headers=BROWSER_HEADERS, timeout=TIMEOUT)
    page.raise_for_status()
    m = re.search(r'"sessionJWT":"([^"]+)"', page.text)
    if not m:
        raise RuntimeError("couldn't start a session with the Paycom board")
    hdrs = {**BROWSER_HEADERS, "Accept": "application/json", "Content-Type": "application/json",
            "Authorization": html.unescape(m.group(1)), "Locale": "en-US",
            "Origin": "https://www.paycomonline.net", "Referer": c["url"]}
    api = "https://portal-applicant-tracking.us-cent.paycomonline.net/api/ats/job-posting-previews/search"
    out, skip = [], 0
    while skip < 500:
        body = {"skip": skip, "take": 50, "filtersForQuery": {
            "distanceFrom": 0, "workEnvironments": [], "positionTypes": [], "educationLevels": [],
            "categories": [], "travelTypes": [], "shiftTypes": [], "otherFilters": [],
            "keywordSearchText": "", "location": "", "sortOption": ""}}
        r = requests.post(api, json=body, headers=hdrs, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        rows = data.get("jobPostingPreviews") or []
        for row in rows:
            jid = row.get("jobId")
            link = html.unescape(row.get("openAdvertUrl") or "") or \
                f"https://www.paycomonline.net/v4/ats/web.php/jobs/ViewJobDetails?job={jid}"
            out.append(job(c["name"], jid, html.unescape(row.get("jobTitle") or ""), link,
                           str(pick(row, "locations", "location", "jobLocation") or ""),
                           pick(row, "description", "jobDescription"), str(row.get("postedOn") or "")))
        skip += 50
        if len(rows) < 50 or skip >= (data.get("jobPostingPreviewsCount") or 0):
            break
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "ukg": fetch_ukg,
    "dayforce": fetch_dayforce,
    "adp": fetch_adp,
    "bamboohr": fetch_bamboohr,
    "hireology": fetch_hireology,
    "isolved": fetch_isolved,
    "icims": fetch_icims,
    "paycom": fetch_paycom,
    "paycor": fetch_paycor,
    "teamwork": fetch_teamwork,
    "rss": fetch_rss,
    "page": fetch_page,
}


# --------------------------------------------------------------- auto names

NAMES_FILE = ROOT / "data" / "names.json"


def _clean_title(title):
    title = re.sub(r"\s+", " ", title or "").strip()
    for junk in ("Current Opportunities", "Career Opportunities", "Job Board", "Careers",
                 "Career Center", "Candidate Portal", "Search Jobs", "Jobs", "Home"):
        title = re.sub(rf"\s*[-|:–]?\s*\b{junk}\b\s*[-|:–]?\s*", " ", title, flags=re.I).strip(" -|:–")
    return title if 2 < len(title) < 60 else ""


def looks_bad(name):
    return bool(not name or re.match(r"^[A-Z]{2}_[0-9a-f]{6,}", name) or
                re.fullmatch(r"(UKG|ADP|DAYFORCE|PAYCOR|PAYCOM|SITE)? ?board.*|Dayforce|UKG", name, re.I))


def detect_name(c):
    """For entries without a name, ask the job board what the organization is called."""
    kind, url = c.get("type"), c.get("url", "")
    try:
        if kind == "adp":
            qs = parse_qs(urlparse(url).query)
            data = get_json("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
                            "staffing/v1/content-links/career-center",
                            params={"cid": qs["cid"][0], "ccId": qs.get("ccId", ["19000101_000001"])[0],
                                    "timeStamp": 0, "locale": "en_US", "lang": "en_US"})
            text = json.dumps(data)
            for key in ("contentTitle", "title", "companyName", "clientName"):
                for m in re.finditer(rf'"{key}"\s*:\s*"([^"]+)"', text):
                    t = _clean_title(html.unescape(m.group(1)))
                    if t and t.lower() not in ("apply", "career", "careers") and not looks_bad(t):
                        return t
            m = re.search(r"client=([A-Za-z0-9_-]+)", text)
            if m:
                return m.group(1)
        page = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT).text
        if kind == "paycom":
            m = re.search(r'"sessionJWT":"([^"]+)"', page)
            if m:
                d = get_json("https://portal-applicant-tracking.us-cent.paycomonline.net/api/ats/company-name",
                             headers={"Authorization": html.unescape(m.group(1)), "Accept": "application/json"})
                if d.get("companyName"):
                    return html.unescape(d["companyName"])
        if kind == "dayforce":
            for key in ("companyName", "clientName", "careerSiteName", "siteName", "displayName"):
                m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', page)
                if m and _clean_title(m.group(1)):
                    return _clean_title(m.group(1))
        t = BeautifulSoup(page, "html.parser").title
        if t and _clean_title(t.get_text()) and not looks_bad(_clean_title(t.get_text())):
            return _clean_title(t.get_text())
    except Exception:
        pass
    # last resort: a readable label from the link itself
    u = urlparse(url)
    ident = ([p for p in u.path.split("/") if p] or [u.netloc])[0]
    if kind == "adp":
        ident = parse_qs(u.query).get("cid", ["?"])[0][:8]
    return f"{(kind or 'site').upper()} board {ident}"


# ----------------------------------------------------------------- filtering

def passes(j, company_cfg, global_filters):
    def pick(name):
        return [w.lower() for w in (company_cfg.get(name) or global_filters.get(name) or [])]
    title, loc = j["title"].lower(), j["location"].lower()
    kws, excl, locs = pick("keywords"), pick("exclude"), pick("locations")
    if kws and not any(k in title for k in kws):
        return False
    if excl and any(k in title for k in excl):
        return False
    if locs and not any(l in loc for l in locs):
        return False
    return True


# ------------------------------------------------------------- notifications

def notify(new_jobs):
    if not new_jobs:
        return
    batch = new_jobs[:50]  # avoid flooding
    extra = len(new_jobs) - len(batch)

    discord = os.environ.get("DISCORD_WEBHOOK_URL")
    if discord:
        for j in batch:
            payload = {"embeds": [{
                "title": f"{j['title']} — {j['company']}"[:256],
                "url": j["url"],
                "description": (f"📍 {j['location']}\n\n" if j["location"] else "") + j["description"],
            }]}
            try:
                requests.post(discord, json=payload, timeout=TIMEOUT).raise_for_status()
            except Exception as e:
                print(f"  ! Discord alert failed: {e}")

    slack = os.environ.get("SLACK_WEBHOOK_URL")
    if slack:
        for j in batch:
            text = f"*<{j['url']}|{j['title']}>* at *{j['company']}*"
            if j["location"]:
                text += f"\n📍 {j['location']}"
            if j["description"]:
                text += f"\n{j['description']}"
            try:
                requests.post(slack, json={"text": text}, timeout=TIMEOUT).raise_for_status()
            except Exception as e:
                print(f"  ! Slack alert failed: {e}")

    if os.environ.get("EMAIL_TO") and os.environ.get("SMTP_USER"):
        lines = []
        for j in batch:
            lines.append(f"{j['title']} — {j['company']}"
                         + (f" ({j['location']})" if j["location"] else ""))
            if j["description"]:
                lines.append(f"  {j['description']}")
            lines.append(f"  {j['url']}\n")
        if extra:
            lines.append(f"…and {extra} more on your job feed page.")
        msg = EmailMessage()
        msg["Subject"] = f"{len(new_jobs)} new job posting{'s' if len(new_jobs) != 1 else ''}"
        msg["From"] = os.environ["SMTP_USER"]
        msg["To"] = os.environ["EMAIL_TO"]
        msg.set_content("\n".join(lines))
        try:
            with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST") or "smtp.gmail.com",
                                  int(os.environ.get("SMTP_PORT") or "465")) as s:
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                s.send_message(msg)
        except Exception as e:
            print(f"  ! Email alert failed: {e}")


# ------------------------------------------------------------------ website

def build_page(feed, companies, status):
    template = PAGE_TEMPLATE.read_text(encoding="utf-8")
    data = {
        "jobs": feed,
        "companies": [c["name"] for c in companies],
        "status": status,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    PAGE_OUT.parent.mkdir(parents=True, exist_ok=True)
    PAGE_OUT.write_text(template.replace("/*__DATA__*/null", blob), encoding="utf-8")


# --------------------------------------------------------------------- main

def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    companies = cfg.get("companies") or []
    gfilters = cfg.get("filters") or {}
    seen = load(SEEN_FILE, {})          # {company_name: [job keys]}
    feed = load(FEED_FILE, [])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    names = load(NAMES_FILE, {})
    for c in companies:
        if not c.get("name"):
            key = c.get("url", "")
            if key not in names or looks_bad(names[key]):
                names[key] = detect_name(c)
            c["name"] = names[key]

    last_checked = load(ROOT / "data" / "last_checked.json", {})
    now_ts = datetime.now(timezone.utc).timestamp()
    new_jobs, status = [], {}
    for c in companies:
        name, kind = c.get("name"), c.get("type")
        ckey = c.get("url") or f"{kind}:{c.get('id')}"   # unique even if names repeat
        every = c.get("every_minutes")
        if every and ckey in seen and now_ts - last_checked.get(ckey, 0) < every * 60 - 120:
            status[name] = "ok"   # checked recently; this site is on a slower schedule
            continue
        last_checked[ckey] = now_ts
        if kind not in FETCHERS:
            print(f"  ! {name}: unknown type '{kind}'")
            status[name] = "config error"
            continue
        try:
            jobs = FETCHERS[kind](c)
        except BlockedByRobots as e:
            print(f"  ! {name}: {e}")
            status[name] = "this site doesn't allow automated checks"
            continue
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            print(f"  ! {name}: could not check (HTTP {code}) {e}")
            host = urlparse(e.response.url).netloc if e.response is not None else ""
            status[name] = f"check failed (error {code} from {host})"
            continue
        except Exception as e:
            print(f"  ! {name}: could not check ({type(e).__name__}: {e})")
            status[name] = f"check failed ({str(e)[:160] or type(e).__name__})"
            continue
        finally:
            time.sleep(0.5)  # be gentle with the sites

        jobs = list({j["key"]: j for j in jobs if passes(j, c, gfilters)}.values())
        first_run = ckey not in seen
        known = set(seen.get(ckey, []))
        fresh = [j for j in jobs if j["key"] not in known]

        # On the first check of a company, record everything currently open
        # without alerting, so you only hear about jobs posted from now on.
        # Set SHOW_EXISTING=1 to include current openings on the first run.
        if first_run and os.environ.get("SHOW_EXISTING") != "1":
            print(f"  {name}: {len(jobs)} open roles recorded (first check, no alerts)")
            fresh_for_alerts = []
        else:
            print(f"  {name}: {len(jobs)} open, {len(fresh)} new")
            fresh_for_alerts = fresh

        for j in fresh_for_alerts:
            j["found"] = now
        new_jobs.extend(fresh_for_alerts)
        # keep only currently listed keys so the file doesn't grow forever
        seen[ckey] = sorted({j["key"] for j in jobs})
        status[name] = "ok"

    # newest postings first: within this check, order by the site's own posted date when it has one
    def posted_ts(j):
        try:
            return datetime.fromisoformat(str(j.get("posted", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0
    new_jobs.sort(key=posted_ts, reverse=True)
    feed = sorted(new_jobs + feed, key=lambda j: j.get("found", ""), reverse=True)[:FEED_LIMIT]
    notify(new_jobs)

    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=1), encoding="utf-8")
    (ROOT / "data" / "last_checked.json").write_text(json.dumps(last_checked, indent=1), encoding="utf-8")
    NAMES_FILE.write_text(json.dumps(names, indent=1), encoding="utf-8")
    FEED_FILE.write_text(json.dumps(feed, indent=1, ensure_ascii=False), encoding="utf-8")
    build_page(feed, companies, status)
    print(f"Done. {len(new_jobs)} new job(s).")


if __name__ == "__main__":
    sys.exit(main())
