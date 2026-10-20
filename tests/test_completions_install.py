"""``deals completions SHELL --install`` writes one file and edits nothing else."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from audible_deals import completion_install
from audible_deals.cli import cli

EXPECTED = {
    "bash": (".local", "share", "bash-completion", "completions", "deals"),
    "zsh": (".zfunc", "_deals"),
    "fish": (".config", "fish", "completions", "deals.fish"),
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway home with no XDG overrides."""
    directory = tmp_path / "userhome"
    directory.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(directory))
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    return directory


def run(*args):
    return CliRunner().invoke(cli, ["completions", *args])


@pytest.mark.parametrize("shell", completion_install.SHELLS)
class TestInstall:
    def test_without_the_flag_it_still_prints_and_writes_nothing(self, home, shell):
        result = run(shell)

        assert result.exit_code == 0
        assert "_DEALS_COMPLETE" in result.stdout
        assert list(home.iterdir()) == []

    def test_it_writes_the_script_to_the_shells_own_directory(self, home, shell):
        printed = run(shell).stdout

        result = run(shell, "--install")

        target = home.joinpath(*EXPECTED[shell])
        assert result.exit_code == 0, result.output
        assert target.read_text(encoding="utf-8") == printed.rstrip("\n") + "\n"
        assert f"Installed: {target}" in result.stdout
        assert "start a new shell" in result.stdout.lower()

    def test_running_it_again_changes_nothing(self, home, shell):
        run(shell, "--install")
        target = home.joinpath(*EXPECTED[shell])
        before = target.read_bytes()

        result = run(shell, "--install")

        assert result.exit_code == 0
        assert "Already up to date" in result.stdout
        assert target.read_bytes() == before

    def test_an_older_copy_of_ours_is_replaced(self, home, shell):
        target = home.joinpath(*EXPECTED[shell])
        target.parent.mkdir(parents=True)
        target.write_text("# old\n_DEALS_COMPLETE=old\n", encoding="utf-8")

        result = run(shell, "--install")

        assert result.exit_code == 0
        assert "Updated" in result.stdout
        assert "_DEALS_COMPLETE=old" not in target.read_text(encoding="utf-8")

    def test_someone_elses_file_is_never_overwritten(self, home, shell):
        target = home.joinpath(*EXPECTED[shell])
        target.parent.mkdir(parents=True)
        target.write_text("my own completion\n", encoding="utf-8")

        result = run(shell, "--install")

        assert result.exit_code == 1
        assert "not a deals completion script" in result.stderr
        assert target.read_text(encoding="utf-8") == "my own completion\n"

    def test_startup_files_are_never_touched(self, home, shell):
        rc_files = [home / ".bashrc", home / ".zshrc", home / ".profile"]
        for rc in rc_files:
            rc.write_text("# mine\n", encoding="utf-8")

        run(shell, "--install")

        assert [rc.read_text(encoding="utf-8") for rc in rc_files] == ["# mine\n"] * 3

    def test_an_unwritable_location_is_a_clean_error(self, home, shell):
        blocker = home.joinpath(EXPECTED[shell][0])
        blocker.write_text("a file where a directory should be", encoding="utf-8")

        result = run(shell, "--install")

        assert result.exit_code == 1
        assert result.stderr.startswith("Error: Could not write")


def test_the_zsh_hint_says_how_to_put_the_directory_on_fpath(home):
    result = run("zsh", "--install")

    assert "fpath+=~/.zfunc" in result.stdout
    assert "compinit" in result.stdout


class TestXdgLocations:
    def test_bash_follows_xdg_data_home(self, home, tmp_path, monkeypatch):
        data = tmp_path / "xdg-data"
        monkeypatch.setenv("XDG_DATA_HOME", str(data))

        run("bash", "--install")

        assert (data / "bash-completion" / "completions" / "deals").is_file()
        assert not (home / ".local").exists()

    def test_fish_follows_xdg_config_home(self, home, tmp_path, monkeypatch):
        config = tmp_path / "xdg-config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

        run("fish", "--install")

        assert (config / "fish" / "completions" / "deals.fish").is_file()

    def test_a_relative_xdg_value_is_ignored(self, home, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")

        run("fish", "--install")

        assert home.joinpath(*EXPECTED["fish"]).is_file()


def test_the_documented_locations_match_the_code(home):
    docs = (
        __import__("pathlib").Path(completion_install.__file__).parents[2]
        / "docs"
        / "commands.md"
    ).read_text(encoding="utf-8")

    for shell, parts in EXPECTED.items():
        assert str(home.joinpath(*parts)).replace(str(home), "~") in docs, shell
    for shell in completion_install.SHELLS:
        assert f"deals completions {shell} --install" in docs
