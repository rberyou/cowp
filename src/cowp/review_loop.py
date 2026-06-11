from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

REVIEW_LOOP_STATUSES = {
    "not_started",
    "reviewing",
    "fixing",
    "re_reviewing",
    "clean",
    "blocked_decision",
    "blocked_replan",
    "blocked_max_rounds",
    "blocked_stable_failure",
}

REVIEW_LOOP_STOP_STATUSES = {
    "blocked_decision",
    "blocked_replan",
    "blocked_max_rounds",
    "blocked_stable_failure",
}

REVIEW_LOOP_GATE_OPEN_STATUSES = REVIEW_LOOP_STATUSES - {"not_started", "clean"}


def default_review_loop(max_rounds: int = 3) -> dict[str, Any]:
    return {
        "status": "not_started",
        "round": 0,
        "max_rounds": max(1, int(max_rounds or 3)),
        "blocked_by": [],
        "rounds": [],
    }


def normalize_review_loop(value: Any, max_rounds: int = 3) -> dict[str, Any]:
    loop = default_review_loop(max_rounds)
    if isinstance(value, dict):
        loop.update(value)
    loop["status"] = _status(loop.get("status"))
    loop["round"] = max(0, int(loop.get("round") or 0))
    loop["max_rounds"] = max(1, int(loop.get("max_rounds") or max_rounds or 3))
    if not isinstance(loop.get("blocked_by"), list):
        loop["blocked_by"] = []
    if not isinstance(loop.get("rounds"), list):
        loop["rounds"] = []
    return loop


def begin_review_loop(value: Any, max_rounds: int, now: str) -> dict[str, Any]:
    loop = normalize_review_loop(value, max_rounds)
    active = loop["status"] in {"reviewing", "re_reviewing"}
    if not active:
        loop["round"] += 1
    if loop["round"] > loop["max_rounds"]:
        loop["round"] = loop["max_rounds"]
        return stop_review_loop(loop, "blocked_max_rounds", [], "max review rounds exceeded", now)
    loop["status"] = "re_reviewing" if loop["round"] > 1 else "reviewing"
    loop["blocked_by"] = []
    loop["last_reviewed_at"] = now
    loop["updated_at"] = now
    _append_round_event(loop, "begin", now, round=loop["round"])
    return loop


def mark_review_loop_fix(
    value: Any,
    summary: str,
    files: Iterable[str],
    now: str,
    current_sha: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    loop = normalize_review_loop(value)
    if loop["round"] <= 0:
        loop["round"] = 1
    loop["status"] = "fixing"
    loop["needs_review"] = True
    loop["last_fix_at"] = now
    loop["last_fix_sha"] = current_sha
    loop["last_fix_summary"] = summary
    changed_files = [str(path).replace("\\", "/") for path in files]
    loop["last_fix_files"] = changed_files
    if fingerprint:
        loop["last_fix_fingerprint"] = fingerprint
    loop["updated_at"] = now
    _append_round_event(
        loop,
        "record-fix",
        now,
        round=loop["round"],
        summary=summary,
        files=changed_files,
        current_sha=current_sha,
        fingerprint=fingerprint,
    )
    return loop


def mark_review_loop_reviewed(
    value: Any,
    max_rounds: int,
    now: str,
    *,
    snapshot_hash: str | None = None,
) -> dict[str, Any]:
    loop = normalize_review_loop(value, max_rounds)
    if loop["status"] == "not_started":
        return loop

    needs_new_round = loop["status"] == "fixing"
    if needs_new_round:
        loop["round"] += 1
        if loop["round"] > loop["max_rounds"]:
            loop["round"] = loop["max_rounds"]
            return stop_review_loop(loop, "blocked_max_rounds", [], "max review rounds exceeded", now)
        loop["status"] = "re_reviewing" if loop["round"] > 1 else "reviewing"
        loop["needs_review"] = False
        loop["blocked_by"] = []
    elif loop["status"] in {"reviewing", "re_reviewing"}:
        loop["status"] = "re_reviewing" if loop["round"] > 1 else "reviewing"
    else:
        return loop

    loop["needs_review"] = False
    loop["last_reviewed_at"] = now
    loop["last_review_snapshot_at"] = now
    loop["last_review_snapshot_hash"] = snapshot_hash
    loop["updated_at"] = now
    _append_round_event(loop, "review", now, round=loop["round"], snapshot_hash=snapshot_hash)
    return loop


def mark_review_loop_clean(value: Any, now: str) -> dict[str, Any]:
    loop = normalize_review_loop(value)
    loop["needs_review"] = False
    loop["status"] = "clean"
    loop["blocked_by"] = []
    loop["completed_at"] = now
    loop["updated_at"] = now
    _append_round_event(loop, "complete", now, round=loop["round"])
    return loop


def stop_review_loop(value: Any, status: str, blockers: Iterable[str], reason: str, now: str) -> dict[str, Any]:
    if status not in REVIEW_LOOP_STOP_STATUSES:
        raise ValueError(f"invalid review loop stop status: {status}")
    loop = normalize_review_loop(value)
    loop["status"] = status
    loop["blocked_by"] = [str(item) for item in blockers]
    loop["stop_reason"] = reason
    loop["stopped_at"] = now
    loop["updated_at"] = now
    _append_round_event(loop, "stop", now, round=loop["round"], status=status, blockers=loop["blocked_by"], reason=reason)
    return loop


def validate_review_loop(loop: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(loop, dict):
        return ["review_loop must be an object"]
    status = str(loop.get("status") or "not_started")
    if status not in REVIEW_LOOP_STATUSES:
        errors.append(f"invalid review_loop.status: {status}")
    try:
        if int(loop.get("round") or 0) < 0:
            errors.append("review_loop.round must be >= 0")
    except (TypeError, ValueError):
        errors.append("review_loop.round must be an integer")
    try:
        if int(loop.get("max_rounds") or 1) < 1:
            errors.append("review_loop.max_rounds must be >= 1")
    except (TypeError, ValueError):
        errors.append("review_loop.max_rounds must be an integer")
    if "blocked_by" in loop and not isinstance(loop.get("blocked_by"), list):
        errors.append("review_loop.blocked_by must be an array")
    if "rounds" in loop and not isinstance(loop.get("rounds"), list):
        errors.append("review_loop.rounds must be an array")
    return errors


def review_loop_gate_blockers(value: Any, label: str = "review loop") -> list[str]:
    loop = normalize_review_loop(value)
    status = loop["status"]
    if status in REVIEW_LOOP_GATE_OPEN_STATUSES:
        blockers = [f"{label} is {status}"]
        blocked_by = loop.get("blocked_by")
        if isinstance(blocked_by, list) and blocked_by:
            blockers[0] += ": " + ", ".join(str(item) for item in blocked_by)
        return blockers
    return []


def active_finding_blockers(findings: Iterable[Any] | None) -> list[str]:
    blockers: list[str] = []
    for item in findings or []:
        finding = _finding_data(item)
        finding_id = str(finding.get("id") or "<finding>")
        status = str(finding.get("status") or "open")
        if status == "open":
            blockers.append(f"{finding_id} open")
        if status == "wontfix" and is_disallowed_wontfix(finding):
            blockers.append(f"{finding_id} disallowed wontfix")
        if status != "invalid" and str(finding.get("type") or "") == "boundary":
            blockers.append(f"{finding_id} active boundary")
        if status != "invalid" and bool(finding.get("contract_change", False)):
            blockers.append(f"{finding_id} active contract_change")
    return blockers


def decision_finding_blockers(findings: Iterable[Any] | None) -> list[str]:
    blockers: list[str] = []
    for item in findings or []:
        finding = _finding_data(item)
        finding_id = str(finding.get("id") or "<finding>")
        status = str(finding.get("status") or "open")
        active_boundary = status != "invalid" and str(finding.get("type") or "") == "boundary"
        active_contract = status != "invalid" and bool(finding.get("contract_change", False))
        open_decision = status == "open" and bool(finding.get("requires_decision", False))
        if active_boundary or active_contract or open_decision:
            blockers.append(finding_id)
    return blockers


def is_disallowed_wontfix(finding: dict[str, Any]) -> bool:
    severity = str(finding.get("severity") or "").upper()
    return (
        severity in {"P0", "P1"}
        or str(finding.get("type") or "") == "boundary"
        or bool(finding.get("contract_change", False))
    )


def apply_decision_classification(
    finding: dict[str, Any],
    *,
    requires_decision: bool = False,
    decision_reason: str | None = None,
    clear_requires_decision: bool = False,
    explicit_requires_decision: bool = False,
) -> None:
    active_boundary = str(finding.get("type") or "") == "boundary"
    active_contract = bool(finding.get("contract_change", False))
    implied_decision = active_boundary or active_contract
    reason = (decision_reason or "").strip()

    if explicit_requires_decision and not reason:
        raise ValueError("--decision-reason is required with --requires-decision")
    if reason and not (requires_decision or implied_decision or bool(finding.get("requires_decision", False))):
        raise ValueError("--decision-reason requires a decision finding")

    if clear_requires_decision:
        if implied_decision and str(finding.get("status") or "open") != "invalid":
            raise ValueError("--clear-requires-decision requires clearing boundary/contract_change or marking invalid")
        finding["requires_decision"] = False
        finding["decision_reason"] = None
        return

    if requires_decision or implied_decision:
        finding["requires_decision"] = True
        if reason:
            finding["decision_reason"] = reason
        elif implied_decision and not finding.get("decision_reason"):
            finding["decision_reason"] = "boundary or contract-change finding"
    elif "requires_decision" not in finding:
        finding["requires_decision"] = False
        finding.setdefault("decision_reason", None)
    elif reason:
        finding["decision_reason"] = reason


def review_loop_fingerprint(
    findings: Iterable[Any] | None,
    *,
    snapshot_hash: str | None = None,
    changed_files: Iterable[str] = (),
) -> str:
    payload = {
        "findings": [
            {
                "id": str(finding.get("id") or ""),
                "type": str(finding.get("type") or ""),
                "severity": str(finding.get("severity") or ""),
                "message": str(finding.get("message") or ""),
                "files": sorted(str(path).replace("\\", "/") for path in finding.get("files") or []),
                "requires_decision": bool(finding.get("requires_decision", False)),
                "contract_change": bool(finding.get("contract_change", False)),
            }
            for finding in (_finding_data(item) for item in findings or [])
            if str(finding.get("status") or "open") == "open"
            or (str(finding.get("status") or "open") != "invalid" and str(finding.get("type") or "") == "boundary")
            or (str(finding.get("status") or "open") != "invalid" and bool(finding.get("contract_change", False)))
        ],
        "snapshot_hash": snapshot_hash,
        "changed_files": sorted(str(path).replace("\\", "/") for path in changed_files),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _append_round_event(loop: dict[str, Any], event: str, at: str, **details: Any) -> None:
    rounds = loop.setdefault("rounds", [])
    if not isinstance(rounds, list):
        rounds = []
        loop["rounds"] = rounds
    rounds.append({"event": event, "at": at, **details})


def _status(value: Any) -> str:
    status = str(value or "not_started")
    return status if status in REVIEW_LOOP_STATUSES else "not_started"


def _finding_data(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    data = getattr(item, "data", None)
    if isinstance(data, dict):
        return data
    return {}
