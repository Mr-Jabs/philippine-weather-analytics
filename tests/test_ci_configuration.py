"""Check CI credential encoding without using real credentials or a database."""

import io
import unittest
from unittest.mock import mock_open, patch

from sqlalchemy.engine import make_url

from scripts.configure_ci_database import build_database_url, main


class CIConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(DB_HOST="db.example.com", DB_PORT="5432", DB_USER="weather@user",
                           DB_PASSWORD="example:p@ss/word?#%$", DB_NAME="weather_analytics")

    def test_reserved_credentials_round_trip_without_shell_interpolation(self):
        parsed = make_url(build_database_url(self.values))
        self.assertEqual(parsed.username, self.values["DB_USER"])
        self.assertEqual(parsed.password, self.values["DB_PASSWORD"])
        self.assertEqual(parsed.port, 5432)
        self.assertEqual(parsed.query["sslmode"], "require")

    def test_missing_secret_error_does_not_disclose_other_values(self):
        self.values.pop("DB_HOST")
        with self.assertRaisesRegex(ValueError, "DB_HOST") as error:
            build_database_url(self.values)
        self.assertNotIn(self.values["DB_PASSWORD"], str(error.exception))

    def test_invalid_ports_and_multiline_secrets_are_rejected(self):
        for port in ("not-a-port", "0", "65536"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                build_database_url({**self.values, "DB_PORT": port})
        with self.assertRaisesRegex(ValueError, "single-line"):
            build_database_url({**self.values, "DB_PASSWORD": "example\nNEW_ENV=value"})

    def test_ci_exports_url_and_escapes_mask_commands(self):
        output = io.StringIO()
        expected_url = build_database_url(self.values)
        with patch.dict("os.environ", {**self.values, "GITHUB_ENV": "runner.env"}, clear=True), \
                patch("pathlib.Path.open", mock_open()) as file, \
                patch("sys.stdout", output):
            self.assertEqual(main(), 0)
        file().write.assert_called_once_with(f"DATABASE_URL={expected_url}\n")
        self.assertIn("::add-mask::" + expected_url.replace("%", "%25"), output.getvalue())
        self.assertNotIn(self.values["DB_PASSWORD"], output.getvalue())


if __name__ == "__main__":
    unittest.main()
