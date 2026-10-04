import subprocess

from scripts.check_publication import candidates, scan_file


def test_source_scan_works_without_git_installed(tmp_path, monkeypatch):
    source = tmp_path / "README.md"
    source.write_text("Source archive", encoding="utf-8")
    environment = tmp_path / ".venv"
    environment.mkdir()
    (environment / "local.txt").write_text("local only", encoding="utf-8")

    def no_git(*args, **kwargs):
        raise FileNotFoundError("Git is not installed")

    monkeypatch.setattr("scripts.check_publication.subprocess.run", no_git)
    assert candidates(tmp_path) == [source]


def test_sensitive_values_are_flagged_without_printing_them(tmp_path):
    secret = "sk-" + "a1b2c3d4" * 5
    personal_path = "C:/" + "Users/" + "private-person/work"
    path = tmp_path / "source.py"
    path.write_text(secret + "\n" + personal_path, encoding="utf-8")
    findings = "\n".join(scan_file(tmp_path, path))
    assert "possible API key" in findings
    assert "personal home path" in findings
    assert secret not in findings
    assert "private-person" not in findings


def test_private_artifacts_blocked_and_examples_allowed(tmp_path):
    database = tmp_path / "snapshot.db-wal"
    database.write_bytes(b"private")
    assert "private runtime/generated file" in scan_file(tmp_path, database)[0]
    example = tmp_path / ".env.example"
    example.write_text('DEEPSEEK_API_KEY_1=""\n', encoding="utf-8")
    assert scan_file(tmp_path, example) == []


def test_unrelated_project_folder_is_blocked_without_names_in_source(tmp_path):
    unrelated = tmp_path / "unrelated-project" / "README.md"
    unrelated.parent.mkdir()
    unrelated.write_text("Private work notes", encoding="utf-8")
    assert "unexpected top-level folder" in scan_file(tmp_path, unrelated)[0]
    docs = tmp_path / "docs" / "guide.md"
    docs.parent.mkdir()
    docs.write_text("Public setup guide", encoding="utf-8")
    assert scan_file(tmp_path, docs) == []


def test_tracked_files_are_scanned_even_if_later_ignored(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    secret_file = tmp_path / ".env"
    secret_file.write_text("private", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".env"], check=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    assert secret_file in candidates(tmp_path)
    assert scan_file(tmp_path, secret_file)
