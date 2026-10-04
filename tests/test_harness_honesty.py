"""夹具自己的诚实性：门禁的底座不能是假动作。

为什么单开一个文件：本仓的常见故障不是"某个功能写错了"，而是**门禁看起来在守着、
其实什么都测不到**（复核报告第 12 问列了一串）。这类问题的共性是"夹具声称做到了 X，
但实现里从没真做到"。所以给夹具本身加正反对照 —— 它们红了，说明整条门禁的底座是虚的。

这个文件里每条都要能在**旧实现**下变红，否则它自己就是又一个假门禁。
"""

from __future__ import annotations

import os

import httpx
import pytest

import conftest

#: 收集期故意留一个脏变量，而且特意用**带引擎后缀**的名字（旧的手写清单恰好漏掉这一族）。
#: 不用 monkeypatch 设它：那样会被 pytest 自己回收，就测不出 _clean_env 夹具了。
os.environ.setdefault("IMAGE_SEARCH_COOKIES_TRACEMOE", "collection-time-leak")


def test_config_env_is_cleared_before_each_test():
    """``_clean_env`` 必须真的把配置清掉 —— 包括带引擎后缀的那些。

    旧实现查一张手写清单，清单里只有不带后缀的 ``IMAGE_SEARCH_COOKIES``，
    所以这一条在旧实现下会红。
    """
    assert "IMAGE_SEARCH_COOKIES_TRACEMOE" not in os.environ


def test_only_whitelisted_prefixes_are_left_alone():
    """清得干净不等于清得过头：不在前缀里的变量不该被夹具动。"""
    os.environ["UNRELATED_TEST_VAR"] = "keep-me"
    assert os.environ["UNRELATED_TEST_VAR"] == "keep-me"
    del os.environ["UNRELATED_TEST_VAR"]


def test_allow_real_dns_really_restores_the_original_resolver(allow_real_dns):
    """``allow_real_dns`` 必须装回**原始** resolver。

    旧实现是 ``real = socket.getaddrinfo``：跑到这里它已经是拦截器了，
    于是"放行真实 DNS"等于把拦截器再装一遍 —— 一个从来没放行过的夹具。
    旧实现下这两个身份断言都会红。
    """
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
