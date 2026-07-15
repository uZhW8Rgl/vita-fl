import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent


class OllamaComposeTemplateTests(unittest.TestCase):
    def test_shell_process_variables_are_escaped_for_docker_compose(self) -> None:
        template = (ROOT / "dstack-compose.ollama.phala.tftpl").read_text()

        self.assertIn("server_pid=$$!", template)
        self.assertIn('kill "$$server_pid"', template)
        self.assertIn('wait "$$server_pid"', template)
        self.assertNotIn("server_pid=$!\n", template)
        self.assertNotIn('wait "$server_pid"', template)


if __name__ == "__main__":
    unittest.main()
