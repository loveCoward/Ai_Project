import os
from unittest.mock import patch

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker", action="store_true", default=False, help="run docker integration tests"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: mark test as requiring docker containers")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-docker"):
        skip_docker = pytest.mark.skip(reason="need --run-docker option to run")
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(skip_docker)


@pytest.fixture
def mock_env():
    """Fixture to ensure environment is clean for each test."""
    # Home-directory variables are kept: Streamlit resolves its config path with
    # Path.home() (via streamlit.file_util.get_streamlit_file_path), which raises
    # RuntimeError when USERPROFILE/HOME is missing on Windows. Secrets and API
    # keys are still removed because everything else is cleared.
    home_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"} and value
    }
    with patch.dict(os.environ, home_env, clear=True):
        yield
