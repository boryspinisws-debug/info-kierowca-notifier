import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from info_kierowca_notifier.booking import first_booking


def sample_results(*, toyota=True):
    slot_dt = (datetime.now() + timedelta(days=5)).replace(hour=8, minute=0, second=0, microsecond=0)
    exam = {
        "examType": "Practice",
        "practiceDateTime": slot_dt.isoformat(),
        "placePracticeAmount": 2,
        "additionalInfo": "TOYOTA" if toyota else "SKODA",
        "practiceId": 98765,
    }
    return [{"wordId": 44, "wordName": "WORD Częstochowa", "examCollectionForDay": [exam]}]


def sample_config():
    return {
        "first_booking_enabled": True,
        "organization_ids": [44],
        "category": 5,
        "profile_number": "TEST-PKK",
        "search_start_date": "",
        "earliest_slot_hour": 7,
        "latest_slot_hour": 12,
    }


class FirstBookingTests(unittest.TestCase):
    def test_candidate_requires_toyota_and_practical_slot(self):
        self.assertIsNotNone(first_booking._find_candidate(sample_results(), sample_config()))
        self.assertIsNone(first_booking._find_candidate(sample_results(toyota=False), sample_config()))

    def test_verified_place_reserved_is_success_and_payload_is_practical_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(first_booking, "ATTEMPT_FILE", Path(tmp) / "attempt.json"):
                with patch.object(first_booking.client, "do_request", side_effect=[
                    (201, json.dumps({"reservation": {"id": 123}}).encode(), {}),
                    (200, json.dumps({"reservation": {"status": "PlaceReserved"}}).encode(), {}),
                ]) as request:
                    outcome, message, candidate = first_booking.try_first_booking(
                        unittest.mock.Mock(), sample_config(), {"cookies": {}}, sample_results()
                    )
                    self.assertEqual(outcome, "reserved")
                    self.assertIn("manually", message)
                    self.assertEqual(candidate["center_id"], 44)
                    self.assertEqual(request.call_count, 2)
                    payload = request.call_args_list[0].kwargs["json_body"]
                    self.assertEqual(payload["category"], 5)
                    self.assertEqual(payload["examType"], "Practice")
                    self.assertEqual(payload["organizationId"], 44)
                    self.assertEqual(payload["practiceExamId"], "98765")
                    self.assertEqual(payload["profileNumber"], "TEST-PKK")

    def test_ambiguous_create_response_is_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(first_booking, "ATTEMPT_FILE", Path(tmp) / "attempt.json"):
                with patch.object(first_booking.client, "do_request", return_value=(None, b"offline", None)) as request:
                    logger = unittest.mock.Mock()
                    first = first_booking.try_first_booking(logger, sample_config(), {"cookies": {}}, sample_results())
                    second = first_booking.try_first_booking(logger, sample_config(), {"cookies": {}}, sample_results())
                    self.assertEqual(first[0], "needs_review")
                    self.assertEqual(second[0], "needs_review")
                    self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
