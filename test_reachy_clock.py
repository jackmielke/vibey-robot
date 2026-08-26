#!/usr/bin/env python3
"""
test_reachy_clock.py — the clock says the same thing every time, and never
throws mid-conversation.

    python3 -m unittest test_reachy_clock -v

Every case pins an instant and a zone, so nothing here depends on when or where
it runs. stdlib only.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import reachy_clock as clock


class ClockTestCase(unittest.TestCase):
    """Point the config at a scratch file and clear the env, so a real
    clock.json (or Jack's shell) can't change what the tests see."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / "clock.json"
        self._real_cfg = clock.CONFIG_PATH
        clock.CONFIG_PATH = self.cfg
        self._env = {k: os.environ.pop(k, None) for k in
                     ("VIBEY_TZ", "TZ", "VIBEY_HOUR_CYCLE",
                      "LC_TIME", "LC_ALL", "LANG")}
        self.addCleanup(self._restore)

    def _restore(self):
        clock.CONFIG_PATH = self._real_cfg
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def at(self, y, mo, d, h, mi, zone="America/Denver") -> datetime:
        return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(zone))


class TestSpokenClock(ClockTestCase):
    def test_12_hour_faces(self):
        cases = {
            (0, 0): "midnight",
            (12, 0): "noon",
            (3, 0): "three o'clock",
            (15, 15): "quarter past three",
            (15, 30): "half past three",
            (15, 45): "quarter to four",
            (23, 45): "quarter to twelve",
            (15, 5): "three oh five",
            (15, 22): "three twenty-two",
            (9, 59): "nine fifty-nine",
        }
        for (h, mi), want in cases.items():
            with self.subTest(h=h, minute=mi):
                self.assertEqual(
                    clock.spoken_clock(self.at(2026, 6, 1, h, mi), "12"), want)

    def test_24_hour_faces(self):
        cases = {
            (0, 0): "midnight",
            (15, 0): "fifteen hundred",
            (15, 5): "fifteen oh five",
            (15, 22): "fifteen twenty-two",
            (7, 30): "seven thirty",
        }
        for (h, mi), want in cases.items():
            with self.subTest(h=h, minute=mi):
                self.assertEqual(
                    clock.spoken_clock(self.at(2026, 6, 1, h, mi), "24"), want)

    def test_no_digits_ever_reach_the_speaker(self):
        for h in range(24):
            for mi in (0, 1, 7, 15, 29, 30, 44, 45, 59):
                said = clock.spoken_time(self.at(2026, 6, 1, h, mi),
                                         zone="America/Denver", cycle="12")
                self.assertFalse(any(c.isdigit() for c in said), said)


class TestSpokenTime(ClockTestCase):
    def test_daypart(self):
        for h, want in ((8, "in the morning"), (14, "in the afternoon"),
                        (19, "in the evening"), (23, "at night"),
                        (2, "at night")):
            with self.subTest(hour=h):
                said = clock.spoken_time(self.at(2026, 6, 1, h, 20),
                                         zone="America/Denver", cycle="12")
                self.assertIn(want, said)

    def test_noon_and_midnight_stand_alone(self):
        self.assertEqual(clock.spoken_time(self.at(2026, 6, 1, 12, 0),
                                           zone="America/Denver",
                                           with_zone=False, cycle="12"),
                         "It's noon.")
        self.assertEqual(clock.spoken_time(self.at(2026, 6, 1, 0, 0),
                                           zone="America/Denver",
                                           with_zone=False, cycle="12"),
                         "It's midnight.")

    def test_24_hour_drops_the_daypart(self):
        said = clock.spoken_time(self.at(2026, 6, 1, 15, 20),
                                 zone="America/Denver", cycle="24")
        self.assertIn("fifteen twenty", said)
        self.assertNotIn("afternoon", said)

    def test_zone_named_the_way_people_say_it(self):
        said = clock.spoken_time(self.at(2026, 6, 1, 15, 20), zone="Asia/Tokyo",
                                 cycle="12")
        self.assertIn("in Tokyo", said)
        self.assertNotIn("Asia/", said)
        self.assertIn("New York",
                      clock.spoken_time(self.at(2026, 6, 1, 9, 5),
                                        zone="America/New_York", cycle="12"))

    def test_aware_input_is_converted_not_relabelled(self):
        # 22:00 UTC is 3pm in Denver on a summer day (UTC-7).
        utc = datetime(2026, 6, 1, 22, 0, tzinfo=timezone.utc)
        said = clock.spoken_time(utc, zone="America/Denver", cycle="12")
        self.assertIn("three o'clock", said)
        self.assertIn("in the afternoon", said)

    def test_naive_input_is_read_as_local_to_the_zone(self):
        naive = datetime(2026, 6, 1, 15, 0)
        self.assertIn("three o'clock",
                      clock.spoken_time(naive, zone="Asia/Tokyo", cycle="12"))

    def test_same_instant_same_words(self):
        when = self.at(2026, 6, 1, 15, 22)
        first = clock.spoken_time(when, zone="America/Denver", cycle="12")
        self.assertEqual(first,
                         clock.spoken_time(when, zone="America/Denver",
                                           cycle="12"))


class TestDaylightSaving(ClockTestCase):
    """US DST in 2026: forward on March 8, back on November 1."""

    def test_offset_follows_the_season(self):
        winter = self.at(2026, 1, 15, 12, 0)
        summer = self.at(2026, 7, 15, 12, 0)
        self.assertEqual(winter.utcoffset().total_seconds(), -7 * 3600)
        self.assertEqual(summer.utcoffset().total_seconds(), -6 * 3600)

    def test_spring_forward_is_announced_the_day_before(self):
        self.assertEqual(clock.dst_note(self.at(2026, 3, 7, 20, 0)),
                         "The clocks go forward within the day.")
        self.assertIn("go forward",
                      clock.spoken_time(self.at(2026, 3, 7, 20, 0),
                                        zone="America/Denver", cycle="12"))

    def test_fall_back_is_announced_the_day_before(self):
        self.assertEqual(clock.dst_note(self.at(2026, 10, 31, 20, 0)),
                         "The clocks go back within the day.")

    def test_ordinary_day_says_nothing_about_clocks(self):
        self.assertEqual(clock.dst_note(self.at(2026, 6, 1, 20, 0)), "")
        self.assertNotIn("clocks",
                         clock.spoken_time(self.at(2026, 6, 1, 20, 0),
                                           zone="America/Denver", cycle="12"))

    def test_zone_without_dst_never_mentions_it(self):
        for month in (3, 6, 10, 11):
            with self.subTest(month=month):
                self.assertEqual(
                    clock.dst_note(self.at(2026, month, 7, 20, 0, "Asia/Tokyo")),
                    "")

    def test_crossing_the_spring_gap_still_answers(self):
        # 2am–3am does not exist in Denver on 2026-03-08; zoneinfo picks a side
        # rather than raising, and the answer must still be sayable.
        said = clock.spoken_time(self.at(2026, 3, 8, 2, 30),
                                 zone="America/Denver", cycle="12")
        self.assertTrue(said.startswith("It's "))
        self.assertFalse(any(c.isdigit() for c in said), said)


class TestConfiguration(ClockTestCase):
    def test_set_zone_persists_and_is_used(self):
        self.assertEqual(clock.set_zone("Asia/Tokyo"), "Asia/Tokyo")
        self.assertEqual(json.loads(self.cfg.read_text())["timezone"],
                         "Asia/Tokyo")
        self.assertEqual(getattr(clock.resolve_zone(None), "key", None),
                         "Asia/Tokyo")

    def test_config_beats_env(self):
        os.environ["VIBEY_TZ"] = "Europe/Paris"
        clock.set_zone("Asia/Tokyo")
        self.assertEqual(getattr(clock.resolve_zone(None), "key", None),
                         "Asia/Tokyo")

    def test_env_used_when_nothing_saved(self):
        os.environ["VIBEY_TZ"] = "Europe/Paris"
        self.assertEqual(getattr(clock.resolve_zone(None), "key", None),
                         "Europe/Paris")

    def test_bare_city_name_resolves(self):
        self.assertEqual(getattr(clock.resolve_zone("Tokyo"), "key", None),
                         "Asia/Tokyo")
        self.assertEqual(getattr(clock.resolve_zone("new york"), "key", None),
                         "America/New_York")

    def test_hour_cycle_from_locale(self):
        os.environ["LC_TIME"] = "en_US.UTF-8"
        self.assertEqual(clock.hour_cycle(), "12")
        os.environ["LC_TIME"] = "fr_FR.UTF-8"
        self.assertEqual(clock.hour_cycle(), "24")

    def test_saved_hour_cycle_beats_locale(self):
        os.environ["LC_TIME"] = "fr_FR.UTF-8"
        clock.set_hour_cycle("12")
        self.assertEqual(clock.hour_cycle(), "12")

    def test_bad_hour_cycle_rejected_and_not_saved(self):
        with self.assertRaises(ValueError):
            clock.set_hour_cycle("13")
        self.assertFalse(self.cfg.exists())

    def test_corrupt_config_falls_back_instead_of_crashing(self):
        self.cfg.write_text("{not json at all")
        self.assertIsNotNone(clock.resolve_zone(None))
        self.assertIn(clock.hour_cycle(), ("12", "24"))


class TestErrorHandling(ClockTestCase):
    def test_unknown_zone_raises_for_callers_who_care(self):
        with self.assertRaises(clock.UnknownZone):
            clock.resolve_zone("Middle Earth")
        with self.assertRaises(clock.UnknownZone):
            clock.set_zone("Middle Earth")

    def test_bad_zone_is_not_persisted(self):
        clock.set_zone("Asia/Tokyo")
        with self.assertRaises(clock.UnknownZone):
            clock.set_zone("Middle Earth")
        self.assertEqual(json.loads(self.cfg.read_text())["timezone"],
                         "Asia/Tokyo")

    def test_report_apologises_rather_than_raising(self):
        said = clock.time_report("Middle Earth")
        self.assertIn("don't know", said)
        self.assertNotIn("Traceback", said)

    def test_report_speaks_and_remembers(self):
        said = clock.time_report("Tokyo", remember=True)
        self.assertIn("Tokyo", said)
        self.assertEqual(json.loads(self.cfg.read_text())["timezone"], "Tokyo")

    def test_report_survives_an_unwritable_config(self):
        clock.CONFIG_PATH = Path(self._tmp.name) / "nope" / "clock.json"
        said = clock.time_report("Tokyo", remember=True)
        self.assertTrue(said and not said.startswith("Traceback"))

    def test_report_always_returns_something_sayable(self):
        for zone in (None, "Tokyo", "Asia/Tokyo", "", "   ", "Middle Earth"):
            with self.subTest(zone=zone):
                said = clock.time_report(zone or None)
                self.assertTrue(said.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
