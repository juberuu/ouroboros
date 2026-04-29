function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function renderMarkdownSafe(rawMd) {
    const text = String(rawMd ?? '');
    if (!text) return '';
    if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
        return `<pre><code>${escapeHtml(text)}</code></pre>`;
    }
    try {
        const rendered = marked.parse(text, { async: false, gfm: true, breaks: false });
        return DOMPurify.sanitize(rendered, {
            USE_PROFILES: { html: true },
            FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'input', 'img', 'video', 'audio', 'source'],
            FORBID_ATTR: ['style', 'src', 'srcset', 'srcdoc'],
        });
    } catch (err) {
        console.warn('widgets: markdown render failed', err);
        return `<pre><code>${escapeHtml(text)}</code></pre>`;
    }
}

function pageTemplate() {
    return `
        <section class="page" id="page-widgets">
            <div class="page-header">
                <h2>Widgets</h2>
                <button id="widgets-refresh" class="btn btn-default">Refresh</button>
            </div>
            <p class="muted">Reviewed extension UI surfaces live here, separate from the skill catalogue.</p>
            <div id="widgets-list" class="widgets-list"></div>
        </section>
    `;
}

async function fetchExtensions() {
    const resp = await fetch('/api/extensions', { cache: 'no-store' });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

function renderShell(host, tabs) {
    if (!tabs.length) {
        host.innerHTML = '<div class="muted">No live widgets yet. Review and enable an extension that registers a UI tab.</div>';
        return;
    }
    host.innerHTML = tabs.map((tab) => {
        // v5.2.3: the previous "skill:tab_id" muted label leaked
        // internal registry keys to end users (e.g. "weather:widget").
        // Show the skill name as a friendly subtitle only when it
        // differs from the widget title; otherwise omit it entirely
        // so the card header stays visually clean.
        const title = tab.title || tab.tab_id || tab.skill;
        const subtitle = tab.skill && tab.skill !== title
            ? `<span class="widgets-card-source">from ${escapeHtml(tab.skill)}</span>`
            : '';
        return `
        <article class="widgets-card" data-widget-key="${escapeHtml(tab.key || `${tab.skill}:${tab.tab_id}`)}">
            <div class="widgets-card-head">
                <strong>${escapeHtml(title)}</strong>
                ${subtitle}
            </div>
            <div class="widgets-card-body" data-widget-mount></div>
        </article>
        `;
    }).join('');
}

function cleanWidgetRoute(value) {
    const route = String(value || '').trim().replace(/^\/+/, '');
    const parts = route.split('/').filter(Boolean);
    if (!route || route.includes('\\') || parts.some((part) => part === '.' || part === '..')) {
        return '';
    }
    return parts.map(encodeURIComponent).join('/');
}

function extensionRouteUrl(tab, route, params) {
    const cleanRoute = cleanWidgetRoute(route);
    if (!cleanRoute) return '';
    const base = `/api/extensions/${encodeURIComponent(tab.skill)}/${cleanRoute}`;
    const query = params instanceof URLSearchParams && String(params) ? `?${params}` : '';
    return base + query;
}

function getPath(root, path, fallback = '') {
    if (!path) return root ?? fallback;
    let current = root;
    for (const part of String(path).split('.').filter(Boolean)) {
        if (current == null || typeof current !== 'object') return fallback;
        current = current[part];
    }
    return current ?? fallback;
}

function safeMediaSrc(tab, spec, state) {
    const route = spec.route || spec.api_route || '';
    if (route) {
        const params = new URLSearchParams();
        for (const [key, value] of Object.entries(spec.query || {})) {
            params.set(key, String(value ?? ''));
        }
        return extensionRouteUrl(tab, route, params);
    }
    const value = getPath(state[spec.target || 'result'], spec.path || '', spec.src || '');
    const text = String(value || '').trim();
    if (/^data:(image\/(?:png|jpeg|jpg|gif|webp)|audio\/(?:mpeg|wav|ogg)|video\/(?:mp4|webm|ogg));base64,[A-Za-z0-9+/=]+$/i.test(text)) {
        return text;
    }
    if (text.startsWith('/api/extensions/')) {
        try {
            const parsed = new URL(text, window.location.origin);
            const expectedPrefix = `/api/extensions/${encodeURIComponent(tab.skill)}/`;
            if (parsed.origin === window.location.origin && parsed.pathname.startsWith(expectedPrefix)) {
                return parsed.pathname + parsed.search;
            }
        } catch {
            return '';
        }
    }
    return '';
}

function fieldValue(form, field) {
    const name = String(field.name || '');
    const input = form.elements[name];
    if (!input) return '';
    if (input.type === 'checkbox') return input.checked;
    return input.value;
}

function renderField(field, savedValues) {
    const name = escapeHtml(field.name || '');
    const label = escapeHtml(field.label || field.name || '');
    const rawName = String(field.name || '');
    const hasSaved = Object.prototype.hasOwnProperty.call(savedValues || {}, rawName);
    const saved = hasSaved ? savedValues[rawName] : field.default;
    const value = escapeHtml(saved ?? '');
    const required = field.required ? 'required' : '';
    if (field.type === 'textarea') {
        return `<label class="widget-field"><span>${label}</span><textarea name="${name}" ${required}>${value}</textarea></label>`;
    }
    if (field.type === 'select') {
        const options = (field.options || []).map((option) => {
            const optValue = typeof option === 'object' ? option.value : option;
            const optLabel = typeof option === 'object' ? (option.label ?? option.value) : option;
            return `<option value="${escapeHtml(optValue)}"${String(optValue) === String(saved ?? '') ? ' selected' : ''}>${escapeHtml(optLabel)}</option>`;
        }).join('');
        return `<label class="widget-field"><span>${label}</span><select name="${name}" ${required}>${options}</select></label>`;
    }
    if (field.type === 'checkbox') {
        return `<label class="widget-field widget-field-inline"><input type="checkbox" name="${name}" ${saved ? 'checked' : ''}> <span>${label}</span></label>`;
    }
    const type = ['text', 'number', 'url', 'email'].includes(field.type) ? field.type : 'text';
    return `<label class="widget-field"><span>${label}</span><input type="${type}" name="${name}" value="${value}" ${required}></label>`;
}

function renderDataComponent(tab, component, state, status) {
    const type = String(component.type || '');
    const target = component.target || 'result';
    const data = state[target] || {};
    if (type === 'status') {
        const current = status[target] || 'idle';
        return `<div class="widget-status" data-state="${escapeHtml(current)}">${escapeHtml(component[current] || current)}</div>`;
    }
    if (type === 'kv') {
        const fields = component.fields || [];
        const rows = fields.map((field) => {
            const label = escapeHtml(field.label || field.path || '');
            const value = getPath(data, field.path, '—');
            return `<div class="widget-kv-row"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`;
        }).join('');
        return `<div class="widget-kv">${rows || '<div class="muted">No data.</div>'}</div>`;
    }
    if (type === 'table') {
        const rows = getPath(data, component.path || '', []);
        const cols = component.columns || [];
        if (!Array.isArray(rows)) return '<div class="muted">No rows.</div>';
        return `<div class="widget-table-wrap"><table class="widget-table"><thead><tr>${cols.map((c) => `<th>${escapeHtml(c.label || c.path || '')}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${cols.map((c) => `<td>${escapeHtml(getPath(row, c.path, ''))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    }
    if (type === 'markdown') {
        const value = component.text ?? getPath(data, component.path || '', '');
        return `<div class="widget-markdown">${renderMarkdownSafe(value)}</div>`;
    }
    if (type === 'json') {
        const value = component.path ? getPath(data, component.path, {}) : data;
        return `<details class="widget-json"><summary>${escapeHtml(component.label || 'JSON')}</summary><pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></details>`;
    }
    if (['image', 'audio', 'video', 'file'].includes(type)) {
        const src = safeMediaSrc(tab, component, state);
        const label = escapeHtml(component.label || component.alt || type);
        if (!src) return `<div class="muted">${label}: no safe media source.</div>`;
        if (type === 'image') return `<figure class="widget-media"><img src="${escapeHtml(src)}" alt="${escapeHtml(component.alt || label)}"><figcaption>${label}</figcaption></figure>`;
        if (type === 'audio') return `<div class="widget-media"><div>${label}</div><audio controls src="${escapeHtml(src)}"></audio></div>`;
        if (type === 'video') return `<div class="widget-media"><div>${label}</div><video controls src="${escapeHtml(src)}"></video></div>`;
        return `<a class="btn btn-default" href="${escapeHtml(src)}" download>${label}</a>`;
    }
    if (type === 'gallery') {
        const items = component.items || getPath(data, component.path || '', []);
        if (!Array.isArray(items)) return '<div class="muted">No media items.</div>';
        return `<div class="widget-gallery">${items.map((item) => renderDataComponent(tab, { ...item, type: item.type || 'image' }, state, status)).join('')}</div>`;
    }
    if (type === 'progress') {
        const value = Number(getPath(data, component.path || 'progress', 0));
        const bounded = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
        return `<div class="widget-progress"><progress max="100" value="${bounded}"></progress><span>${bounded}%</span></div>`;
    }
    return '';
}

const widgetDisposers = new Map();

function boundedNumber(value, fallback, min, max) {
    const parsed = Number(value);
    const safe = Number.isFinite(parsed) ? parsed : fallback;
    return Math.max(min, Math.min(safe, max));
}

async function callWidgetRoute(tab, spec, values, signal) {
    const method = String(spec.method || 'GET').toUpperCase();
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(values || {})) {
        params.set(key, String(value ?? ''));
    }
    const noBody = method === 'GET' || method === 'HEAD';
    const url = extensionRouteUrl(tab, spec.route || spec.api_route, noBody ? params : null);
    if (!url) throw new Error('invalid widget route');
    const init = noBody
        ? { method, signal }
        : {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(values || {}),
            signal,
        };
    const resp = await fetch(url, init);
    const contentType = resp.headers.get('content-type') || '';
    const data = contentType.includes('application/json')
        ? await resp.json().catch(() => ({}))
        : { text: await resp.text() };
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

async function mountDeclarativeWidget(mount, tab, render) {
    const components = Array.isArray(render.components) ? render.components : [];
    const state = {};
    const status = {};
    const formValues = {};
    const timers = new Set();
    const controllers = new Set();
    const activePolls = new Set();
    const autoStarted = new Set();
    let disposed = false;

    const schedule = (fn, delay) => {
        if (disposed) return null;
        const timer = setTimeout(() => {
            timers.delete(timer);
            fn();
        }, delay);
        timers.add(timer);
        return timer;
    };
    const dispose = () => {
        disposed = true;
        controllers.forEach((controller) => controller.abort());
        controllers.clear();
        timers.forEach((timer) => clearTimeout(timer));
        timers.clear();
        activePolls.clear();
    };
    const callRoute = async (spec, values) => {
        if (disposed) throw new Error('widget disposed');
        const controller = new AbortController();
        controllers.add(controller);
        try {
            return await callWidgetRoute(tab, spec, values, controller.signal);
        } finally {
            controllers.delete(controller);
        }
    };
    const rememberFormValues = () => {
        mount.querySelectorAll('[data-widget-form]').forEach((form) => {
            const idx = form.dataset.widgetForm;
            formValues[idx] = formValues[idx] || {};
            const spec = components[Number(idx)] || {};
            for (const field of spec.fields || []) {
                formValues[idx][field.name] = fieldValue(form, field);
            }
        });
    };
    const startPoll = (idx) => {
        if (disposed || activePolls.has(idx)) return;
        const spec = components[Number(idx)] || {};
        const target = spec.target || 'result';
        const maxTicks = boundedNumber(spec.max_ticks, 20, 1, 100);
        const intervalMs = boundedNumber(spec.interval_ms, 2000, 1000, 30000);
        let ticks = 0;
        activePolls.add(idx);
        const poll = async () => {
            if (disposed) return;
            ticks += 1;
            status[target] = 'loading';
            renderAll();
            try {
                state[target] = await callRoute(spec, {});
                if (disposed) return;
                status[target] = 'success';
            } catch (err) {
                state[target] = { error: err.message || String(err) };
                status[target] = 'error';
            }
            const stopValue = getPath(state[target], spec.stop_path || '', undefined);
            if (ticks < maxTicks && String(stopValue) !== String(spec.stop_value ?? 'done')) {
                schedule(poll, intervalMs);
            } else {
                activePolls.delete(idx);
            }
            renderAll();
        };
        poll();
    };
    const renderAll = () => {
        if (disposed) return;
        rememberFormValues();
        mount.innerHTML = components.map((component, idx) => {
            const type = String(component.type || '');
            if (type === 'form') {
                const fields = (component.fields || [])
                    .map((field) => renderField(field, formValues[idx] || {}))
                    .join('');
                return `<form class="widget-form" data-widget-form="${idx}">${component.title ? `<h4>${escapeHtml(component.title)}</h4>` : ''}${fields}<button class="btn btn-primary" type="submit">${escapeHtml(component.submit_label || 'Submit')}</button></form>`;
            }
            if (type === 'action') {
                return `<button class="btn btn-default" data-widget-action="${idx}">${escapeHtml(component.label || 'Run')}</button>`;
            }
            if (type === 'poll') {
                return `<button class="btn btn-default" data-widget-poll="${idx}">${escapeHtml(component.label || 'Start polling')}</button>`;
            }
            return renderDataComponent(tab, component, state, status);
        }).join('');
        mount.querySelectorAll('[data-widget-form]').forEach((form) => {
            form.addEventListener('submit', async (event) => {
                event.preventDefault();
                const spec = components[Number(form.dataset.widgetForm)] || {};
                const target = spec.target || 'result';
                const values = {};
                for (const field of spec.fields || []) values[field.name] = fieldValue(form, field);
                status[target] = 'loading';
                renderAll();
                try {
                    state[target] = await callRoute(spec, values);
                    if (disposed) return;
                    status[target] = 'success';
                } catch (err) {
                    state[target] = { error: err.message || String(err) };
                    status[target] = 'error';
                }
                renderAll();
            });
        });
        mount.querySelectorAll('[data-widget-action]').forEach((button) => {
            button.addEventListener('click', async () => {
                const spec = components[Number(button.dataset.widgetAction)] || {};
                const target = spec.target || 'result';
                status[target] = 'loading';
                renderAll();
                try {
                    state[target] = await callRoute(spec, spec.body || {});
                    if (disposed) return;
                    status[target] = 'success';
                } catch (err) {
                    state[target] = { error: err.message || String(err) };
                    status[target] = 'error';
                }
                renderAll();
            });
        });
        mount.querySelectorAll('[data-widget-poll]').forEach((button) => {
            button.addEventListener('click', () => {
                startPoll(Number(button.dataset.widgetPoll));
            });
        });
        components.forEach((component, idx) => {
            if (String(component.type || '') === 'poll' && component.auto_start === true && !autoStarted.has(idx)) {
                autoStarted.add(idx);
                queueMicrotask(() => startPoll(idx));
            }
        });
    };
    renderAll();
    return dispose;
}

async function mountTab(card, tab) {
    const mount = card.querySelector('[data-widget-mount]');
    const render = tab.render || {};
    if (!mount) return;
    if (render.kind === 'iframe' && render.route) {
        const route = cleanWidgetRoute(render.route);
        if (!route) throw new Error('invalid widget iframe route');
        mount.innerHTML = `<iframe class="widgets-frame" sandbox="" src="/api/extensions/${encodeURIComponent(tab.skill)}/${route}"></iframe>`;
        return;
    }
    if (render.kind === 'inline_card' && render.api_route) {
        const apiRoute = cleanWidgetRoute(render.api_route);
        if (!apiRoute) throw new Error('invalid widget api_route');
        mount.innerHTML = `
            <form class="skill-widget-weather-form" data-widget-form>
                <input class="skill-widget-weather-city" value="Moscow" autocomplete="off" maxlength="80" aria-label="Widget query">
                <button type="submit" class="btn btn-default">Refresh</button>
            </form>
            <div class="skill-widget-weather-body" data-widget-result><div class="muted">Press Refresh.</div></div>
        `;
        const form = mount.querySelector('[data-widget-form]');
        const input = mount.querySelector('input');
        const result = mount.querySelector('[data-widget-result]');
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const query = (input.value || '').trim();
            result.innerHTML = '<div class="muted">Loading…</div>';
            const resp = await fetch(`/api/extensions/${encodeURIComponent(tab.skill)}/${apiRoute}?city=${encodeURIComponent(query)}`);
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || data.error) {
                result.innerHTML = `<div class="skills-load-error">${escapeHtml(data.error || `HTTP ${resp.status}`)}</div>`;
                return;
            }
            result.innerHTML = `
                <div class="skill-widget-weather-card">
                    <strong>${escapeHtml(data.resolved_to || data.city || query)}</strong>
                    <div class="skill-widget-weather-temp">${escapeHtml(data.temp_c)}°C <span class="muted">feels like ${escapeHtml(data.feels_like_c)}°C</span></div>
                    <div>${escapeHtml(data.condition || 'Unknown')}</div>
                </div>
            `;
        });
        return;
    }
    if (render.kind === 'declarative') {
        return mountDeclarativeWidget(mount, tab, render);
    }
    mount.innerHTML = `<div class="muted">Widget render kind <code>${escapeHtml(render.kind || 'unknown')}</code> is not supported yet.</div>`;
    return null;
}

function disposeMountedWidgets() {
    widgetDisposers.forEach((dispose) => {
        try {
            dispose();
        } catch (err) {
            console.warn('widgets: dispose failed', err);
        }
    });
    widgetDisposers.clear();
}

async function mountTrackedTab(card, tab) {
    const key = tab.key || `${tab.skill}:${tab.tab_id}`;
    const existing = widgetDisposers.get(key);
    if (existing) {
        existing();
        widgetDisposers.delete(key);
    }
    const dispose = await mountTab(card, tab);
    if (typeof dispose === 'function') {
        widgetDisposers.set(key, dispose);
        return;
    }
}

export function initWidgets() {
    const page = document.createElement('div');
    page.innerHTML = pageTemplate();
    document.getElementById('content').appendChild(page.firstElementChild);
    const list = document.getElementById('widgets-list');
    const refreshBtn = document.getElementById('widgets-refresh');
    let renderGeneration = 0;
    let widgetsVisible = false;

    async function render() {
        const generation = ++renderGeneration;
        widgetsVisible = true;
        disposeMountedWidgets();
        list.innerHTML = '<div class="muted">Loading widgets…</div>';
        try {
            const data = await fetchExtensions();
            if (!widgetsVisible || generation !== renderGeneration) return;
            const tabs = Array.isArray(data.live?.ui_tabs) ? data.live.ui_tabs : [];
            renderShell(list, tabs);
            for (const tab of tabs) {
                if (!widgetsVisible || generation !== renderGeneration) return;
                const key = tab.key || `${tab.skill}:${tab.tab_id}`;
                const card = list.querySelector(`[data-widget-key="${CSS.escape(key)}"]`);
                if (!card) continue;
                try {
                    await mountTrackedTab(card, tab);
                } catch (err) {
                    const mount = card.querySelector('[data-widget-mount]');
                    if (mount) mount.innerHTML = `<div class="skills-load-error">widget failed: ${escapeHtml(err.message || err)}</div>`;
                }
            }
        } catch (err) {
            if (!widgetsVisible || generation !== renderGeneration) return;
            list.innerHTML = `<div class="skills-load-error">Failed to load widgets: ${escapeHtml(err.message || err)}</div>`;
        }
    }

    refreshBtn.addEventListener('click', render);
    window.addEventListener('ouro:page-shown', (event) => {
        if (event.detail?.page === 'widgets') {
            render();
        } else {
            widgetsVisible = false;
            renderGeneration += 1;
            disposeMountedWidgets();
        }
    });
}
