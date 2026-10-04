import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from swarm.toolbox import Toolbox, ToolError, TOOL_SCHEMAS, _safe_environment


class ToolboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.approve = AsyncMock(return_value=True)
        self.permissions = {"read_files": True, "write_files": True, "internet": False,
                            "commands": "deny", "deploy": "deny"}
        self.box = Toolbox(str(self.workspace), self.permissions, self.approve)

    async def asyncTearDown(self):
        await self.box.close()
        self.directory.cleanup()

    async def test_denied_tools_never_perform_work(self):
        (self.workspace / "file.txt").write_text("original", encoding="utf-8")
        self.permissions["read_files"] = False
        self.permissions["write_files"] = False
        for name, args in [
            ("read_file", {"path": "file.txt"}),
            ("list_files", {}),
            ("write_file", {"path": "file.txt", "content": "changed"}),
            ("edit_file", {"path": "file.txt", "old_text": "original", "new_text": "changed", "expected_sha256": hashlib.sha256(b"original").hexdigest()}),
            ("web_fetch", {"url": "https://example.com"}),
            ("run_command", {"command": "echo forbidden"}),
            ("deploy_command", {"command": "echo forbidden"}),
        ]:
            result = await self.box.execute(name, args, "agent-1")
            self.assertFalse(result["ok"], name)
            self.assertIn("Permission denied", result["error"])
        self.assertEqual((self.workspace / "file.txt").read_text(), "original")
        self.approve.assert_not_called()

    async def test_traversal_secrets_and_runtime_paths_are_blocked(self):
        for path in ["../outside.txt", str(self.workspace / "absolute.txt"), ".env", ".env.local",
                     ".ssh/id_rsa", ".swarm-backups/revision.bin", "data.txt:stream", "NUL"]:
            result = await self.box.execute("write_file", {"path": path, "content": "bad"}, "agent-1")
            self.assertFalse(result["ok"], path)
        runtime = self.workspace / "runtime"
        runtime.mkdir()
        (runtime / "keys.json").write_text("secret")
        protected = Toolbox(str(self.workspace), self.permissions, self.approve, protected_paths=[str(runtime)])
        try:
            result = await protected.execute("write_file", {"path": "runtime/new.txt", "content": "bad"}, "agent-1")
            self.assertFalse(result["ok"])
            listing = await protected.execute("list_files", {}, "agent-1")
            self.assertNotIn("runtime", [entry["name"] for entry in listing["entries"]])
        finally:
            await protected.close()

    async def test_symlink_escape_is_blocked(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks requires Windows developer mode or privilege")
            result = await self.box.execute("write_file", {"path": "escape/file.txt", "content": "bad"}, "agent-1")
            self.assertFalse(result["ok"])
            self.assertFalse((Path(outside) / "file.txt").exists())

    async def test_revision_conflict_preserves_file_and_backup(self):
        created = await self.box.execute("write_file", {"path": "nested/file.txt", "content": "one"}, "agent-1")
        self.assertTrue(created["ok"])
        read = await self.box.execute("read_file", {"path": "nested/file.txt"}, "agent-2")
        self.assertEqual(read["sha256"], created["sha256"])
        missing_revision = await self.box.execute("write_file", {"path": "nested/file.txt", "content": "two"}, "agent-2")
        self.assertFalse(missing_revision["ok"])
        changes = await asyncio.gather(*[
            self.box.execute("write_file", {"path": "nested/file.txt", "content": value,
                                           "expected_sha256": created["sha256"]}, f"agent-{index}")
            for index, value in enumerate(("two", "three"))
        ])
        self.assertEqual(sum(result["ok"] for result in changes), 1)
        self.assertEqual((self.workspace / "nested/file.txt").read_text(), "two")
        backups = list((self.workspace / ".swarm-backups").glob("*.bin"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"one")
        listing = await self.box.execute("list_files", {}, "agent-1")
        self.assertNotIn(".swarm-backups", [entry["name"] for entry in listing["entries"]])

    async def test_read_pages_preserve_full_revision_and_line_boundaries(self):
        content = "".join(f"line {index:04}\r\n" for index in range(1, 451))
        data = content.encode("utf-8")
        (self.workspace / "source.txt").write_bytes(data)
        page = await self.box.execute("read_file", {"path": "source.txt"}, "agent-1")
        self.assertTrue(page["ok"])
        self.assertEqual(page["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(page["total_lines"], 450)
        self.assertEqual(page["returned_lines"], 200)
        self.assertEqual(page["next_line"], 201)
        self.assertTrue(page["truncated"])
        self.assertFalse(page["line_truncated"])
        remaining = await self.box.execute("read_file", {"path": "source.txt", "start_line": 201, "max_lines": 1000}, "agent-1")
        self.assertEqual(page["content"] + remaining["content"], content)
        self.assertEqual(remaining["sha256"], page["sha256"])
        self.assertEqual(remaining["returned_lines"], 250)
        self.assertIsNone(remaining["next_line"])
        self.assertFalse(remaining["truncated"])

    async def test_large_read_keeps_hash_before_bounded_content(self):
        content = "".join(f"row{index:04}: " + "x" * 200 + "\n" for index in range(600))
        (self.workspace / "large.txt").write_bytes(content.encode())
        assembled = []
        line = 1
        while line is not None:
            page = await self.box.execute("read_file", {"path": "large.txt", "start_line": line, "max_lines": 1000}, "agent-1")
            self.assertTrue(page["ok"])
            self.assertLessEqual(len(page["content"]), 16000)
            self.assertFalse(page["line_truncated"])
            serialized = json.dumps(page)
            self.assertLess(serialized.index('"sha256"'), serialized.index('"content"'))
            self.assertIn(page["sha256"], serialized[:30000])
            assembled.append(page["content"])
            line = page["next_line"]
        self.assertEqual("".join(assembled), content)

    async def test_long_single_line_is_explicitly_marked_clipped(self):
        (self.workspace / "long.txt").write_text("x" * 20000 + "\nlast line\n", encoding="utf-8")
        page = await self.box.execute("read_file", {"path": "long.txt"}, "agent-1")
        self.assertEqual(len(page["content"]), 16000)
        self.assertTrue(page["line_truncated"])
        self.assertTrue(page["truncated"])
        self.assertEqual(page["next_line"], 2)
        tail = await self.box.execute("read_file", {"path": "long.txt", "start_line": 2}, "agent-1")
        self.assertEqual(tail["content"].strip(), "last line")

    async def test_invalid_read_ranges_fail(self):
        (self.workspace / "source.txt").write_text("line\n")
        for extra in [{"start_line": 0}, {"start_line": True}, {"start_line": 1.5},
                      {"max_lines": 0}, {"max_lines": 1001}, {"max_lines": True}]:
            result = await self.box.execute("read_file", {"path": "source.txt", **extra}, "agent-1")
            self.assertFalse(result["ok"], extra)

    async def test_exact_edit_preserves_large_file_and_creates_backup(self):
        original = "".join(f"value_{index:04} = {index}\r\n" for index in range(5000))
        target = self.workspace / "source.py"
        target.write_bytes(original.encode())
        page = await self.box.execute("read_file", {"path": "source.py", "start_line": 1001, "max_lines": 5}, "agent-1")
        result = await self.box.execute("edit_file", {"path": "source.py", "old_text": "value_1000 = 1000\r\n",
                                        "new_text": "value_1000 = 42\r\n", "expected_sha256": page["sha256"]}, "agent-1")
        self.assertTrue(result["ok"], result)
        expected = original.replace("value_1000 = 1000\r\n", "value_1000 = 42\r\n")
        self.assertEqual(target.read_bytes(), expected.encode())
        backups = list((self.workspace / ".swarm-backups").glob("*.bin"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original.encode())
        stale = await self.box.execute("edit_file", {"path": "source.py", "old_text": "value_1001 = 1001",
                                       "new_text": "value_1001 = 42", "expected_sha256": page["sha256"]}, "agent-2")
        self.assertFalse(stale["ok"])
        self.assertIn("revision conflict", stale["error"])
        self.assertEqual(target.read_bytes(), expected.encode())

    async def test_ambiguous_missing_and_unversioned_edits_do_not_write(self):
        original = "aaaa\nunique line\n"
        target = self.workspace / "source.txt"
        target.write_bytes(original.encode())
        revision = hashlib.sha256(original.encode()).hexdigest()
        for old, expected, error in [("aaa", revision, "ambiguous"), ("missing", revision, "not found"),
                                     ("unique", "", "expected_sha256"), ("", revision, "nonempty")]:
            result = await self.box.execute("edit_file", {"path": "source.txt", "old_text": old,
                                           "new_text": "replacement", "expected_sha256": expected}, "agent-1")
            self.assertFalse(result["ok"])
            self.assertIn(error, result["error"])
            self.assertEqual(target.read_bytes(), original.encode())
        self.assertFalse((self.workspace / ".swarm-backups").exists())

    async def test_edits_enforce_file_limit_and_protected_paths(self):
        oversized = self.workspace / "large.txt"
        oversized.write_bytes(b"x" * 1_000_001)
        result = await self.box.execute("edit_file", {"path": "large.txt", "old_text": "x", "new_text": "y",
                                       "expected_sha256": "0" * 64}, "agent-1")
        self.assertFalse(result["ok"])
        self.assertIn("byte limit", result["error"])
        for path in ["../outside.txt", ".env", ".swarm-backups/saved.bin"]:
            result = await self.box.execute("edit_file", {"path": path, "old_text": "x", "new_text": "y",
                                           "expected_sha256": "0" * 64}, "agent-1")
            self.assertFalse(result["ok"], path)

    async def test_day_long_timeout_is_approved_exactly_and_still_bounded(self):
        self.permissions["commands"] = "ask"
        command = "Write-Output 'instant'" if os.name == "nt" else "printf instant"
        result = await self.box.execute("run_command", {"command": command, "timeout": 86400}, "agent-1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.approve.await_args.args[2]["timeout"], 86400)
        self.approve.reset_mock()
        invalid = await self.box.execute("run_command", {"command": command, "timeout": 86401}, "agent-1")
        self.assertFalse(invalid["ok"])
        self.approve.assert_not_awaited()
        for schema in TOOL_SCHEMAS:
            if schema["function"]["name"] in ("run_command", "deploy_command"):
                self.assertEqual(schema["function"]["parameters"]["properties"]["timeout"]["maximum"], 86400)

    @unittest.skipUnless(os.name == "nt", "Windows process window flags")
    async def test_shell_and_cancellation_helper_use_no_window(self):
        self.permissions["commands"] = "allow"
        real_spawn = asyncio.create_subprocess_exec
        with patch("swarm.toolbox.asyncio.create_subprocess_exec", wraps=real_spawn) as spawn:
            task = asyncio.create_task(self.box.execute("run_command", {"command": "Start-Sleep -Seconds 30"}, "agent-1"))
            async with asyncio.timeout(10):
                while not self.box._processes:
                    await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        self.assertGreaterEqual(spawn.call_count, 2)
        for call in spawn.call_args_list:
            self.assertTrue(call.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(spawn.call_args_list[0].kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)

    async def test_command_approval_is_exact_and_deploy_independent(self):
        self.permissions["commands"] = "ask"
        command = "Write-Output 'approved'" if os.name == "nt" else "printf approved"
        result = await self.box.execute("run_command", {"command": command, "timeout": 10}, "agent-7")
        self.assertTrue(result["ok"], result)
        self.assertIn("approved", result["stdout"])
        self.approve.assert_awaited_once_with("agent-7", "commands", {
            "command": command, "cwd": str(self.workspace.resolve()), "timeout": 10})
        blocked = await self.box.execute("deploy_command", {"command": command}, "agent-7")
        self.assertFalse(blocked["ok"])
        self.permissions["deploy"] = "ask"
        self.approve.return_value = False
        declined = await self.box.execute("deploy_command", {"command": command}, "agent-7")
        self.assertFalse(declined["ok"])
        self.assertEqual(self.approve.await_args.args[1], "deploy")

    async def test_cancellation_stops_process_and_clears_registry(self):
        self.permissions["commands"] = "allow"
        command = "Start-Sleep -Seconds 30; Set-Content 'must-not-exist.txt' 'bad'" if os.name == "nt" else "sleep 30; printf bad > must-not-exist.txt"
        task = asyncio.create_task(self.box.execute("run_command", {"command": command}, "agent-1"))
        async with asyncio.timeout(10):
            while not self.box._processes:
                await asyncio.sleep(0.02)
        process = next(iter(self.box._processes.values()))[0]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(self.box._processes, {})
        self.assertFalse((self.workspace / "must-not-exist.txt").exists())

    async def test_timeout_preserves_bounded_output(self):
        self.permissions["commands"] = "allow"
        command = "Write-Output 'before-timeout'; Start-Sleep -Seconds 30" if os.name == "nt" else "printf before-timeout; sleep 30"
        result = await self.box.execute("run_command", {"command": command, "timeout": 2}, "agent-1")
        self.assertFalse(result["ok"], result)
        self.assertIn("exceeded", result["error"])
        self.assertIn("before-timeout", result["stdout"])
        self.assertEqual(self.box._processes, {})

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup")
    async def test_windows_cancellation_terminates_descendants(self):
        import ctypes
        from ctypes import wintypes
        self.permissions["commands"] = "allow"
        script = self.workspace / "spawn_child.py"
        script.write_text("import pathlib,subprocess,sys,time\n"
                          "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
                          "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
                          "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                          "pathlib.Path('child.pid').write_text(str(child.pid))\n"
                          "time.sleep(30)\n", encoding="utf-8")
        executable = sys.executable.replace("'", "''")
        task = asyncio.create_task(self.box.execute("run_command", {"command": f"& '{executable}' 'spawn_child.py'"}, "agent-1"))
        handle = None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            async with asyncio.timeout(10):
                while not (self.workspace / "child.pid").exists():
                    await asyncio.sleep(0.02)
            child_pid = int((self.workspace / "child.pid").read_text())
            handle = kernel.OpenProcess(0x00100000, False, child_pid)  # SYNCHRONIZE
            self.assertTrue(handle)
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)  # WAIT_TIMEOUT: alive
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)  # WAIT_OBJECT_0: exited
        finally:
            if handle:
                kernel.CloseHandle(handle)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_network_rejects_credentials_and_private_targets(self):
        for url in ["file:///etc/passwd", "http://user:password@example.com", "http://127.0.0.1", "http://[::1]", "http://169.254.169.254", "http://10.0.0.1"]:
            with self.assertRaises(ToolError, msg=url):
                await self.box._public_target(url)

    async def test_redirects_are_revalidated(self):
        self.permissions["internet"] = True
        self.box._public_target = AsyncMock(side_effect=[(object(), "93.184.216.34"), ToolError("Private target blocked")])
        with patch.object(self.box, "_http_request", return_value={"status": 302, "location": "http://127.0.0.1/private", "content_type": "text/html", "body": b"", "truncated": False}):
            result = await self.box.execute("web_fetch", {"url": "https://example.com"}, "agent-1")
        self.assertFalse(result["ok"])
        self.assertEqual(self.box._public_target.await_args.args[0], "http://127.0.0.1/private")

    async def test_search_falls_back_when_primary_has_no_results(self):
        self.permissions["internet"] = True
        self.box._fetch = AsyncMock(side_effect=[
            {"url": "https://html.duckduckgo.com", "body": b"<html>Temporarily unavailable</html>"},
            {"body": b"<rss><channel><item><title>Result</title><link>https://example.com</link><description>Snippet</description></item></channel></rss>"},
        ])
        result = await self.box.execute("web_search", {"query": "test query"}, "agent-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "Bing")
        self.assertEqual(result["results"][0]["url"], "https://example.com")
        self.assertTrue(result["untrusted_external_content"])

    async def test_command_output_is_bounded(self):
        self.permissions["commands"] = "allow"
        command = "Write-Output ('x' * 100000)" if os.name == "nt" else "head -c 100000 /dev/zero"
        result = await self.box.execute("run_command", {"command": command, "timeout": 10}, "agent-1")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["stdout"]), 64000)

    def test_subprocess_environment_strips_secrets(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret", "GITHUB_TOKEN": "secret", "MY_AUTH": "secret", "ORDINARY_SETTING": "fine"}):
            environment = _safe_environment()
        for name in ("DEEPSEEK_API_KEY", "GITHUB_TOKEN", "MY_AUTH"):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["ORDINARY_SETTING"], "fine")


if __name__ == "__main__":
    unittest.main()
