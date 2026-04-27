/**
 * Ouroboros ClawHub Marketplace UI (v4.50).
 *
 * Renders inside the ``Marketplace`` sub-tab of the Skills page.
 * Talks to ``/api/marketplace/clawhub/*`` and the existing
 * ``/api/skills/<name>/{toggle,review}`` endpoints. Uses the same
 * design-system primitives (``.btn``, ``.skills-badge``, ``.muted``,
 * ``.field-note``) as the rest of the app so dark/light theme parity
 * is automatic.
 */

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}


/**
 * Render an untrusted Markdown string from the registry as sanitised HTML.
 *
 * The vendored ``marked`` (parser) + ``DOMPurify`` (sanitiser) globals are
 * loaded at page boot from ``index.html``. We pass marked's output through
 * DOMPurify with a conservative allowlist that bans every script-bearing
 * tag and any ``javascript:`` / ``data:`` URLs in attributes.
 *
 * If either library is missing (e.g. the operator runs against an older
 * cached ``index.html``), we fall back to a plain ``<pre><code>`` block —
 * still safe because the HTML is escaped, just not styled as Markdown.
 */
function renderMarkdownSafe(rawMd) {
    const text = String(rawMd ?? '');
    if (!text) return '<div class="muted"><i>empty</i></div>';
    if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
        return `<pre class="marketplace-skillmd"><code>${escapeHtml(text)}</code></pre>`;
    }
    try {
        // ``async: false`` forces a synchronous string return.
        // ``mangle`` and ``headerIds`` were removed in marked@5.0.0
        // (we vendor v12.0.2), so passing them here is a no-op; the
        // explicit options below are the actually-honored set.
        const rendered = marked.parse(text, {
            async: false,
            gfm: true,
            breaks: false,
        });
        // ``img`` is forbidden so a malicious publisher cannot ship a
        // SKILL.md with ``<img src="https://attacker.com/track.png">``
        // and use the marketplace preview modal as a tracking-pixel
        // beacon. The preview is meant to be a *static* description of
        // the skill — anything that reaches out to a remote host
        // breaks that contract. ``style`` is refused for the same
        // reason. ``href`` survives but DOMPurify's default URI regex
        // restricts it to safe schemes.
        //
        // NOTE: on-event attributes (onclick, onerror, etc.) are
        // already blocked by DOMPurify's default ALLOWED_ATTR
        // allowlist, NOT by an explicit denylist (the ``'on*'`` token
        // was misleading dead code — DOMPurify v3.1.0 does exact-match
        // attribute lookups, not glob expansion). If a future
        // contributor adds an attribute via ``ADD_ATTR``, they must
        // re-verify the on-handler protection still holds — DOMPurify
        // does NOT reapply the on-handler rule to attributes that
        // ADD_ATTR explicitly admits.
        return DOMPurify.sanitize(rendered, {
            USE_PROFILES: { html: true },
            FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'input', 'img', 'video', 'audio', 'source'],
            FORBID_ATTR: ['style', 'srcset', 'srcdoc'],
        });
    } catch (err) {
        console.warn('marketplace: markdown render failed', err);
        return `<pre class="marketplace-skillmd"><code>${escapeHtml(text)}</code></pre>`;
    }
}


/**
 * Validate that an untrusted URL string uses a safe scheme before
 * rendering it as an `<a href="...">` target. Registry-supplied
 * homepage / website fields can carry `javascript:` / `data:` /
 * `vbscript:` payloads that escapeHtml does NOT neutralise — the
 * browser decodes the entity escapes inside `href` BEFORE scheme
 * parsing, so a `javascript:fetch(...)` payload still executes on
 * click. Returns the (escaped) URL when the scheme is http/https,
 * empty string otherwise.
 */
function safeExternalUrl(value) {
    const text = String(value ?? '').trim();
    if (!text) return '';
    try {
        const parsed = new URL(text);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
            return escapeHtml(parsed.toString());
        }
    } catch {
        // Not a parseable absolute URL — refuse rather than guessing
        // (a relative path in homepage doesn't make sense anyway).
    }
    return '';
}


function formatNumber(value) {
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) return '—';
    if (num >= 1_000_000) return (num / 1_000_000).toFixed(1) + 'M';
    if (num >= 1_000) return (num / 1_000).toFixed(1) + 'k';
    return String(num);
}


function paneTemplate() {
    return `
        <div class="marketplace-shell">
            <div class="marketplace-controls">
                <input type="search" id="mp-query" class="marketplace-search"
                       placeholder="Search ClawHub skills by name or summary…" autocomplete="off">
                <label class="marketplace-checkbox">
                    <input type="checkbox" id="mp-only-official"> Official only
                </label>
                <button class="btn btn-primary" data-mp-search>Search</button>
            </div>
            <div id="mp-status" class="muted marketplace-status"></div>
            <div id="mp-results" class="marketplace-results"></div>
            <div id="mp-pagination" class="marketplace-pagination" hidden></div>
        </div>
        <div id="mp-modal-host"></div>
    `;
}


function statusBadgeForReview(status) {
    const tone = status === 'pass' ? 'ok'
        : status === 'fail' ? 'danger'
        : status === 'advisory' ? 'warn'
        : 'muted';
    return `<span class="skills-badge skills-badge-${tone}">${escapeHtml(status || 'pending')}</span>`;
}


function summaryCard(summary, installedMap, isPlugin) {
    const slug = summary.slug;
    const installed = installedMap.get(slug);
    const installedAtVersion = installed?.provenance?.version || installed?.version || '';
    const isInstalled = !!installed;
    const updateAvailable = isInstalled
        && summary.latest_version
        && installedAtVersion
        && summary.latest_version !== installedAtVersion;
    const downloads = formatNumber(summary.stats?.downloads);
    const stars = formatNumber(summary.stats?.stars);
    const license = summary.license || 'no-license';
    const homepageHref = safeExternalUrl(summary.homepage);
    const description = summary.summary || summary.description || '';
    const installedBadge = isInstalled
        ? `<span class="skills-badge skills-badge-ok">installed v${escapeHtml(installedAtVersion || summary.latest_version)}</span>`
        : '';
    const updateBadge = updateAvailable
        ? `<span class="skills-badge skills-badge-warn">update v${escapeHtml(summary.latest_version)}</span>`
        : '';
    const officialBadge = summary.badges?.official
        ? '<span class="skills-badge skills-badge-ok">official</span>'
        : '';
    const pluginBadge = isPlugin
        ? '<span class="skills-badge skills-badge-danger">plugin (not installable)</span>'
        : '';
    const reviewBadge = isInstalled
        ? statusBadgeForReview(installed.review_status)
        : '';
    const buttons = isPlugin
        ? `<button class="btn btn-default" disabled title="Plugins are not installable">Plugin</button>`
        : isInstalled
            ? `
                <button class="btn btn-default" data-mp-preview="${escapeHtml(slug)}">Details</button>
                <button class="btn btn-default" data-mp-update="${escapeHtml(slug)}">Update</button>
                <button class="btn btn-default" data-mp-uninstall="${escapeHtml(slug)}" data-name="${escapeHtml(installed.name || '')}">Uninstall</button>
            `
            : `
                <button class="btn btn-default" data-mp-preview="${escapeHtml(slug)}">Details</button>
                <button class="btn btn-default btn-primary" data-mp-install="${escapeHtml(slug)}">Install</button>
            `;
    return `
        <div class="marketplace-card" data-slug="${escapeHtml(slug)}">
            <div class="marketplace-card-head">
                <div class="marketplace-card-title">
                    <strong>${escapeHtml(summary.display_name || slug)}</strong>
                    <span class="muted">${escapeHtml(slug)} · v${escapeHtml(summary.latest_version || '—')}</span>
                </div>
                <div class="marketplace-card-badges">
                    ${officialBadge}
                    ${pluginBadge}
                    ${installedBadge}
                    ${updateBadge}
                    ${reviewBadge}
                </div>
            </div>
            <div class="marketplace-card-body">${escapeHtml(description)}</div>
            <div class="marketplace-card-meta muted">
                <span>downloads: ${downloads}</span>
                <span>stars: ${stars}</span>
                <span>license: ${escapeHtml(license)}</span>
                ${homepageHref ? `<a href="${homepageHref}" target="_blank" rel="noopener noreferrer">homepage</a>` : ''}
                ${(summary.os || []).length ? `<span>os: ${(summary.os || []).map((o) => escapeHtml(o)).join(', ')}</span>` : ''}
            </div>
            <div class="marketplace-card-actions">${buttons}</div>
        </div>
    `;
}


function renderResults(host, summaries, installedMap, registryCount, diagnostics) {
    if (!summaries.length) {
        if (registryCount > 0) {
            host.innerHTML = '<div class="muted">Registry returned skills, but none are installable in this view.</div>';
        } else {
            const path = diagnostics?.registryPath || 'skills';
            const attempts = Array.isArray(diagnostics?.attempts) && diagnostics.attempts.length
                ? `<details class="marketplace-debug"><summary>Registry diagnostics</summary><pre>${escapeHtml(JSON.stringify(diagnostics.attempts, null, 2))}</pre></details>`
                : '';
            host.innerHTML = `
                <div class="muted">
                    ClawHub returned zero installable skills from <code>${escapeHtml(path)}</code>.
                    Browse uses <code>packages?family=skill</code>; text search uses
                    <code>packages/search?family=skill</code>.
                </div>
                ${attempts}
            `;
        }
        return;
    }
    host.innerHTML = summaries
        .map((s) => summaryCard(s, installedMap, !!s.is_plugin))
        .join('');
}


function renderPagination(host, { offset, limit, count, cursor, nextCursor }) {
    if (!nextCursor && count < limit && offset === 0) {
        host.hidden = true;
        host.innerHTML = '';
        return;
    }
    host.hidden = false;
    const nextDisabled = nextCursor ? '' : (count < limit ? 'disabled' : '');
    host.innerHTML = `
        <button class="btn btn-default" data-mp-prev ${offset <= 0 && !cursor ? 'disabled' : ''}>Prev</button>
        <span class="muted">${cursor ? 'cursor page' : `offset ${offset}`} · ${count} shown</span>
        <button class="btn btn-default" data-mp-next ${nextDisabled}>Next</button>
    `;
}


function showStatus(host, message, tone) {
    const el = document.getElementById('mp-status');
    if (!el) return;
    el.dataset.tone = tone || '';
    el.textContent = message || '';
}


// ---------------------------------------------------------------------------
// Network helpers
// ---------------------------------------------------------------------------


async function fetchJson(url, init) {
    const resp = await fetch(url, init);
    let body = null;
    try {
        body = await resp.json();
    } catch (err) {
        body = { error: `non-json response (HTTP ${resp.status})` };
    }
    if (!resp.ok) {
        const err = new Error(body?.error || `HTTP ${resp.status}`);
        err.status = resp.status;
        err.body = body;
        throw err;
    }
    return body;
}


async function loadInstalled() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    try {
        const data = await fetchJson('/api/marketplace/clawhub/installed', {
            signal: controller.signal,
        });
        const map = new Map();
        for (const skill of data.skills || []) {
            const provSlug = skill.provenance?.slug;
            if (provSlug) map.set(provSlug, skill);
        }
        return map;
    } catch (err) {
        if (err?.name !== 'AbortError') {
            console.warn('marketplace: installed lookup failed', err);
        }
        return new Map();
    } finally {
        clearTimeout(timer);
    }
}


async function runSearch(state) {
    const params = new URLSearchParams();
    if (state.query) params.set('q', state.query);
    params.set('limit', String(state.limit));
    params.set('offset', String(state.offset));
    if (state.cursor) params.set('cursor', state.cursor);
    if (state.onlyOfficial) params.set('official', '1');
    return fetchJson(`/api/marketplace/clawhub/search?${params.toString()}`);
}


// ---------------------------------------------------------------------------
// Detail modal
// ---------------------------------------------------------------------------


function modalTemplate(title) {
    return `
        <div class="marketplace-modal-backdrop" data-mp-modal>
            <div class="marketplace-modal">
                <div class="marketplace-modal-head">
                    <strong>${escapeHtml(title)}</strong>
                    <button class="btn btn-default" data-mp-modal-close>Close</button>
                </div>
                <div class="marketplace-modal-body" data-mp-modal-body>
                    <div class="muted">Loading…</div>
                </div>
                <div class="marketplace-modal-actions" data-mp-modal-actions></div>
            </div>
        </div>
    `;
}


function renderManifestTable(translated) {
    if (!translated || typeof translated !== 'object') return '';
    const rows = Object.entries(translated)
        .filter(([, v]) => v !== '' && v !== null && (Array.isArray(v) ? v.length : true))
        .map(([k, v]) => {
            let value;
            if (Array.isArray(v)) {
                value = v.length
                    ? `<ul>${v.map((item) => `<li><code>${escapeHtml(typeof item === 'string' ? item : JSON.stringify(item))}</code></li>`).join('')}</ul>`
                    : '<i>—</i>';
            } else if (typeof v === 'object') {
                value = `<code>${escapeHtml(JSON.stringify(v))}</code>`;
            } else {
                value = `<code>${escapeHtml(String(v))}</code>`;
            }
            return `<tr><th>${escapeHtml(k)}</th><td>${value}</td></tr>`;
        });
    return `<table class="marketplace-manifest-table">${rows.join('')}</table>`;
}


function renderAdapterNotes(adapter) {
    if (!adapter) return '';
    const blockers = (adapter.blockers || [])
        .map((msg) => `<li>${escapeHtml(msg)}</li>`)
        .join('');
    const warnings = (adapter.warnings || [])
        .map((msg) => `<li>${escapeHtml(msg)}</li>`)
        .join('');
    const blockHtml = blockers
        ? `<div class="marketplace-block-list"><h4>Blockers</h4><ul>${blockers}</ul></div>`
        : '';
    const warnHtml = warnings
        ? `<div class="marketplace-warn-list"><h4>Warnings</h4><ul>${warnings}</ul></div>`
        : '';
    return blockHtml + warnHtml;
}


async function openDetailModal(host, slug, options) {
    const existing = host.querySelector('[data-mp-modal]');
    if (existing) existing.remove();
    host.insertAdjacentHTML('beforeend', modalTemplate(slug));
    const backdrop = host.querySelector('[data-mp-modal]');
    const body = backdrop.querySelector('[data-mp-modal-body]');
    const actions = backdrop.querySelector('[data-mp-modal-actions]');
    backdrop.addEventListener('click', (event) => {
        if (event.target === backdrop) backdrop.remove();
    });
    backdrop.querySelector('[data-mp-modal-close]').addEventListener('click', () => backdrop.remove());

    // Initial preview always uses the registry's latest version. The
    // user can pick a different version from the Version dropdown
    // (rendered below) which re-fetches and re-renders this modal in
    // place. We keep this preview-orchestrator separate from
    // ``runPreview`` so the version-change handler can call it without
    // mutating the modal scaffolding.
    //
    // ``previewToken`` is incremented on every preview request so a
    // late v2 response cannot overwrite a newer v3 render — Cycle 1
    // GPT critic Finding 6.
    const initialVersion = options?.preselectVersion || null;
    let previewToken = 0;

    async function runPreview(version) {
        const myToken = ++previewToken;
        body.innerHTML = '<div class="muted">Loading…</div>';
        actions.innerHTML = '';
        let preview;
        try {
            const url = version
                ? `/api/marketplace/clawhub/preview/${encodeURIComponent(slug)}?version=${encodeURIComponent(version)}`
                : `/api/marketplace/clawhub/preview/${encodeURIComponent(slug)}`;
            preview = await fetchJson(url);
        } catch (err) {
            if (myToken !== previewToken) return;
            body.innerHTML = `<div class="skills-load-error">Failed to load preview: ${escapeHtml(err.message)}</div>`;
            return;
        }
        if (myToken !== previewToken) {
            // A newer version was requested while we were waiting —
            // discard this stale response.
            return;
        }
        const summary = preview.summary || {};
        const adapter = preview.adapter || {};
        const archive = preview.archive || {};
        const previewedVersion = preview.version || version || summary.latest_version || '';

        const fileList = (preview.staging?.files || [])
            .map((f) => `<li><code>${escapeHtml(f)}</code></li>`)
            .join('');

        // Version-pinning dropdown: ``summary.versions`` is the
        // registry-supplied set; we always include the previewed
        // version even if the registry omitted it from the list.
        const allVersions = Array.from(new Set([
            ...(summary.versions || []),
            previewedVersion,
        ].filter(Boolean)));
        const versionOptions = allVersions
            .map((v) => `<option value="${escapeHtml(v)}"${v === previewedVersion ? ' selected' : ''}>${escapeHtml(v)}</option>`)
            .join('');
        const homepageHref = safeExternalUrl(summary.homepage);

        const skillMdRaw = adapter.openclaw_md_text || adapter.skill_md_text || '';
        const skillMdHtml = renderMarkdownSafe(skillMdRaw);

        body.innerHTML = `
            <section>
                <h3>${escapeHtml(summary.display_name || slug)}</h3>
                <div class="muted">${escapeHtml(summary.summary || summary.description || '')}</div>
                <div class="marketplace-modal-meta muted">
                    <label class="marketplace-version-pin">
                        Version:
                        <select data-mp-modal-version-select>
                            ${versionOptions || `<option value="${escapeHtml(previewedVersion)}">${escapeHtml(previewedVersion || '—')}</option>`}
                        </select>
                    </label>
                    <span>sha256: <code>${escapeHtml((archive.sha256 || '').slice(0, 16))}…</code></span>
                    <span>files: ${preview.staging?.file_count ?? 0}</span>
                    <span>size: ${Number(archive.size_bytes || 0).toLocaleString()} bytes</span>
                    ${summary.license ? `<span>license: ${escapeHtml(summary.license)}</span>` : ''}
                    ${homepageHref ? `<a href="${homepageHref}" target="_blank" rel="noopener noreferrer">homepage</a>` : ''}
                </div>
            </section>
            <section>
                <h4>Translated manifest</h4>
                ${renderManifestTable(adapter.translated_manifest)}
            </section>
            <section>
                ${renderAdapterNotes(adapter)}
            </section>
            <section>
                <h4>Files</h4>
                <ul class="marketplace-file-list">${fileList || '<li><i>no files</i></li>'}</ul>
            </section>
            <section>
                <h4>SKILL.md preview</h4>
                <p class="muted">Original OpenClaw frontmatter preserved on disk as <code>SKILL.openclaw.md</code>; Ouroboros runs the adapter-translated copy.</p>
                <div class="marketplace-skillmd-rendered">${skillMdHtml}</div>
            </section>
        `;

        const versionSelect = body.querySelector('[data-mp-modal-version-select]');
        if (versionSelect) {
            versionSelect.addEventListener('change', () => {
                runPreview(versionSelect.value).catch((err) => {
                    console.warn('marketplace: version reselect failed', err);
                });
            });
        }

        const installable = preview.adapter?.ok && !preview.staging?.is_plugin;
        actions.innerHTML = installable
            ? `<button class="btn btn-default btn-primary"
                       data-mp-modal-install="${escapeHtml(slug)}"
                       data-version="${escapeHtml(previewedVersion)}">
                 Install v${escapeHtml(previewedVersion)} + auto-review
               </button>`
            : `<div class="muted">${preview.staging?.is_plugin
                ? 'This is an OpenClaw Node plugin and cannot be installed.'
                : 'Install blocked by adapter — see Blockers above.'}</div>`;
    }

    await runPreview(initialVersion);
}


// ---------------------------------------------------------------------------
// Public init
// ---------------------------------------------------------------------------


export function initMarketplace(pane) {
    pane.innerHTML = paneTemplate();

    const state = {
        query: '',
        limit: 25,
        offset: 0,
        onlyOfficial: false,
        results: [],
        installedMap: new Map(),
        cursor: '',
        nextCursor: '',
        registryPath: 'packages',
        registryAttempts: [],
    };

    const queryInput = pane.querySelector('#mp-query');
    const onlyOfficial = pane.querySelector('#mp-only-official');
    const searchBtn = pane.querySelector('[data-mp-search]');
    const resultsHost = pane.querySelector('#mp-results');
    const paginationHost = pane.querySelector('#mp-pagination');
    const modalHost = pane.querySelector('#mp-modal-host');

    let debounceTimer = null;

    async function refresh() {
        showStatus(pane, 'Loading…', 'muted');
        try {
            state.installedMap = new Map();
            const data = await runSearch(state);
            state.results = data.results || [];
            state.nextCursor = data.next_cursor || '';
            state.registryPath = data.registry_path || 'packages';
            state.registryAttempts = data.registry_attempts || [];
            renderResults(resultsHost, state.results, state.installedMap, state.results.length, {
                registryPath: state.registryPath,
                attempts: state.registryAttempts,
            });
            renderPagination(paginationHost, {
                offset: state.offset,
                limit: state.limit,
                count: state.results.length,
                cursor: state.cursor,
                nextCursor: state.nextCursor,
            });
            const mode = state.query ? 'search' : 'browse';
            const official = state.onlyOfficial ? ' · official only' : '';
            showStatus(pane, `${state.results.length} skill${state.results.length === 1 ? '' : 's'} · ${mode}${official} · ${state.registryPath}`, 'muted');
            loadInstalled().then((installedMap) => {
                state.installedMap = installedMap;
                renderResults(resultsHost, state.results, state.installedMap, state.results.length, {
                    registryPath: state.registryPath,
                    attempts: state.registryAttempts,
                });
            }).catch(() => {});
        } catch (err) {
            const message = err?.body?.error || err?.message || String(err);
            showStatus(pane, `Error: ${message}`, 'danger');
            resultsHost.innerHTML = `<div class="skills-load-error">${escapeHtml(message)}</div>`;
            paginationHost.hidden = true;
        }
    }

    function scheduleRefresh(immediate) {
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(refresh, immediate ? 0 : 300);
    }

    queryInput.addEventListener('input', (event) => {
        state.query = event.target.value || '';
        state.offset = 0;
        state.cursor = '';
        scheduleRefresh(false);
    });
    onlyOfficial.addEventListener('change', () => {
        state.onlyOfficial = onlyOfficial.checked;
        state.offset = 0;
        state.cursor = '';
        scheduleRefresh(true);
    });
    searchBtn.addEventListener('click', () => scheduleRefresh(true));

    paginationHost.addEventListener('click', (event) => {
        const prev = event.target.closest('[data-mp-prev]');
        const next = event.target.closest('[data-mp-next]');
        if (prev) {
            state.offset = Math.max(0, state.offset - state.limit);
            state.cursor = '';
            scheduleRefresh(true);
        } else if (next) {
            if (state.nextCursor) {
                state.cursor = state.nextCursor;
            } else {
                state.offset = state.offset + state.limit;
            }
            scheduleRefresh(true);
        }
    });

    resultsHost.addEventListener('click', async (event) => {
        const previewBtn = event.target.closest('[data-mp-preview]');
        const installBtn = event.target.closest('[data-mp-install]');
        const updateBtn = event.target.closest('[data-mp-update]');
        const uninstallBtn = event.target.closest('[data-mp-uninstall]');
        if (previewBtn) {
            await openDetailModal(modalHost, previewBtn.dataset.mpPreview);
            return;
        }
        if (installBtn) {
            installBtn.disabled = true;
            const slug = installBtn.dataset.mpInstall;
            showStatus(pane, `Installing ${slug}…`, 'muted');
            try {
                const result = await fetchJson('/api/marketplace/clawhub/install', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ slug, auto_review: true }),
                });
                if (!result.ok) {
                    showStatus(pane, `Install failed: ${result.error}`, 'danger');
                } else if (result.review_error) {
                    showStatus(
                        pane,
                        `Installed ${slug} — AUTO-REVIEW FAILED (${result.review_error}). Skill is non-executable; rerun review from the Skills tab.`,
                        'danger',
                    );
                } else {
                    showStatus(pane, `Installed ${slug} — review ${result.review_status}`, result.review_status === 'pass' ? 'ok' : 'warn');
                }
            } catch (err) {
                showStatus(pane, `Install error: ${err.message}`, 'danger');
            } finally {
                installBtn.disabled = false;
                refresh();
            }
            return;
        }
        if (updateBtn) {
            updateBtn.disabled = true;
            const slug = updateBtn.dataset.mpUpdate;
            const installed = state.installedMap.get(slug);
            const sanitized = installed?.name;
            if (!sanitized) {
                showStatus(pane, `Cannot update ${slug}: no provenance found`, 'danger');
                updateBtn.disabled = false;
                return;
            }
            // Optional: let the operator pick a non-latest target via
            // a small prompt. The summary already lists every published
            // version; we offer a freeform prompt seeded with the
            // registry latest. Empty / cancelled = skip; the install
            // path treats falsy version as "latest".
            const summary = state.results.find((s) => s.slug === slug);
            const latest = summary?.latest_version || '';
            const userVersion = window.prompt(
                `Update ${slug} to which version? Leave empty for latest (${latest || 'unknown'}).`,
                latest,
            );
            if (userVersion === null) {
                // operator cancelled
                updateBtn.disabled = false;
                return;
            }
            const targetVersion = (userVersion || '').trim();
            showStatus(pane, `Updating ${slug}${targetVersion ? ` → v${targetVersion}` : ' (latest)'}…`, 'muted');
            try {
                const body = targetVersion ? { version: targetVersion } : {};
                const result = await fetchJson(`/api/marketplace/clawhub/update/${encodeURIComponent(sanitized)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!result.ok) {
                    showStatus(pane, `Update failed: ${result.error}`, 'danger');
                } else {
                    showStatus(pane, `Updated ${slug} — review ${result.review_status}`, result.review_status === 'pass' ? 'ok' : 'warn');
                }
            } catch (err) {
                showStatus(pane, `Update error: ${err.message}`, 'danger');
            } finally {
                updateBtn.disabled = false;
                refresh();
            }
            return;
        }
        if (uninstallBtn) {
            const slug = uninstallBtn.dataset.mpUninstall;
            const sanitized = uninstallBtn.dataset.name;
            if (!confirm(`Uninstall ${slug}? This deletes data/skills/clawhub/${sanitized}/.`)) return;
            uninstallBtn.disabled = true;
            try {
                await fetchJson(`/api/marketplace/clawhub/uninstall/${encodeURIComponent(sanitized)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({}),
                });
                showStatus(pane, `Uninstalled ${slug}`, 'ok');
            } catch (err) {
                showStatus(pane, `Uninstall error: ${err.message}`, 'danger');
            } finally {
                uninstallBtn.disabled = false;
                refresh();
            }
        }
    });

    modalHost.addEventListener('click', async (event) => {
        const installBtn = event.target.closest('[data-mp-modal-install]');
        if (!installBtn) return;
        const slug = installBtn.dataset.mpModalInstall;
        const pinnedVersion = installBtn.dataset.version || '';
        installBtn.disabled = true;
        try {
            const body = pinnedVersion
                ? { slug, version: pinnedVersion, auto_review: true }
                : { slug, auto_review: true };
            const result = await fetchJson('/api/marketplace/clawhub/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!result.ok) {
                showStatus(pane, `Install failed: ${result.error}`, 'danger');
            } else if (result.review_error) {
                showStatus(
                    pane,
                    `Installed ${slug} — AUTO-REVIEW FAILED (${result.review_error}). Skill is non-executable; rerun review from the Skills tab.`,
                    'danger',
                );
                const backdrop = modalHost.querySelector('[data-mp-modal]');
                if (backdrop) backdrop.remove();
            } else {
                showStatus(pane, `Installed ${slug} — review ${result.review_status}`, result.review_status === 'pass' ? 'ok' : 'warn');
                const backdrop = modalHost.querySelector('[data-mp-modal]');
                if (backdrop) backdrop.remove();
            }
        } catch (err) {
            showStatus(pane, `Install error: ${err.message}`, 'danger');
        } finally {
            installBtn.disabled = false;
            refresh();
        }
    });

    refresh();
}


export default { initMarketplace };
