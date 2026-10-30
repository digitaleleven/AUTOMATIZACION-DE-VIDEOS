import unittest

from video_pipeline.cost_controls import (
    MAX_JOB_BUDGET_USD,
    MAX_GEMINI_INPUT_TOKENS,
    MAX_GEMINI_OUTPUT_TOKENS,
    estimate_gemini_cost_usd,
    estimate_job_budget,
    enforce_job_budget,
)


class CostControlTests(unittest.TestCase):
    def test_video_job_worst_case_estimate_stays_under_hard_ceiling(self):
        estimate = enforce_job_budget(59.98)
        self.assertLessEqual(estimate.maximum_usd, MAX_JOB_BUDGET_USD)
        self.assertGreater(estimate.modal_compute_usd, 0)
        self.assertGreater(estimate.buffer_usd, 0)

    def test_budget_bounds_tokens_duration_and_longer_jobs(self):
        self.assertLess(
            estimate_gemini_cost_usd(MAX_GEMINI_INPUT_TOKENS, MAX_GEMINI_OUTPUT_TOKENS),
            0.15,
        )
        with self.assertRaises(ValueError):
            estimate_gemini_cost_usd(MAX_GEMINI_INPUT_TOKENS + 1, 0)
        with self.assertRaises(ValueError):
            estimate_job_budget(1800.01)
        self.assertLessEqual(estimate_job_budget(1800.0).maximum_usd, MAX_JOB_BUDGET_USD)

