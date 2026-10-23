"""`cne serve` answers each page in the language the page is set to."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from cnequity import i18n
from cnequity.serve.app import create_app, request_language
from cnequity.serve.display import local


def test_the_page_choice_wins_over_the_browser_and_the_server():
    assert request_language({"x-cne-lang": "en", "accept-language": "zh-CN,zh;q=0.9"}) == "en"
    assert request_language({"x-cne-lang": "zh-CN", "accept-language": "en-US"}) == "zh_CN"
    assert request_language({"accept-language": "zh-CN,zh;q=0.9,en;q=0.8"}) == "zh_CN"
    assert request_language({"accept-language": "fr-FR,en;q=0.5"}) == "en"
    assert request_language({"accept-language": "fr-FR"}) is None
    assert request_language({}) is None


def test_one_error_reads_in_each_pages_language(config):
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    missing = "/api/ops/jobs/" + "ab" * 16
    english = client.get(missing, headers={"X-CNE-Lang": "en"})
    chinese = client.get(missing, headers={"X-CNE-Lang": "zh-CN"})
    assert english.status_code == chinese.status_code == 422
    assert english.json()["detail"] == "No such task."
    assert chinese.json()["detail"] == "没有这个任务。"
    # A page opened directly carries only the browser's Accept-Language.
    browser = client.get(missing, headers={"Accept-Language": "zh-CN,zh;q=0.9"})
    assert browser.json()["detail"] == "没有这个任务。"
    # No preference at all: the server's own language (English under tests).
    assert client.get(missing).json()["detail"] == "No such task."


def test_the_scope_does_not_leak_past_the_request(config):
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    client.get("/api/ops/jobs/" + "ab" * 16, headers={"X-CNE-Lang": "zh-CN"})
    assert i18n.current_language() == "en"


def test_a_background_thread_keeps_the_language_it_was_started_in():
    seen: list[str] = []
    with i18n.use_language("zh_CN"):
        plain = threading.Thread(target=lambda: seen.append(i18n.current_language()))
        carried = threading.Thread(
            target=i18n.carry_language(lambda: seen.append(i18n.current_language()))
        )
    plain.start(), plain.join()
    carried.start(), carried.join()
    assert seen == ["en", "zh_CN"]


def test_a_stored_title_follows_the_sentence_it_is_woven_into():
    with i18n.use_language("en"):
        assert local("创建数据备份") == "Create data backup"
        assert local("Create data backup") == "Create data backup"
    with i18n.use_language("zh_CN"):
        assert local("Create data backup") == "创建数据备份"
        assert local("创建数据备份") == "创建数据备份"
