"""实时信息层 · 去重/分级/标的关联管道测试。"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.events import NewsCategory, Priority
from info_layer.base import RawNews
from info_layer.pipeline import NewsPipeline, classify_priority, extract_symbols


def _raw(title: str, body: str = "") -> RawNews:
    return RawNews(title=title, body=body, source="t",
                   published_at=datetime.now(timezone.utc))


class TestDedup:
    def test_identical_title_filtered(self):
        p = NewsPipeline()
        assert p.process(_raw("美联储降息")) is not None
        assert p.process(_raw("美联储降息")) is None

    def test_punctuation_ignored(self):
        p = NewsPipeline()
        assert p.process(_raw("美联储降息！")) is not None
        # 标点/空白差异不改变指纹
        assert p.process(_raw("美 联 储 降 息！！！")) is None

    def test_different_title_passes(self):
        p = NewsPipeline()
        assert p.process(_raw("美联储降息")) is not None
        assert p.process(_raw("欧洲央行加息")) is not None


class TestClassify:
    def test_p1_monetary(self):
        assert classify_priority("美联储宣布降息50bp") == Priority.P1

    def test_p1_geopolitics(self):
        assert classify_priority("某地遭导弹袭击") == Priority.P1

    def test_p2_macro_data(self):
        assert classify_priority("美国CPI数据今晚公布") == Priority.P2

    def test_p3_routine(self):
        assert classify_priority("某公司发布季报") == Priority.P3


class TestSymbolExtraction:
    def test_fed_maps_btc(self):
        syms = extract_symbols("美联储紧急降息")
        assert "BTC/USDT" in syms

    def test_no_match(self):
        assert extract_symbols("一场普通的足球比赛") == []


class TestEventShape:
    def test_process_output(self):
        p = NewsPipeline()
        ev = p.process(_raw("美联储降息", body="正文"))
        assert ev is not None
        assert ev.priority == Priority.P1
        assert ev.fingerprint
        assert ev.category == NewsCategory.CRYPTO   # RawNews 默认类目透传
