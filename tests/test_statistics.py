import os
import tempfile
import unittest
import importlib
from pathlib import Path


class StatisticsStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        os.environ["STATISTICS_DB_PATH"] = str(Path(cls.directory.name) / "statistics.sqlite3")
        import main

        cls.main = importlib.reload(main)
        main._init_statistics_db()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_resources_are_initialized_and_counts_are_aggregated(self):
        self.main._record_statistics_event(
            "tool/audio/audio-clipper",
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            "visit",
        )
        self.main._record_statistics_event(
            "tool/audio/audio-clipper",
            "11111111-1111-4111-8111-111111111111",
            "33333333-3333-4333-8333-333333333333",
            "complete",
        )
        self.main._record_statistics_event(
            "blog/integrating-salesforce-crm-leads-with-a-next-js-page-router-app-7b29bac20ea9",
            "11111111-1111-4111-8111-111111111111",
            "44444444-4444-4444-8444-444444444444",
            "open",
        )
        stats = {row["resource_key"]: row for row in self.main._read_statistics()}
        self.assertEqual(stats["tool/audio/audio-clipper"]["visitors"], 1)
        self.assertEqual(stats["tool/audio/audio-clipper"]["completed"], 1)
        self.assertEqual(stats["blog/integrating-salesforce-crm-leads-with-a-next-js-page-router-app-7b29bac20ea9"]["opens"], 1)

    def test_visits_are_unique_and_event_ids_are_idempotent(self):
        visitor = "55555555-5555-4555-8555-555555555555"
        event_id = "66666666-6666-4666-8666-666666666666"
        self.main._record_statistics_event("tool/audio/audio-clipper", visitor, event_id, "visit")
        self.main._record_statistics_event("tool/audio/audio-clipper", visitor, event_id, "visit")
        stats = {row["resource_key"]: row for row in self.main._read_statistics()}
        self.assertEqual(stats["tool/audio/audio-clipper"]["visitors"], 2)

    def test_unknown_resources_and_wrong_events_are_rejected(self):
        with self.assertRaises(ValueError):
            self.main._record_statistics_event("tool/no-such-tool", "77777777-7777-4777-8777-777777777777", "88888888-8888-4888-8888-888888888888", "visit")
        with self.assertRaises(ValueError):
            self.main._record_statistics_event("blog/integrating-salesforce-crm-leads-with-a-next-js-page-router-app-7b29bac20ea9", "77777777-7777-4777-8777-777777777777", "99999999-9999-4999-8999-999999999999", "complete")


if __name__ == "__main__":
    unittest.main()
