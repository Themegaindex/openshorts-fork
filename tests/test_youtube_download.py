import io
import json
import sys
from types import SimpleNamespace

import pytest

import quality_probe
import youtube_download as youtube


COOKIES = '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-test-value\n'


@pytest.fixture(autouse=True)
def clean_youtube_config(monkeypatch):
    for key in ('YOUTUBE_COOKIES', 'YOUTUBE_COOKIES_FILE', 'YOUTUBE_COOKIES_BROWSER', 'YOUTUBE_COOKIES_PROFILE'):
        monkeypatch.delenv(key, raising=False)


def test_browser_is_opt_in_and_anonymous_is_not_probed_twice(tmp_path):
    assert youtube.youtube_auth_attempts(tmp_path) == [
        ('anonymous-fallback', {'cookiefile': None, 'cookiesfrombrowser': None}),
    ]


def test_explicit_browser_profile_and_file_are_separate_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv('YOUTUBE_COOKIES_BROWSER', 'Brave')
    monkeypatch.setenv('YOUTUBE_COOKIES_PROFILE', 'Profile 2')
    monkeypatch.setenv('YOUTUBE_COOKIES_FILE', 'fresh.txt')
    (tmp_path / 'www.youtube.com_cookies.txt').write_text(COOKIES)

    attempts = youtube.youtube_auth_attempts(tmp_path)

    assert [name for name, _ in attempts] == ['browser-auth', 'cookie-auth', 'anonymous-fallback']
    assert attempts[0][1] == {'cookiesfrombrowser': ('brave', 'Profile 2'), 'cookiefile': None}
    assert attempts[1][1] == {'cookiesfrombrowser': None, 'cookiefile': tmp_path / 'fresh.txt'}
    assert attempts[-1][1] == {'cookiesfrombrowser': None, 'cookiefile': None}


@pytest.mark.parametrize('source_kind', ['file', 'environment'])
def test_cookie_sessions_never_modify_source_or_reuse_mutated_snapshot(monkeypatch, tmp_path, source_kind):
    path = tmp_path / 'www.youtube.com_cookies.txt'
    path.write_text(COOKIES, encoding='utf-8')
    if source_kind == 'environment':
        monkeypatch.setenv('YOUTUBE_COOKIES', COOKIES.replace('private-test-value', 'env-private'))
    options = youtube.youtube_auth_attempts(tmp_path)[0][1]
    snapshots = []
    seen = []

    class FakeYDL:
        def __init__(self, opts):
            self.cookiefile = opts['cookiefile']
            snapshots.append(self.cookiefile)
            seen.append(self.cookiefile.read())

        def __enter__(self):
            return self

        def __exit__(self, *args):
            # Real yt-dlp writes response cookies on exit, even after a failure.
            self.cookiefile.seek(0)
            self.cookiefile.truncate()
            self.cookiefile.write('mutated session')

    with youtube.open_youtube_dl(options, FakeYDL):
        pass
    with pytest.raises(RuntimeError, match='download failed'):
        with youtube.open_youtube_dl(options, FakeYDL):
            raise RuntimeError('download failed')

    assert seen[0] == seen[1] == (COOKIES if source_kind == 'file' else COOKIES.replace('private-test-value', 'env-private'))
    assert snapshots[0] is not snapshots[1]
    assert all(snapshot.closed for snapshot in snapshots)
    assert path.read_text(encoding='utf-8') == COOKIES


@pytest.mark.parametrize('content', [
    'private-test-value',
    COOKIES.replace('\t0\t', '\tinvalid-private-test-value\t'),
    COOKIES.replace('\tTRUE\t/', '\tFALSE\t/'),
    COOKIES.replace('\t0\t', '\t' + ('9' * 400) + '\t'),
    '# Netscape HTTP Cookie File\nprivate-test-value',
])
def test_malformed_cookie_contents_never_reach_downloader_or_logs(content, capsys):
    with pytest.raises(youtube.YouTubeDownloadError) as error:
        with youtube.open_youtube_dl({'cookiefile': io.StringIO(content)}, lambda _: pytest.fail('must validate first')):
            pass
    assert 'Netscape' in str(error.value)
    assert 'private-test-value' not in str(error.value)
    assert 'private-test-value' not in str(capsys.readouterr())


def test_bom_and_httponly_cookies_are_accepted():
    content = '\ufeff' + COOKIES.replace('.youtube.com', '#HttpOnly_.youtube.com')
    assert youtube._validated_cookie_text(io.StringIO(content)) == content.lstrip('\ufeff')


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('expires', ['0', '2000000000.0'])
def test_real_ytdlp_loads_and_saves_snapshot_without_changing_export(tmp_path, newline, expires):
    yt_dlp = pytest.importorskip('yt_dlp')
    path = tmp_path / 'cookies.txt'
    content = COOKIES.replace('\t0\t', f'\t{expires}\t').replace('\n', newline)
    path.write_bytes(content.encode('utf-8'))
    for source in (path, io.StringIO(content), path):
        with youtube.open_youtube_dl({'cookiefile': source, 'quiet': True}, yt_dlp.YoutubeDL) as ydl:
            assert any(cookie.name == 'SID' and cookie.value == 'private-test-value' for cookie in ydl.cookiejar)
            ydl.cookiejar.clear()
    assert path.read_bytes() == content.encode('utf-8')


def test_missing_configured_cookie_file_has_actionable_error(tmp_path):
    with pytest.raises(youtube.YouTubeDownloadError, match='YOUTUBE_COOKIES_FILE'):
        youtube._validated_cookie_text(tmp_path / 'missing.txt')


def test_node_is_enabled_for_youtube_challenges(monkeypatch):
    monkeypatch.setattr(youtube.shutil, 'which', lambda name: '/bin/node' if name == 'node' else None)
    options = youtube.youtube_base_options()
    assert options['js_runtimes'] == {'node': {'path': '/bin/node'}}
    assert options['noplaylist'] is True
    assert 'extractor_args' not in options


@pytest.mark.parametrize('message,expected', [
    ("Sign in to confirm you’re not a bot", 'YouTube requires sign-in'),
    ('Could not copy Chrome cookie database', 'Fully close'),
    ('Failed to decrypt with DPAPI', 'Netscape'),
    ('Video unavailable', 'Video unavailable'),
    ('No space left on device', 'No space left on device'),
])
def test_failure_advice_matches_the_actual_cause(message, expected):
    result = youtube.youtube_failure_message([RuntimeError(message)])
    assert expected in result
    assert 'IP ban' not in result


def test_quality_probe_uses_env_cookies_and_same_js_runtime(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('YOUTUBE_COOKIES', COOKIES)
    monkeypatch.setenv('YOUTUBE_COOKIES_FILE', str(tmp_path / 'must-not-be-created.txt'))
    monkeypatch.setattr(sys, 'argv', ['quality_probe.py', '--url', 'https://www.youtube.com/watch?v=test'])
    monkeypatch.setattr(youtube.shutil, 'which', lambda name: '/bin/node' if name == 'node' else None)
    seen = []

    class FakeYDL:
        def __init__(self, options):
            seen.append(options)
            assert options['cookiefile'].read() == COOKIES
            assert options['js_runtimes'] == {'node': {'path': '/bin/node'}}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download):
            assert download is False
            return {'formats': [{'vcodec': 'h264', 'height': 1080}]}

    monkeypatch.setitem(sys.modules, 'yt_dlp', SimpleNamespace(YoutubeDL=FakeYDL))
    assert quality_probe.main() == 0
    assert json.loads(capsys.readouterr().out)['max_height'] == 1080
    assert len(seen) == 1
    assert not (tmp_path / 'must-not-be-created.txt').exists()
