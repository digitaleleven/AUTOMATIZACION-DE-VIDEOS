import json
import unittest

from video_pipeline.webapp import _public_job


class PublicAnalysisStateTests(unittest.TestCase):
    def public(self, analysis):
        return _public_job({
            "id": "a" * 32, "source_id": "b" * 32,
            "status": "completed", "phase": "completed", "progress": 100,
            "error": None, "estimated_cost_usd": None,
            "output_id": "result.mp4", "created_at": 0, "updated_at": 0,
            "result_json": json.dumps({"gemini_analysis": analysis}),
        })

    def test_seo_failure_is_independent_of_completed_video_and_keeps_copy_empty(self):
        job = self.public({
            "model": "gemini-3.8-flash", "status": "failed",
            "error": "private provider diagnostic", "copy_and_seo": {},
        })
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["gemini_analysis"]["status"], "failed")
        self.assertEqual(job["gemini_analysis"]["copy_and_seo"], {})
        self.assertEqual(job["output_id"], "result.mp4")
        self.assertNotIn("private provider diagnostic", json.dumps(job))

    def test_saved_real_copy_is_preserved_with_completed_analysis_status(self):
        copy = {"hook": "Hook de prueba", "caption": "Caption de prueba", "hashtags": ["#prueba"]}
        job = self.public({"model": "gemini-3.8-flash", "copy_and_seo": copy})
        self.assertEqual(job["gemini_analysis"]["status"], "completed")
        self.assertEqual(job["gemini_analysis"]["copy_and_seo"], copy)
