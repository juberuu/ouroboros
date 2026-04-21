/**
 * Utility functions shared across modules.
 */

export function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

export function renderMarkdown(text) {
    let html = escapeHtml(text);
    // Code blocks (``` ... ```)
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');
    // Bold
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Italic
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // Strikethrough
    html = html.replace(/~~(.+?)~~/g, '<del>$1</del>');
    // Headers (order matters: ### before ## before #)
    html = html.replace(/^### (.+)$/gm, '<strong class="md-h3">$1</strong>');
    html = html.replace(/^## (.+)$/gm, '<strong class="md-h2">$1</strong>');
    html = html.replace(/^# (.+)$/gm, '<strong class="md-h1">$1</strong>');
    // Unordered lists
    html = html.replace(/^- (.+)$/gm, '<span class="md-li">\u2022 $1</span>');
    // Links
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function(_, text, url) {
        const safe = /^https?:|^mailto:/i.test(url) ? url : '#';
        return '<a href="' + safe + '" target="_blank" rel="noopener noreferrer" class="md-link">' + text + '</a>';
    });
    // Tables: detect header row + separator + data rows
    html = html.replace(/((?:^\|.+\|$\n?)+)/gm, function(block) {
        const rows = block.trim().split('\n').filter(r => r.trim());
        if (rows.length < 2) return block;
        const isSep = r => /^\|[\s\-:|]+\|$/.test(r.trim());
        let headIdx = -1;
        for (let i = 0; i < rows.length; i++) { if (isSep(rows[i])) { headIdx = i; break; } }
        if (headIdx < 1) return block;
        const parseRow = (r, tag) => '<tr>' + r.trim().replace(/^\||\|$/g, '').split('|').map(c => `<${tag}>${c.trim()}</${tag}>`).join('') + '</tr>';
        let t = '<table class="md-table">';
        for (let i = 0; i < headIdx; i++) t += '<thead>' + parseRow(rows[i], 'th') + '</thead>';
        t += '<tbody>';
        for (let i = headIdx + 1; i < rows.length; i++) t += parseRow(rows[i], 'td');
        t += '</tbody></table>';
        return '<div class="md-table-wrap">' + t + '</div>';
    });
    return html;
}

export function extractVersions(data) {
    const runtimeVersion = data?.runtime_version || data?.version || '?';
    const appVersion = data?.app_version || runtimeVersion;
    return { appVersion, runtimeVersion };
}

export function formatDualVersion(data) {
    const { appVersion, runtimeVersion } = extractVersions(data);
    return `app ${appVersion} | rt ${runtimeVersion}`;
}

export async function loadVersion() {
    try {
        const resp = await fetch('/api/health');
        const data = await resp.json();
        const { runtimeVersion } = extractVersions(data);
        const navVer = document.getElementById('nav-version');
        if (navVer) navVer.textContent = `v${runtimeVersion}`;
    } catch {}
}

export function initMatrixRain() {
    const canvas = document.createElement('canvas');
    canvas.id = 'matrix-rain';
    document.getElementById('app').prepend(canvas);

    const ctx = canvas.getContext('2d');
    const chars = '\u30A2\u30A4\u30A6\u30A8\u30AA\u30AB\u30AD\u30AF\u30B1\u30B3\u30B5\u30B7\u30B9\u30BB\u30BD\u30BF\u30C1\u30C4\u30C6\u30C8\u30CA\u30CB\u30CC\u30CD\u30CE\u30CF\u30D2\u30D5\u30D8\u30DB\u30DE\u30DF\u30E0\u30E1\u30E2\u30E4\u30E6\u30E8\u30E9\u30EA\u30EB\u30EC\u30ED\u30EF\u30F2\u30F3ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789\u03A8\u03A9\u03A6\u0394\u039B\u039E\u03A3\u0398\u0430\u0431\u0432\u0433\u0434\u0435\u0436\u0437\u0438\u043A\u043B\u043C\u043D\u043E\u043F\u0440\u0441\u0442\u0443\u0444\u0445\u0446\u0447\u0448\u0449\u044D\u044E\u044F'.split('');
    const fontSize = 14;
    let columns = [];
    let w = 0, h = 0;

    function resize() {
        w = canvas.width = window.innerWidth;
        h = canvas.height = window.innerHeight;
        const colCount = Math.floor(w / fontSize);
        while (columns.length < colCount) columns.push(Math.random() * h / fontSize | 0);
        columns.length = colCount;
    }
    resize();
    window.addEventListener('resize', resize);

    function draw() {
        ctx.fillStyle = 'rgba(13, 11, 15, 0.06)';
        ctx.fillRect(0, 0, w, h);
        ctx.fillStyle = '#ee3344';
        ctx.font = fontSize + 'px monospace';

        for (let i = 0; i < columns.length; i++) {
            const ch = chars[Math.random() * chars.length | 0];
            ctx.fillText(ch, i * fontSize, columns[i] * fontSize);
            if (columns[i] * fontSize > h && Math.random() > 0.975) {
                columns[i] = 0;
            }
            columns[i]++;
        }
    }

    setInterval(draw, 66);
}
