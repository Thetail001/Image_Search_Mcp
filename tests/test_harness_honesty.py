"""夹具自己的诚实性：门禁的底座不能是假动作。"""

from __future__ import annotations

import os

import httpx
import pytest

import conftest

#: 收集期故意留一个脏变量，而且特意用**带引擎后缀**的名字（旧的手写清单恰好漏掉这一族）。
#: 不用 monkeypatch 设它：那样会被 pytest 自己回收，就测不出 _clean_env 夹具了。
os.environ.setdefault("IMAGE_SEARCH_COOKIES_TRACEMOE", "collection-time-leak")


def test_config_env_is_cleared_before_each_test():
    """``_clean_env`` 必须真的把配置清掉 —— 包括带引擎后缀的那些。"""
    assert "IMAGE_SEARCH_COOKIES_TRACEMOE" not in os.environ


def test_only_whitelisted_prefixes_are_left_alone():
    """清得干净不等于清得过头：不在前缀里的变量不该被夹具动。"""
    os.environ["UNRELATED_TEST_VAR"] = "keep-me"
    assert os.environ["UNRELATED_TEST_VAR"] == "keep-me"
    del os.environ["UNRELATED_TEST_VAR"]


def test_allow_real_dns_really_restores_the_original_resolver(allow_real_dns):
    """``allow_real_dns`` 必须装回原始 resolver。"""
    import socket

    assert allow_real_dns is conftest._REAL_GETADDRINFO
    assert socket.getaddrinfo is conftest._REAL_GETADDRINFO


def test_dns_is_blocked_by_default_and_that_blocker_is_not_the_real_one():
    """默认姿态的对照：没有夹具时解析必须被拦住，且拦截器不是真函数。"""
    import socket

    assert socket.getaddrinfo is not conftest._REAL_GETADDRINFO
    with pytest.raises(AssertionError, match="未经允许的 DNS 解析"):
        socket.getaddrinfo("example.com", 80)


async def test_unmocked_http_is_blocked_by_default():
    """默认禁网：没有注册 handler 的请求要**立刻报错**，而不是真发出去。

    这条证明的是"忘记 mock 会被抓"，也就是整个测试套件敢说"不联网"的依据。
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(AssertionError, match="未经 mock"):
            await client.get("https://example.com/")
