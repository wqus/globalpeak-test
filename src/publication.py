"""A durable local publication queue with a deliberately fake external adapter."""
import copy
import hashlib
import json
import math
import random
import sqlite3
from uuid import UUID


class DomainError(Exception):
    def __init__(self, code, message, http_status=422, fields=None):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status
        self.fields = fields or []

    def as_dict(self):
        return {"code": self.code, "message": self.message, "fields": self.fields}


def content_hash(prop):
    # Operational advertising status/version must not generate a new advertisement.
    content = {k: prop.get(k) for k in ("facts", "description", "images", "approval")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate(prop):
    errors = []
    try:
        UUID(prop["id"])
    except (ValueError, KeyError, TypeError):
        errors.append("id")
    facts = prop.get("facts", {})
    for field in ("address", "area_m2", "price_rub", "category", "deal_type"):
        fact = facts.get(field, {})
        if not fact.get("value") or fact.get("verification") != "confirmed" or fact.get("conflict"):
            errors.append("facts." + field)
    for field in ("area_m2", "price_rub"):
        value = facts.get(field, {}).get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            errors.append("facts." + field)
    if facts.get("category", {}).get("value") != "commercial" or facts.get("deal_type", {}).get("value") != "sale":
        errors.append("category_mapping")
    if not prop.get("description", "").strip():
        errors.append("description")
    images = prop.get("images", [])
    if not images or any(not im.get("rights_confirmed") or not im.get("asset_id") for im in images):
        errors.append("images")
    if not prop.get("approval", {}).get("approved") or prop.get("approval", {}).get("for_content_hash") != content_hash_for_approval(prop):
        errors.append("approval")
    if errors:
        raise DomainError("VALIDATION_FAILED", "Карточка не готова к публикации. Исправьте поля и подтвердите новую версию.", fields=sorted(set(errors)))


def content_hash_for_approval(prop):
    return hashlib.sha256(json.dumps({k: prop.get(k) for k in ("facts", "description", "images")}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PublicationService:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS properties(id TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS publications(
          id TEXT PRIMARY KEY, property_id TEXT NOT NULL, channel TEXT NOT NULL, account TEXT NOT NULL,
          content_hash TEXT NOT NULL, source_version INTEGER NOT NULL, payload TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_retry_at REAL,
          external_id TEXT, url TEXT, error TEXT, sync_status TEXT NOT NULL DEFAULT 'pending',
          UNIQUE(property_id, channel, account));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, publication_id TEXT, event TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fake_remote(id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, status TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def seed(self, prop):
        with self.db:
            self.db.execute("INSERT INTO properties VALUES (?,?,?)", (prop["id"], prop["version"], json.dumps(prop, ensure_ascii=False)))

    def get_property(self, pid):
        row = self.db.execute("SELECT payload FROM properties WHERE id=?", (pid,)).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "Объект не найден", 404)
        return json.loads(row[0])

    def patch_property(self, pid, expected_version, changes):
        # This is the proposed mock GP OS contract, not production implementation.
        with self.db:
            prop = self.get_property(pid)
            if prop["version"] != expected_version:
                raise DomainError("VERSION_CONFLICT", "Карточка изменена: перечитайте её и повторите согласование.", 409)
            allowed = {"facts", "description", "images", "approval", "advertising"}
            if set(changes) - allowed:
                raise DomainError("INVALID_PATCH", "Неизвестные поля PATCH", 400)
            prop.update(copy.deepcopy(changes))
            prop["version"] += 1
            changed = self.db.execute("UPDATE properties SET version=?,payload=? WHERE id=? AND version=?", (prop["version"], json.dumps(prop, ensure_ascii=False), pid, expected_version)).rowcount
            if changed != 1:
                raise DomainError("VERSION_CONFLICT", "Конкурирующее изменение карточки", 409)
        return prop

    def enqueue(self, pid, expected_version):
        # Serialize competing workers/requests around read + compare + upsert.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prop = self.get_property(pid)
            if prop["version"] != expected_version:
                raise DomainError("VERSION_CONFLICT", "Ожидаемая версия устарела", 409)
            validate(prop)
            digest = content_hash(prop)
            existing = self.db.execute("SELECT * FROM publications WHERE property_id=? AND channel='avito' AND account='demo-account'", (pid,)).fetchone()
            if existing and existing["content_hash"] == digest:
                self.db.commit()
                return self.get_job(existing["id"]), False
            if existing and existing["status"] in ("queued", "retry_wait", "submitted", "reconciling"):
                raise DomainError("IN_FLIGHT", "Сначала сверить предыдущую отправку; новый объект на площадке не создаётся.", 409)
            jid = existing["id"] if existing else "gp-" + pid
            external_id = existing["external_id"] if existing else None
            self.db.execute("""INSERT INTO publications(id,property_id,channel,account,content_hash,source_version,payload,status,external_id)
                VALUES (?,?,'avito','demo-account',?,?,?,'queued',?)
                ON CONFLICT(property_id,channel,account) DO UPDATE SET content_hash=excluded.content_hash,
                source_version=excluded.source_version,payload=excluded.payload,status='queued',attempts=0,
                next_retry_at=NULL,error=NULL,sync_status='pending'""", (jid, pid, digest, prop["version"], json.dumps(prop, ensure_ascii=False), external_id))
            self._audit(jid, "queued", {"content_hash": digest, "source_version": prop["version"], "operation": "update" if external_id else "create"})
            self.db.commit()
            return self.get_job(jid), True
        except Exception:
            self.db.rollback()
            raise

    def _audit(self, jid, event, detail):
        self.db.execute("INSERT INTO audit(publication_id,event,detail) VALUES (?,?,?)", (jid, event, json.dumps(detail, ensure_ascii=False)))

    def get_job(self, jid):
        row = self.db.execute("SELECT * FROM publications WHERE id=?", (jid,)).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "Задача не найдена", 404)
        job = dict(row)
        job["error"] = json.loads(job["error"]) if job["error"] else None
        job.pop("payload")
        job["events"] = [{"event": r["event"], "detail": json.loads(r["detail"])} for r in self.db.execute("SELECT * FROM audit WHERE publication_id=? ORDER BY id", (jid,))]
        return job

    def work(self, jid, behavior="success", now=0):
        """Advance one durable step; no network calls. Retry time is explicit."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            job = self.get_job(jid)
            if job["status"] not in ("queued", "retry_wait", "reconciling"):
                self.db.commit()
                return job
            if job["next_retry_at"] is not None and now < job["next_retry_at"]:
                self.db.commit()
                return job
            prop = self.get_property(job["property_id"])
            if content_hash(prop) != job["content_hash"]:
                self.db.execute("UPDATE publications SET status='superseded',error=? WHERE id=?", (json.dumps({"code": "CONTENT_CHANGED", "message": "Карточка изменилась до отправки"}), jid))
                self._audit(jid, "superseded", {})
            else:
                # Reconcile by stable external key before retrying an uncertain send.
                remote = self.db.execute("SELECT * FROM fake_remote WHERE id=?", (jid,)).fetchone()
                if job["status"] == "reconciling" and remote:
                    self.db.execute("UPDATE publications SET status='submitted',external_id=?,error=NULL,next_retry_at=NULL WHERE id=?", (jid, jid))
                    self._audit(jid, "reconciled", {"remote_objects": 1, "no_resend": True})
                else:
                    attempts = job["attempts"] + 1
                    self.db.execute("UPDATE publications SET attempts=? WHERE id=?", (attempts, jid))
                    if behavior == "rejected":
                        self.db.execute("UPDATE publications SET status='rejected',error=? WHERE id=?", (json.dumps({"code": "CATEGORY_REJECTED", "message": "Учебный отказ категории: проверьте mapping", "retryable": False}), jid))
                        self._audit(jid, "rejected", {"retryable": False})
                    elif behavior in ("temporary", "timeout_after_accept"):
                        if behavior == "timeout_after_accept":
                            self._remote_accept(jid, job["content_hash"])
                        final = attempts >= 3
                        status = "manual_review" if final else "reconciling" if behavior == "timeout_after_accept" else "retry_wait"
                        delay = 30 * (2 ** (attempts - 1)) + random.Random(jid + str(attempts)).uniform(0, 5)
                        error = {"code": "UNKNOWN_OUTCOME" if behavior == "timeout_after_accept" else "REMOTE_UNAVAILABLE", "message": "Нужна сверка по постоянному Id" if behavior == "timeout_after_accept" else "Временная ошибка тестового адаптера", "retryable": not final}
                        self.db.execute("UPDATE publications SET status=?,next_retry_at=?,error=? WHERE id=?", (status, None if final else now + delay, json.dumps(error, ensure_ascii=False), jid))
                        self._audit(jid, status, {"attempt": attempts, "delay_seconds": round(delay, 2), "error": error})
                    else:
                        self._remote_accept(jid, job["content_hash"])
                        self.db.execute("UPDATE publications SET status='submitted',external_id=?,error=NULL,next_retry_at=NULL WHERE id=?", (jid, jid))
                        self._audit(jid, "submitted", {"adapter": "fake", "published": False})
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.get_job(jid)

    def _remote_accept(self, jid, digest):
        self.db.execute("INSERT INTO fake_remote VALUES (?,?,'processing') ON CONFLICT(id) DO UPDATE SET content_hash=excluded.content_hash,status='processing'", (jid, digest))

    def poll(self, jid):
        with self.db:
            job = self.get_job(jid)
            if job["status"] == "submitted":
                self.db.execute("UPDATE fake_remote SET status='published' WHERE id=?", (jid,))
                url = "https://example.invalid/avito-demo/" + jid
                self.db.execute("UPDATE publications SET status='published',url=? WHERE id=?", (url, jid))
                self._audit(jid, "published", {"source": "fake moderation report", "url_is_fake": True})
        return self.get_job(jid)

    def sync_back(self, jid, expected_version):
        job = self.get_job(jid)
        result = {k: job[k] for k in ("id", "status", "url", "error", "content_hash", "source_version")}
        try:
            prop = self.get_property(job["property_id"])
            advertising = dict(prop.get("advertising", {}))
            advertising["avito"] = result
            updated = self.patch_property(prop["id"], expected_version, {"advertising": advertising})
            with self.db:
                self.db.execute("UPDATE publications SET sync_status='synced' WHERE id=?", (jid,))
                self._audit(jid, "gp_os_synced", {"version": updated["version"]})
        except DomainError as exc:
            if exc.http_status != 409:
                raise
            with self.db:
                self.db.execute("UPDATE publications SET sync_status='conflict' WHERE id=?", (jid,))
                self._audit(jid, "gp_os_sync_conflict", {"action": "GET latest; merge advertising only; retry PATCH"})
        return self.get_job(jid)
