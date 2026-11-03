"""Build a static, portable report by actually executing the local prototypes."""
import base64
import argparse
import csv
import hashlib
import json
import html
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from src.demo import SCENARIOS, run_scenario
from src.monitor import Monitor
from tools.fixtures import monitoring_batches, sample_property
from tools.render_notes import render, page

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def poster():
    photo = base64.b64encode((ROOT / "assets/originals/01-front.png").read_bytes()).decode()
    # Code-native vector layout. The original photograph is embedded byte for byte.
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="1080" height="1350" viewBox="0 0 1080 1350" role="img" aria-label="Здание с арендатором Вкусно — и точка, 416 квадратных метров, М-2 Крым. По данным объявления.">
<rect width="1080" height="1350" fill="#efede6"/>
<g font-family="Arial, sans-serif" fill="#183e34">
<text x="56" y="67" font-size="18" letter-spacing="3">КОММЕРЧЕСКАЯ НЕДВИЖИМОСТЬ / ПРОДАЖА</text>
<path d="M56 91 H1024" stroke="#183e34" stroke-width="1"/>
<text x="52" y="187" font-size="80" font-weight="700" letter-spacing="-3">Здание с арендатором.</text>
<text x="56" y="247" font-size="43">«Вкусно — и точка»</text>
</g>
<image x="0" y="292" width="1080" height="607.5" preserveAspectRatio="xMidYMid meet" xlink:href="data:image/png;base64,{photo}"/>
<rect x="0" y="900" width="1080" height="450" fill="#183e34"/>
<g font-family="Arial, sans-serif" fill="#efede6">
<text x="52" y="1027" font-size="112" font-weight="700" letter-spacing="-5">416 м²</text>
<text x="610" y="979" font-size="25">М-2 «Крым»</text>
<text x="610" y="1019" font-size="25">Московская область</text>
<path d="M56 1070 H1024" stroke="#8b9b8b" stroke-width="1"/>
<text x="56" y="1138" font-size="36">Запросить документы</text>
<text x="56" y="1186" font-size="36">и условия продажи ↗</text>
<text x="56" y="1268" font-size="19" fill="#c1cab9">Сведения об объекте — из объявления № 8145299815.</text>
<text x="56" y="1301" font-size="19" fill="#c1cab9">Площадь и арендатор требуют проверки по документам.</text>
</g></svg>'''
    (DIST / "assets/poster.svg").write_text(svg, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-url")
    parser.add_argument("--repo-url")
    args = parser.parse_args()
    if bool(args.public_url) != bool(args.repo_url):
        parser.error("--public-url and --repo-url must be provided together")
    if args.public_url:
        for value in (args.public_url, args.repo_url):
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                parser.error("Release links must be plain absolute HTTPS URLs")
        if urlsplit(args.repo_url).hostname != "github.com":
            parser.error("Repository URL must point to github.com")
    (DIST / "assets").mkdir(parents=True, exist_ok=True)
    (DIST / "data").mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "ui").iterdir():
        if source.is_file():
            shutil.copy2(source, DIST / source.name)
    if args.public_url:
        index = (DIST / "index.html").read_text(encoding="utf-8")
        index = index.replace("<strong>Локальная версия</strong>", "<strong>Учебная демонстрация</strong>")
        links = f'<p id="publication-state">Материалы для сдачи: <a href="{html.escape(args.public_url, quote=True)}">страница отчёта</a> и <a href="{html.escape(args.repo_url, quote=True)}">GitHub-репозиторий</a>. На странице доступны сохранённые прогоны; новый запуск Python — по инструкции в README.</p>'
        index = re.sub(r'<p id="publication-state">.*?</p>', lambda _: links, index)
        (DIST / "index.html").write_text(index, encoding="utf-8")
    for source in (ROOT / "assets/originals").glob("*.png"):
        shutil.copy2(source, DIST / "assets" / source.name)
    manifest = [{"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "origin": "user-supplied Avito listing images; original bytes preserved"} for p in sorted((ROOT / "assets/originals").glob("*.png"))]
    write_json(DIST / "data/asset-manifest.json", manifest)
    write_json(ROOT / "fixtures/property.json", sample_property())
    batches = monitoring_batches()
    write_json(ROOT / "fixtures/monitoring.json", batches)
    with tempfile.TemporaryDirectory() as temp:
        monitor = Monitor(Path(temp) / "monitor.sqlite")
        reports = [monitor.ingest(batch) for batch in batches]
        watchdog = monitor.watchdog("2026-11-04T08:45:00+03:00", "2026-11-04T08:30:00+03:00")
        monitor.close()
        write_json(DIST / "data/monitoring.json", {"reports": reports, "watchdog": watchdog})
    write_json(DIST / "data/publication.json", {s: run_scenario(s) for s in SCENARIOS})
    for report in reports:
        csv_path = DIST / f'data/daily-report-{report["run_id"]}.csv'
        with csv_path.open("w", encoding="utf-8-sig", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["Учебные данные", "Адрес", "Площадь, м²", "Класс", "Источник", "Цена, ₽ (вымышленная)", "Последнее наблюдение", "Пропуски полного обхода", "Статус источника"])
            for obj in report["objects"]:
                for row in obj["listings"]:
                    p = row["payload"]
                    writer.writerow([obj["object_id"], p["address"], p["area_m2"], p["classification"], row["source"], p.get("price_rub"), row["last_seen"], row["missed"], p["status"]])
    shutil.copy2(csv_path, DIST / "data/daily-report.csv")
    poster()
    (DIST / "notes").mkdir(exist_ok=True)
    for p in list((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]:
        shutil.copy2(p, DIST / "data" / p.name)
        md = p.read_text(encoding="utf-8")
        (DIST / "notes" / (p.stem + ".html")).write_text(page(md.splitlines()[0].lstrip('# '), render(md), p.name), encoding="utf-8")
    (DIST / "assets/evidence").mkdir(exist_ok=True)
    for p in (ROOT / "evidence").glob("*.jpg"):
        shutil.copy2(p, DIST / "assets/evidence" / p.name)
    (DIST / ".nojekyll").write_text("", encoding="utf-8")
    write_json(DIST / "data/build.json", {"built_at": datetime.now(timezone.utc).isoformat(), "data_mode": "educational", "external_calls": 0, "public_url": args.public_url, "repository_url": args.repo_url})
    print("Built dist/: 3 monitoring snapshots, 7 publication scenarios, CSV, SVG, 6 original images.")


if __name__ == "__main__":
    main()
