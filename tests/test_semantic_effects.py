import unittest

from video_pipeline.models import WordTiming
from video_pipeline.semantic_effects import (
    build_semantic_overlay_filter,
    plan_semantic_effects,
    retained_source_ranges,
    semantic_effect_svg,
    semantic_overlay_input_specs,
)


class SemanticEffectsTests(unittest.TestCase):
    CUTS = [
        {"start_seconds": 0.0, "end_seconds": 0.55},
        {"start_seconds": 33.72, "end_seconds": 34.35},
        {"start_seconds": 36.54, "end_seconds": 37.07},
    ]

    def test_exact_word_cues_are_mapped_after_cuts_without_new_transcription(self):
        cues = plan_semantic_effects(
            [
                WordTiming("tónico", 30.82, 31.10),
                WordTiming("perlado", 51.56, 52.26),
                WordTiming("¡Mua!", 58.22, 58.74),
            ],
            self.CUTS,
            59.98,
        )
        self.assertEqual(
            [(cue["effect"], cue["trigger_word"], cue["start_seconds"]) for cue in cues],
            [("droplet", "tonico", 30.27), ("pearl", "perlado", 49.85), ("kiss", "mua", 56.51)],
        )
        self.assertEqual([cue["duration_seconds"] for cue in cues], [0.95, 1.05, 0.85])
        self.assertTrue(all(cue["position"] == "topRight" for cue in cues))

    def test_unrelated_or_segment_level_text_does_not_create_an_effect(self):
        cues = plan_semantic_effects(
            [
                WordTiming("hola", 1.0, 1.3),
                WordTiming("Mua", 2.0, 2.4),
            ],
            [],
            10.0,
        )
        self.assertEqual([(cue["effect"], cue["start_seconds"]) for cue in cues], [("kiss", 2.0)])

    def test_triggers_removed_by_cuts_are_skipped(self):
        cuts = [{"start_seconds": 5.0, "end_seconds": 6.0}]
        cues = plan_semantic_effects([WordTiming("mua", 5.2, 5.7)], cuts, 10.0)
        self.assertEqual(cues, [])

    def test_silence_removal_intervals_are_converted_to_retained_ranges(self):
        ranges = retained_source_ranges(self.CUTS, 59.98)
        self.assertEqual(
            ranges,
            [(0.55, 33.72), (34.35, 36.54), (37.07, 59.98)],
        )
        self.assertAlmostEqual(sum(end - start for start, end in ranges), 58.27, places=3)

    def test_effect_vectors_are_transparent_local_svg_and_allowlisted(self):
        for name in ("kiss-lips", "kiss-heart", "water-drop", "pearl-glints"):
            svg = semantic_effect_svg(name)
            self.assertIn("<svg", svg)
            self.assertIn("no third-party asset", svg)
            self.assertNotIn("<rect", svg)
        with self.assertRaisesRegex(ValueError, "no permitido"):
            semantic_effect_svg("<svg/>")

    def test_overlay_graph_is_dynamic_faded_and_uses_only_allowlisted_assets(self):
        cues = plan_semantic_effects(
            [
                WordTiming("tónico", 30.82, 31.10),
                WordTiming("perlado", 51.56, 52.26),
                WordTiming("¡Mua!", 58.22, 58.74),
            ],
            self.CUTS,
            59.98,
        )
        specs = semantic_overlay_input_specs(cues, "work/effects")
        graph, output_label, inputs_used = build_semantic_overlay_filter(cues)
        self.assertEqual([spec["input_index"] for spec in specs], [1, 2, 3, 4])
        self.assertEqual(inputs_used, 4)
        self.assertEqual(output_label, "vfx3")
        self.assertEqual(graph.count("overlay="), 4)
        self.assertEqual(graph.count("fade=t=in"), 4)
        self.assertIn("enable='between(t,56.510,57.360)'", graph)
        self.assertIn("fade=t=in:st=56.510", graph)
        self.assertIn("setpts=PTS-STARTPTS[fxin3]", graph)
        self.assertNotIn("setpts=PTS-STARTPTS+56.510/TB", graph)
        self.assertTrue(all(spec["path"].startswith("work/effects/") for spec in specs))


if __name__ == "__main__":
    unittest.main()
