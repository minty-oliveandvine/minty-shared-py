"""minty_shared.config: the environment readers every backend shares. Lifted with Flask's
tests/test_env_helpers.py and the Django APIs' test_dburl / test_smtpurl / test_preview_origin /
test_settings_guard, which these replace."""

import re

import pytest

from minty_shared import config

# --------------------------------------------------------------------------- APP_ENV, flags


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("development", "development"),
        (" Development ", "development"),
        ("production", "production"),
        ("staging", "production"),
        ("", "production"),
        (None, "production"),
    ],
)
def test_app_env_is_development_only_when_it_says_so(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", raw)
    assert config.app_env() == expected
    assert config.is_development() is (expected == "development")


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_flag_on(monkeypatch, raw):
    monkeypatch.setenv("X_FLAG", raw)
    assert config.flag("X_FLAG", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no", "nonsense", ""])
def test_flag_off(monkeypatch, raw):
    monkeypatch.setenv("X_FLAG", raw)
    assert config.flag("X_FLAG", True) is False


def test_flag_unset_or_blank_as_unset_is_the_default(monkeypatch):
    monkeypatch.delenv("X_FLAG", raising=False)
    assert config.flag("X_FLAG", True) is True
    monkeypatch.setenv("X_FLAG", "")
    assert config.flag("X_FLAG", True, blank_is_unset=True) is True


def test_the_placeholder_key_is_refused_outside_development():
    with pytest.raises(RuntimeError, match="SECRET_KEY is not set"):
        config.require_secret_key(None, placeholder="change-me", development=False)
    with pytest.raises(RuntimeError):
        config.require_secret_key("change-me", placeholder="change-me", development=False)
    assert config.require_secret_key(None, placeholder="change-me", development=True) == "change-me"
    assert config.require_secret_key("real", placeholder="change-me", development=False) == "real"


# --------------------------------------------------------------------------- PREVIEW_ORIGIN_REGEX

GOOD = r"^https://pr-[a-z0-9-]{1,40}-(my|payment-request)\.dailyminty\.com$"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_preview_origin_unset_is_off(raw):
    assert config.preview_origin_regexes(raw) == []
    assert config.preview_origin_regex(raw) is None


def test_preview_origin_reads_the_terraform_value():
    assert config.preview_origin_regexes(GOOD) == [GOOD]
    assert config.preview_origin_regex(f"  {GOOD}  ").match("https://pr-feat-x-my.dailyminty.com")


@pytest.mark.parametrize(
    "raw",
    [
        r"^https://pr-.*",  # no suffix
        r"https://pr-x\.dailyminty\.com$",  # no anchor
        r"^https://pr-x\.dailyminty\.com$|^https://evil\.com$",  # top-level bar
        r"^https://pr-x\.dailyminty\.com$|.*",
        r"^https://pr-(\.dailyminty\.com$",  # does not compile
    ],
)
def test_a_loose_preview_origin_is_refused(raw):
    with pytest.raises(ValueError, match="PREVIEW_ORIGIN_REGEX"):
        config.preview_origin_regexes(raw)


def test_a_bar_inside_a_group_or_class_is_allowed():
    raw = r"^https://pr-[a|b]-(my|web)\.dailyminty\.com$"
    assert config.preview_origin_regexes(raw) == [raw]


# --------------------------------------------------------------------------- DATABASE_URL


def test_database_url_schema_is_popped_and_other_params_kept():
    db = config.parse_database_url(
        "postgresql://u:p%40ss@db.host:6543/minty?schema=development&sslmode=require"
    )
    assert (db.user, db.password, db.host, db.port, db.name) == (
        "u",
        "p@ss",
        "db.host",
        6543,
        "minty",
    )
    assert db.schema == "development"
    assert db.params == (("sslmode", "require"),)
    assert db.sqlalchemy == "postgresql+psycopg2://u:p%40ss@db.host:6543/minty?sslmode=require"
    assert db.libpq == "postgresql://u:p%40ss@db.host:6543/minty?sslmode=require"


@pytest.mark.parametrize("url", ["postgresql://u@h/d", "postgresql://u@h/d?schema="])
def test_database_url_schema_defaults(url):
    assert config.parse_database_url(url).schema == "pettycashv3"
    assert config.parse_database_url(url).port == 5432


@pytest.mark.parametrize(
    "scheme", ["postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"]
)
def test_database_url_accepts_every_postgres_scheme(scheme):
    assert config.parse_database_url(f"{scheme}://u@h/d").sqlalchemy.startswith(
        "postgresql+psycopg2://"
    )


@pytest.mark.parametrize("url", ["mysql://u@h/d", "sqlite:///x.db", "http://h/d"])
def test_database_url_refuses_other_databases(url):
    with pytest.raises(ValueError, match="postgresql://"):
        config.parse_database_url(url)


@pytest.mark.parametrize("schema", ["pettycashv3,public", "x;drop table y", "1abc", "a-b", "a b"])
def test_database_url_refuses_a_schema_that_is_not_an_identifier(schema):
    # It is spliced into search_path: only a plain identifier may get there.
    with pytest.raises(ValueError, match="plain identifier"):
        config.parse_database_url(f"postgresql://u@h/d?schema={schema}")


def test_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "  postgresql://u@h/d  ")
    assert config.database_url_from_env() == "postgresql://u@h/d"
    monkeypatch.setenv("DATABASE_URL", "   ")
    assert config.database_url_from_env(config.DEFAULT_LOCAL_DATABASE_URL).endswith("/postgres")
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        config.database_url_from_env()


def test_django_database():
    db, schema = config.django_database("postgresql://u:p%2Fw@h:5433/d?schema=dev&sslmode=require")
    assert schema == "dev"
    assert db == {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "d",
        "USER": "u",
        "PASSWORD": "p/w",
        "HOST": "h",
        "PORT": "5433",
        "OPTIONS": {"sslmode": "require", "options": "-c search_path=dev,public"},
    }


def test_with_schema_replaces_the_schema():
    url = config.with_schema("postgresql://u@h/d?schema=a&sslmode=require", "b")
    assert config.parse_database_url(url).schema == "b"
    assert "sslmode=require" in url


# --------------------------------------------------------------------------- S3_URL


def test_s3_url_backblaze():
    s3 = config.parse_s3_url("https://key:se%2Fcret@s3.us-east-005.backblazeb2.com/pettycash")
    assert s3 == config.S3Settings(
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        bucket="pettycash",
        key="key",
        secret="se/cret",
        region="us-east-005",
    )


def test_s3_url_region_query_wins_and_default_and_port():
    assert config.parse_s3_url("https://k:s@s3.a-b.host/b?region=eu-1").region == "eu-1"
    s3 = config.parse_s3_url("http://k:s@localhost:9000/b")
    assert (s3.region, s3.endpoint_url) == ("us-east-1", "http://localhost:9000")


@pytest.mark.parametrize(
    "url", ["https://k:s@host/", "https://k:s@host/a/b", "ftp://k:s@host/b", "nonsense"]
)
def test_s3_url_refuses_a_bad_shape(url):
    with pytest.raises(ValueError, match="S3_URL"):
        config.parse_s3_url(url)


# --------------------------------------------------------------------------- SMTP_URL


def test_smtp_url_starttls():
    smtp = config.parse_smtp_url("smtp://user%40x:pa%2Fss@smtp-relay.brevo.com:587?timeout=5")
    assert (smtp.host, smtp.port, smtp.username, smtp.password) == (
        "smtp-relay.brevo.com",
        587,
        "user@x",
        "pa/ss",
    )
    assert (smtp.use_tls, smtp.use_ssl, smtp.timeout) == (True, False, 5.0)


def test_smtp_url_implicit_ssl_and_default_port():
    smtp = config.parse_smtp_url("smtps://u:p@mail.host")
    assert (smtp.port, smtp.use_tls, smtp.use_ssl, smtp.timeout) == (465, False, True, 10.0)


def test_smtp_url_local_catcher():
    smtp = config.parse_smtp_url("smtp://localhost:1025?tls=0")
    assert (smtp.use_tls, smtp.username, smtp.password) == (False, None, None)


@pytest.mark.parametrize("url", ["http://h:25", "smtp://", "nonsense"])
def test_smtp_url_refuses_another_shape(url):
    with pytest.raises(ValueError, match="SMTP_URL"):
        config.parse_smtp_url(url)


@pytest.mark.parametrize("url", [None, "", "  "])
def test_django_email_unset_is_the_console(url):
    email = config.django_email(url)
    assert email["EMAIL_BACKEND"] == config.CONSOLE_BACKEND
    assert email["EMAIL_HOST"] == ""


def test_django_email_from_the_url():
    email = config.django_email("smtps://u:p@mail.host?timeout=3")
    assert email == {
        "EMAIL_BACKEND": config.SMTP_BACKEND,
        "EMAIL_HOST": "mail.host",
        "EMAIL_PORT": 465,
        "EMAIL_HOST_USER": "u",
        "EMAIL_HOST_PASSWORD": "p",
        "EMAIL_USE_TLS": False,
        "EMAIL_USE_SSL": True,
        "EMAIL_TIMEOUT": 3.0,
    }


def test_no_framework_import():
    import minty_shared.config as mod

    source = open(mod.__file__, encoding="utf-8").read()
    assert not re.search(r"^\s*(from|import)\s+(django|flask)\b", source, re.M)
