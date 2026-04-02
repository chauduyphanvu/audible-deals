/* Minimal JS helpers for the web UI */

// Copy text to clipboard (used for ASINs, URLs)
function copyToClipboard(text, btn) {
    navigator.clipboard.writeText(text).then(() => {
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = orig; }, 1500);
    });
}

// Confirm destructive actions
function confirmAction(msg) {
    return confirm(msg);
}

// Open URL in new tab
function openExternal(url) {
    window.open(url, '_blank', 'noopener');
}
