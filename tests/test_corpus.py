"""Tests for scripts/build_corpus.py: how each source is cut into articles.

The inputs are small hand-made copies of each source's shape (e-Gov's JSON,
the model rules' tables, the childcare rules' paragraphs), not downloads.
"""
from __future__ import annotations

import pytest

from scripts.build_corpus import (
    Source, childcare_rules_articles, egov_articles, make_chunk, model_rules_articles,
)


def _article(num: str, caption: str, *paragraphs: str) -> dict:
    return {"tag": "Article", "children": [
        {"tag": "ArticleCaption", "children": [f"（{caption}）"]},
        {"tag": "ArticleTitle", "children": [num]},
        *[{"tag": "Paragraph", "children": [
            {"tag": "ParagraphNum", "children": [str(i + 1)] if i else []},
            {"tag": "ParagraphSentence", "children": [{"tag": "Sentence", "children": [p]}]},
        ]} for i, p in enumerate(paragraphs)],
    ]}


def test_egov_takes_main_provision_articles_only():
    law = {"law_full_text": {"tag": "Law", "children": [{"tag": "LawBody", "children": [
        {"tag": "MainProvision", "children": [
            {"tag": "Chapter", "children": [_article("第一条", "目的", "目的を定める。", "次項。")]},
            {"tag": "Article", "children": [
                {"tag": "ArticleTitle", "children": ["第二条"]},
                {"tag": "Paragraph", "children": [{"tag": "ParagraphSentence", "children": ["削除"]}]},
            ]},
        ]},
        {"tag": "SupplProvision", "children": [_article("第一条", "施行期日", "公布の日から施行する。")]},
    ]}]}}
    assert egov_articles(law) == [("第一条", "目的", "目的を定める。\n2　次項。")]


def _p(text: str) -> tuple[str, list[str]]:
    return ("p", [text])


def test_model_rules_take_the_first_alternative_and_number_from_the_contents():
    blocks = [
        _p("第１条（目的）"), _p("第２条（休日）"), _p("[例２]　別の案"), _p("第２条（休日）"), _p("第３条（解雇）"),
        ("tbl", ["（目的）", "　この規則は目的を定める。", "２　次項。"]),
        _p("　目的】"), _p("解説は入れない。"),
        ("tbl", ["（休日）", "第２条　休日は土日とする。"]),
        _p("〔例２〕"),
        ("tbl", ["（休日）", "第２条　休日は日曜とする。"]),
        _p("（解雇）"), _p("第３条　解雇することがある。"), _p("２　予告する。"), _p("　解雇】"), _p("解説。"),
    ]
    assert model_rules_articles(blocks) == [
        ("第1条", "目的", "　この規則は目的を定める。\n２　次項。"),
        ("第2条", "休日", "休日は土日とする。"),
        ("第3条", "解雇", "解雇することがある。\n２　予告する。"),
    ]


def test_model_rules_fail_loudly_when_an_article_is_missing():
    with pytest.raises(ValueError, match="not found"):
        model_rules_articles([_p("第１条（目的）"), ("tbl", ["（別の見出し）", "本文。"])])


def test_childcare_rules_skip_the_alternative_examples():
    paragraphs = [
        "規定例［簡易版］",
        "第１条（育児休業）", "１　休業できる。", "《除外する例》", "２　拒むことができる。", "※以下、１項ずつ繰り下げ", "２　申し出る。",
        "第２条（介護休暇）", "１　取得できる。", "《除外する例》", "１　代わりの案。",
        "（附則）本規則は、○年○月○日から適用する。",
    ]
    assert childcare_rules_articles(paragraphs) == [
        ("第1条", "育児休業", "１　休業できる。\n２　申し出る。"),
        ("第2条", "介護休暇", "１　取得できる。"),
    ]


def test_chunk_text_starts_with_where_it_comes_from():
    src = Source("travel", "旅費法", "egov", "https://example.invalid/api", "https://example.invalid/law")
    chunk = make_chunk(src, "2025-04-01施行", 7, "第七条", "旅費の請求手続", "旅費を請求する。\n")
    assert chunk.id == "travel-007"
    assert chunk.text == "旅費法 第七条（旅費の請求手続）\n旅費を請求する。"
    assert chunk.source == "https://example.invalid/law"
