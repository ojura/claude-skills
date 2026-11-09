#!/usr/bin/env python3
"""Check frame attribution and partial-feedback reporting without a display."""

import importlib.util
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True

from ..verify.profile_gui import (desktop_workarea, fit_window_layout, scene_metrics,
                         validate_window_layout, window_rectangles)

from ..verify import runtime as packaged_smoke


def frame(counter, position, playing=True, run=1000000, clock=1):
    return dict(event="frame", view="presentation", scene="sample", playback_id=run,
                frame_clock_id=clock, frame_counter=counter,
                clock_us=1000000 + round(position*1e6),
                frame_time_us=1000000 + round(position*1e6),
                sample_clock_us=1000000 + round(position*1e6), displayed_time=position,
                playing=playing, anchor_clock_us=1000000, anchor_time=0, speed=1,
                target=0.03, render_us=2000, render_cpu_us=1500, width=100,
                height=60, refresh_interval_us=10000)


def receipt(items, clock=1):
    # The receipt may arrive after playback has moved to a different scene.
    return dict(event="timing", view="presentation", scene="next_scene",
                playback_id=2000000, playing=False, frame_clock_id=clock,
                clock_us=2000000, feedback_collected_us=2000001,
                completed_timings=[dict(counter=c, presentation_time_us=t, expired=False)
                                   for c, t in items])


class FrameAccounting(unittest.TestCase):
    def setUp(self):
        self.frames = [frame(1, 0), frame(2, 0.01), frame(3, 0.03, False),
                       frame(4, 0.03, False)]

    def metrics(self, events):
        return scene_metrics(events, "sample", 1000000, 0.03, "presentation")

    def test_late_receipts_follow_origin_through_scene_change(self):
        result = self.metrics(self.frames + [receipt([(1, 1000000), (2, 1010000),
                                                      (3, 1030000), (4, 1035000)])])
        self.assertEqual((result["frames"], result["completed_frames"], result["presented_frames"]),
                         (3, 3, 3))
        self.assertEqual(result["missing_completion_frames"], 0)
        self.assertAlmostEqual(result["presented_fps"], 200/3)
        self.assertEqual(result["presentation_gap_counts"]["16.7"], 1)
        self.assertEqual(result["per_view_clock_error_ms"]["max"], 0)
        self.assertTrue(result["complete_feedback"])

    def test_clock_counter_collisions_are_isolated(self):
        result = self.metrics(self.frames + [receipt([(1, 1000000), (2, 1010000),
                                                      (3, 1030000)], clock=2)])
        self.assertEqual(result["completed_frames"], 0)
        self.assertEqual(result["missing_completion_frames"], 3)

    def test_coalesced_refresh_preserves_known_continuity(self):
        result = self.metrics(self.frames + [receipt([(1, 1000000), (2, 1030000), (3, 1030000)])])
        self.assertEqual(result["presented_frames"], 2)
        self.assertEqual(result["positive_timestamp_frames"], 3)
        self.assertEqual(result["coalesced_presentation_frames"], 1)
        self.assertAlmostEqual(result["presented_fps"], 100/3)
        self.assertEqual(result["scene_step_error_ms"]["max"], 0)
        self.assertEqual(result["unknown_feedback_span_count"], 0)
        self.assertEqual(result["feedback_coverage"], 1)

    def test_missing_final_feedback_is_explicit(self):
        result = self.metrics(self.frames + [receipt([(1, 1000000), (2, 1010000)])])
        self.assertEqual(result["missing_completion_frames"], 1)

    def test_unavailable_feedback_is_not_a_presentation(self):
        event = receipt([(1, 1000000), (2, 1010000), (3, 0)])
        event["completed_timings"][-1]["expired"] = True
        result = self.metrics(self.frames + [event])
        self.assertEqual(result["unavailable_presentation_frames"], 1)
        self.assertEqual(result["expired_frames"], 1)
        self.assertEqual(result["presented_frames"], 2)
        self.assertEqual(result["missing_completion_frames"], 0)

    def test_other_runs_do_not_enter_scene_sample(self):
        result = self.metrics([frame(0, 0, False, run=500000)] + self.frames +
                              [receipt([(0, 900000), (1, 1000000), (2, 1010000), (3, 1030000)])])
        self.assertEqual(result["frames"], 3)
        self.assertEqual(result["completed_frames"], 3)

    def test_cpu_and_wall_time_remain_separate(self):
        result = self.metrics(self.frames)
        self.assertEqual(result["native_draw_ms"]["mean"], 2)
        self.assertEqual(result["native_cpu_ms"]["mean"], 1.5)
        self.assertEqual(result["native_wall_minus_cpu_ms"]["mean"], 0.5)

    def test_future_gdk_clock_does_not_change_monotonic_playback_metrics(self):
        frames = [dict(item, frame_time_us=item["frame_time_us"] + 5000000 + index*10000)
                  for index, item in enumerate(self.frames)]
        result = self.metrics(frames)
        self.assertEqual(result["per_view_clock_error_ms"]["max"], 0)
        self.assertAlmostEqual(result["draw_fps"], 200/3)
        self.assertEqual(result["frame_clock_fps"], 40)
        self.assertEqual(result["frame_clock_lead_ms"]["max"], 5020)
        self.assertEqual(result["first_final_wall_seconds"], 0.03)
        self.assertEqual(result["expected_wall_seconds"], 0.03)
        self.assertFalse(result["early_final_frame"])
        self.assertEqual(result["timing_errors"], [])

    def test_future_sample_fails_even_when_interpolation_equation_matches(self):
        frames = [dict(item, clock_us=item["clock_us"] - 5000) for item in self.frames]
        result = self.metrics(frames)
        self.assertEqual(result["per_view_clock_error_ms"]["max"], 0)
        self.assertEqual(result["sample_clock_errors"]["after_record"], 3)
        self.assertTrue(result["early_final_frame"])
        self.assertEqual(len(result["timing_errors"]), 2)

    def test_premature_stopped_final_fails_independent_wall_check(self):
        frames = [frame(1, 0), frame(2, 0.01),
                  dict(frame(3, 0.03, False), clock_us=1011000, sample_clock_us=1011000)]
        result = self.metrics(frames)
        self.assertEqual(result["per_view_clock_error_ms"]["max"], 0)
        self.assertFalse(any(result["sample_clock_errors"].values()))
        self.assertTrue(result["early_final_frame"])
        self.assertEqual(result["first_final_wall_seconds"], 0.011)
        self.assertEqual(len(result["timing_errors"]), 1)

    def test_wall_deadline_accounts_for_anchor_position_and_speed(self):
        frames = [dict(frame(1, 0.01), anchor_time=0.01, speed=4,
                       clock_us=1000000, sample_clock_us=1000000),
                  dict(frame(2, 0.03, False), anchor_time=0.01, speed=4,
                       clock_us=1005000, sample_clock_us=1005000)]
        result = self.metrics(frames)
        self.assertAlmostEqual(result["expected_wall_seconds"], 0.005)
        self.assertEqual(result["first_final_wall_seconds"], 0.005)
        self.assertEqual(result["timing_errors"], [])

    def test_missing_sample_cannot_fall_back_to_the_gdk_clock(self):
        frames = [dict(item) for item in self.frames]
        for item in frames:
            del item["sample_clock_us"]
        result = self.metrics(frames)
        self.assertEqual(result["sample_clock_errors"]["missing"], 3)
        self.assertEqual(len(result["timing_errors"]), 1)

    def test_playing_sample_must_follow_its_shared_anchor(self):
        frames = [dict(self.frames[0], sample_clock_us=999999)] + self.frames[1:]
        result = self.metrics(frames)
        self.assertEqual(result["sample_clock_errors"]["before_anchor"], 1)
        self.assertEqual(len(result["timing_errors"]), 1)

    def assert_unknown_middle(self, result):
        self.assertEqual(result["positive_timestamp_frames"], 2)
        self.assertAlmostEqual(result["feedback_coverage"], 2/3)
        self.assertFalse(result["complete_feedback"])
        self.assertIsNone(result["presented_fps"])
        self.assertAlmostEqual(result["observed_positive_fps"], 100/3)
        self.assertEqual(result["unknown_feedback_span_count"], 1)
        self.assertEqual(result["unknown_feedback_span_ms"]["max"], 30)
        self.assertIsNone(result["presentation_interval_ms"])
        self.assertIsNone(result["scene_step_error_ms"])
        self.assertTrue(all(value == 0 for value in result["presentation_gap_counts"].values()))

    def test_unavailable_middle_frame_splits_verified_intervals(self):
        result = self.metrics(self.frames + [receipt([(1, 1000000), (2, 0), (3, 1030000)])])
        self.assert_unknown_middle(result)

    def test_expired_middle_frame_splits_verified_intervals(self):
        event = receipt([(1, 1000000), (2, 0), (3, 1030000)])
        event["completed_timings"][1]["expired"] = True
        result = self.metrics(self.frames + [event])
        self.assert_unknown_middle(result)
        self.assertEqual(result["expired_frames"], 1)

    def test_missing_middle_frame_splits_verified_intervals(self):
        result = self.metrics(self.frames + [receipt([(1, 1000000), (3, 1030000)])])
        self.assert_unknown_middle(result)
        self.assertEqual(result["missing_completion_frames"], 1)

    def test_missing_edge_frame_keeps_known_interval_but_marks_partial_coverage(self):
        result = self.metrics(self.frames + [receipt([(1, 1000000), (2, 1010000)])])
        self.assertEqual(result["unknown_feedback_span_count"], 0)
        self.assertEqual(result["presentation_interval_ms"]["count"], 1)
        self.assertEqual(result["presentation_interval_ms"]["max"], 10)
        self.assertAlmostEqual(result["feedback_coverage"], 2/3)
        self.assertFalse(result["complete_feedback"])
        self.assertIsNone(result["presented_fps"])
        self.assertEqual(result["observed_positive_fps"], 100)

    def test_unknown_feedback_span_is_not_reported_as_a_long_stall(self):
        frames = [frame(1, 0), frame(2, 0.01), frame(3, 0.02), frame(4, 0.03, False)]
        result = self.metrics(frames + [receipt([(1, 1000000), (2, 0), (3, 0), (4, 1108332)])])
        self.assertEqual(result["unknown_feedback_span_count"], 1)
        self.assertEqual(result["unknown_feedback_span_ms"]["max"], 108.332)
        self.assertIsNone(result["presentation_interval_ms"])
        self.assertEqual(result["presentation_gap_counts"]["33.4"], 0)


def delayed_receipt_fixture():
    frames = [frame(1, 0), frame(2, 0.01), frame(3, 0.03, False)]
    event = receipt([(1, 1000000), (2, 1010000), (3, 1030355)])
    event.update(clock_us=1030000, feedback_collected_us=1030500)
    return frames, event


class FeedbackClockSemantics(unittest.TestCase):
    def setUp(self):
        self.frames, self.receipt = delayed_receipt_fixture()

    def metrics(self):
        return scene_metrics(self.frames + [self.receipt], "sample", 1000000, 0.03, "presentation")

    def test_feedback_after_event_start_uses_actual_collection_bound(self):
        result = self.metrics()
        self.assertEqual(result["timing_errors"], [])
        self.assertEqual(result["feedback_timestamp_bounds_verified_frames"], 3)
        self.assertEqual(result["feedback_timestamp_bounds_unverified_frames"], 0)

    def test_presentation_at_exact_collection_boundary_is_valid(self):
        self.receipt["completed_timings"][-1]["presentation_time_us"] = 1030500
        self.assertEqual(self.metrics()["timing_errors"], [])

    def test_presentation_after_collection_is_rejected(self):
        self.receipt["completed_timings"][-1]["presentation_time_us"] = 1030501
        self.assertEqual(self.metrics()["feedback_clock_errors"]["presentation_after_collection"], 1)
        self.assertTrue(self.metrics()["timing_errors"])

    def test_presentation_before_its_originating_sample_is_rejected(self):
        self.receipt["completed_timings"][1]["presentation_time_us"] = 1009999
        self.assertEqual(self.metrics()["feedback_clock_errors"]["presentation_before_sample"], 1)
        self.assertTrue(self.metrics()["timing_errors"])

    def test_collection_before_event_construction_is_rejected(self):
        self.receipt["clock_us"] = 1030501
        self.assertEqual(self.metrics()["feedback_clock_errors"]["collection_before_event"], 1)
        self.assertTrue(self.metrics()["timing_errors"])

    def test_historical_missing_collection_time_is_reported_as_unverified(self):
        del self.receipt["feedback_collected_us"]
        result = self.metrics()
        self.assertEqual(result["timing_errors"], [])
        self.assertEqual(result["feedback_collection_missing_events"], 1)
        self.assertEqual(result["feedback_timestamp_bounds_verified_frames"], 0)
        self.assertEqual(result["feedback_timestamp_bounds_unverified_frames"], 3)
        self.assertTrue(result["complete_feedback"])
        self.assertIsNotNone(result["presented_fps"])

    def test_present_but_invalid_collection_time_is_rejected(self):
        for value in (None, True, 0, "1030500"):
            with self.subTest(value=value):
                self.receipt["feedback_collected_us"] = value
                self.assertEqual(self.metrics()["feedback_clock_errors"]["invalid_collection_time"], 1)
                self.assertTrue(self.metrics()["timing_errors"])

    def test_empty_completion_arrays_still_carry_collection_metadata(self):
        self.receipt["completed_timings"] = []
        self.assertEqual(self.metrics()["feedback_collection_missing_events"], 0)
        del self.receipt["feedback_collected_us"]
        self.assertEqual(self.metrics()["feedback_collection_missing_events"], 1)
        self.assertEqual(self.metrics()["timing_errors"], [])

    def test_unavailable_and_expired_feedback_skip_positive_timestamp_bounds(self):
        for timing in self.receipt["completed_timings"]:
            timing["presentation_time_us"] = 0
        self.receipt["completed_timings"][-1]["expired"] = True
        self.assertEqual(self.metrics()["timing_errors"], [])
        result = self.metrics()
        self.assertEqual(result["positive_timestamp_frames"], 0)
        self.assertEqual(result["unavailable_presentation_frames"], 3)
        self.assertEqual(result["expired_frames"], 1)
        self.assertEqual(result["missing_completion_frames"], 0)


@unittest.skipIf(packaged_smoke is None, "GitHub-only packaged smoke script is absent from this source copy")
class PackagedFeedbackClockSemantics(unittest.TestCase):
    def setUp(self):
        self.frames, self.receipt = delayed_receipt_fixture()

    def smoke(self):
        return packaged_smoke.validate_feedback(self.frames + [self.receipt])

    def test_feedback_after_event_start_uses_actual_collection_bound(self):
        self.assertEqual(self.smoke()["positive_timestamp_frames"], 3)

    def test_presentation_at_exact_collection_boundary_is_valid(self):
        self.receipt["completed_timings"][-1]["presentation_time_us"] = 1030500
        self.assertEqual(self.smoke()["positive_timestamp_frames"], 3)

    def test_presentation_after_collection_is_rejected(self):
        self.receipt["completed_timings"][-1]["presentation_time_us"] = 1030501
        with self.assertRaises(AssertionError):
            self.smoke()

    def test_presentation_before_its_originating_sample_is_rejected(self):
        self.receipt["completed_timings"][1]["presentation_time_us"] = 1009999
        with self.assertRaises(AssertionError):
            self.smoke()

    def test_collection_before_event_construction_is_rejected(self):
        self.receipt["clock_us"] = 1030501
        with self.assertRaises(AssertionError):
            self.smoke()

    def test_current_package_requires_collection_time(self):
        del self.receipt["feedback_collected_us"]
        with self.assertRaises(AssertionError):
            self.smoke()

    def test_present_but_invalid_collection_time_is_rejected(self):
        for value in (None, True, 0, "1030500"):
            with self.subTest(value=value):
                self.receipt["feedback_collected_us"] = value
                with self.assertRaises(AssertionError):
                    self.smoke()

    def test_empty_completion_arrays_still_carry_collection_metadata(self):
        self.receipt["completed_timings"] = []
        self.assertEqual(self.smoke()["collection_events"], 1)
        del self.receipt["feedback_collected_us"]
        with self.assertRaises(AssertionError):
            self.smoke()

    def test_unavailable_and_expired_feedback_skip_positive_timestamp_bounds(self):
        for timing in self.receipt["completed_timings"]:
            timing["presentation_time_us"] = 0
        self.receipt["completed_timings"][-1]["expired"] = True
        result = self.smoke()
        self.assertEqual(result["positive_timestamp_frames"], 0)
        self.assertEqual(result["unavailable_timestamp_frames"], 2)
        self.assertEqual(result["expired_frames"], 1)
        self.assertEqual(result["pending_frames"], 0)


class WindowLayout(unittest.TestCase):
    ORIGINAL = {
        "presenter": {"x": 1470, "y": 60, "width": 1000, "height": 800},
        "presentation": {"x": 10, "y": 60, "width": 1440, "height": 1080},
    }

    def test_full_size_audience_and_shorter_presenter_fit(self):
        scale, geometry = fit_window_layout(2560, 1600)
        self.assertEqual(scale, 1)
        self.assertEqual(geometry, self.ORIGINAL)

    def assert_scaled_layout(self, width, height):
        scale, geometry = fit_window_layout(width, height)
        self.assertAlmostEqual(scale, min(1, width/2480, height/1140))
        for view, box in geometry.items():
            self.assertGreaterEqual(box["x"], 0)
            self.assertGreaterEqual(box["y"], 0)
            self.assertLessEqual(box["x"] + box["width"], width)
            self.assertLessEqual(box["y"] + box["height"], height)
            for dimension, value in box.items():
                self.assertLessEqual(abs(value - self.ORIGINAL[view][dimension]*scale), 1)

    def test_full_hd_windows_fit_without_distorting_the_layout(self):
        self.assert_scaled_layout(1920, 1080)

    def test_small_desktop_windows_fit_without_distorting_the_layout(self):
        self.assert_scaled_layout(1280, 720)

    def test_workarea_reserves_the_dock_and_selects_the_active_desktop(self):
        properties = ("_NET_CURRENT_DESKTOP(CARDINAL) = 1\n"
                      "_NET_WORKAREA(CARDINAL) = 0, 0, 2560, 1600, 66, 32, 2494, 1568\n")
        area = desktop_workarea(properties, 2560, 1600)
        self.assertEqual(area, {"x": 66, "y": 32, "width": 2494, "height": 1568})
        scale, geometry = fit_window_layout(area["width"], area["height"])
        self.assertEqual(scale, 1)
        self.assertEqual(geometry["presentation"]["width"], 1440)

    def test_missing_desktop_index_uses_identical_workareas(self):
        properties = ("_NET_CURRENT_DESKTOP:  not found.\n"
                      "_NET_WORKAREA(CARDINAL) = 66, 32, 2494, 1568, 66, 32, 2494, 1568\n"
                      "_GTK_WORKAREAS_D0(CARDINAL) = 66, 32, 2494, 1568\n")
        self.assertEqual(desktop_workarea(properties, 2560, 1600),
                         {"x": 66, "y": 32, "width": 2494, "height": 1568})

    def test_missing_desktop_index_uses_one_valid_workarea(self):
        properties = "_NET_WORKAREA(CARDINAL) = 66, 32, 2494, 1568\n"
        self.assertEqual(desktop_workarea(properties, 2560, 1600),
                         {"x": 66, "y": 32, "width": 2494, "height": 1568})

    def test_missing_desktop_index_rejects_differing_workareas(self):
        properties = ("_NET_CURRENT_DESKTOP:  not found.\n"
                      "_NET_WORKAREA(CARDINAL) = 0, 32, 2560, 1568, 66, 32, 2494, 1568\n")
        self.assertEqual(desktop_workarea(properties, 2560, 1600),
                         {"x": 0, "y": 0, "width": 2560, "height": 1600})

    def test_missing_desktop_index_rejects_malformed_or_invalid_workareas(self):
        for values in ("", "66, 32, 2494", "66, 32, 2494, 1568, 66",
                       "-1, 32, 2494, 1568, -1, 32, 2494, 1568",
                       "66, 32, 0, 1568", "66, 32, 2494, 0",
                       "66, 32, 2495, 1568", "66, 32, 2494, 1569"):
            with self.subTest(values=values):
                properties = ("_NET_CURRENT_DESKTOP:  not found.\n"
                              f"_NET_WORKAREA(CARDINAL) = {values}\n"
                              "_GTK_WORKAREAS_D0(CARDINAL) = 66, 32, 2494, 1568\n")
                self.assertEqual(desktop_workarea(properties, 2560, 1600),
                                 {"x": 0, "y": 0, "width": 2560, "height": 1600})

    def test_missing_workarea_does_not_read_the_following_property(self):
        properties = ("_NET_CURRENT_DESKTOP:  not found.\n"
                      "_NET_WORKAREA:  not found.\n"
                      "_GTK_WORKAREAS_D0(CARDINAL) = 66, 32, 2494, 1568\n")
        self.assertEqual(desktop_workarea(properties, 2560, 1600),
                         {"x": 0, "y": 0, "width": 2560, "height": 1600})

    def test_xvfb_without_workarea_uses_its_canvas(self):
        self.assertEqual(desktop_workarea("_NET_WORKAREA: not found", 3200, 1800),
                         {"x": 0, "y": 0, "width": 3200, "height": 1800})

    def test_height_limited_fit_reserves_actual_unscaled_decorations(self):
        for height in (1080, 900):
            with self.subTest(height=height):
                scale, geometry = fit_window_layout(2560, height, decoration_height=37)
                self.assertAlmostEqual(scale, (height - 37)/1140)
                for rectangle in geometry.values():
                    self.assertLessEqual(rectangle["y"] + rectangle["height"] + 37, height)

    def test_actual_client_and_decorated_coordinates_are_distinct(self):
        rectangles = window_rectangles("""
  Absolute upper-left X: 76
  Absolute upper-left Y: 129
  Width: 1440
  Height: 1080
  Frame extents: 0, 0, 37, 0
""")
        self.assertEqual(rectangles["client"], {"x": 76, "y": 129, "width": 1440, "height": 1080})
        self.assertEqual(rectangles["decorated"], {"x": 76, "y": 92, "width": 1440, "height": 1117})

    def test_actual_overlap_and_offscreen_windows_are_rejected(self):
        area = {"x": 66, "y": 32, "width": 2494, "height": 1568}
        audience = {"decorated": {"x": 76, "y": 92, "width": 1440, "height": 1117}}
        presenter = {"decorated": {"x": 1536, "y": 92, "width": 1000, "height": 837}}
        validate_window_layout({"presentation": audience, "presenter": presenter}, area)
        for x in (1500, 1561):
            with self.subTest(x=x):
                invalid = {"decorated": dict(presenter["decorated"], x=x)}
                with self.assertRaises(AssertionError):
                    validate_window_layout({"presentation": audience, "presenter": invalid}, area)


if __name__ == "__main__":
    unittest.main(verbosity=2)
