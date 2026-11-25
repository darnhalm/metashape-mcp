import unittest

from workflow_catalog import (
    VERSION,
    aerial_workflow_prompt,
    alignment_diagnostic_prompt,
    batch_export_prompt,
    gis_quarry_workflow_prompt,
    object_workflow_prompt,
    quality_settings_prompt,
    safe_workflow_catalog,
)


class WorkflowCatalogTests(unittest.TestCase):
    def test_catalog_has_version_lifecycles_and_six_workflows(self):
        catalog = safe_workflow_catalog()
        self.assertEqual(VERSION, catalog["version"])
        self.assertEqual({"object", "aerial", "gis_quarry", "diagnose_alignment",
                          "batch_export", "quality_settings"}, set(catalog["workflows"]))
        self.assertIn("copy_job", catalog["lifecycles"])
        self.assertIn("user_script", catalog["lifecycles"])

    def test_catalog_is_returned_as_a_copy(self):
        first = safe_workflow_catalog()
        first["principles"].clear()
        self.assertTrue(safe_workflow_catalog()["principles"])

    def test_every_prompt_keeps_review_and_script_guards(self):
        for prompt in (object_workflow_prompt(), aerial_workflow_prompt(), gis_quarry_workflow_prompt(),
                       alignment_diagnostic_prompt(), batch_export_prompt(), quality_settings_prompt()):
            with self.subTest(prompt=prompt[:30]):
                self.assertIn("preflight", prompt)
                self.assertIn("copy_estimate", prompt)
                self.assertIn("accept_job_result", prompt)
                self.assertIn("reject_job_result", prompt)
                self.assertIn("Не вызывай run_user_script", prompt)

    def test_object_prompt_covers_model_validation(self):
        prompt = object_workflow_prompt("GLB для архива", "маски уже есть")
        self.assertIn("get_model_statistics", prompt)
        self.assertIn("export_model", prompt)
        self.assertIn("GLB для архива", prompt)
        self.assertIn("маски уже есть", prompt)

    def test_aerial_prompt_requires_independent_accuracy_evidence(self):
        prompt = aerial_workflow_prompt()
        self.assertIn("check points", prompt)
        self.assertIn("независимая точность не подтверждена", prompt)
        self.assertIn("не выдавай GPS EXIF за RTK", prompt)

    def test_gis_prompt_preserves_units_and_shape_limit(self):
        prompt = gis_quarry_workflow_prompt()
        self.assertIn("get_shape_measurements", prompt)
        self.assertIn("создание и редактирование shapes пока не предоставлено", prompt)
        self.assertIn("chunk units", prompt)
        self.assertIn("export_tiled_model", prompt)

    def test_new_diagnostic_and_export_prompts_keep_evidence_gates(self):
        diagnostic = alignment_diagnostic_prompt()
        self.assertIn("get_alignment_diagnostics", diagnostic)
        self.assertIn("не применяй универсальные пороги", diagnostic)
        export = batch_export_prompt()
        self.assertIn("файлы и размеры", export)
        self.assertIn("точные ключи", export)
        quality = quality_settings_prompt()
        self.assertIn("Не обещай время и память без измерений", quality)
        self.assertIn("черновой/сбалансированный/детальный", quality)


if __name__ == "__main__":
    unittest.main()
