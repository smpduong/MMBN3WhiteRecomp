#!/usr/bin/env python3
"""Offline parser fixtures. No game launches or personal-file writes."""
import unittest
from pathlib import Path
from unittest import mock

import audio_replay_check as check


class AudioReplayFixtures(unittest.TestCase):
    def test_numeric_fields_preserve_hexadecimal_addresses(self):
        actual = check.numeric_fields(
            "final_pc=0x08000366 mode=0X12 delta=-0x10 "
            "bridge_underrun=0(0.00/s) corr=+0.005% fill_ms=25.7 count=0012")
        self.assertEqual(actual, {"final_pc": 0x08000366, "mode": 18,
                                 "delta": -16, "bridge_underrun": 0,
                                 "corr": 0.005, "fill_ms": 25.7, "count": 12})

    def test_trace_ends_released_within_budget(self):
        self.assertEqual(check.bounded_trace([(0, 0), (1, 0)], 1), [(0, 0x3FF)])
        trace = check.bounded_trace([(0, 0x3FF), (5, 0x3FE), (15, 0x3FE)], 10)
        self.assertEqual(trace, [(0, 0x3FF), (5, 0x3FE), (9, 0x3FF)])

    def test_inference_maps_input_to_preceding_frame(self):
        lines = ("[event-probe] input_ns=90 keyinput=0x3FF\n"
                 "[event-probe] audio_push_ns=100 count=1096\n"
                 "[event-probe] input_ns=101 keyinput=0x3FE\n"
                 "[event-probe] audio_push_ns=200 count=1096\n"
                 "[event-probe] input_ns=201 keyinput=0x3FF\n")
        with mock.patch.object(check, "read_phases", return_value=[{"frame": 1}, {"frame": 3}]), \
                mock.patch.object(Path, "read_text", return_value=lines), \
                mock.patch.object(check, "file_hash", return_value="fixture"):
            trace, provenance = check.infer_trace(Path("/fixture"))
        self.assertEqual(trace, [(0, 0x3FF), (1, 0x3FE), (3, 0x3FF)])
        self.assertEqual(provenance["kind"], "inferred_from_host_event_probe")

    def test_inference_rejects_missing_audio_frame_alignment(self):
        with mock.patch.object(check, "read_phases", return_value=[{"frame": 1}]), \
                mock.patch.object(Path, "read_text", return_value=""):
            with self.assertRaisesRegex(ValueError, "cannot infer trace"):
                check.infer_trace(Path("/fixture"))

    def test_callback_frame_gap_and_coverage_metrics(self):
        events = ("[event-probe] audio_push_ns=100000000 count=1096\n"
                  "[event-probe] audio_pull_ns=99000000 frames=512 pulled=512 fill_before_ms=26.0 fill_after_ms=18.2 source_pos=520.5 stretched=0\n"
                  "[event-probe] audio_push_ns=140000000 count=2048\n"
                  "[event-probe] audio_pull_ns=132000000 frames=512 pulled=1024 fill_before_ms=31.0 fill_after_ms=23.2 source_pos=1032.5 stretched=0\n"
                  "[event-probe] audio_pull_observations_dropped=2\n"
                  "[gba-audio-probe] pushes=120 audio=2.0s fill_ms=25.7 corr=+0.005%\n")
        stdout = ("final_pc=0x08000366 ppu_frames=120 frames_presented=120\n"
                  "self_heal_coverage=FULLY_STATIC dispatch_misses=0 interpreted_insns=0 healed_native=0\n")
        with mock.patch.object(check, "read_phases", return_value=[{"frame": 1}, {"frame": 3}]), \
                mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(Path, "read_text", lambda path, **kwargs: events if path.name == "events.log" else stdout):
            metrics = check.analyze_logs(Path("/fixture"))
        self.assertEqual(metrics["callback"]["observation_count"], 2)
        self.assertEqual(metrics["callback"]["observations_dropped"], 2)
        self.assertEqual(metrics["callback"]["gaps_over_25ms"][0]["gap_ms"], 33.0)
        self.assertEqual(len(metrics["chunks_over_1200"]), 1)
        self.assertEqual(metrics["frame_gaps"][0]["skipped_frames"], 1)
        self.assertEqual(metrics["coverage"]["status"], "FULLY_STATIC")
        self.assertEqual(metrics["final_runtime_fields"]["final_pc"], 0x08000366)


if __name__ == "__main__":
    unittest.main()
