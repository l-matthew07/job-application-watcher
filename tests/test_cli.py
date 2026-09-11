import io
import json

import pytest
import yaml

from twitter_sentiment import cli
from twitter_sentiment.cli import EXIT_API, EXIT_CONFIG, EXIT_OK


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(yaml.safe_dump({
        "output_path": str(tmp_path / "out.jsonl"),
        "state_path": str(tmp_path / "state.json"),
        "companies": [
            {"name": "Tesla", "terms": ["tesla", "$TSLA"]},
            {"name": "Rivian", "terms": ["rivian"]},
        ],
    }), encoding="utf-8")
    return path


def run_cli(argv):
    out = io.StringIO()
    return cli.main(argv, stream=out), out.getvalue()


def test_dry_run_prints_queries_without_a_token(config_file, monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    code, out = run_cli(["--config", str(config_file), "--dry-run"])

    assert code == EXIT_OK
    assert "Tesla: (tesla OR $TSLA) -is:retweet lang:en" in out
    assert "Rivian: rivian -is:retweet lang:en" in out


def test_only_filters_companies(config_file):
    code, out = run_cli(["--config", str(config_file), "--only", "rivian", "--dry-run"])

    assert code == EXIT_OK
    assert "Rivian:" in out and "Tesla:" not in out


def test_only_with_an_unknown_company_is_an_error(config_file, capsys):
    code, _ = run_cli(["--config", str(config_file), "--only", "nope", "--dry-run"])

    assert code == EXIT_CONFIG
    assert "unknown companies" in capsys.readouterr().err


def test_ad_hoc_company_needs_no_config(tmp_path):
    code, out = run_cli(["--company", "Rivian", "--term", "rivian",
                         "--term", "$RIVN", "--dry-run"])

    assert code == EXIT_OK
    assert out.strip() == "Rivian: (rivian OR $RIVN) -is:retweet lang:en"


def test_ad_hoc_company_defaults_terms_to_its_name():
    _, out = run_cli(["--company", "Rivian", "--dry-run"])

    assert out.strip() == "Rivian: Rivian -is:retweet lang:en"


def test_lang_and_paging_overrides_apply(config_file):
    _, out = run_cli(["--config", str(config_file), "--lang", "es", "--dry-run"])

    assert "lang:es" in out


def test_invalid_override_is_reported_not_sent(config_file, capsys):
    code, _ = run_cli(["--config", str(config_file), "--max-results", "500", "--dry-run"])

    assert code == EXIT_CONFIG
    assert "max_results" in capsys.readouterr().err


def test_config_and_company_together_is_an_error(config_file, capsys):
    code, _ = run_cli(["--config", str(config_file), "--company", "Tesla", "--dry-run"])

    assert code == EXIT_CONFIG
    assert "not both" in capsys.readouterr().err


def test_no_target_is_an_error(capsys):
    code, _ = run_cli(["--dry-run"])

    assert code == EXIT_CONFIG
    assert "nothing to watch" in capsys.readouterr().err


def test_missing_config_file_is_an_error(tmp_path, capsys):
    code, _ = run_cli(["--config", str(tmp_path / "nope.yaml"), "--dry-run"])

    assert code == EXIT_CONFIG
    assert "not found" in capsys.readouterr().err


def test_missing_token_is_reported_before_any_request(config_file, monkeypatch, capsys):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)

    code, _ = run_cli(["--config", str(config_file)])

    assert code == EXIT_CONFIG
    assert "X_BEARER_TOKEN is not set" in capsys.readouterr().err


def test_api_failure_exits_with_the_api_code(config_file, monkeypatch, capsys):
    from twitter_sentiment.client import XApiError

    monkeypatch.setenv("X_BEARER_TOKEN", "token")
    monkeypatch.setattr(cli, "XSearchClient", lambda *a, **k: _BoomClient())

    def boom(*args, **kwargs):
        raise XApiError("search failed with HTTP 503")

    monkeypatch.setattr(cli, "run_company", boom)
    code, _ = run_cli(["--config", str(config_file)])

    assert code == EXIT_API
    assert "X API error" in capsys.readouterr().err


class _BoomClient:
    requests_made = 0


def test_parser_rejects_unknown_flags():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--nonsense"])
