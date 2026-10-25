import json
from pathlib import Path
import subprocess
import unittest

from video_pipeline.modal_pipeline import _render_transcript_payload, _validate_supplied_word_timings
from video_pipeline.models import WordTiming


class SuppliedCaptionTimingsTests(unittest.TestCase):
    def test_preserves_zero_length_marker_without_inventing_a_word_interval(self):
        words = [WordTiming("Una", 1, 1), WordTiming("prueba", 1.1, 1.5)]
        _validate_supplied_word_timings(words, 5)
        self.assertEqual(words[0].start, words[0].end)
        self.assertEqual(len(words), 2)

    def test_rejects_trailing_or_remote_anchors_and_too_many_shared_words(self):
        for words in [
            [WordTiming("Una", 1, 1)],
            [WordTiming("Una", 1, 1), WordTiming("prueba", 2, 2.5)],
            [WordTiming("Uno dos tres", 1, 1), WordTiming("cuatro", 1.1, 1.4)],
        ]:
            with self.subTest(words=words), self.assertRaises(ValueError):
                _validate_supplied_word_timings(words, 5)

    def test_rejects_nonfinite_negative_and_reversed_intervals(self):
        for start, end in [(float("nan"), 1), (1, float("inf")), (-1, 0), (2, 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                _validate_supplied_word_timings([WordTiming("prueba", start, end)], 5)

    def test_python_payload_passes_the_actual_remotion_coverage_validator(self):
        words = [WordTiming("Una", 1, 1), WordTiming("prueba", 1.1, 1.5)]
        payload = _render_transcript_payload(words)
        self.assertEqual(payload["text"], "Una prueba")
        self.assertEqual(len(payload["words"]), 2)
        self.assertEqual(payload["words"][0]["start"], payload["words"][0]["end"])
        # Contract test: invoke only the pure validator, never a renderer or provider.
        process = subprocess.run(
            [
                "node", "--input-type=module", "-e",
                'import {prepareCaptionTrack} from "./remotion-renderer/src/caption-coverage.mjs";'
                'let input=""; for await (const chunk of process.stdin) input+=chunk;'
                'const result=prepareCaptionTrack(JSON.parse(input),'
                '[{sourceStart:0,frames:300,outputStart:0}],60,2);'
                'console.log(JSON.stringify({groups:result.groups.length}));',
            ],
            input=json.dumps(payload), capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[1], timeout=15, check=True,
        )
        self.assertEqual(json.loads(process.stdout)["groups"], 1)
