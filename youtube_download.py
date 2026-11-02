"""Shared yt-dlp sessions for the quality probe and the video worker."""
from contextlib import contextmanager
import io
import os
from pathlib import Path
import re
import shutil


class YouTubeDownloadError(RuntimeError):
    """An actionable download failure, safe to show in the job summary."""


def youtube_base_options():
    options = {'cachedir': False, 'noplaylist': True}
    for runtime in ('deno', 'node'):
        path = shutil.which(runtime)
        if path:
            options['js_runtimes'] = {runtime: {'path': path}}
            break
    return options


def youtube_auth_attempts(project_dir=None):
    """Only read a browser when explicitly configured; never combine sessions."""
    project_dir = Path(project_dir or Path(__file__).resolve().parent)
    attempts = []
    browser = os.environ.get('YOUTUBE_COOKIES_BROWSER', '').strip().lower()
    if browser:
        profile = os.environ.get('YOUTUBE_COOKIES_PROFILE', '').strip() or None
        attempts.append(('browser-auth', {
            'cookiesfrombrowser': (browser, profile), 'cookiefile': None,
        }))

    content = os.environ.get('YOUTUBE_COOKIES')
    configured_path = os.environ.get('YOUTUBE_COOKIES_FILE', '').strip()
    if content:
        # Kept in memory. An env override must never overwrite an exported file.
        cookie_source = io.StringIO(content)
    elif configured_path:
        cookie_source = Path(configured_path).expanduser()
        if not cookie_source.is_absolute():
            cookie_source = project_dir / cookie_source
    else:
        cookie_source = next((path for path in (
            project_dir / 'www.youtube.com_cookies.txt',
            project_dir / 'cookies.txt',
            Path('/app/cookies.txt'),
        ) if path.is_file()), None)
    if cookie_source is not None:
        attempts.append(('cookie-auth', {
            'cookiefile': cookie_source, 'cookiesfrombrowser': None,
        }))
    attempts.append(('anonymous-fallback', {'cookiefile': None, 'cookiesfrombrowser': None}))
    return attempts


def _validated_cookie_text(source):
    try:
        content = source.getvalue() if isinstance(source, io.StringIO) else Path(source).read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        raise YouTubeDownloadError(
            'Cannot read the YouTube cookie file. Check YOUTUBE_COOKIES_FILE or export fresh cookies.'
        ) from None
    content = content.lstrip('\ufeff')
    lines = content.splitlines()
    valid = bool(lines) and lines[0] in ('# Netscape HTTP Cookie File', '# HTTP Cookie File')
    for line in lines:
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        elif not line.strip() or line.startswith('#'):
            continue
        fields = line.split('\t')
        if (len(fields) != 7
                or fields[1] not in ('TRUE', 'FALSE')
                or fields[3] not in ('TRUE', 'FALSE')
                or (fields[4] and not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', fields[4]))
                or (fields[1] == 'TRUE') != fields[0].startswith('.')):
            valid = False
            break
        if fields[4]:
            try:
                # cookiejar performs this conversion and can otherwise log the
                # entire private cookie row on overflow.
                int(float(fields[4]))
            except (ValueError, OverflowError):
                valid = False
                break
    if not valid:
        # yt-dlp otherwise logs malformed cookie rows, including their values.
        raise YouTubeDownloadError('Invalid YouTube cookie file. Export cookies in Netscape cookies.txt format.')
    # StringIO does not perform file text-mode universal newline conversion.
    # Without normalization CRLF env content leaves a CR in each cookie value.
    return '\n'.join(lines) + '\n'


@contextmanager
def open_youtube_dl(options, factory):
    """Give each yt-dlp invocation its own writable in-memory cookie snapshot."""
    options = dict(options)
    source = options.get('cookiefile')
    cookies = None
    try:
        if source is not None:
            cookies = io.StringIO(_validated_cookie_text(source))
            options['cookiefile'] = cookies
        with factory(options) as ydl:
            yield ydl
    finally:
        if cookies is not None:
            cookies.close()


_LOCKED_BROWSER_ADVICE = (
    'The browser cookie database is locked. Fully close the configured browser '
    'and retry, or export YouTube cookies to a Netscape cookies.txt file.'
)
_DECRYPT_ADVICE = (
    'yt-dlp could not decrypt the browser cookies. Export YouTube cookies '
    'to a Netscape cookies.txt file and set YOUTUBE_COOKIES_FILE to that file.'
)


def youtube_failure_message(errors):
    messages = [str(error) for error in errors]
    lowered = '\n'.join(messages).lower().replace('’', "'")
    # The browser session often fails first and the anonymous fallback then
    # hits the bot check; the browser cause is the one the user can fix.
    if 'could not copy chrome cookie database' in lowered:
        browser_advice = _LOCKED_BROWSER_ADVICE
    elif 'decrypt' in lowered or 'dpapi' in lowered:
        browser_advice = _DECRYPT_ADVICE
    else:
        browser_advice = ''
    if "sign in to confirm you're not a bot" in lowered:
        sign_in = (
            'YouTube requires sign-in or a bot check and rejected the available sessions. '
            'Open the video in your browser and complete any check, then refresh your '
            'YouTube cookies or set YOUTUBE_COOKIES_BROWSER to your signed-in browser '
            '(e.g. brave). You can also upload the video file directly.'
        )
        return f'{browser_advice} {sign_in}' if browser_advice else sign_in
    if browser_advice:
        # Another attempt may have failed for its own reason (private or
        # removed video); keep that error next to the browser advice.
        others = [
            message for message in messages
            if not any(key in message.lower() for key in ('cookie database', 'decrypt', 'dpapi'))
        ]
        return f'{browser_advice} Other attempt: {others[-1]}' if others else browser_advice
    return messages[-1] if messages else 'YouTube returned no downloadable video formats.'
