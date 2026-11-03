import copy
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from src.monitor import Monitor, classify
from src.publication import PublicationService, DomainError, content_hash_for_approval
from src.demo import run_scenario
from src.server import create_server
from tools.fixtures import monitoring_batches, sample_property


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.monitor = Monitor(Path(self.temp.name) / "monitor.sqlite")

    def tearDown(self):
        self.monitor.close()
        self.temp.cleanup()

    def test_classification_keeps_ambiguity(self):
        self.assertEqual(classify("ГАБ без арендатора"), "conflict")
        self.assertEqual(classify("Без арендатора"), "vacant")
        self.assertEqual(classify("Торговое помещение"), "unknown")

    def test_replay_is_idempotent_and_snapshot_is_historical(self):
        a, b, _ = monitoring_batches()
        first = self.monitor.ingest(a)
        self.assertEqual(first, self.monitor.ingest(a))
        self.monitor.ingest(b)
        self.assertEqual(first, self.monitor.report(a["run_id"]))
        a["listings"][0]["price_rub"] += 1
        with self.assertRaises(ValueError):
            self.monitor.ingest(a)

    def test_failed_source_does_not_withdraw_or_increment_misses(self):
        a, b, c = monitoring_batches()
        self.monitor.ingest(a)
        self.monitor.ingest(b)
        report = self.monitor.ingest(c)
        listings = [r for o in report["objects"] for r in o["listings"]]
        self.assertTrue(all(r["missed"] == 0 for r in listings if r["source"] == "partner-b"))
        self.assertTrue(any(e["kind"] == "source_failed" for e in report["events"]))
        self.assertTrue(any(r["missed"] == 2 and r["payload"]["status"] == "listed" for r in listings))

    def test_duplicates_conflicts_and_price_history(self):
        a, b, _ = monitoring_batches()
        first = self.monitor.ingest(a)
        self.assertEqual(len(first["objects"]), 5)
        self.assertEqual(len(first["merge_candidates"]), 1)
        self.assertTrue(any("different_current_prices" in o["conflicts"] for o in first["objects"]))
        second = self.monitor.ingest(b)
        changes = [e for e in second["events"] if e["kind"] == "price_rub_changed"]
        self.assertEqual(changes[0]["detail"]["before"], 22000000)
        self.assertEqual(changes[0]["detail"]["after"], 21000000)

    def test_bad_snapshot_rolls_back_atomically(self):
        a = monitoring_batches()[0]
        a["listings"][-1]["area_m2"] = -1
        with self.assertRaises(ValueError):
            self.monitor.ingest(a)
        self.assertEqual(self.monitor.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.monitor.db.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 0)

    def test_partial_and_zero_volume_are_explicit(self):
        a, b, _ = monitoring_batches()
        self.monitor.ingest(a)
        b["sources"]["partner-a"]["complete"] = False
        self.monitor.ingest(b)
        a102 = self.monitor.db.execute("SELECT missed FROM listings WHERE listing_id='A102'").fetchone()[0]
        self.assertEqual(a102, 0)
        b["run_id"] = "empty"
        b["listings"] = []
        b["sources"]["partner-a"]["complete"] = True
        result = self.monitor.ingest(b)
        self.assertTrue(any(e["kind"] == "volume_drop" for e in result["events"]))

    def test_independent_watchdog_can_detect_no_run(self):
        result = self.monitor.watchdog("2026-11-03T09:00:00+03:00", "2026-11-03T08:30:00+03:00")
        self.assertTrue(result["alert"])


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "publication.sqlite"
        self.s = PublicationService(self.path)
        self.p = sample_property()
        self.s.seed(self.p)

    def tearDown(self):
        self.s.close()
        self.temp.cleanup()

    def test_success_waits_for_report_and_deduplicates(self):
        job, _ = self.s.enqueue(self.p["id"], 7)
        sent = self.s.work(job["id"])
        self.assertEqual(sent["status"], "submitted")
        self.assertIsNone(sent["url"])
        published = self.s.poll(job["id"])
        self.assertEqual(published["status"], "published")
        self.s.sync_back(job["id"], 7)
        duplicate, created = self.s.enqueue(self.p["id"], 8)
        self.assertFalse(created)
        self.assertEqual(duplicate["id"], job["id"])

    def test_unknown_outcome_reconciles_without_duplicate(self):
        result = run_scenario("timeout")
        self.assertEqual(result["remote_objects"], 1)
        self.assertEqual(result["final"]["attempts"], 1)
        self.assertTrue(any(e["event"] == "reconciled" for e in result["final"]["events"]))

    def test_retry_survives_restart_and_obeys_due_time(self):
        job, _ = self.s.enqueue(self.p["id"], 7)
        waiting = self.s.work(job["id"], "temporary", 100)
        self.s.close()
        self.s = PublicationService(self.path)
        early = self.s.work(job["id"], now=101)
        self.assertEqual(early["attempts"], 1)
        resumed = self.s.work(job["id"], now=waiting["next_retry_at"])
        self.assertEqual(resumed["status"], "submitted")
        self.assertEqual(resumed["attempts"], 2)

    def test_retry_budget_and_nonretryable_rejection(self):
        job, _ = self.s.enqueue(self.p["id"], 7)
        for now in (100, 200, 400):
            end = self.s.work(job["id"], "temporary", now)
        self.assertEqual(end["status"], "manual_review")
        self.assertIsNone(end["next_retry_at"])
        self.assertEqual(run_scenario("rejected")["final"]["status"], "rejected")

    def test_unconfirmed_price_and_conflicting_address_block(self):
        result = run_scenario("validation")
        self.assertEqual(result["http_status"], 422)
        self.assertIn("facts.price_rub", result["error"]["fields"])
        self.assertIn("facts.address", result["error"]["fields"])
        self.assertEqual(result["remote_objects"], 0)

    def test_sync_conflict_does_not_resubmit(self):
        result = run_scenario("conflict")
        self.assertEqual(result["remote_objects"], 1)
        self.assertEqual(result["final"]["sync_status"], "synced")
        self.assertTrue(any(e["event"] == "gp_os_sync_conflict" for e in result["final"]["events"]))

    def test_changed_content_requires_approval_and_stale_job_stops(self):
        job, _ = self.s.enqueue(self.p["id"], 7)
        facts = copy.deepcopy(self.p["facts"])
        facts["price_rub"]["value"] = 20000000
        self.s.patch_property(self.p["id"], 7, {"facts": facts})
        self.assertEqual(self.s.work(job["id"])["status"], "superseded")
        with self.assertRaises(DomainError) as exc:
            self.s.enqueue(self.p["id"], 8)
        self.assertIn("approval", exc.exception.fields)

    def test_update_preserves_remote_identity(self):
        result = run_scenario("update")
        self.assertEqual(result["remote_objects"], 1)
        self.assertEqual(result["final"]["status"], "published")
        self.assertEqual(result["frames"][0]["job"]["id"], result["final"]["external_id"])

    def test_competing_enqueue_creates_one_job(self):
        def enqueue():
            s = PublicationService(self.path)
            try:
                return s.enqueue(self.p["id"], 7)[1]
            finally:
                s.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: enqueue(), range(2)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = create_server(0, Path(cls.temp.name) / "http.sqlite")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def test_auth_csrf_and_real_http_round_trip(self):
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with self.assertRaises(urllib.error.HTTPError) as exc:
            client.open(self.base + "/api/properties/" + sample_property()["id"])
        self.assertEqual(exc.exception.code, 401)
        exc.exception.close()
        session = json.load(client.open(self.base + "/api/session"))
        data = json.dumps({"propertyId": sample_property()["id"], "expectedVersion": 7}).encode()
        with self.assertRaises(urllib.error.HTTPError) as exc:
            client.open(urllib.request.Request(self.base + "/api/publications", data=data))
        self.assertEqual(exc.exception.code, 403)
        exc.exception.close()
        request = urllib.request.Request(self.base + "/api/publications", data=data, headers={"X-CSRF-Token": session["csrf"], "Content-Type": "application/json"})
        response = client.open(request)
        self.assertEqual(response.status, 202)
        self.assertEqual(json.load(response)["status"], "queued")
        prop = json.load(client.open(self.base + "/api/properties/" + sample_property()["id"]))
        self.assertEqual(prop["version"], 7)
        patch = json.dumps({"expectedVersion": 999, "changes": {"advertising": {}}}).encode()
        with self.assertRaises(urllib.error.HTTPError) as exc:
            client.open(urllib.request.Request(self.base + "/api/properties/" + prop["id"], data=patch, method="PATCH", headers={"X-CSRF-Token": session["csrf"]}))
        self.assertEqual(exc.exception.code, 409)
        exc.exception.close()

    def test_unknown_scenario_is_400_not_server_crash(self):
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        session = json.load(client.open(self.base + "/api/session"))
        with self.assertRaises(urllib.error.HTTPError) as exc:
            client.open(urllib.request.Request(self.base + "/api/demo", data=b'{"scenario":"bad"}', headers={"X-CSRF-Token": session["csrf"]}))
        self.assertEqual(exc.exception.code, 400)
        exc.exception.close()


if __name__ == "__main__":
    unittest.main()
