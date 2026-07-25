/**
 * Turn a failed API response into a sentence a user can read.
 *
 * FastAPI reports errors as {"detail": "..."} (or a list of validation
 * objects). Several call sites used to put the raw response body straight into
 * the error banner, so users were shown
 * '{"detail":"No words found for this clip range."}' instead of the message.
 */
export async function readApiError(res) {
    let text = '';
    try {
        text = await res.text();
    } catch {
        return `Request failed (${res.status})`;
    }
    try {
        const parsed = JSON.parse(text);
        if (typeof parsed?.detail === 'string') return parsed.detail;
        if (Array.isArray(parsed?.detail)) {
            return parsed.detail.map((entry) => entry?.msg || String(entry)).join(', ');
        }
        if (typeof parsed?.message === 'string') return parsed.message;
    } catch {
        // Not JSON (proxy error page, plain network text) — show it as-is.
    }
    return text.trim() || `Request failed (${res.status})`;
}
