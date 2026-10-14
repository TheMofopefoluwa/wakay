"""Collect jobs from Greenhouse, Lever, Ashby (public APIs) and Twine (best effort).
Writes jobs.json. Optional: set BRAVE_API_KEY to discover new company boards
from your site: queries."""
import json, os, re, time, datetime, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
import random, smtplib
from email.message import EmailMessage
from html.parser import HTMLParser

UA = {"User-Agent": "job-watcher/1.0 (personal use)"}
cfg = json.load(open("config.json"))
try: found = json.load(open("found_boards.json"))
except Exception: found = {"greenhouse": [], "lever": [], "ashby": []}
try: dead = json.load(open("dead_boards.json"))
except Exception: dead = {"greenhouse": [], "lever": [], "ashby": []}

def get(url, as_json=True, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "ignore")
    return json.loads(body) if as_json else body

def add_board(src, slug):
    slug = urllib.parse.unquote(slug).strip()
    if slug and slug not in found[src] and slug not in dead.get(src, []): found[src].append(slug)

PATTERNS = {"ashby": r"jobs\.ashbyhq\.com/([^/?#\"]+)",
            "greenhouse": r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([^/?#\"]+)",
            "lever": r"jobs\.lever\.co/([^/?#\"]+)"}
SKIP = {"embed", "v1", "api", "static", "favicon.ico", "robots.txt"}

def discover_commoncrawl():
    """Free, no key: lists company boards seen by Common Crawl's public index.
    Their server is often busy, so use small queries, retry, and fall back to older crawls."""
    crawls = [c["cdx-api"] for c in get("https://index.commoncrawl.org/collinfo.json", timeout=60)[:4]]
    hosts = {"ashby": ["jobs.ashbyhq.com"], "lever": ["jobs.lever.co"],
             "greenhouse": ["job-boards.greenhouse.io", "boards.greenhouse.io"]}
    for src, hs in hosts.items():
        for h in hs:
            got = False
            for cdx in crawls:
                for attempt in range(2):
                    try:
                        lines = get(f"{cdx}?url={h}/*&output=json&fl=url&limit=1500", as_json=False, timeout=120).splitlines()
                        for line in lines:
                            m = re.search(PATTERNS[src], json.loads(line)["url"])
                            if m and m.group(1).lower() not in SKIP: add_board(src, m.group(1))
                        got = True; break
                    except Exception as e:
                        print("commoncrawl", h, cdx[-20:], e); time.sleep(5)
                if got: break

def discover_tavily():
    key = os.environ.get("TAVILY_API_KEY")
    if not key: return
    doms = {"ashby": "jobs.ashbyhq.com", "greenhouse": "job-boards.greenhouse.io", "lever": "jobs.lever.co"}
    roles = ["software engineer", "frontend developer", "backend developer", "data engineer", "product designer",
             "devops engineer", "mobile developer", "graphic designer", "ui ux designer", "machine learning engineer",
             "product manager", "qa engineer", "security engineer"]
    for src in random.sample(list(doms), 2):          # 2 credits per run
        try:
            body = json.dumps({"query": f"remote {random.choice(roles)} job", "include_domains": [doms[src]], "max_results": 20}).encode()
            req = urllib.request.Request("https://api.tavily.com/search", body,
                {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            for r in json.load(urllib.request.urlopen(req, timeout=30)).get("results", []):
                m = re.search(PATTERNS[src], r["url"])
                if m: add_board(src, m.group(1))
        except Exception as e: print("tavily", src, e)

def discover():
    if os.environ.get("DISCOVER") == "1": discover_commoncrawl()   # once a day
    discover_tavily()                                              # every run
    json.dump(found, open("found_boards.json", "w"), indent=1)

def greenhouse(t):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(t)}/jobs")
    for j in d.get("jobs", []):
        yield dict(title=j["title"], company=t, location=(j.get("location") or {}).get("name", ""),
                   url=j["absolute_url"], source="Greenhouse")

def lever(t):
    for j in get(f"https://api.lever.co/v0/postings/{urllib.parse.quote(t)}?mode=json"):
        c = j.get("categories") or {}
        yield dict(title=j["text"], company=t, location=c.get("location", "") + " " + (j.get("workplaceType") or ""),
                   url=j["hostedUrl"], source="Lever")

def ashby(t):
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(t)}")
    for j in d.get("jobs", []):
        loc = (j.get("location") or "") + (" Remote" if j.get("isRemote") else "")
        yield dict(title=j["title"], company=t, location=loc, url=j["jobUrl"], source="Ashby")

class Links(HTMLParser):
    def __init__(s): super().__init__(); s.out = []; s.href = None
    def handle_starttag(s, tag, a):
        if tag == "a": s.href = dict(a).get("href")
    def handle_data(s, data):
        if s.href and "/jobs/" in s.href and data.strip():
            s.out.append((data.strip(), s.href)); s.href = None

def twine():
    # Twine has no public API; this reads the public listing page. May break or be blocked.
    p = Links(); p.feed(get(cfg["twine_url"], as_json=False))
    for title, href in p.out:
        yield dict(title=title, company="Twine", location="", url=urllib.parse.urljoin("https://www.twine.net", href), source="Twine")

def remotive(_=None):
    for cat in ("software-dev", "design", "marketing", "customer-support", "writing", "sales-business",
                "project-management", "data", "devops", "product", "human-resources", "all-others"):
        try:
            for j in get(f"https://remotive.com/api/remote-jobs?category={cat}").get("jobs", []):
                yield dict(title=j["title"], company=j["company_name"], location="Remote " + j.get("candidate_required_location", ""), url=j["url"], source="Remotive")
        except Exception as e: print("remotive", cat, e)

def remoteok(_=None):
    for j in get("https://remoteok.com/api")[1:]:
        yield dict(title=j.get("position", ""), company=j.get("company", ""), location="Remote " + j.get("location", ""), url=j.get("url", ""), source="RemoteOK")

def weworkremotely(_=None):
    for feed in ("remote-programming-jobs", "remote-design-jobs", "remote-customer-support-jobs",
                 "remote-sales-and-marketing-jobs", "remote-management-and-finance-jobs", "remote-all-other-jobs"):
        try:
            root = ET.fromstring(get(f"https://weworkremotely.com/categories/{feed}.rss", as_json=False))
            for it in root.iter("item"):
                co, _, title = (it.findtext("title") or "").partition(": ")
                yield dict(title=title or co, company=co, location="Remote", url=it.findtext("link") or "", source="WeWorkRemotely")
        except Exception as e: print("wwr", feed, e)

def arbeitnow(_=None):
    for j in get("https://www.arbeitnow.com/api/job-board-api").get("data", []):
        yield dict(title=j["title"], company=j["company_name"], location=j.get("location", "") + (" Remote" if j.get("remote") else ""), url=j["url"], source="Arbeitnow")

def jobicy(_=None):
    for j in get("https://jobicy.com/api/v2/remote-jobs?count=50").get("jobs", []):
        yield dict(title=j.get("jobTitle", ""), company=j.get("companyName", ""), location="Remote " + str(j.get("jobGeo", "")), url=j.get("url", ""), source="Jobicy")

def themuse(_=None):
    for pg in (1, 2):
        for j in get(f"https://www.themuse.com/api/public/jobs?page={pg}&category=Software%20Engineering&category=Design%20and%20UX&category=Data%20and%20Analytics")["results"]:
            locs = " ".join(l.get("name", "") for l in j.get("locations", []))
            yield dict(title=j["name"], company=j["company"]["name"], location=locs, url=j["refs"]["landing_page"], source="TheMuse")

def himalayas(_=None):
    for j in get("https://himalayas.app/jobs/api?limit=100").get("jobs", []):
        yield dict(title=j.get("title", ""), company=j.get("companyName", ""), location="Remote " + " ".join(j.get("locationRestrictions") or []), url=j.get("applicationLink") or j.get("guid", ""), source="Himalayas")

def jooble(_=None):
    key = os.environ.get("JOOBLE_KEY")
    if not key: return
    for kw in ("software developer", "designer", "virtual assistant", "social media manager", "content creator", "customer support", "marketing"):
        body = json.dumps({"keywords": kw, "location": "Nigeria"}).encode()
        req = urllib.request.Request(f"https://jooble.org/api/{key}", body, {"Content-Type": "application/json"})
        for j in json.load(urllib.request.urlopen(req, timeout=25)).get("jobs", []):
            yield dict(title=j.get("title", ""), company=j.get("company", ""), location=(j.get("location") or "") + " Remote", url=j.get("link", ""), source="Jooble")

EXTRA = {"jobicy": jobicy, "themuse": themuse, "himalayas": himalayas, "jooble": jooble, "remotive": remotive, "remoteok": remoteok, "weworkremotely": weworkremotely, "arbeitnow": arbeitnow}
PRIORITY = {"Ashby", "Greenhouse", "Lever", "Twine"}

def keep(j):
    t = j["title"].lower(); loc = j["location"].lower()
    if not any(re.search(r"\b" + re.escape(k) + r"\b", t) for k in cfg["tech_keywords"]): return False
    if any(x in t for x in cfg["exclude"]): return False
    if cfg["remote_only"] and j["source"] != "Twine" and "remote" not in loc: return False
    return True

def send_digest(new):
    host, user, pw, to = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and pw and to and new): return
    new = sorted(new, key=lambda j: not j.get("priority"))[:40]
    body = "\n\n".join(f"{'* ' if j.get('priority') else ''}{j['title']}\n{j['company']} | {j['source']} | {j['location'].strip()}\n{j['url']}" for j in new)
    m = EmailMessage(); m["Subject"] = f"{len(new)} new jobs"; m["From"] = user; m["To"] = to; m.set_content(body)
    host = host or "smtp.gmail.com"; pw = pw.replace(" ", "")
    for how in ("ssl465", "starttls587"):
        try:
            if how == "ssl465":
                srv = smtplib.SMTP_SSL(host, 465, timeout=30)
            else:
                srv = smtplib.SMTP(host, 587, timeout=30); srv.starttls()
            srv.login(user, pw); srv.send_message(m); srv.quit()
            print("email sent via", how); return
        except Exception as e: print("email failed", how, type(e).__name__, e)

def main():
    discover()
    try: old = {j["url"]: j for j in json.load(open("jobs.json"))["jobs"]}
    except Exception: old = {}
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    jobs, errors = {}, []
    tasks = [(greenhouse, t) for t in set(cfg["greenhouse"] + found["greenhouse"])]
    tasks += [(lever, t) for t in set(cfg["lever"] + found["lever"])]
    tasks += [(ashby, t) for t in set(cfg["ashby"] + found["ashby"])]
    tasks.append((lambda _: twine(), None))
    tasks += [(EXTRA[n], None) for n in cfg["extra_sources"] if n in EXTRA]
    from concurrent.futures import ThreadPoolExecutor
    def run(task):
        fn, arg = task
        try: return task, list(fn(arg)), None
        except Exception as e: return task, [], e
    with ThreadPoolExecutor(12) as ex:
        for (fn, arg), items, err in ex.map(run, tasks):
            if err:
                if arg and "404" in str(err):   # board no longer exists: stop checking it
                    src = fn.__name__
                    dead.setdefault(src, []).append(arg)
                    if arg in found.get(src, []): found[src].remove(arg)
                else: errors.append(f"{arg or fn.__name__}: {err}")
            for j in items:
                if j["url"] and keep(j):
                    jobs[j["url"]] = {**j, "priority": j["source"] in PRIORITY, "first_seen": old.get(j["url"], {}).get("first_seen", now), "last_seen": now}
    json.dump(found, open("found_boards.json", "w"), indent=1)
    json.dump(dead, open("dead_boards.json", "w"), indent=1)
    cutoff = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=30)).isoformat() + "Z"
    for u, j in old.items():   # keep a job for 30 days after it last appeared
        if u not in jobs and j.get("last_seen", j["first_seen"]) > cutoff: jobs[u] = j
    new = [j for u, j in jobs.items() if u not in old]
    if old: send_digest(new)   # no email on the very first load
    out = sorted(jobs.values(), key=lambda j: j["first_seen"], reverse=True)
    json.dump({"updated": now, "errors": errors, "jobs": out}, open("jobs.json", "w"), separators=(",", ":"))
    print(len(out), "jobs;", len(errors), "errors")

main()
