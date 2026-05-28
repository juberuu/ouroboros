import { jsonPost } from './api_client.js';
/** MCP settings cards; preserves masked auth tokens until the user edits them. */
import { escapeHtmlAttr as escapeHtml } from './utils.js';

const TRANSPORTS = [
    { value: 'streamable_http', label: 'Streamable HTTP' },
    { value: 'sse', label: 'SSE (Server-Sent Events)' },
];

let mcpServers = [];
let mcpStatusByServer = {};
let mcpStatusEnvelope = null;
let mcpDirtyTokens = new Set();
let onChangeCallback = null;

function looksMasked(value) {
    const text = String(value ?? '').trim();
    if (!text) return false;
    return text === '***' || text.endsWith('...');
}

function emptyServer() {
    return {
        id: '',
        name: '',
        enabled: false,
        transport: 'streamable_http',
        url: '',
        auth_header: 'Authorization',
        auth_token: '',
        allowed_tools: [],
    };
}

function notifyChanged() {
    if (typeof onChangeCallback === 'function') {
        try { onChangeCallback(); } catch (err) { /* swallow */ }
    }
}

function toolCountLabel(count) {
    return `${count} tool${count === 1 ? '' : 's'}`;
}

function renderServerCard(server, index) {
    const id = String(server.id ?? '');
    const name = String(server.name ?? '');
    const transport = String(server.transport ?? 'streamable_http');
    const url = String(server.url ?? '');
    const authHeader = String(server.auth_header ?? 'Authorization');
    const authToken = String(server.auth_token ?? '');
    const enabled = server.enabled === true || server.enabled === 'True' || server.enabled === 'true';
    const allowedTools = Array.isArray(server.allowed_tools) ? server.allowed_tools.join(', ') : '';
    const transportOptions = TRANSPORTS.map((opt) => {
        const selected = opt.value === transport ? ' selected' : '';
        return `<option value="${escapeHtml(opt.value)}"${selected}>${escapeHtml(opt.label)}</option>`;
    }).join('');
    const status = mcpStatusByServer[id] || null;
    const toolCount = status ? Number(status.tool_count || 0) : 0;
    const lastError = status ? String(status.last_error || '') : '';
    const lastRefreshed = status ? String(status.last_refreshed || '') : '';
    const tools = status && Array.isArray(status.tools) ? status.tools : [];

    let statusBadgeText = 'Not refreshed yet';
    let statusClass = 'mcp-server-status-muted';
    if (lastError) {
        statusBadgeText = `Error: ${lastError}`;
        statusClass = 'mcp-server-status-danger';
    } else if (toolCount > 0) {
        statusBadgeText = `${toolCount} tool${toolCount === 1 ? '' : 's'} discovered`;
        statusClass = 'mcp-server-status-ok';
    } else if (lastRefreshed) {
        statusBadgeText = '0 tools discovered';
        statusClass = 'mcp-server-status-warn';
    }

    const toolsHtml = tools.length
        ? `<ul class="mcp-tools-list">${tools.map((t) => `
                <li>
                    <strong>${escapeHtml(t.name || t.prefixed_name || '')}</strong>
                    ${t.description ? `<span class="mcp-tool-desc">${escapeHtml(String(t.description).slice(0, 220))}</span>` : ''}
                </li>
            `).join('')}</ul>`
        : '';

    const authPlaceholder = authToken && looksMasked(authToken)
        ? authToken
        : (authToken ? '••••••' : 'Bearer xxxxx (optional)');

    return `
        <article class="mcp-server-card" data-mcp-card data-mcp-index="${index}">
            <header class="mcp-server-card-head">
                <div class="mcp-server-card-title">
                    <strong>${escapeHtml(name || id || `MCP Server ${index + 1}`)}</strong>
                    <span class="mcp-server-status ${statusClass}">${escapeHtml(statusBadgeText)}</span>
                </div>
                <div class="mcp-server-card-actions">
                    <label class="mcp-server-enabled">
                        <input type="checkbox" data-mcp-field="enabled" ${enabled ? 'checked' : ''}>
                        <span>Enabled</span>
                    </label>
                    <button type="button" class="settings-ghost-btn" data-mcp-test>Test</button>
                    <button type="button" class="settings-ghost-btn" data-mcp-refresh>Refresh tools</button>
                    <button type="button" class="settings-ghost-btn mcp-server-remove" data-mcp-remove>Remove</button>
                </div>
            </header>
            <div class="form-grid two">
                <div class="form-field">
                    <label>Server ID</label>
                    <input type="text" data-mcp-field="id" value="${escapeHtml(id)}" placeholder="github" autocomplete="off" spellcheck="false">
                </div>
                <div class="form-field">
                    <label>Display name</label>
                    <input type="text" data-mcp-field="name" value="${escapeHtml(name)}" placeholder="GitHub MCP" autocomplete="off" spellcheck="false">
                </div>
            </div>
            <div class="form-grid two">
                <div class="form-field">
                    <label>Transport</label>
                    <select data-mcp-field="transport">${transportOptions}</select>
                </div>
                <div class="form-field">
                    <label>Server URL</label>
                    <input type="text" data-mcp-field="url" value="${escapeHtml(url)}" placeholder="https://example.com/mcp" autocomplete="off" spellcheck="false">
                </div>
            </div>
            <div class="form-grid two">
                <div class="form-field">
                    <label>Auth header</label>
                    <input type="text" data-mcp-field="auth_header" value="${escapeHtml(authHeader)}" placeholder="Authorization" autocomplete="off" spellcheck="false">
                </div>
                <div class="form-field">
                    <label>Auth token (optional)</label>
                    <div class="secret-input-row">
                        <input type="password" data-mcp-field="auth_token" value="${escapeHtml(authToken)}" placeholder="${escapeHtml(authPlaceholder)}" autocomplete="off" spellcheck="false">
                        <button type="button" class="settings-ghost-btn" data-mcp-token-toggle>Show</button>
                        <button type="button" class="settings-ghost-btn" data-mcp-token-clear>Clear</button>
                    </div>
                </div>
            </div>
            <div class="form-row">
                <div class="form-field">
                    <label>Allowed tools (optional, comma-separated)</label>
                    <input type="text" data-mcp-field="allowed_tools" value="${escapeHtml(allowedTools)}" placeholder="search, read_repo" autocomplete="off" spellcheck="false">
                </div>
            </div>
            <div class="settings-inline-status mcp-server-message" data-mcp-message hidden></div>
            ${toolsHtml ? `<details class="mcp-tools-disclosure"><summary>Discovered tools</summary>${toolsHtml}</details>` : ''}
        </article>
    `;
}

function bindCardEvents(card) {
    const idx = Number(card.dataset.mcpIndex || 0);
    const setMessage = (text, tone = 'muted') => {
        const el = card.querySelector('[data-mcp-message]');
        if (!el) return;
        if (!text) {
            el.hidden = true;
            el.textContent = '';
            return;
        }
        el.textContent = text;
        el.dataset.tone = tone;
        el.hidden = false;
    };

    card.querySelectorAll('[data-mcp-field]').forEach((input) => {
        input.addEventListener('input', () => {
            const field = input.dataset.mcpField;
            const server = mcpServers[idx];
            if (!server) return;
            if (field === 'enabled') {
                server.enabled = Boolean(input.checked);
            } else if (field === 'allowed_tools') {
                server.allowed_tools = String(input.value || '')
                    .split(',')
                    .map((s) => s.trim())
                    .filter(Boolean);
            } else if (field === 'auth_token') {
                if (looksMasked(input.value)) {
                    mcpDirtyTokens.delete(`${idx}`);
                } else {
                    mcpDirtyTokens.add(`${idx}`);
                }
                server.auth_token = input.value;
            } else {
                server[field] = input.value;
            }
            notifyChanged();
        });
        input.addEventListener('change', () => {
            const field = input.dataset.mcpField;
            const server = mcpServers[idx];
            if (!server) return;
            if (field === 'enabled') {
                server.enabled = Boolean(input.checked);
                notifyChanged();
            }
        });
    });

    const tokenInput = card.querySelector('[data-mcp-field="auth_token"]');
    const tokenToggle = card.querySelector('[data-mcp-token-toggle]');
    const tokenClear = card.querySelector('[data-mcp-token-clear]');
    if (tokenToggle && tokenInput) {
        tokenToggle.addEventListener('click', () => {
            if (tokenInput.type === 'password') {
                tokenInput.type = 'text';
                tokenToggle.textContent = 'Hide';
            } else {
                tokenInput.type = 'password';
                tokenToggle.textContent = 'Show';
            }
        });
    }
    if (tokenClear && tokenInput) {
        tokenClear.addEventListener('click', () => {
            tokenInput.value = '';
            tokenInput.type = 'password';
            const server = mcpServers[idx];
            if (server) server.auth_token = '';
            mcpDirtyTokens.add(`${idx}`);
            notifyChanged();
        });
    }

    const removeBtn = card.querySelector('[data-mcp-remove]');
    if (removeBtn) {
        removeBtn.addEventListener('click', () => {
            mcpServers.splice(idx, 1);
            mcpDirtyTokens.delete(`${idx}`);
            renderAll();
            notifyChanged();
        });
    }

    const testBtn = card.querySelector('[data-mcp-test]');
    if (testBtn) {
        testBtn.addEventListener('click', async () => {
            const server = mcpServers[idx];
            if (!server) return;
            testBtn.disabled = true;
            setMessage('Testing connection...', 'muted');
            try {
                // Masked token + server_id lets Test use saved auth with edited URL/transport.
                const sid = String(server.id || '').trim();
                const tokenMasked = looksMasked(server.auth_token);
                const body = sid && tokenMasked
                    ? { server_id: sid, server: { ...server } }
                    : { server: serverForTest(server) };
                const data = await jsonPost('/api/mcp/test', body, { rejectOkFalse: true });
                setMessage(`Test OK — ${toolCountLabel(Number(data.tool_count || 0))} reported.`, 'ok');
            } catch (err) {
                setMessage(`Test failed: ${err && err.message ? err.message : err}`, 'danger');
            } finally {
                testBtn.disabled = false;
            }
        });
    }

    const refreshBtn = card.querySelector('[data-mcp-refresh]');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', async () => {
            const server = mcpServers[idx];
            if (!server) return;
            const sid = String(server.id || '').trim();
            if (!sid) {
                setMessage('Save the server (with an ID) before refreshing.', 'warn');
                return;
            }
            refreshBtn.disabled = true;
            setMessage('Refreshing tools...', 'muted');
            try {
                const data = await jsonPost('/api/mcp/refresh', { server_id: sid }, { rejectOkFalse: true });
                setMessage(`Refreshed — ${Number(data.tool_count || 0)} tools discovered.`, 'ok');
                await refreshStatus();
            } catch (err) {
                setMessage(`Refresh failed: ${err && err.message ? err.message : err}`, 'danger');
            } finally {
                refreshBtn.disabled = false;
            }
        });
    }
}

function serverForTest(server) {
    const out = { ...server };
    if (looksMasked(out.auth_token)) {
        // Drop literal masks so inline tests never send "***" as Bearer auth.
        out.auth_token = '';
    }
    return out;
}

function renderAll() {
    const host = document.getElementById('mcp-servers-list');
    if (!host) return;
    if (!mcpServers.length) {
        host.innerHTML = '<div class="muted">No MCP servers configured. Click "Add Server" to start.</div>';
        return;
    }
    host.innerHTML = mcpServers.map((s, idx) => renderServerCard(s, idx)).join('');
    host.querySelectorAll('[data-mcp-card]').forEach((card) => bindCardEvents(card));
}

function renderEnvelopeStatus() {
    const el = document.getElementById('mcp-global-status');
    if (!el) return;
    if (!mcpStatusEnvelope) {
        el.textContent = '';
        el.dataset.tone = 'muted';
        return;
    }
    if (!mcpStatusEnvelope.sdk_available) {
        el.textContent = `MCP SDK not installed: ${mcpStatusEnvelope.sdk_error || 'install `mcp>=1.6` to enable MCP integration.'}`;
        el.dataset.tone = 'warn';
        return;
    }
    if (!mcpStatusEnvelope.enabled) {
        el.textContent = 'MCP client is disabled. Enable it above to allow tool discovery.';
        el.dataset.tone = 'muted';
        return;
    }
    const total = Array.isArray(mcpStatusEnvelope.servers) ? mcpStatusEnvelope.servers.length : 0;
    const totalTools = Array.isArray(mcpStatusEnvelope.servers)
        ? mcpStatusEnvelope.servers.reduce((sum, s) => sum + Number(s.tool_count || 0), 0)
        : 0;
    el.textContent = `${total} server${total === 1 ? '' : 's'} configured, ${totalTools} tool${totalTools === 1 ? '' : 's'} discovered.`;
    el.dataset.tone = 'ok';
}

async function refreshStatus() {
    try {
        const resp = await apiFetch('/api/mcp/status', { cache: 'no-store' });
        if (!resp.ok) return;
        const data = await resp.json();
        mcpStatusEnvelope = data;
        const map = {};
        for (const entry of data.servers || []) {
            if (entry && entry.id) map[entry.id] = entry;
        }
        mcpStatusByServer = map;
        renderEnvelopeStatus();
        renderAll();
    } catch (err) {
    }
}

function bindAddButton() {
    const btn = document.getElementById('btn-mcp-add-server');
    if (!btn) return;
    btn.addEventListener('click', () => {
        mcpServers.push(emptyServer());
        renderAll();
        notifyChanged();
    });
}

function bindRefreshAllButton() {
    const btn = document.getElementById('btn-mcp-refresh-all');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        btn.disabled = true;
        const wasText = btn.textContent;
        btn.textContent = 'Refreshing...';
        try {
            await jsonPost('/api/mcp/refresh', {}, { rejectOkFalse: true });
            await refreshStatus();
        } finally {
            btn.disabled = false;
            btn.textContent = wasText;
        }
    });
}

export function initMcpSettings({ onChange } = {}) {
    onChangeCallback = typeof onChange === 'function' ? onChange : null;
    bindAddButton();
    bindRefreshAllButton();
    const enabled = document.getElementById('s-mcp-enabled');
    if (enabled) enabled.addEventListener('change', notifyChanged);
    const timeout = document.getElementById('s-mcp-tool-timeout');
    if (timeout) timeout.addEventListener('input', notifyChanged);
}

export function applyMcpSettings(settings) {
    const enabledCheckbox = document.getElementById('s-mcp-enabled');
    if (enabledCheckbox) {
        enabledCheckbox.checked = settings.MCP_ENABLED === true || settings.MCP_ENABLED === 'True';
    }
    const timeoutInput = document.getElementById('s-mcp-tool-timeout');
    if (timeoutInput) {
        const value = Number(settings.MCP_TOOL_TIMEOUT_SEC || 60);
        timeoutInput.value = String(Number.isFinite(value) && value > 0 ? value : 60);
    }
    const incoming = Array.isArray(settings.MCP_SERVERS) ? settings.MCP_SERVERS : [];
    mcpServers = incoming.map((s) => ({
        id: String(s.id ?? ''),
        name: String(s.name ?? ''),
        enabled: Boolean(s.enabled),
        transport: String(s.transport ?? 'streamable_http'),
        url: String(s.url ?? ''),
        auth_header: String(s.auth_header ?? 'Authorization'),
        auth_token: String(s.auth_token ?? ''),
        allowed_tools: Array.isArray(s.allowed_tools) ? s.allowed_tools.map(String) : [],
    }));
    mcpDirtyTokens = new Set();
    renderAll();
    refreshStatus();
}

export function collectMcpSettings() {
    const enabledCheckbox = document.getElementById('s-mcp-enabled');
    const timeoutInput = document.getElementById('s-mcp-tool-timeout');
    const enabled = enabledCheckbox ? enabledCheckbox.checked : false;
    const timeoutRaw = timeoutInput ? Number(timeoutInput.value) : 60;
    const timeout = Number.isFinite(timeoutRaw) && timeoutRaw > 0 ? Math.floor(timeoutRaw) : 60;
    const out = {
        MCP_ENABLED: Boolean(enabled),
        MCP_TOOL_TIMEOUT_SEC: timeout,
        MCP_SERVERS: mcpServers.map((s) => ({
            id: String(s.id || '').trim(),
            name: String(s.name || '').trim(),
            enabled: Boolean(s.enabled),
            transport: String(s.transport || 'streamable_http'),
            url: String(s.url || '').trim(),
            auth_header: String(s.auth_header || 'Authorization').trim() || 'Authorization',
            auth_token: String(s.auth_token || ''),
            allowed_tools: Array.isArray(s.allowed_tools) ? s.allowed_tools.map(String) : [],
        })),
    };
    return out;
}
