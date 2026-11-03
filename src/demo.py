"""Repeatable scenarios executed by the real local domain code."""
import copy
import tempfile
from pathlib import Path
from src.publication import PublicationService, DomainError, content_hash_for_approval
from tools.fixtures import sample_property

SCENARIOS = ("success", "validation", "retry", "timeout", "conflict", "update", "rejected")


def run_scenario(name, path=None):
    if name not in SCENARIOS:
        raise DomainError("UNKNOWN_SCENARIO", "Неизвестный сценарий", 400)
    if path is None:
        with tempfile.TemporaryDirectory() as temp:
            return run_scenario(name, Path(temp) / "demo.sqlite")
    service = PublicationService(path)
    p = sample_property()
    if name == "validation":
        p["facts"]["price_rub"]["verification"] = "unconfirmed"
        p["facts"]["address"]["conflict"] = True
    service.seed(p)
    frames = []
    try:
        if name == "conflict":
            service.patch_property(p["id"], 7, {"advertising": {"note": "Учебное параллельное изменение"}})
            try:
                service.enqueue(p["id"], 7)
            except DomainError as exc:
                frames.append({"step": "stale_enqueue", "http_status": exc.http_status, "error": exc.as_dict()})
        job, created = service.enqueue(p["id"], service.get_property(p["id"])["version"])
        jid = job["id"]
        frames.append({"step": "enqueue", "created": created, "job": copy.deepcopy(job)})
        again, created = service.enqueue(p["id"], service.get_property(p["id"])["version"])
        frames.append({"step": "repeat_enqueue", "created": created, "same_job": again["id"] == jid})
        behavior = {"retry": "temporary", "timeout": "timeout_after_accept", "rejected": "rejected"}.get(name, "success")
        job = service.work(jid, behavior, now=1000)
        frames.append({"step": "send", "job": copy.deepcopy(job)})
        if name in ("retry", "timeout"):
            job = service.work(jid, "success", now=job["next_retry_at"])
            frames.append({"step": "retry_or_reconcile", "job": copy.deepcopy(job)})
        job = service.poll(jid)
        frames.append({"step": "moderation_report", "job": copy.deepcopy(job)})
        if name == "conflict":
            service.sync_back(jid, 7)
            frames.append({"step": "sync_409", "job": service.get_job(jid)})
        job = service.sync_back(jid, service.get_property(p["id"])["version"])
        frames.append({"step": "gp_os_result", "job": copy.deepcopy(job)})
        if name == "update":
            latest = service.get_property(p["id"])
            latest["facts"]["price_rub"]["value"] = 23500000
            latest["approval"]["for_content_hash"] = content_hash_for_approval(latest)
            latest = service.patch_property(p["id"], latest["version"], {k: latest[k] for k in ("facts", "approval")})
            job, _ = service.enqueue(p["id"], latest["version"])
            service.work(jid, now=2000)
            service.poll(jid)
            job = service.sync_back(jid, service.get_property(p["id"])["version"])
            frames.append({"step": "update_same_external_id", "job": copy.deepcopy(job)})
        result = {"scenario": name, "educational": True, "adapter": "fake; no Avito calls", "frames": frames,
                  "final": job, "property": service.get_property(p["id"]), "remote_objects": service.db.execute("SELECT COUNT(*) FROM fake_remote").fetchone()[0]}
    except DomainError as exc:
        result = {"scenario": name, "educational": True, "adapter": "fake; no Avito calls", "frames": frames,
                  "error": exc.as_dict(), "http_status": exc.http_status, "remote_objects": 0}
    finally:
        service.close()
    return result
