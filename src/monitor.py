"""Conservative inventory monitoring over explicitly supplied snapshots.

No crawlers. An incomplete or failed source run cannot prove withdrawal.
Monetary examples are fictional and kept separate from the supplied object.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta


def normalize(text):
    return re.sub(r"[^\w]+", "", str(text).casefold().replace("ё", "е"))


def classify(text):
    text = text.casefold()
    occupied = bool(re.search(r"\bгаб\b|с арендатор|действующ\w* аренд", text))
    empty = bool(re.search(r"без арендатор|свободное помещение|помещение свободно|вакант", text))
    if occupied and empty:
        return "conflict"
    return "gab" if occupied else "vacant" if empty else "unknown"


def connection(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, sources TEXT NOT NULL,
        input_hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS objects (
        object_id TEXT PRIMARY KEY, identity_key TEXT UNIQUE NOT NULL);
    CREATE TABLE IF NOT EXISTS listings (
        source TEXT NOT NULL, listing_id TEXT NOT NULL, object_id TEXT NOT NULL,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, missed INTEGER NOT NULL DEFAULT 0,
        payload TEXT NOT NULL, PRIMARY KEY(source, listing_id));
    CREATE TABLE IF NOT EXISTS observations (
        run_id TEXT NOT NULL, source TEXT NOT NULL, listing_id TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(run_id, source, listing_id));
    CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, object_id TEXT,
        kind TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS reports(run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
    """)
    return db


class Monitor:
    def __init__(self, path):
        self.db = connection(path)

    def close(self):
        self.db.close()

    def ingest(self, batch):
        """Atomic run; run_id is immutable and replays do not duplicate events."""
        run_id, now = batch["run_id"], batch["captured_at"]
        digest = hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()
        prior = self.db.execute("SELECT input_hash FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if prior:
            if prior[0] != digest:
                raise ValueError("run_id already used for different input")
            return self.report(run_id)
        sources = batch["sources"]
        seen = set()
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run_id, now, json.dumps(sources), digest))
            for row in batch["listings"]:
                source, lid = row["source"], row["listing_id"]
                source_run = sources.get(source, {})
                if source_run.get("status") != "ok":
                    raise ValueError("Rows supplied for failed/unregistered source")
                if (source, lid) in seen:
                    raise ValueError("duplicate source/listing_id in snapshot")
                seen.add((source, lid))
                area = float(row["area_m2"])
                if area <= 0 or (row.get("price_rub") is not None and row["price_rub"] <= 0):
                    raise ValueError("Invalid area/price")
                # A shared building cadastral number is insufficient: require exact unit.
                cad = row.get("unit_cadastral_id")
                identity = f"unit:{cad}" if cad else f"listing:{source}:{lid}"
                obj = "obj-" + hashlib.sha256(identity.encode()).hexdigest()[:12]
                self.db.execute("INSERT OR IGNORE INTO objects VALUES (?,?)", (obj, identity))
                previous = self.db.execute("SELECT * FROM listings WHERE source=? AND listing_id=?", (source, lid)).fetchone()
                if previous:
                    # Do not silently move a listing to another property when its identity changes.
                    if previous["object_id"] != obj:
                        self._event(run_id, previous["object_id"], "identity_conflict", {"source": source, "listing_id": lid})
                        obj = previous["object_id"]
                    old = json.loads(previous["payload"])
                    for field in ("price_rub", "status"):
                        if old.get(field) != row.get(field):
                            self._event(run_id, obj, field + "_changed", {"source": source, "listing_id": lid, "before": old.get(field), "after": row.get(field)})
                else:
                    existing = self.db.execute("SELECT 1 FROM listings WHERE object_id=?", (obj,)).fetchone()
                    self._event(run_id, obj, "duplicate_linked" if existing else "new", {"source": source, "listing_id": lid})
                row = dict(row, classification=classify(row["description"]))
                payload = json.dumps(row, ensure_ascii=False)
                self.db.execute("INSERT INTO observations VALUES (?,?,?,?)", (run_id, source, lid, payload))
                self.db.execute("""INSERT INTO listings VALUES (?,?,?,?,?,0,?)
                    ON CONFLICT(source,listing_id) DO UPDATE SET last_seen=excluded.last_seen,
                    missed=0, payload=excluded.payload""", (source, lid, obj, now, now, payload))
            for source, health in sources.items():
                if health.get("status") != "ok":
                    self._event(run_id, None, "source_failed", {"source": source, "reason": health.get("reason", "unknown")})
                elif not health.get("complete", False):
                    self._event(run_id, None, "partial_source", {"source": source})
                else:
                    count = sum(1 for src, _ in seen if src == source)
                    if count < health.get("expected_min", 0):
                        self._event(run_id, None, "volume_drop", {"source": source, "received": count, "expected_min": health["expected_min"]})
                    for old in self.db.execute("SELECT * FROM listings WHERE source=?", (source,)).fetchall():
                        if (source, old["listing_id"]) not in seen:
                            self.db.execute("UPDATE listings SET missed=missed+1 WHERE source=? AND listing_id=?", (source, old["listing_id"]))
                            self._event(run_id, old["object_id"], "not_seen", {"source": source, "listing_id": old["listing_id"], "consecutive_complete_misses": old["missed"] + 1, "action": "recheck; not proof of sale"})
            result = self.report(run_id)
            self.db.execute("INSERT INTO reports VALUES (?,?)", (run_id, json.dumps(result, ensure_ascii=False)))
        return result

    def _event(self, run, obj, kind, detail):
        self.db.execute("INSERT INTO events(run_id,object_id,kind,detail) VALUES (?,?,?,?)", (run, obj, kind, json.dumps(detail, ensure_ascii=False)))

    def report(self, run_id):
        saved = self.db.execute("SELECT payload FROM reports WHERE run_id=?", (run_id,)).fetchone()
        if saved:
            return json.loads(saved[0])
        run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        events = [dict(r) for r in self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY event_id", (run_id,))]
        for e in events:
            e["detail"] = json.loads(e["detail"])
        listings = [dict(r) for r in self.db.execute("SELECT * FROM listings ORDER BY object_id,source")]
        objects = {}
        for listing in listings:
            listing["payload"] = json.loads(listing["payload"])
            objects.setdefault(listing["object_id"], []).append(listing)
        result = []
        candidates = []
        for oid, group in objects.items():
            # Compare simultaneously observed prices; stale quotes are explicitly separate.
            current = [r["payload"] for r in group if r["last_seen"] == run["captured_at"] and r["payload"]["status"] == "listed"]
            prices = {r["price_rub"] for r in current if r.get("price_rub") is not None}
            states = {r["classification"] for r in current if r["classification"] != "unknown"}
            conflicts = []
            if len(prices) > 1:
                conflicts.append("different_current_prices")
            if "conflict" in states or {"gab", "vacant"}.issubset(states):
                conflicts.append("occupancy_conflict")
            result.append({"object_id": oid, "listings": group, "conflicts": conflicts})
        # Address + area is only a review candidate, never an automatic merge.
        for i, a in enumerate(result):
            pa = a["listings"][0]["payload"]
            for b in result[i + 1:]:
                pb = b["listings"][0]["payload"]
                if normalize(pa["address"]) == normalize(pb["address"]) and abs(pa["area_m2"] - pb["area_m2"]) <= 2:
                    candidates.append({"a": a["object_id"], "b": b["object_id"], "reason": "same address and close area; manual review"})
        return {"run_id": run_id, "captured_at": run["captured_at"], "sources": json.loads(run["sources"]), "events": events, "objects": result, "merge_candidates": candidates, "educational": True}

    def watchdog(self, now, expected_by):
        """Designed for an independent scheduler; a stopped job cannot alert itself."""
        last = self.db.execute("SELECT MAX(captured_at) FROM runs").fetchone()[0]
        deadline = datetime.fromisoformat(expected_by)
        overdue = datetime.fromisoformat(now) > deadline and (last is None or datetime.fromisoformat(last) < deadline - timedelta(hours=1))
        return {"alert": overdue, "last_completed_ingest": last, "deadline": expected_by}
