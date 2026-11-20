"""Waterjet/forming counting, the durable Google Sheets queue, URL config and outbox cleanup."""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

SYSTEM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for path in (SYSTEM_DIR, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from sdy_google_sender import MemoryGoogleQueue, send_next_google_item  # noqa: E402
from sdy_persistence import ProductionSQLiteStore  # noqa: E402
from test_tablet_protocol import PUBLISHED, RecordingMQTTClient, load_tracker, message, results  # noqa: E402

VALID_URL = "https://script.google.com/macros/s/NEW-DEPLOYMENT/exec"


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        PUBLISHED.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.g = load_tracker(os.path.join(self.directory.name, "sdy1.db"))
        # load_tracker shuts the tracker down; give it a live store and queue again.
        self.store = ProductionSQLiteStore(os.path.join(self.directory.name, "live.db"))
        self.g["persistence_store"] = self.store
        self.g["google_queue"] = self.store
        self.g["google_sender"] = None
        PUBLISHED.clear()

    def tearDown(self):
        self.g["persistence_store"] = None
        self.store.close()
        self.directory.cleanup()


class WaterjetCountingTests(TrackerTestCase):
    def feed(self, a, b, t, enabled=True):
        return self.g["update_waterjet_counts"](a, b, t, enabled)

    def test_block_unblock_block_is_one_cycle_and_first_block_is_ignored(self):
        self.assertEqual(self.feed(False, False, 0), 0)
        self.assertEqual(self.feed(True, False, 1), 0)   # first block after startup: arm only
        self.assertEqual(self.feed(False, False, 2), 0)
        self.assertEqual(self.feed(True, False, 3), 1)   # unblock -> block = one cycle
        self.assertEqual(self.feed(True, False, 4), 0)   # still blocked, no double count
        self.assertEqual(self.feed(False, False, 5), 0)
        self.assertEqual(self.feed(True, False, 6), 1)
        self.assertEqual((self.g["waterjet_a_output"], self.g["waterjet_b_output"]), (2, 0))
        self.assertEqual((self.g["hourly_output"], self.g["total_output"], self.g["shift_total_output"]), (2, 2, 2))

    def test_sensor_already_blocked_at_startup_counts_from_the_next_block(self):
        self.feed(False, True, 0)                         # B blocked at power-on: arm only
        self.feed(False, False, 1)
        self.assertEqual(self.feed(False, True, 2), 1)
        self.assertEqual(self.g["waterjet_b_output"], 1)

    def test_two_waterjets_count_independently_and_add_up(self):
        self.feed(True, True, 0)
        self.feed(False, False, 1)
        self.assertEqual(self.feed(True, True, 2), 2)
        self.assertEqual(self.g["shift_total_output"], 2)
        self.assertEqual((self.g["waterjet_a_output"], self.g["waterjet_b_output"]), (1, 1))

    def test_two_second_debounce_per_waterjet(self):
        self.feed(True, False, 0)
        self.feed(False, False, 1)
        self.assertEqual(self.feed(True, False, 2), 1)
        self.feed(False, False, 2.5)
        self.assertEqual(self.feed(True, False, 3.0), 0)  # 1.0 s after the last count: chatter
        self.assertEqual(self.g["waterjet_a_output"], 1)

    def test_blocks_outside_normal_production_are_never_counted_later(self):
        self.feed(True, False, 0)
        self.feed(False, False, 1)
        self.assertEqual(self.feed(True, False, 3, enabled=False), 0)   # e.g. during REST
        self.assertEqual(self.feed(True, False, 6, enabled=True), 0)    # back to NORMAL, still blocked
        self.assertEqual(self.g["shift_total_output"], 0)

    def test_shift_reset_clears_waterjet_and_forming_but_keeps_sensors_armed(self):
        self.feed(True, False, 0)
        self.g["forming_output"] = 7
        self.g["reset_production_runtime_counters"]()
        self.assertEqual((self.g["waterjet_a_output"], self.g["forming_output"]), (0, 0))
        self.feed(False, False, 1)
        self.assertEqual(self.feed(True, False, 3), 1)

    def test_counters_survive_a_restart_snapshot(self):
        self.g["waterjet_a_output"], self.g["waterjet_b_output"], self.g["forming_output"] = 3, 4, 5
        counters = self.g["build_runtime_snapshot"]()["counters"]
        self.assertEqual((counters["waterjet_a_output"], counters["waterjet_b_output"], counters["forming_output"]), (3, 4, 5))

    def test_live_payload_has_waterjet_and_forming_panels(self):
        self.g["shift_total_output"], self.g["forming_output"] = 9, 4
        payload = self.g["publish_live_data"]()
        self.assertEqual((payload["waterjet_output"], payload["total_product_count"], payload["forming_output"]), (9, 9, 4))

    def test_forming_limit_switch_no_longer_changes_main_output(self):
        with open(os.path.join(SYSTEM_DIR, "sdy1.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        forming_block = source[source.index("# --- FORMING COUNTING"):source.index("# --- STATE LOGIC")]
        self.assertIn("forming_output += 1", forming_block)
        for main_counter in ("hourly_output", "total_output", "current_cycle_time"):
            self.assertNotIn(main_counter, forming_block)


class SheetsUrlConfigTests(TrackerTestCase):
    def test_tracker_listens_for_its_config_topic(self):
        self.g["on_connect"](RecordingMQTTClient(), None, None, 0)
        topics = [entry for name, entry in PUBLISHED if name == "SUBSCRIBE"][0]
        self.assertIn(("sdy1/config", 1), topics)

    def test_valid_url_is_applied_saved_and_acknowledged(self):
        self.g["on_message"](None, None, message("sdy1/config", {"google_web_app_url": VALID_URL}))
        self.assertEqual(self.g["current_web_app_url"](), VALID_URL)
        self.assertEqual(self.store.get_setting("google_web_app_url"), VALID_URL)
        ack = results("sdy1/config_result")[-1]
        self.assertEqual((ack["status"], ack["google_web_app_url_source"]), ("applied", "smartchecksheet"))
        self.assertEqual(self.g["load_web_app_url"](), (VALID_URL, "smartchecksheet"))

    def test_invalid_url_is_refused_and_the_old_one_kept(self):
        before = self.g["current_web_app_url"]()
        self.g["on_message"](None, None, message("sdy1/config", {"google_web_app_url": "http://evil.example/exec"}))
        self.assertEqual(self.g["current_web_app_url"](), before)
        self.assertEqual(results("sdy1/config_result")[-1]["status"], "rejected")

    def test_workspace_links_are_accepted_and_lookalikes_refused(self):
        valid = self.g["is_valid_web_app_url"]
        self.assertTrue(valid("https://script.google.com/a/macros/example.com/s/AKfycbTESTDEPLOYMENT/exec"))
        self.assertTrue(valid(self.g["DEFAULT_WEB_APP_URL"]))
        for bad in ("https://script.google.com/macros/s/AKfycbTESTDEPLOYMENT/dev",
                    "https://script.google.com.evil.example/macros/s/AKfycbTESTDEPLOYMENT/exec",
                    "https://script.google.com/macros/s/AKfyc bTEST/exec"):
            self.assertFalse(valid(bad), bad)

    def test_empty_url_returns_to_the_built_in_fallback(self):
        self.g["apply_line_config"]({"google_web_app_url": VALID_URL})
        self.g["apply_line_config"]({"google_web_app_url": ""})
        self.assertEqual(self.g["current_web_app_url"](), self.g["DEFAULT_WEB_APP_URL"])
        self.assertIsNone(self.store.get_setting("google_web_app_url"))


class Response:
    def __init__(self, body):
        self.body, self.text = body, json.dumps(body)

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class DurableGoogleQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "queue.db")
        self.store = ProductionSQLiteStore(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_queued_rows_survive_a_restart(self):
        self.store.enqueue_google({"tab": "Hourly_Data", "row": [1], "request_id": "r1"})
        self.store.close()
        self.store = ProductionSQLiteStore(self.path)
        self.assertEqual(self.store.peek_google()["request_id"], "r1")

    def test_same_request_id_is_queued_once(self):
        self.assertTrue(self.store.enqueue_google({"tab": "Hourly_Data", "row": [1], "request_id": "r1"}))
        self.assertFalse(self.store.enqueue_google({"tab": "Hourly_Data", "row": [1], "request_id": "r1"}))
        self.assertEqual(self.store.google_queue_count(), 1)

    def test_failed_pdf_keeps_its_request_id_backs_off_and_blocks_later_items(self):
        for queue in (self.store, MemoryGoogleQueue()):
            calls = []

            def failing_post(url, json, timeout):
                calls.append((json, timeout))
                raise RuntimeError("simulated timeout")

            queue.enqueue_google({"action": "GENERATE_PDF", "shift_id": "S1", "request_id": "pdf-1"})
            queue.enqueue_google({"tab": "Hourly_Data", "row": [2], "request_id": "row-2"})
            self.assertFalse(send_next_google_item(queue, "https://x/exec", failing_post, now=100.0))
            self.assertEqual(calls[0][1], 120)
            self.assertEqual(queue.peek_google()["next_retry_at"], 105.0)
            self.assertFalse(send_next_google_item(queue, "https://x/exec", failing_post, now=104.0))
            self.assertEqual(len(calls), 1, "retry must wait for its backoff; later items must wait too")
            send_next_google_item(queue, "https://x/exec", failing_post, now=105.0)
            self.assertEqual(calls[1][0]["request_id"], "pdf-1")
            self.assertEqual(queue.peek_google()["next_retry_at"], 115.0)

            ok = lambda url, json, timeout: Response({"status": "success"})
            self.assertTrue(send_next_google_item(queue, "https://x/exec", ok, now=115.0))
            self.assertEqual(queue.peek_google()["request_id"], "row-2")
            self.assertTrue(send_next_google_item(queue, "https://x/exec", ok, now=115.0))
            self.assertIsNone(queue.peek_google())


class OutboxRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ProductionSQLiteStore(os.path.join(self.directory.name, "outbox.db"))
        for event_id in ("old-delivered", "old-undelivered", "new-delivered"):
            self.store.save_state_and_enqueue("SDY1", {}, "ACTIVE", "hourly.finalized", "S1", {}, event_id=event_id)
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        db = self.store._connection
        db.execute("UPDATE outbox_events SET created_at=? WHERE event_id LIKE 'old-%'", (old,))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def install_relay_ledger(self):
        db = self.store._connection
        db.execute("""CREATE TABLE checksheet_deliveries (event_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, delivered_at REAL, last_error TEXT)""")
        db.execute("INSERT INTO checksheet_deliveries(event_id, attempts, delivered_at) VALUES ('old-delivered', 1, 1.0)")
        db.execute("INSERT INTO checksheet_deliveries(event_id, attempts, delivered_at) VALUES ('new-delivered', 1, 1.0)")
        db.execute("INSERT INTO checksheet_deliveries(event_id, attempts, next_attempt) VALUES ('old-undelivered', 3, 9e9)")

    def remaining(self):
        return sorted(row[0] for row in self.store._connection.execute("SELECT event_id FROM outbox_events"))

    def test_nothing_is_deleted_before_the_relay_is_installed(self):
        self.assertEqual(self.store.prune_finished_events(api_enabled=False), 0)
        self.assertEqual(len(self.remaining()), 3)

    def test_only_old_events_the_relay_delivered_are_deleted(self):
        self.install_relay_ledger()
        self.assertEqual(self.store.prune_finished_events(api_enabled=False), 1)
        self.assertEqual(self.remaining(), ["new-delivered", "old-undelivered"])
        ledger = sorted(row[0] for row in self.store._connection.execute("SELECT event_id FROM checksheet_deliveries"))
        self.assertEqual(ledger, ["new-delivered", "old-undelivered"])

    def test_enabled_api_must_also_have_sent_the_event(self):
        self.install_relay_ledger()
        self.assertEqual(self.store.prune_finished_events(api_enabled=True), 0)
        self.store.mark_sent("old-delivered")
        self.assertEqual(self.store.prune_finished_events(api_enabled=True), 1)


if __name__ == "__main__":
    unittest.main()
