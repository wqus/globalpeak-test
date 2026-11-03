"""Run meaningful checks and save exact results; never synthesize passing tests."""
import base64
import hashlib
import io
import json
import re
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, unquote

ROOT = Path(__file__).resolve().parents[1]


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        for key in ("href", "src"):
            if key in attrs:
                self.links.append(attrs[key])


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    broken = []
    for file in (ROOT / "dist").rglob("*.html"):
        parser = References()
        parser.feed(file.read_text(encoding="utf-8"))
        for link in parser.links:
            url = urlparse(link)
            if url.scheme or url.netloc:
                continue
            target = (file.parent / unquote(url.path)).resolve() if url.path else file
            if target == ROOT / "dist/data/verification.json":
                continue  # This command writes the referenced output below.
            if not target.exists():
                broken.append(f"{file.name}: {link}")
            elif url.fragment:
                page = References()
                page.feed(target.read_text(encoding="utf-8"))
                if url.fragment not in page.ids:
                    broken.append(f"{file.name}: missing anchor {link}")
    assets = json.loads((ROOT / "dist/data/asset-manifest.json").read_text(encoding="utf-8"))
    assets_ok = all(hashlib.sha256((ROOT / "dist/assets" / item["file"]).read_bytes()).hexdigest() == item["sha256"] for item in assets)
    svg = (ROOT / "dist/assets/poster.svg").read_text(encoding="utf-8")
    encoded = re.search(r"base64,([A-Za-z0-9+/=]+)", svg).group(1)
    photo_ok = base64.b64decode(encoded) == (ROOT / "assets/originals/01-front.png").read_bytes()
    passed = result.wasSuccessful() and not broken and assets_ok and photo_ok
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "success": passed,
              "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "broken_internal_references": broken, "original_asset_hashes_ok": assets_ok,
              "poster_photo_byte_identical": photo_ok, "external_integrations_tested": False,
              "test_log": output.getvalue()}
    (ROOT / "dist/data/verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output.getvalue())
    print(json.dumps({k: v for k, v in report.items() if k != "test_log"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
