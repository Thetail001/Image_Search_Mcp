"""参数契约 vs 上游真实签名的契约测试。

== 为什么这个文件存在 ==

早期版本对外公示了一批"高级参数"，其中至少三个是编的或无效的：

- ``Yandex.rpt`` / ``Yandex.cbir_page`` —— 签名里没有，上游在 ``engines/yandex.py``
  内写死，用户传什么都被覆盖
- ``TraceMoe.cutBorders`` —— HTTP 查询串里的名字；Python 侧叫 ``cut_borders``，
  传旧名字会被 ``**kwargs`` 吞掉，出站请求仍是默认值
- ``SauceNAO.output_type`` —— 参数存在，但解析永远走 ``json_loads``，
  设成非 2 会让 SauceNAO 返回 HTML 而解析失败

根因是"公示"和"行为"是两份手写副本，没有东西把它们绑在一起。本文件就是把它们绑起来：
**契约里出现的每个参数，必须在已安装上游的真实签名里存在；契约里排除的，必须真的不在。**
上游一改签名，这里先红。
"""

from __future__ import annotations

import inspect

import pytest
from PicImageSearch import (
    Ascii2D,
    BaiDu,
    Bing,
    EHentai,
    Google,
    GoogleLens,
    Iqdb,
    SauceNAO,
    Tineye,
    TraceMoe,
    Yandex,
)

from image_search_mcp import params

ENGINE_CLASSES = {
    "SauceNAO": SauceNAO,
    "Google": Google,
    "TraceMoe": TraceMoe,
    "Ascii2D": Ascii2D,
    "BaiDu": BaiDu,
    "Bing": Bing,
    "EHentai": EHentai,
    "GoogleLens": GoogleLens,
    "Iqdb": Iqdb,
    "Tineye": Tineye,
    "Yandex": Yandex,
}


def _accepted_kwargs(func) -> set[str]:
    """函数显式接受的具名参数（不含 **kwargs 本身）。"""
    return {
        name
        for name, p in inspect.signature(func).parameters.items()
        if name != "self"
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }


def test_contract_covers_exactly_the_advertised_engines():
    assert set(params.CONTRACTS) == set(ENGINE_CLASSES)


@pytest.mark.parametrize("engine", sorted(ENGINE_CLASSES))
def test_init_params_exist_in_real_signature(engine: str):
    """契约里声明的 init 参数，上游 __init__ 必须真的接受。"""
    contract = params.CONTRACTS[engine]
    accepted = _accepted_kwargs(ENGINE_CLASSES[engine].__init__)
    for key in contract.init:
        assert key in accepted, (
            f"{engine}.__init__ 不接受 '{key}'，"
            f"但契约把它公示为可用参数。上游签名已变，或契约写错了。"
        )


@pytest.mark.parametrize("engine", sorted(ENGINE_CLASSES))
def test_search_params_exist_in_real_signature(engine: str):
    """契约里声明的 search 参数，上游 search 必须真的接受。

    这条抓的就是 ``cutBorders`` 那类"名字对了但拼错"的缺陷：上游 search 带
    ``**kwargs``，拼错的键不会报错，只会被静默吞掉。
    """
    contract = params.CONTRACTS[engine]
    accepted = _accepted_kwargs(ENGINE_CLASSES[engine].search)
    for key in contract.search:
        assert key in accepted, (
            f"{engine}.search 不接受 '{key}'。注意上游带 **kwargs，"
            f"传错的键不会报错、只会被静默忽略。"
        )


@pytest.mark.parametrize("engine,key", [
    # 旧版公示过的无效参数：必须永远不在契约里
    ("Yandex", "rpt"),
    ("Yandex", "cbir_page"),
    ("TraceMoe", "cutBorders"),      # 正确名字是 cut_borders
    ("SauceNAO", "output_type"),
])
def test_known_invalid_params_stay_out_of_the_contract(engine: str, key: str):
    contract = params.CONTRACTS[engine]
    assert key not in contract.init, f"{engine} 的 {key} 曾被证实无效，不该回到白名单"
    assert key not in contract.search, f"{engine} 的 {key} 曾被证实无效，不该回到白名单"


def test_cut_borders_is_the_real_name():
    """TraceMoe 的正确参数名是下划线形式，且默认值为 True。"""
    spec = params.CONTRACTS["TraceMoe"].search["cut_borders"]
    assert spec.default is True
    assert "cutBorders" not in params.CONTRACTS["TraceMoe"].search


def test_contract_has_no_reserved_keys():
    """保留键绝不能出现在任何引擎的参数面里。"""
    for engine, contract in params.CONTRACTS.items():
        for key in list(contract.init) + list(contract.search):
            assert key not in params.RESERVED_KEYS, (
                f"{engine} 的契约把保留键 '{key}' 暴露给了调用方"
            )
