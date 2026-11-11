"""Settings must survive the python-dotenv `KEY=   # comment` gotcha."""
from __future__ import annotations

from scholarship_finder.config import Settings

_KEYS = ("JINA_READER_KEY", "JINA_API_KEY", "DEMO_ACCESS_CODE", "DEPLOYMENT_PROFILE",
         "GROQ_API_KEY", "KB_PATH", "TAVILY_API_KEY")


def _settings(tmp_path, monkeypatch, body: str) -> Settings:
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text(body, encoding="utf-8")
    return Settings(_env_file=env)


def test_comment_value_is_dropped_and_reader_key_falls_back(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch,
                  "JINA_API_KEY=jina_abc\nJINA_READER_KEY=   # free key \u2192 500 RPM\n")
    assert s.jina_reader_key == ""
    assert s.effective_jina_reader_key() == "jina_abc"


def test_comment_access_code_does_not_gate_the_app(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "DEMO_ACCESS_CODE=   # shared passcode; empty = open\n")
    assert not s.demo_access_code


def test_values_are_stripped_and_unquoted(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, 'GROQ_API_KEY="gsk_123"  \nTAVILY_API_KEY=  tvly-x  \n')
    assert s.groq_api_key == "gsk_123"
    assert s.tavily_api_key == "tvly-x"


def test_inline_comment_after_value_and_enum_still_parse(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "DEPLOYMENT_PROFILE=FULL   # container\n")
    assert s.deployment_profile.value == "FULL"


def test_comment_on_defaulted_str_field_keeps_default(tmp_path, monkeypatch):
    default = Settings(_env_file=None).kb_path
    s = _settings(tmp_path, monkeypatch, "KB_PATH=   # where the cache lives\n")
    assert s.kb_path == default
