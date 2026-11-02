import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import mock_wx  # noqa: E402


@pytest.fixture()
def mock():
    """本地风控模拟服务：列表接口 + 文章页 + 验证页 + 探针页。"""
    srv, state, base = mock_wx.start(0)
    srv.daemon_threads = True
    try:
        yield state, base
    finally:
        srv.shutdown()
        srv.server_close()
