import pytest

from ytp.common.config import Settings
from ytp.common.runtime import build
from ytp.fakes import fake_services
from ytp.pipeline import Pipeline


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, backend="local", data_dir=tmp_path / "data", news_context=True,
                    top_n_topics=2, console_dev_auth=True, platforms=["YOUTUBE", "INSTAGRAM"])


@pytest.fixture
def rt(settings):
    return build(settings)


@pytest.fixture
def services():
    return fake_services()


@pytest.fixture
def pipeline(rt, services):
    return Pipeline(rt, services)
