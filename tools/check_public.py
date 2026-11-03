"""Verify the hosted report using fresh requests without authentication."""
import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag
from urllib.request import Request, urlopen


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("src", "href") and value and not value.startswith("#"):
                self.links.append(value)


def fetch(url):
    try:
        # No Authorization header, CookieJar or user browser session is used.
        with urlopen(Request(url, headers={"User-Agent": "GlobalPeak-public-link-check"}), timeout=20) as response:
            return {"url": url, "status": response.status, "final_url": response.geturl(), "mime": response.headers.get_content_type(), "body": response.read()}
    except Exception as error:
        return {"url": url, "status": getattr(error, "code", None), "error": str(error)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default="https://wqus.github.io/globalpeak-test/")
    parser.add_argument("--repository", default="https://github.com/wqus/globalpeak-test")
    args = parser.parse_args()
    site = args.site.rstrip("/") + "/"
    root = urlsplit(site)
    pending = {site, args.repository}
    for path in ("data/monitoring.json", "data/publication.json", "data/verification.json", "data/build.json", "data/asset-manifest.json", "data/daily-report-2026-11-01.csv", "data/daily-report-2026-11-02.csv", "data/daily-report-2026-11-03.csv"):
        pending.add(urljoin(site, path))
    results = {}
    while pending:
        with ThreadPoolExecutor(max_workers=6) as pool:
            batch = list(pool.map(fetch, sorted(pending)))
        pending = set()
        for response in batch:
            url = response["url"]
            results[url] = response
            if response.get("status") != 200 or not url.startswith(site):
                continue
            if response.get("mime") == "text/html":
                links = Links()
                links.feed(response["body"].decode("utf-8-sig"))
                for value in links.links:
                    target = urldefrag(urljoin(url, value))[0]
                    parsed = urlsplit(target)
                    if parsed.scheme == "https" and parsed.netloc == root.netloc and parsed.path.startswith(root.path) and target not in results:
                        pending.add(target)
        pending -= set(results)
    failed = [{k: v for k, v in result.items() if k != "body"} for result in results.values() if result.get("status") != 200]
    index = results[site].get("body", b"").decode("utf-8-sig")
    date_ok = "9 ноября 2026" in index
    public_label = "Учебная демонстрация" in index and args.repository in index
    hashes = []
    manifest_response = results[urljoin(site, "data/asset-manifest.json")]
    manifest = json.loads(manifest_response["body"])
    # Some preserved originals are not visible in the compact gallery.
    for asset in manifest:
        url = urljoin(site, "assets/" + asset["file"])
        if url not in results:
            results[url] = fetch(url)
        response = results[url]
        hashes.append({"file": asset["file"], "ok": response.get("status") == 200 and hashlib.sha256(response.get("body", b"")).hexdigest() == asset["sha256"]})
    passed = not failed and date_ok and public_label and all(item["ok"] for item in hashes)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "site": site, "repository": args.repository,
              "authenticated": False, "success": passed, "resources_checked": len(results), "failed": failed,
              "candidate_start_date_present": date_ok, "public_links_present": public_label,
              "original_photo_hashes": hashes,
              "resources": [{k: v for k, v in result.items() if k != "body"} for result in results.values()]}
    out = Path(__file__).resolve().parents[1] / "evidence/public-checks.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "resources"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
