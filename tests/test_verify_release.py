"""Tests for ``scripts/verify_release.py``, the pre-publish artifact gate.

The gate is the last thing standing between a bad tag and a published release,
so it is tested like code rather than trusted. Each test builds a synthetic
wheel/sdist and asserts the corresponding check passes or blocks.

The checks deliberately err toward *not* blocking: a gate that cries wolf gets
disabled, and a disabled gate blocks nothing. Both directions are covered here.
"""

from __future__ import annotations

import importlib.util
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = REPO_ROOT / "scripts" / "verify_release.py"
    spec = importlib.util.spec_from_file_location("verify_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = _load_module()

ENTRY_POINTS_OK = (
    "[console_scripts]\nhermes-eink-dashboard = a:main\nhermes-dashboard-pair = b:main\n"
)
ENTRY_POINTS_LEGACY = (
    "[console_scripts]\nhermes-eink-dashboard = a:main\n"
    "hermes-dashboard-pair = b:main\nhermes-kindle-dashboard = a:main\n"
)

# Synthetic credential shapes used to prove the scanner detects them. They are
# assembled from fragments on purpose: this file ships inside the sdist, and a
# contiguous literal would make the scanner flag the test suite itself.
FAKE_OPENAI_KEY = "sk-" + "abcdefghijklmnopqrst" + "uvwxyz012345"
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"
FAKE_PRIVATE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----"
FAKE_GITHUB_TOKEN = "ghp_" + "a" * 36
FAKE_GENERIC_TOKEN = "8fJq2vXk91LmNpQrStUvWxYz" + "0123456789"


def build_wheel(
    dist: Path,
    *,
    entry_points: str = ENTRY_POINTS_OK,
    extra_files: dict[str, str] | None = None,
) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    wheel = dist / "hermes_eink_dashboard-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("hermes_eink_dashboard/__init__.py", "__version__ = '1.0.0'\n")
        archive.writestr("hermes_eink_dashboard-1.0.0.dist-info/entry_points.txt", entry_points)
        for name, text in (extra_files or {}).items():
            archive.writestr(name, text)
    return wheel


def build_sdist(dist: Path, *, extra_files: dict[str, str] | None = None) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    tarball = dist / "hermes_eink_dashboard-1.0.0.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        for name, text in (extra_files or {}).items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, __import__("io").BytesIO(payload))
    return tarball


class TestConsoleScripts:
    def test_accepts_the_intended_entry_points(self, tmp_path: Path) -> None:
        wheel = build_wheel(tmp_path)
        verify.check_console_scripts(wheel)

    def test_rejects_the_retired_console_script(self, tmp_path: Path) -> None:
        wheel = build_wheel(tmp_path, entry_points=ENTRY_POINTS_LEGACY)
        with pytest.raises(verify.Failure, match="console scripts"):
            verify.check_console_scripts(wheel)

    def test_rejects_a_wheel_with_no_entry_points(self, tmp_path: Path) -> None:
        wheel = tmp_path / "broken-1.0.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("hermes_eink_dashboard/__init__.py", "")
        with pytest.raises(verify.Failure, match="no entry_points"):
            verify.check_console_scripts(wheel)

    def test_parser_ignores_other_sections(self) -> None:
        text = "[console_scripts]\nhermes-eink-dashboard = a:main\n[gui_scripts]\nx = y:main\n"
        assert verify._declared_console_scripts(text) == {"hermes-eink-dashboard"}


class TestRetiredNames:
    def test_accepts_clean_artifacts(self, tmp_path: Path) -> None:
        build_wheel(tmp_path)
        build_sdist(tmp_path)
        paths = {
            "wheel": ["hermes_eink_dashboard/__init__.py"],
            "sdist": ["hermes_eink_dashboard-1.0.0/pyproject.toml"],
        }
        verify.check_retired_names(paths)
        verify.check_console_script_names(paths)

    def test_rejects_a_surviving_import_package(self, tmp_path: Path) -> None:
        with pytest.raises(verify.Failure, match="retired import package"):
            verify.check_retired_names({"wheel": ["hermes_kindle_dashboard/__init__.py"]})

    def test_rejects_a_surviving_installed_script(self, tmp_path: Path) -> None:
        with pytest.raises(verify.Failure, match="retired console script"):
            verify.check_console_script_names(
                {"wheel": ["hermes_eink_dashboard-1.0.0.data/scripts/bin/hermes-kindle-dashboard"]}
            )


class TestSecretScanning:
    @pytest.mark.parametrize(
        "text",
        [
            'DASHBOARD_TOKEN="PLACEHOLDER_TOKEN"',
            'DASHBOARD_TOKEN="CHANGE_ME"',
            'dashboard_token="your-read-token-here"',
            'CONTROL_TOKEN = "control-token-456"',
            "password: ${HERMES_PASSWORD}",
            'api_key = os.environ["API_KEY"]',
        ],
    )
    def test_placeholders_and_fixtures_do_not_block(self, tmp_path: Path, text: str) -> None:
        build_wheel(
            tmp_path,
            extra_files={"hermes_eink_dashboard/config.py": text},
        )
        build_sdist(tmp_path)
        verify.check_no_secrets(tmp_path)

    @pytest.mark.parametrize(
        "text,expected",
        [
            (f'TOKEN = "{FAKE_OPENAI_KEY}"', "OpenAI-style key"),
            (f'TOKEN = "{FAKE_GITHUB_TOKEN}"', "GitHub token"),
            (f'KEY = "{FAKE_AWS_KEY}"', "AWS access key id"),
            (FAKE_PRIVATE_KEY, "private key block"),
            (f'CONTROL_TOKEN = "{FAKE_GENERIC_TOKEN}"', "assigned credential"),
        ],
    )
    def test_real_credentials_block(self, tmp_path: Path, text: str, expected: str) -> None:
        build_wheel(tmp_path, extra_files={"hermes_eink_dashboard/config.py": text})
        build_sdist(tmp_path)
        with pytest.raises(verify.Failure) as excinfo:
            verify.check_no_secrets(tmp_path)
        assert expected in str(excinfo.value)

    def test_scans_the_sdist_too(self, tmp_path: Path) -> None:
        build_wheel(tmp_path)
        build_sdist(
            tmp_path,
            extra_files={"hermes_eink_dashboard-1.0.0/leak.py": f'TOKEN = "{FAKE_OPENAI_KEY}"'},
        )
        with pytest.raises(verify.Failure, match="OpenAI-style key"):
            verify.check_no_secrets(tmp_path)

    def test_the_shipped_test_fixtures_do_not_trip_the_scanner(self, tmp_path: Path) -> None:
        """This module must not plant a contiguous secret-looking literal."""
        own_text = Path(__file__).read_text(encoding="utf-8")
        build_wheel(tmp_path, extra_files={"tests/test_verify_release.py": own_text})
        build_sdist(tmp_path)
        verify.check_no_secrets(tmp_path)

    def test_entropy_filter_rejects_low_entropy_values(self) -> None:
        assert not verify._looks_like_a_real_credential("control-token-456")
        assert not verify._looks_like_a_real_credential("a" * 40)
        assert not verify._looks_like_a_real_credential("8fJq2vXk91LmNpQrStUvWxYz0123456789"[:20])
        assert verify._looks_like_a_real_credential(FAKE_GENERIC_TOKEN)


class TestVersionAgreement:
    def test_repository_versions_agree(self) -> None:
        verify.check_versions_agree()

    def test_detects_a_version_mismatch(self, tmp_path: Path, monkeypatch) -> None:
        """A Kindle extension left at 0.9.0 must block the release."""
        (tmp_path / "src" / "hermes_eink_dashboard").mkdir(parents=True)
        (tmp_path / "kindle" / "hermes_dashboard").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
        (tmp_path / "src" / "hermes_eink_dashboard" / "__init__.py").write_text(
            '__version__ = "1.0.0"\n', encoding="utf-8"
        )
        (tmp_path / "kindle" / "hermes_dashboard" / "config.xml").write_text(
            "<version>0.9.0</version>", encoding="utf-8"
        )
        monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)
        with pytest.raises(verify.Failure, match="version mismatch"):
            verify.check_versions_agree()

    def test_detects_a_missing_version_string(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "src" / "hermes_eink_dashboard").mkdir(parents=True)
        (tmp_path / "kindle" / "hermes_dashboard").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('name = "x"\n', encoding="utf-8")
        (tmp_path / "src" / "hermes_eink_dashboard" / "__init__.py").write_text(
            "# no version here\n", encoding="utf-8"
        )
        (tmp_path / "kindle" / "hermes_dashboard" / "config.xml").write_text(
            "<extension/>", encoding="utf-8"
        )
        monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)
        with pytest.raises(verify.Failure, match="could not read"):
            verify.check_versions_agree()

    def test_reports_a_missing_file_clearly(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)
        with pytest.raises(verify.Failure, match="missing version source"):
            verify.check_versions_agree()


class TestMain:
    def test_clean_dist_passes(self, tmp_path: Path, monkeypatch) -> None:
        build_wheel(tmp_path)
        build_sdist(tmp_path)
        monkeypatch.setattr(verify, "check_versions_agree", lambda: None)
        assert verify.main(["verify_release.py", str(tmp_path)]) == 0

    def test_missing_dist_returns_two(self, tmp_path: Path, capsys) -> None:
        assert verify.main(["verify_release.py", str(tmp_path / "absent")]) == 2

    def test_a_failing_check_returns_one(self, tmp_path: Path, monkeypatch, capsys) -> None:
        build_wheel(tmp_path, entry_points=ENTRY_POINTS_LEGACY)
        build_sdist(tmp_path)
        monkeypatch.setattr(verify, "check_versions_agree", lambda: None)
        assert verify.main(["verify_release.py", str(tmp_path)]) == 1
        assert "FAIL" in capsys.readouterr().err
