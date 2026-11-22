"""Keep required CI coverage from silently turning into skipped tests."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--fail-on-skip", action="store_true", help="Fail the run if any test is skipped.")


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("fail_on_skip"):
        config.pluginmanager.register(_FailOnSkip())


class _FailOnSkip:
    def __init__(self) -> None:
        self.skips = 0

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        self.skips += report.skipped

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.skips += report.skipped

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.skips and exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.skips:
            terminalreporter.write_line(f"--fail-on-skip: {self.skips} skipped test or collection reports.")
