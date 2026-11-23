from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class EnvFlagTests(unittest.TestCase):
    def test_truthy_falsy_and_default_values(self) -> None:
        for raw in ("1", "true", "YES", " on "):
            with patch.dict(os.environ, {"RIDESURE_TEST_FLAG": raw}):
                self.assertTrue(config.env_flag("RIDESURE_TEST_FLAG"), raw)
        for raw in ("0", "false", "no", "off", ""):
            with patch.dict(os.environ, {"RIDESURE_TEST_FLAG": raw}):
                self.assertFalse(config.env_flag("RIDESURE_TEST_FLAG", True), raw)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RIDESURE_TEST_FLAG", None)
            self.assertTrue(config.env_flag("RIDESURE_TEST_FLAG", True))
            self.assertFalse(config.env_flag("RIDESURE_TEST_FLAG"))


class EnvIntTests(unittest.TestCase):
    def test_parses_integer_and_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {"RIDESURE_TEST_INT": " 9001 "}):
            self.assertEqual(config._env_int("RIDESURE_TEST_INT", 1), 9001)
        os.environ.pop("RIDESURE_TEST_INT", None)
        self.assertEqual(config._env_int("RIDESURE_TEST_INT", 42), 42)

    def test_invalid_integer_names_setting_without_leaking_value(self) -> None:
        with patch.dict(os.environ, {"RIDESURE_TEST_INT": "not-a-number-xyz"}):
            with self.assertRaises(RuntimeError) as ctx:
                config._env_int("RIDESURE_TEST_INT", 1)
        message = str(ctx.exception)
        self.assertIn("RIDESURE_TEST_INT", message)
        self.assertNotIn("not-a-number-xyz", message)


class ProjectPathTests(unittest.TestCase):
    def test_relative_paths_resolve_under_project_root(self) -> None:
        with patch.dict(os.environ, {"RIDESURE_TEST_DIR": "data/example"}):
            resolved = Path(config._project_path("RIDESURE_TEST_DIR", Path("unused")))
        self.assertEqual(resolved, config.PROJECT_ROOT / "data" / "example")

        absolute = (config.PROJECT_ROOT / "absolute-example").resolve()
        with patch.dict(os.environ, {"RIDESURE_TEST_DIR": str(absolute)}):
            self.assertEqual(
                Path(config._project_path("RIDESURE_TEST_DIR", Path("unused"))),
                absolute,
            )


class ValidateRequiredConfigTests(unittest.TestCase):
    def test_unknown_group_and_placeholder_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Unknown configuration group"):
            config.validate_required_config(("not_a_group",))

        with patch.object(config, "KAKAO_MAP_JAVASCRIPT_KEY", "YOUR_KAKAO_JAVASCRIPT_KEY"):
            with self.assertRaisesRegex(RuntimeError, "KAKAO_MAP_JAVASCRIPT_KEY"):
                config.validate_required_config(("kakao",))


if __name__ == "__main__":
    unittest.main()
