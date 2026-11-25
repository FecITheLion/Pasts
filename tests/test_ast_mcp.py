"""Comprehensive unit and integration tests for the refactored `ast_mcp` package."""

from __future__ import annotations

import ast
import asyncio
import builtins
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ast_mcp import DynamicASTMcpProgram, create_server
from ast_mcp.analyzer import (
    build_dependency_graph,
    compute_function_metrics,
    compute_unified_diff,
    generate_default_test_cases,
    generate_pytest_snippet,
)
from ast_mcp.ast_core import safe_deserialize_ast_dump, serialize_function, verify_and_roundtrip_ast
from ast_mcp.console import ANSI_CODES, highlight_python_ansi
from ast_mcp.converter import convert_structure
from ast_mcp.deps import validate_package_name
from ast_mcp.http_server import load_dashboard_html, parse_stdin_line, start_web_dashboard
from ast_mcp.linter import lint_and_autofix_input
from ast_mcp.program import FastMCP
from ast_mcp.signature import parse_eval_args_with_omissions


class TestSafeAstDeserialization(unittest.TestCase):
    def test_valid_ast_dump_roundtrip(self) -> None:
        src = "def add_one(x: int) -> int:\n    return x + 1\n"
        rt = verify_and_roundtrip_ast("add_one", ast.dump(ast.parse(src).body[0]))
        self.assertEqual(len(rt["stages"]), 4)
        node = safe_deserialize_ast_dump(rt["canonical_serialized_ast"])
        self.assertIsInstance(node, ast.FunctionDef)
        self.assertEqual(node.name, "add_one")

    def test_rejects_arbitrary_eval_calls(self) -> None:
        malicious_payloads = [
            "__import__('os').system('echo pwned')",
            "open('/etc/passwd').read()",
            "[x for x in range(3)]",
            "(lambda: 1)()",
            "FunctionDef.__subclasses__()",
            "UnknownClass(x=1)",
        ]
        for payload in malicious_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    safe_deserialize_ast_dump(payload)


class TestLinterAndAutoFix(unittest.TestCase):
    KNOWN_BUILTINS = set(dir(builtins))

    def test_autofixes_b006_e722_e711_f601_w0101_w0104_f841(self) -> None:
        messy = (
            "def f(items=[], val=None):\n"
            "    unused_x = 42\n"
            "    if val == None:\n"
            "        d = {'a': 1, 'a': 2}\n"
            "    try:\n"
            "        pass\n"
            "    except:\n"
            "        pass\n"
            "    return len(items)\n"
            "    dead_after = 99\n"
        )
        res = lint_and_autofix_input("f", messy, self.KNOWN_BUILTINS)
        self.assertFalse(res.valid)
        self.assertTrue(res.modified)
        assert res.fixed_source is not None
        self.assertIn("items=None", res.fixed_source)
        self.assertIn("val is None", res.fixed_source)
        self.assertIn("except Exception:", res.fixed_source)
        self.assertNotIn("unused_x", res.fixed_source)
        self.assertNotIn("dead_after", res.fixed_source)
        fixed_codes = {i.code for i in res.issues}
        self.assertTrue(
            {
                "B006_MUTABLE_DEFAULT",
                "E711_SINGLETON_COMPARE",
                "E722_BARE_EXCEPT",
                "F601_DUPLICATE_DICT_KEY",
                "W0101_UNREACHABLE_CODE",
                "F841_UNUSED_VARIABLE",
            }.issubset(fixed_codes)
        )

        # W0104 implicit trailing return auto-fix
        implicit_res = lint_and_autofix_input("g", "def g(x: int):\n    x * 2\n", self.KNOWN_BUILTINS)
        self.assertTrue(implicit_res.modified)
        assert implicit_res.fixed_source is not None
        self.assertIn("return x * 2", implicit_res.fixed_source)
        self.assertIn("W0104_IMPLICIT_RETURN", {i.code for i in implicit_res.issues})

    def test_missing_colon_with_type_annotation(self) -> None:
        broken = "def foo(x: int) -> int\n    return x + 1\n"
        res = lint_and_autofix_input("foo", broken, self.KNOWN_BUILTINS)
        self.assertFalse(res.valid)
        self.assertTrue(res.modified)
        assert res.fixed_source is not None
        self.assertIn("def foo(x: int) -> int:", res.fixed_source)

    def test_match_case_bindings_not_flagged_undefined(self) -> None:
        code = (
            "def route_val(payload: dict) -> int:\n"
            "    match payload:\n"
            "        case {'x': val, **rest}:\n"
            "            return val + len(rest)\n"
            "        case other:\n"
            "            return 0\n"
        )
        res = lint_and_autofix_input("route_val", code, self.KNOWN_BUILTINS)
        self.assertTrue(res.valid)

    def test_full_script_wrapping_preserves_imports_and_classes(self) -> None:
        script = (
            "import math\n"
            "class Calc:\n"
            "    def root(self, x: float) -> float:\n"
            "        return math.sqrt(x)\n"
            "res = Calc().root(16)\n"
        )
        res = lint_and_autofix_input("my_entry", script, self.KNOWN_BUILTINS)
        self.assertTrue(res.valid)
        assert res.fixed_source is not None
        self.assertIn("def my_entry(", res.fixed_source)
        self.assertIn("import math", res.fixed_source)


class TestAnalyzerMetricsAndDiff(unittest.TestCase):
    def test_metrics_diff_graph_and_pytest(self) -> None:
        fn_src = (
            "def classify(x: int, label: str = 'hi') -> str:\n"
            "    if x > 0:\n"
            "        for i in range(x):\n"
            "            if i % 2 == 0 and x > 2:\n"
            "                return label\n"
            "    return 'none'\n"
        )
        serialized = serialize_function(ast.parse(fn_src).body[0])
        metrics = compute_function_metrics("classify", serialized)
        self.assertGreaterEqual(metrics["cyclomatic_complexity"], 4)
        self.assertEqual(metrics["max_nesting_depth"], 3)
        self.assertEqual(metrics["type_coverage_pct"], 100)

        diff = compute_unified_diff("classify", fn_src, fn_src.replace("'none'", "'zero'"))
        self.assertIn("-    return 'none'", diff)
        self.assertIn("+    return 'zero'", diff)

        cases = generate_default_test_cases("classify", serialized)
        self.assertEqual(cases[0]["args"], [3, "hi"])
        pytest_code = generate_pytest_snippet("classify", serialized, sample_output="hi")
        self.assertIn("def test_classify_smoke()", pytest_code)
        self.assertIn("assert result == 'hi'", pytest_code)

        reg = {
            "leaf": serialize_function(ast.parse("def leaf(x: int) -> int:\n    return x + 1\n").body[0]),
            "caller": serialize_function(ast.parse("def caller(y: int) -> int:\n    return leaf(y)\n").body[0]),
        }
        graph = build_dependency_graph(reg)
        leaf_node = next(n for n in graph["nodes"] if n["name"] == "leaf")
        self.assertEqual(leaf_node["called_by"], ["caller"])
        self.assertFalse(graph["has_cycle"])


class TestProgramLifecycleAndRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.server = FastMCP("test-ast")
        self.prog = DynamicASTMcpProgram(
            mcp_server=self.server,
            initial_registry={
                "ast1": serialize_function(ast.parse("def ast1(x: int) -> int:\n    return x * 2 + 1\n").body[0]),
                "ast2": serialize_function(ast.parse("def ast2(y: int) -> int:\n    return ast1(y) ** 2\n").body[0]),
            },
            auto_expose_keys=["ast2"],
            modules_dir=self.tmpdir.name,
        )

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    async def test_signature_mismatch_and_omission_recovery(self) -> None:
        # Extra positional + kwarg triggers signature_mismatch_autofixed with _new_A
        res = self.prog.eval_target("ast1", [3, 99, {"extra_kw": "hi"}])
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_type"], "signature_mismatch_autofixed")
        self.assertTrue(any("_new_" in p for p in res["added_parameters"]))
        self.assertIn("recovery_chain", res["_links"])

        # Omitted slot shorthand triggers missing_default_value recovery
        call_a, call_kw, o_pos, o_kw = parse_eval_args_with_omissions('[, {"k": 1}]')
        self.assertEqual(o_pos, [0])
        self.assertEqual(call_kw, {"k": 1})

        missing_res = self.prog.eval_target("ast1", '[, {"k": 1}]')
        self.assertEqual(missing_res["status"], "error")
        self.assertEqual(missing_res["error_type"], "missing_default_value")
        self.assertEqual(missing_res["policy"], "set_default_value")
        self.assertEqual(missing_res["missing_default_params"][0]["name"], "x")

        # Direct missing arg TypeError also promotes to missing_default_value
        direct_missing = self.prog.eval_target("ast1", [])
        self.assertEqual(direct_missing["error_type"], "missing_default_value")

    async def test_circular_dependency_detection_and_update_rollback(self) -> None:
        # Updating ast1 to call ast2 creates a cycle ast1 -> ast2 -> ast1
        before_ast1 = self.prog.registry["ast1"]
        res = await self.prog.update("ast1", "def ast1(x: int) -> int:\n    return ast2(x)\n")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_type"], "circular_ast_dependency")
        # Registry rolled back cleanly and ast1 still works
        self.assertEqual(self.prog.registry["ast1"], before_ast1)
        ok_eval = self.prog.eval_target("ast2", [3])
        self.assertEqual(ok_eval["result"], 49)

    async def test_stdout_profiling_tests_diff_and_rollback(self) -> None:
        # Create function that prints to stdout and verify stdout + duration_ms capture
        created = await self.prog.create(
            "noisy_calc",
            "def noisy_calc(x: int = 4) -> int:\n    print('computing', x)\n    if x < 0:\n        raise ValueError('negative')\n    return x * 10\n",
        )
        self.assertEqual(created["status"], "created")
        ev = self.prog.eval_target("noisy_calc", [4])
        self.assertEqual(ev["result"], 40)
        self.assertEqual(ev["stdout"], "computing 4")
        self.assertIsInstance(ev["duration_ms"], float)

        # Unit test runner: auto smoke + custom pass/fail + expected_error
        smoke = self.prog.run_tests("noisy_calc")
        self.assertEqual(smoke["status"], "tests_passed")
        self.assertEqual(smoke["passed"], 1)

        suite = self.prog.run_tests(
            "noisy_calc",
            cases=[
                {"name": "ok_case", "args": [5], "expected": 50},
                {"name": "err_case", "args": [-1], "expected_error": "ValueError"},
                {"name": "bad_assert", "args": [2], "expected": 999},
            ],
        )
        self.assertEqual(suite["status"], "tests_failed")
        self.assertEqual(suite["passed"], 2)
        self.assertEqual(suite["failed"], 1)

        # Update + non-mutating diff preview + rollback to v1
        await self.prog.update("ast1", "def ast1(x: int) -> int:\n    return x * 100\n")
        self.assertEqual(self.prog.eval_target("ast1", [2])["result"], 200)

        diff_res = self.prog.analyze("ast1", candidate_code="def ast1(x: int) -> int:\n    return x * 500\n")
        self.assertEqual(diff_res["status"], "diff_preview")
        self.assertTrue(diff_res["has_changes"])
        self.assertIn("ast2", diff_res["called_by"])
        # Non-mutating check: ast1 still returns 200
        self.assertEqual(self.prog.eval_target("ast1", [2])["result"], 200)

        rb = await self.prog.rollback("ast1")
        self.assertEqual(rb["status"], "rolled_back")
        self.assertEqual(rb["restored_from_version"], 1)
        self.assertEqual(self.prog.eval_target("ast1", [2])["result"], 5)

    async def test_chain_execution_and_converter(self) -> None:
        chain_res = await self.prog.execute_chain(
            [
                {"tool": "eval_ast_call", "arguments": {"target": "ast1", "args": [4]}},
                {"tool": "eval_ast_call", "arguments": {"target": "ast2", "args": ["$prev"]}},
            ]
        )
        self.assertEqual(chain_res["status"], "chain_completed")
        steps = chain_res["_embedded"]["steps"]
        # ast1(4)=9; ast2(9)=(9*2+1)^2=361
        self.assertEqual(steps[1]["output"]["result"], 361)

        # Converter saved_ast -> dataclass and json_schema
        dc = convert_structure("saved_ast", "dataclass", "ast1", "Ast1Args", self.prog.registry)
        self.assertIn("@dataclass", dc["python_source"])
        self.assertIn("x: int", dc["python_source"])

        schema_res = convert_structure("saved_ast", "json_schema", "ast1", "Ast1Args", self.prog.registry)
        self.assertIn("x", schema_res["json_schema"]["properties"])

    async def test_security_and_monotonic_events(self) -> None:
        # Package validation blocks pip flag injection
        with self.assertRaises(ValueError):
            validate_package_name("--index-url=https://evil.example/simple pkg")
        with self.assertRaises(ValueError):
            validate_package_name("-e git+https://evil.example/repo.git")
        self.assertEqual(validate_package_name("scikit-learn"), "scikit-learn")

        # write_local_module_file rejects SyntaxError and confines paths
        bad_syntax = self.prog.write_local_module_file("bad_mod.py", "def broken(:\n", overwrite=True)
        self.assertEqual(bad_syntax["error_type"], "syntax_error")
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir.name, "bad_mod.py")))

        # Path traversal stripped to safe basename inside tmpdir
        ok_mod = self.prog.write_local_module_file("../../safe_mod.py", "VAL = 42\n", overwrite=True)
        self.assertEqual(ok_mod["status"], "module_file_created")
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir.name, "safe_mod.py")))

        # Monotonic event IDs beyond 100 events
        for i in range(110):
            self.prog.record_and_broadcast("rest", "ast_read", {"idx": i}, {"status": "ok"})
        ids = [e["id"] for e in self.prog.event_log]
        self.assertEqual(len(self.prog.event_log), 100)
        self.assertGreater(ids[-1], 105)
        self.assertEqual(ids, sorted(ids))


class TestSyntaxHighlightingAndHttpApi(unittest.TestCase):
    def test_ansi_python_highlighter(self) -> None:
        src = "def fn(x: int = 3) -> int:\n    # calc\n    return 'ok' if x > 0 else 0\n"
        highlighted = highlight_python_ansi(src)
        plain = highlighted
        for code in ANSI_CODES.values():
            plain = plain.replace(code, "")
        self.assertEqual(plain, src)
        bad = "def unclosed(:\n    \"\"\"unterminated"
        self.assertEqual(highlight_python_ansi(bad), bad)

    def test_dashboard_html_has_highlighting_gemini_and_dev_ergonomics(self) -> None:
        html = load_dashboard_html().decode("utf-8")
        for needle in (
            "highlightPython",
            "highlightJson",
            "highlightAstDump",
            "syncHighlightedEditor",
            "sendGeminiChat",
            "generateLineIntoEditor",
            "previewDiffForEditor",
            "runTestsForTarget",
            "rollbackTarget",
            "attachPythonEditorErgonomics",
            "safeParseJsonResponse",
            "extractHateoasActionTracks",
            "buildGeminiChatSuggestions",
            "buildLinePromptSuggestions",
            "buildModalJsonBodySuggestions",
        ):
            self.assertIn(needle, html)

    def test_parse_stdin_line_preserves_flag_inside_code(self) -> None:
        parsed = parse_stdin_line("create demo def demo(): return '--no-roundtrip'")
        self.assertEqual(parsed["tool"], "ast_create")
        self.assertFalse(parsed["arguments"]["skip_roundtrip"])
        self.assertIn("--no-roundtrip", parsed["arguments"]["code_or_ast"])

        parsed_flag = parse_stdin_line("create --no-roundtrip demo2 def demo2(): return 1")
        self.assertTrue(parsed_flag["arguments"]["skip_roundtrip"])
        self.assertEqual(parsed_flag["arguments"]["name"], "demo2")

    def test_http_endpoints_mcp_tools_and_gemini_error_paths(self) -> None:
        _, prog = create_server(stdout_is_mcp_stdio=False)
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()
        httpd = start_web_dashboard(prog, loop, host="127.0.0.1", port=0)
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"

        orig_key = os.environ.pop("GEMINI_API_KEY", None)
        try:
            # Dashboard HTML root includes syntax highlighter & Gemini chat
            with urlopen(f"{base}/") as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
                self.assertIn("highlightPython", body)

            # MCP Tools introspection lists both built-ins and dynamically exposed entrypoint
            with urlopen(f"{base}/api/mcp/tools") as resp:
                self.assertEqual(resp.status, 200)
                tools_doc = json.loads(resp.read().decode("utf-8"))
                tool_names = {t["name"] for t in tools_doc["tools"]}
                self.assertTrue(
                    {"ast_create", "ast_run_tests", "ast_analyze", "ast_rollback", "entrypoint"}.issubset(
                        tool_names
                    )
                )

            # Dependency graph & analysis
            with urlopen(f"{base}/api/graph") as resp:
                graph_doc = json.loads(resp.read().decode("utf-8"))
                self.assertFalse(graph_doc["has_cycle"])
                self.assertGreaterEqual(len(graph_doc["nodes"]), 3)

            with urlopen(f"{base}/api/ast/ast1/analyze") as resp:
                an_doc = json.loads(resp.read().decode("utf-8"))
                self.assertIn("ast2", an_doc["called_by"])
                self.assertIn("def test_ast1_smoke()", an_doc["pytest_source"])

            # Diff & unit test endpoints
            diff_req = Request(
                f"{base}/api/ast/ast1/diff",
                data=json.dumps({"candidate_code": "def ast1(x: int) -> int:\n    return x * 10\n"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(diff_req) as resp:
                diff_doc = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(diff_doc["has_changes"])
                self.assertIn("+    return x * 10", diff_doc["unified_diff"])

            test_req = Request(
                f"{base}/api/ast/entrypoint/test",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(test_req) as resp:
                test_doc = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(test_doc["status"], "tests_passed")

            # HAL root & entrypoint eval with duration_ms
            with urlopen(f"{base}/api/ast/entrypoint/eval", data=b"[3]") as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["result"], 59)  # (3*2+1)**2 + 10 = 59
                self.assertIn("duration_ms", data)

            # Gemini chat & generate-line explicitly surface 503 when GEMINI_API_KEY is missing
            for endpoint, payload in (
                ("/api/gemini/chat", {"message": "List AST functions"}),
                ("/api/gemini/generate-line", {"prompt": "return x + 1", "prefix": "def f(x):\n    "}),
            ):
                g_req = Request(
                    f"{base}{endpoint}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as ctx503:
                    urlopen(g_req)
                self.assertEqual(ctx503.exception.code, 503)
                err_json = json.loads(ctx503.exception.read().decode("utf-8"))
                self.assertEqual(err_json["error_type"], "gemini_api_key_missing")

            # Missing resource returns 404 HAL JSON
            with self.assertRaises(HTTPError) as ctx:
                urlopen(f"{base}/api/ast/does_not_exist")
            self.assertEqual(ctx.exception.code, 404)

            # Oversized Content-Length returns 413 JSON error cleanly
            req_bad = Request(
                f"{base}/api/ast",
                data=b"{}",
                headers={"Content-Type": "application/json", "Content-Length": "999999999"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as ctx413:
                urlopen(req_bad)
            self.assertEqual(ctx413.exception.code, 413)
        finally:
            if orig_key is not None:
                os.environ["GEMINI_API_KEY"] = orig_key
            httpd.shutdown()
            httpd.server_close()
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=2.0)
            loop.close()

    def test_cli_help_smoke(self) -> None:
        proc = subprocess.run(
            [sys.executable, "ast_mcp_server.py", "--help"],
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--disable-server", proc.stdout)
        self.assertIn("--ui-port", proc.stdout)


if __name__ == "__main__":
    unittest.main()
