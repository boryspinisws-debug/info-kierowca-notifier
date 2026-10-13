"""Fail-closed first-time practical exam reservation.

This module is deliberately separate from rescheduling. It sends at most one
reservation-creation request for a selected Toyota slot, persists the attempt
before making that request, and never retries an ambiguous outcome.
"""
import json
import os
import re
from datetime import date, datetime, timedelta

from info_kierowca_notifier import client
from info_kierowca_notifier.paths import STATE_DIR, ensure_state_dir

ATTEMPT_FILE = STATE_DIR / "first-booking-attempt.json"
CENTER_ID = 44
CENTER_NAME = "WORD Częstochowa"
TOYOTA_MARKER = re.compile(r"\bTOYOTA\b", re.I)
RESERVATIONS_URL = f"{client.BASE}/bknd/exam/api/v1/Reservations"


def _load_attempt():
    try:
        value = json.loads(ATTEMPT_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_attempt(value):
    ensure_state_dir()
    tmp = ATTEMPT_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(tmp, ATTEMPT_FILE)


def _slot_id(exam):
    for key in ("practiceId", "practiceExamId", "examId"):
        value = exam.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _find_candidate(results, config, now=None):
    now = now or datetime.now()
    lower_raw = config.get("search_start_date") or now.date().isoformat()
    try:
        lower = date.fromisoformat(lower_raw)
    except (TypeError, ValueError):
        lower = now.date()
    upper = now.date() + timedelta(days=31)
    earliest_hour = int(config.get("earliest_slot_hour", 0))
    latest_hour = int(config.get("latest_slot_hour", 24))
    candidates = []
    for word in results:
        if word.get("wordId") != CENTER_ID:
            continue
        word_name = str(word.get("wordName") or "")
        if "częstochowa" not in word_name.casefold():
            continue
        for exam in word.get("examCollectionForDay", []):
            if exam.get("examType") != "Practice":
                continue
            dt_raw = exam.get("practiceDateTime")
            if not dt_raw:
                continue
            try:
                dt = datetime.fromisoformat(dt_raw)
            except (TypeError, ValueError):
                continue
            if not lower <= dt.date() <= upper:
                continue
            if not earliest_hour <= dt.hour < latest_hour:
                continue
            try:
                places = int(exam.get("placePracticeAmount") or 0)
            except (TypeError, ValueError):
                places = 0
            if places <= 0:
                continue
            details = " ".join(str(exam.get(k) or "") for k in
                               ("additionalInfo", "vehicle", "vehicleName", "car", "description"))
            if not TOYOTA_MARKER.search(details):
                continue
            slot_id = _slot_id(exam)
            if not slot_id:
                continue
            candidates.append({
                "center_id": CENTER_ID,
                "center": CENTER_NAME,
                "date": dt.date().isoformat(),
                "datetime": dt.isoformat(),
                "slot_id": slot_id,
                "places": places,
            })
    candidates.sort(key=lambda item: item["datetime"])
    return candidates[0] if candidates else None


def _json_body(body):
    try:
        value = json.loads(body)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _reservation_id(payload):
    for source in (payload, payload.get("reservation"), payload.get("data")):
        if isinstance(source, dict):
            for key in ("id", "reservationId", "reservation_id"):
                value = source.get(key)
                if value not in (None, ""):
                    return str(value)
    return ""


def _reservation_status(payload):
    for source in (payload, payload.get("reservation"), payload.get("data")):
        if isinstance(source, dict):
            value = source.get("status") or source.get("reservationStatus")
            if value:
                return str(value)
    return ""


def try_first_booking(logger, config, session, results):
    """Return (outcome, safe_message, candidate); never retries an uncertain POST."""
    if not config.get("first_booking_enabled"):
        return "disabled", "", None

    prior = _load_attempt()
    if prior.get("state") == "reserved":
        return "reserved", "A reservation was already verified. Complete payment manually; no new request was sent.", prior.get("candidate")
    if prior.get("state") in ("attempted", "needs_review"):
        return "needs_review", "A previous first-booking attempt needs checking; no new request was sent.", prior.get("candidate")

    candidate = _find_candidate(results, config)
    if not candidate:
        return "no_candidate", "", None
    if prior.get("state") == "rejected" and prior.get("candidate") == candidate:
        return "rejected", "The same slot was already rejected by the site; no repeat request was sent.", candidate

    payload = {
        "profileNumber": config["profile_number"],
        "organizationId": CENTER_ID,
        "examDate": candidate["date"],
        "category": 5,
        "examType": "Practice",
        "language": "Polish",
        "practiceExamId": candidate["slot_id"],
        "profileType": "Pkk",
    }
    attempt = {
        "state": "attempted",
        "created_at": datetime.now().isoformat(),
        "candidate": candidate,
    }
    # Durable guard is written before POST. If the process crashes or the
    # network response is lost, a subsequent poll must not submit twice.
    _save_attempt(attempt)
    status, body, _headers = client.do_request(
        f"{RESERVATIONS_URL}/create", session, method="POST", json_body=payload
    )
    if status is None or status >= 500:
        attempt["state"] = "needs_review"
        attempt["reason"] = "ambiguous_create_response"
        _save_attempt(attempt)
        logger.warning("outcome=first_booking_needs_review stage=create status=%s", status)
        return "needs_review", "Reservation request outcome is uncertain. Check the WORD account manually; no retry was sent.", candidate
    if status not in (200, 201):
        attempt["state"] = "rejected"
        attempt["reason"] = f"http_{status}"
        _save_attempt(attempt)
        logger.info("outcome=first_booking_rejected status=%s", status)
        return "rejected", f"The booking request was rejected (HTTP {status}); no automatic retry was sent.", candidate

    created = _json_body(body)
    reservation_id = _reservation_id(created)
    if not reservation_id:
        attempt["state"] = "needs_review"
        attempt["reason"] = "missing_reservation_id"
        _save_attempt(attempt)
        logger.warning("outcome=first_booking_needs_review stage=parse_create")
        return "needs_review", "The site accepted the request but returned no recognizable reservation ID. Check your account manually; no retry was sent.", candidate

    attempt["reservation_id"] = reservation_id
    verify_status, verify_body, _headers = client.do_request(
        f"{RESERVATIONS_URL}/{reservation_id}", session, method="GET"
    )
    if verify_status != 200:
        attempt["state"] = "needs_review"
        attempt["reason"] = "verification_http_error"
        _save_attempt(attempt)
        logger.warning("outcome=first_booking_needs_review stage=verify status=%s", verify_status)
        return "needs_review", "The request was sent but its reservation status could not be verified. Check the WORD account manually; no retry was sent.", candidate

    reservation = _json_body(verify_body)
    state = _reservation_status(reservation)
    if state == "PlaceReserved":
        attempt["state"] = "reserved"
        attempt["verified_at"] = datetime.now().isoformat()
        attempt["reservation_status"] = state
        _save_attempt(attempt)
        logger.info("outcome=first_booking_reserved status=PlaceReserved")
        return "reserved", "Reservation verified. Complete payment manually within the site's stated deadline.", candidate

    attempt["state"] = "needs_review"
    attempt["reservation_status"] = state or "unknown"
    attempt["reason"] = "not_place_reserved"
    _save_attempt(attempt)
    logger.warning("outcome=first_booking_needs_review stage=verify reservation_status=%s", state or "unknown")
    return "needs_review", f"Reservation status is {state or 'unknown'}, not PlaceReserved. Check the WORD account manually; no retry was sent.", candidate
