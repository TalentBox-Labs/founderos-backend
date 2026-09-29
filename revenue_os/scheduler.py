"""Heartbeat scheduler — recurring autonomous jobs, no external broker needed.

Runs inside the FastAPI process via asyncio, so it works identically on a
laptop (SQLite, `uvicorn runner_api:app`) and in production. Each job run is
persisted to `heartbeat_runs` and every action taken is written to the
`agent_action_log` audit trail.

ACP-1: commercial jobs require explicit autonomous tenant resolution
(allowlist ∩ ACTIVE Organization, or ACTIVE Organization). No global
Contact/Deal commercial queries.

Configuration (environment variables):
    HEARTBEAT_ENABLED=1|0            master switch (default 1)
    HEARTBEAT_LEAD_SCORING_SEC       default 3600  (hourly)
    HEARTBEAT_DEAL_RISK_SEC          default 21600 (every 6 hours)
    HEARTBEAT_METRICS_SNAPSHOT_SEC   default 3600  (hourly)
    ACP1_AUTONOMOUS_ORGANIZATION_IDS optional comma-separated org UUID allowlist
    HEARTBEAT_ORGANIZATION_IDS       alias for the allowlist
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from revenue_os.database import SessionLocal
from revenue_os.models.automation_state import HeartbeatRun
from revenue_os.services.activity_log import log_agent_action
from revenue_os.services.acp1_autonomous_boundary import (
    BLOCKED_MISSING_TENANT,
    assert_contact_org,
    log_autonomous_blocked,
    org_uuid_or_none,
    resolve_autonomous_organization_ids,
)

logger = logging.getLogger(__name__)

ACTOR = "heartbeat"


def _resolve_orgs_or_block(db, *, action_type: str) -> tuple[list[str] | None, dict[str, Any] | None]:
    orgs = resolve_autonomous_organization_ids(db)
    if not orgs:
        blocked = log_autonomous_blocked(
            actor=ACTOR,
            action_type=action_type,
            reason=BLOCKED_MISSING_TENANT,
            detail={"note": "No ACTIVE Organization (or allowlist∩ACTIVE) for autonomous work"},
        )
        return None, blocked
    return orgs, None


# ── Job implementations ──────────────────────────────────────────────────────


def job_score_new_leads() -> dict[str, Any]:
    """Score contacts with no lead score — per authorized organization only.

    ACP-2: each contact score is governed work (propose → ACP-1 evaluate → execute).
    ACP-3: pause/kill gate before discovery.
    """
    from revenue_os.models.contact import Contact
    from revenue_os.services.acp2_work_contract import WORK_LEAD_SCORE, WorkState
    from revenue_os.services.acp3_durable_runtime import gate_new_mutating_work
    from revenue_os.services.acp4_production_runtime import orchestrate_claimed
    from revenue_os.services.lead_scoring_service import score_contact

    gate = gate_new_mutating_work(actor=ACTOR)
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        orgs, blocked = _resolve_orgs_or_block(db, action_type="lead_score_blocked")
        if blocked is not None:
            return blocked

        total_considered = 0
        total_scored = 0
        blocked_n = 0
        per_org: list[dict[str, Any]] = []
        for organization_id in orgs or []:
            org_uuid = org_uuid_or_none(organization_id)
            if org_uuid is None:
                continue
            contacts = (
                db.query(Contact)
                .filter(Contact.organization_id == org_uuid)
                .filter((Contact.lead_score == None) | (Contact.lead_score == 0))  # noqa: E711
                .limit(25)
                .all()
            )
            scored = 0
            for contact in contacts:
                total_considered += 1
                if not assert_contact_org(contact, organization_id):
                    log_autonomous_blocked(
                        actor=ACTOR,
                        action_type="lead_score_blocked",
                        reason="tenant_entity_mismatch",
                        organization_id=organization_id,
                        target_type="contact",
                        target_id=str(contact.id),
                    )
                    blocked_n += 1
                    continue

                def _exec(work, c=contact):  # noqa: ANN001
                    payload = score_contact(db, c)
                    log_agent_action(
                        actor=ACTOR,
                        action_type="lead_scored",
                        target_type="contact",
                        target_id=str(c.id),
                        organization_id=organization_id,
                        detail={
                            "score": payload["score"],
                            "status_changed": False,
                            "work_id": work.work_id,
                        },
                    )
                    return {"score": payload["score"]}

                work = orchestrate_claimed(
                    db,
                    work_kind=WORK_LEAD_SCORE,
                    organization_id=organization_id,
                    source="scheduler",
                    actor=ACTOR,
                    executor=_exec,
                    target_type="contact",
                    target_id=str(contact.id),
                    logical_key="unscored",
                )
                if work.state == WorkState.SUCCEEDED and not work.result.get("deduplicated"):
                    scored += 1
                elif work.state != WorkState.SUCCEEDED:
                    blocked_n += 1
            total_scored += scored
            per_org.append(
                {
                    "organization_id": organization_id,
                    "contacts_considered": len(contacts),
                    "contacts_scored": scored,
                }
            )
        db.commit()
        return {
            "ok": True,
            "contacts_considered": total_considered,
            "contacts_scored": total_scored,
            "blocked_or_skipped": blocked_n,
            "organizations": per_org,
            "orchestrated": True,
        }
    finally:
        db.close()


def job_scan_follow_up_eligibility() -> dict[str, Any]:
    """Propose governed follow-ups per authorized org (human approval still required).

    ACP-2: proposal is orchestrated AUTONOMOUS; send remains HUMAN_REQUIRED via ApprovalRequest.
    ACP-3: pause/kill gate before discovery.
    """
    from revenue_os.services.acp2_work_contract import WORK_FOLLOW_UP_PROPOSE, WorkState
    from revenue_os.services.acp3_durable_runtime import gate_new_mutating_work
    from revenue_os.services.acp4_production_runtime import orchestrate_claimed
    from revenue_os.services.follow_up_eligibility import scan_eligible_follow_ups
    from revenue_os.services.revenue_orchestration_service import run_follow_up_proposal_scheduled

    gate = gate_new_mutating_work(actor=ACTOR)
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        orgs, blocked = _resolve_orgs_or_block(db, action_type="followup_scan_blocked")
        if blocked is not None:
            return blocked

        proposed = 0
        skipped = 0
        candidates_total = 0
        for organization_id in orgs or []:
            candidates = scan_eligible_follow_ups(db, organization_id=organization_id)
            candidates_total += len(candidates)
            for item in candidates:
                if str(item.get("organization_id")) != str(organization_id):
                    skipped += 1
                    log_autonomous_blocked(
                        actor=ACTOR,
                        action_type="followup_proposal_blocked",
                        reason="tenant_entity_mismatch",
                        organization_id=organization_id,
                        target_type="contact",
                        target_id=item.get("contact_id"),
                    )
                    continue

                def _exec(work, it=item, oid=organization_id):  # noqa: ANN001
                    result = run_follow_up_proposal_scheduled(
                        db, oid, it["contact_id"]
                    )
                    if result.get("ok"):
                        log_agent_action(
                            actor=ACTOR,
                            action_type="followup_proposal_scheduled",
                            target_type="contact",
                            target_id=it["contact_id"],
                            organization_id=oid,
                            detail={
                                "cadence_step": it.get("cadence_step"),
                                "approval_id": result.get("approval_id"),
                                "work_id": work.work_id,
                            },
                        )
                        return {
                            "ok": True,
                            "approval_id": result.get("approval_id"),
                            "waiting_human": True,
                            "escalation_reason": "follow_up_send_requires_approval",
                        }
                    return {"blocked": True, "blocked_reason": result.get("reason") or "not_eligible"}

                work = orchestrate_claimed(
                    db,
                    work_kind=WORK_FOLLOW_UP_PROPOSE,
                    organization_id=organization_id,
                    source="scheduler",
                    actor=ACTOR,
                    executor=_exec,
                    target_type="contact",
                    target_id=item["contact_id"],
                    logical_key=str(item.get("cadence_step") or "step"),
                )
                if work.state in (WorkState.SUCCEEDED, WorkState.WAITING_HUMAN):
                    proposed += 1
                else:
                    skipped += 1
        db.commit()
        return {
            "ok": True,
            "candidates": candidates_total,
            "proposals_filed": proposed,
            "skipped": skipped,
            "organizations": list(orgs or []),
            "orchestrated": True,
        }
    finally:
        db.close()


def job_check_deals_at_risk() -> dict[str, Any]:
    """Detect at-risk deals per authorized organization and emit events.

    ACP-3: each deal flag is governed WorkItem (observational emit + provenance).
    """
    from revenue_os.automation.events import emit_deal_at_risk
    from revenue_os.services.acp2_work_contract import WORK_DEAL_AT_RISK, WorkState
    from revenue_os.services.acp3_durable_runtime import gate_new_mutating_work
    from revenue_os.services.acp4_production_runtime import orchestrate_claimed
    from revenue_os.services.deal_automation_service import get_deals_at_risk

    gate = gate_new_mutating_work(actor=ACTOR)
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        orgs, blocked = _resolve_orgs_or_block(db, action_type="deal_at_risk_blocked")
        if blocked is not None:
            return blocked

        flagged = 0
        day_key = datetime.now(timezone.utc).strftime("%Y%m%d")
        for organization_id in orgs or []:
            at_risk = get_deals_at_risk(db, organization_id=organization_id)
            for deal in at_risk:
                deal_id = str(deal.get("deal_id") or deal.get("id") or "")

                def _exec(work, d=deal, did=deal_id, oid=organization_id):  # noqa: ANN001
                    try:
                        emit_deal_at_risk(
                            deal_id=did,
                            risk_score=int(d.get("risk_score", 0)),
                            days_overdue=int(d.get("days_overdue", 0)),
                        )
                    except Exception as e:
                        logger.warning(f"emit_deal_at_risk failed for {did}: {e}")
                    log_agent_action(
                        actor=ACTOR,
                        action_type="deal_at_risk_flagged",
                        target_type="deal",
                        target_id=did,
                        organization_id=oid,
                        detail={**d, "work_id": work.work_id},
                    )
                    return {"ok": True, "deal_id": did}

                work = orchestrate_claimed(
                    db,
                    work_kind=WORK_DEAL_AT_RISK,
                    organization_id=organization_id,
                    source="scheduler",
                    actor=ACTOR,
                    executor=_exec,
                    target_type="deal",
                    target_id=deal_id,
                    logical_key=day_key,
                )
                if work.state == WorkState.SUCCEEDED:
                    flagged += 1
        return {
            "ok": True,
            "deals_at_risk": flagged,
            "organizations": list(orgs or []),
            "orchestrated": True,
        }
    finally:
        db.close()


def job_hermes_goal_check() -> dict[str, Any]:
    """Run Hermes cycles only when autonomous tenants resolve; org injected into steps."""
    from revenue_os.services.acp3_durable_runtime import gate_new_mutating_work
    from revenue_os.services.hermes_planner import check_all_active_goals

    gate = gate_new_mutating_work(actor=ACTOR)
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        orgs, blocked = _resolve_orgs_or_block(db, action_type="hermes_goal_blocked")
        if blocked is not None:
            return blocked
    finally:
        db.close()

    return check_all_active_goals(organization_ids=list(orgs or []))


def job_sync_gmail_inbox() -> dict[str, Any]:
    """Pull Gmail inbox; match contacts only within authorized organizations.

    ACP-3: per-org sync is a governed WorkItem (Activity writes are org-scoped).
    Beta freeze: isolate this job only — do not disable the full heartbeat.
    """
    from revenue_os.services.gmail_beta_freeze import (
        gmail_beta_frozen,
        gmail_beta_frozen_detail,
    )

    if gmail_beta_frozen():
        return {
            "ok": False,
            "reason": gmail_beta_frozen_detail(),
            "gmail_beta_enabled": False,
            "executed": False,
        }

    from revenue_os.integrations.gmail_sync import sync_inbox
    from revenue_os.services.acp2_work_contract import WORK_GMAIL_INBOUND, WorkState
    from revenue_os.services.acp3_durable_runtime import gate_new_mutating_work
    from revenue_os.services.acp4_production_runtime import orchestrate_claimed

    gate = gate_new_mutating_work(actor=ACTOR)
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        orgs, blocked = _resolve_orgs_or_block(db, action_type="gmail_sync_blocked")
        if blocked is not None:
            return blocked
    finally:
        db.close()

    hour_key = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    results = []
    db = SessionLocal()
    try:
        for organization_id in orgs or []:

            def _exec(_work, oid=organization_id):  # noqa: ANN001
                return sync_inbox(organization_ids=[oid])

            work = orchestrate_claimed(
                db,
                work_kind=WORK_GMAIL_INBOUND,
                organization_id=organization_id,
                source="scheduler",
                actor=ACTOR,
                executor=_exec,
                target_type="organization",
                target_id=organization_id,
                logical_key=hour_key,
            )
            results.append(
                {
                    "organization_id": organization_id,
                    "state": work.state.value,
                    "result": work.result,
                }
            )
        db.commit()
    finally:
        db.close()

    return {
        "ok": True,
        "organizations": list(orgs or []),
        "results": results,
        "orchestrated": True,
        "succeeded": sum(1 for r in results if r["state"] == WorkState.SUCCEEDED.value),
    }


def job_run_marketing_cycle() -> dict[str, Any]:
    """The AI CMO's daily cycle — market research, persona refresh, content
    strategy, and drafted campaigns, all in one pass, per authorized org.

    ACP-3: per-org run is a governed WorkItem, same shape as gmail sync.
    """
    from revenue_os.services.acp2_work_contract import WORK_MARKETING_CYCLE, WorkState
    from revenue_os.services.acp3_durable_runtime import gate_new_mutating_work
    from revenue_os.services.acp4_production_runtime import orchestrate_claimed
    from revenue_os.services.marketing_orchestrator import run_marketing_cycle

    gate = gate_new_mutating_work(actor=ACTOR)
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        orgs, blocked = _resolve_orgs_or_block(db, action_type="marketing_cycle_blocked")
        if blocked is not None:
            return blocked
    finally:
        db.close()

    day_key = datetime.now(timezone.utc).strftime("%Y%m%d")
    results = []
    db = SessionLocal()
    try:
        for organization_id in orgs or []:

            def _exec(_work, oid=organization_id):  # noqa: ANN001
                return run_marketing_cycle(organization_id=oid)

            work = orchestrate_claimed(
                db,
                work_kind=WORK_MARKETING_CYCLE,
                organization_id=organization_id,
                source="scheduler",
                actor=ACTOR,
                executor=_exec,
                target_type="organization",
                target_id=organization_id,
                logical_key=day_key,
            )
            results.append(
                {
                    "organization_id": organization_id,
                    "state": work.state.value,
                    "result": work.result,
                }
            )
        db.commit()
    finally:
        db.close()

    return {
        "ok": True,
        "organizations": list(orgs or []),
        "results": results,
        "orchestrated": True,
        "succeeded": sum(1 for r in results if r["state"] == WorkState.SUCCEEDED.value),
    }


def job_snapshot_pipeline_metrics() -> dict[str, Any]:
    """Persist pipeline-health snapshots per authorized organization.

    Observational/read-aggregate — still routed through ACP-2 for provenance
    and pause/kill consistency (ACP-3 coverage).
    """
    from revenue_os.analytics.core import AnalyticsEngine, AnalyticsMetric, MetricType
    from revenue_os.services.acp2_orchestration import orchestrate
    from revenue_os.services.acp2_work_contract import WORK_METRICS_SNAPSHOT, WorkState
    from revenue_os.services.acp3_durable_runtime import gate_new_mutating_work
    from revenue_os.services.deal_automation_service import get_pipeline_health

    # Metrics are observational; pause still gates scheduler initiation for consistency.
    gate = gate_new_mutating_work(actor=ACTOR)
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        orgs, blocked = _resolve_orgs_or_block(db, action_type="metrics_snapshot_blocked")
        if blocked is not None:
            return blocked

        hour_key = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        recorded_all: dict[str, Any] = {"ok": True, "organizations": {}, "orchestrated": True}
        for organization_id in orgs or []:

            def _exec(_work, oid=organization_id):  # noqa: ANN001
                health = get_pipeline_health(db, organization_id=oid)
                snapshot_metrics = {
                    "pipeline_total_value": (
                        MetricType.REVENUE, "$", "Total open pipeline value",
                        float(health.get("total_pipeline_value", 0) or 0),
                    ),
                    "pipeline_deal_count": (
                        MetricType.COUNT, "deals", "Number of open deals",
                        float(health.get("total_deals", 0) or 0),
                    ),
                    "pipeline_weighted_forecast": (
                        MetricType.REVENUE, "$", "Probability-weighted forecast",
                        float(health.get("weighted_forecast", 0) or 0),
                    ),
                }
                recorded: dict[str, Any] = {}
                for metric_id, (mtype, unit, description, value) in snapshot_metrics.items():
                    if AnalyticsEngine.get_metric(metric_id) is None:
                        AnalyticsEngine.register_metric(AnalyticsMetric(
                            id=metric_id,
                            name=metric_id.replace("_", " ").title(),
                            metric_type=mtype,
                            calculation="heartbeat snapshot",
                            unit=unit,
                            description=description,
                        ))
                    AnalyticsEngine.record_data_point(
                        metric_id, value, dimension=f"org:{oid}"
                    )
                    recorded[metric_id] = value
                log_agent_action(
                    actor=ACTOR,
                    action_type="metrics_snapshot",
                    target_type="pipeline",
                    organization_id=oid,
                    detail=recorded,
                )
                return {"ok": True, "metrics": recorded}

            work = orchestrate(
                db,
                work_kind=WORK_METRICS_SNAPSHOT,
                organization_id=organization_id,
                source="scheduler",
                actor=ACTOR,
                executor=_exec,
                target_type="pipeline",
                target_id=organization_id,
                logical_key=hour_key,
            )
            if work.state == WorkState.SUCCEEDED:
                recorded_all["organizations"][organization_id] = work.result.get("metrics") or {}
        return recorded_all
    finally:
        db.close()


def job_acp3_reconcile() -> dict[str, Any]:
    """ACP-3 reconciliation / bounded resume pass across authorized tenants."""
    from revenue_os.services.acp3_durable_runtime import job_reconcile_autonomous_work

    return job_reconcile_autonomous_work()


# ── Scheduler core ───────────────────────────────────────────────────────────


@dataclass
class HeartbeatJob:
    name: str
    func: Callable[[], dict[str, Any]]
    interval_seconds: int
    last_run_at: datetime | None = None
    last_status: str | None = None
    runs: int = 0


class HeartbeatScheduler:
    """Asyncio-based recurring job runner with persisted run history."""

    def __init__(self) -> None:
        self.jobs: dict[str, HeartbeatJob] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def register(self, name: str, func: Callable[[], dict[str, Any]], interval_seconds: int) -> None:
        self.jobs[name] = HeartbeatJob(name=name, func=func, interval_seconds=interval_seconds)

    def run_job_now(self, name: str) -> dict[str, Any]:
        """Execute one job synchronously, recording the run. Raises KeyError if unknown."""
        job = self.jobs[name]
        db = SessionLocal()
        run = HeartbeatRun(job_name=name, status="running")
        try:
            db.add(run)
            db.commit()
            db.refresh(run)
        except Exception as e:
            logger.warning(f"Could not persist heartbeat run start: {e}")
        finally:
            db.close()

        job.last_run_at = datetime.now(timezone.utc)
        job.runs += 1
        try:
            result = job.func() or {}
            job.last_status = "completed"
            self._finish_run(run.id, "completed", result=result)
            logger.info(f"Heartbeat job completed: {name} -> {result}")
            return {"job": name, "status": "completed", "result": result}
        except Exception as e:
            job.last_status = "failed"
            self._finish_run(run.id, "failed", error=str(e))
            logger.error(f"Heartbeat job failed: {name}: {e}")
            return {"job": name, "status": "failed", "error": str(e)}

    @staticmethod
    def _finish_run(run_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
        try:
            db = SessionLocal()
            try:
                run = db.get(HeartbeatRun, run_id)
                if run is not None:
                    run.status = status
                    run.finished_at = datetime.now(timezone.utc)
                    run.result = result
                    run.error = error
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Could not persist heartbeat run finish: {e}")

    async def _loop(self) -> None:
        logger.info(f"Heartbeat started with {len(self.jobs)} jobs: {list(self.jobs)}")
        # Stagger: wait a moment after boot before the first pass.
        await asyncio.sleep(10)
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            for job in self.jobs.values():
                due = (
                    job.last_run_at is None
                    or (now - job.last_run_at).total_seconds() >= job.interval_seconds
                )
                if due:
                    await asyncio.to_thread(self.run_job_now, job.name)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
        logger.info("Heartbeat stopped")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            # Python 3.12+: get_event_loop() raises when no loop is set for
            # the thread (e.g. after Starlette TestClient tears down anyio).
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError("event loop is closed")
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            self._task = loop.create_task(self._loop())

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "running": self._task is not None and not self._task.done(),
            "jobs": [
                {
                    "name": j.name,
                    "interval_seconds": j.interval_seconds,
                    "last_run_at": j.last_run_at.isoformat() if j.last_run_at else None,
                    "last_status": j.last_status,
                    "runs": j.runs,
                }
                for j in self.jobs.values()
            ],
        }


scheduler = HeartbeatScheduler()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def heartbeat_enabled() -> bool:
    return os.environ.get("HEARTBEAT_ENABLED", "1") not in ("0", "false", "False")


def initialize_heartbeat() -> HeartbeatScheduler:
    """Register default jobs and start the loop (call from app startup)."""
    scheduler.register(
        "score_new_leads", job_score_new_leads,
        _env_int("HEARTBEAT_LEAD_SCORING_SEC", 3600),
    )
    scheduler.register(
        "check_deals_at_risk", job_check_deals_at_risk,
        _env_int("HEARTBEAT_DEAL_RISK_SEC", 21600),
    )
    scheduler.register(
        "snapshot_pipeline_metrics", job_snapshot_pipeline_metrics,
        _env_int("HEARTBEAT_METRICS_SNAPSHOT_SEC", 3600),
    )
    scheduler.register(
        "hermes_goal_check", job_hermes_goal_check,
        _env_int("HEARTBEAT_GOAL_CHECK_SEC", 3600),
    )
    # Always register so operators can inspect the job; execution is gated by
    # FOUNDER_OS_GMAIL_BETA_ENABLED (default frozen) inside job_sync_gmail_inbox.
    scheduler.register(
        "sync_gmail_inbox", job_sync_gmail_inbox,
        _env_int("HEARTBEAT_GMAIL_SYNC_SEC", 900),
    )
    scheduler.register(
        "scan_follow_up_eligibility", job_scan_follow_up_eligibility,
        _env_int("HEARTBEAT_FOLLOWUP_SCAN_SEC", 3600),
    )
    scheduler.register(
        "acp3_reconcile", job_acp3_reconcile,
        _env_int("HEARTBEAT_ACP3_RECONCILE_SEC", 1800),
    )
    scheduler.register(
        "run_marketing_cycle", job_run_marketing_cycle,
        _env_int("HEARTBEAT_MARKETING_CYCLE_SEC", 86400),
    )
    if heartbeat_enabled():
        scheduler.start()
    else:
        logger.info("Heartbeat disabled via HEARTBEAT_ENABLED=0")
    return scheduler
