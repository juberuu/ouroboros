import { apiFetch } from './api_client.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

const TONES = new Set(['ok', 'danger', 'warn', 'muted', 'info']);
const TONE_ALIASES = Object.freeze({
    error: 'danger',
    success: 'ok',
    warning: 'warn',
    neutral: 'muted',
});
const SAFE_FIELD_TYPES = new Set(['text', 'number', 'url', 'email', 'password', 'textarea', 'select', 'checkbox']);

function safeFieldType(value) {
    const type = String(value || 'text').toLowerCase();
    return SAFE_FIELD_TYPES.has(type) ? type : 'text';
}

function safeNumericAttribute(name, value) {
    if (value === '' || value === null || value === undefined || !Number.isFinite(Number(value))) return '';
    return ` ${name}="${escapeHtml(value)}"`;
}

/** Render the narrow host-owned field contract shared by Widgets and Settings. */
export function renderSafeField(field = {}, savedValues = {}, options = {}) {
    const rawName = String(field.name || '');
    const name = escapeHtml(rawName);
    const label = escapeHtml(field.label || rawName);
    const type = safeFieldType(field.type);
    const hasSaved = type !== 'password' && Object.prototype.hasOwnProperty.call(savedValues || {}, rawName);
    const saved = type === 'password' ? '' : (hasSaved ? savedValues[rawName] : field.default);
    const value = escapeHtml(saved ?? '');
    const placeholder = field.placeholder ? ` placeholder="${escapeHtml(field.placeholder)}"` : '';
    const required = field.required ? ' required' : '';
    const disabled = field.disabled || options.disabled ? ' disabled' : '';
    const fieldClass = escapeHtml(options.fieldClass || 'widget-field');
    const inlineClass = escapeHtml(options.inlineClass || `${options.fieldClass || 'widget-field'} widget-field-inline`);
    const helpClass = escapeHtml(options.helpClass || 'widget-field-help');
    const maxSpan = Math.max(1, Math.min(4, Number(options.maxSpan) || 4));
    const span = Math.max(1, Math.min(maxSpan, Number(field.span) || 1));
    const spanClass = options.spanClassPrefix ? ` ${escapeHtml(options.spanClassPrefix)}${span}` : '';
    const help = field.help ? `<small class="${helpClass}">${escapeHtml(field.help)}</small>` : '';
    if (type === 'textarea') {
        return `<label class="${fieldClass}${spanClass}"><span>${label}</span><textarea name="${name}"${placeholder}${required}${disabled}>${value}</textarea>${help}</label>`;
    }
    if (type === 'select') {
        const optionsHtml = (Array.isArray(field.options) ? field.options : []).map((option) => {
            const optionValue = typeof option === 'object' && option !== null ? option.value : option;
            const optionLabel = typeof option === 'object' && option !== null ? (option.label ?? option.value) : option;
            const selected = String(optionValue ?? '') === String(saved ?? '') ? ' selected' : '';
            return `<option value="${escapeHtml(optionValue ?? '')}"${selected}>${escapeHtml(optionLabel ?? '')}</option>`;
        }).join('');
        return `<label class="${fieldClass}${spanClass}"><span>${label}</span><select name="${name}"${required}${disabled}>${optionsHtml}</select>${help}</label>`;
    }
    if (type === 'checkbox') {
        return `<label class="${inlineClass}${spanClass}"><input type="checkbox" name="${name}"${saved ? ' checked' : ''}${required}${disabled}> <span>${label}</span>${help}</label>`;
    }
    const numeric = type === 'number'
        ? `${safeNumericAttribute('min', field.min)}${safeNumericAttribute('max', field.max)}${safeNumericAttribute('step', field.step)}`
        : '';
    const autocomplete = type === 'password' ? ' autocomplete="new-password"' : '';
    return `<label class="${fieldClass}${spanClass}"><span>${label}</span><input type="${type}" name="${name}" value="${value}"${placeholder}${numeric}${required}${disabled}${autocomplete}>${help}</label>`;
}

/** Collect values according to the same closed field contract used for rendering. */
export function collectSafeFieldValues(form, fields = [], { includePasswords = true } = {}) {
    const values = {};
    for (const field of Array.isArray(fields) ? fields : []) {
        const name = String(field?.name || '');
        const type = safeFieldType(field?.type);
        if (!name || (type === 'password' && !includePasswords)) continue;
        const input = form?.elements?.namedItem
            ? form.elements.namedItem(name)
            : form?.elements?.[name];
        if (!input) continue;
        values[name] = type === 'checkbox' ? Boolean(input.checked) : input.value;
    }
    return values;
}

export function normalizeTone(tone = 'muted', fallback = 'muted') {
    const canonical = (value) => {
        const clean = String(value || '').toLowerCase();
        const normalized = TONE_ALIASES[clean] || clean;
        return TONES.has(normalized) ? normalized : '';
    };
    return canonical(tone) || canonical(fallback) || 'muted';
}

export function renderToneBadge(label, tone = 'muted', className = 'skills-badge') {
    const cleanTone = normalizeTone(tone);
    return `<span class="${className} ${className}-${cleanTone}">${escapeHtml(label || '')}</span>`;
}

export function installedTime(item) {
    const time = Date.parse(item?.installed_at || item?.provenance?.installed_at || item?.provenance?.updated_at || '');
    return Number.isFinite(time) ? time : 0;
}

export function formatRelativeAge(time, freshLabel = 'Just installed') {
    if (!time) return '';
    const minutes = Math.floor(Math.max(0, Date.now() - time) / 60000);
    if (minutes < 2) return freshLabel;
    if (minutes < 90) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return days < 45 ? `${days}d ago` : new Date(time).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

export function setInlineStatus(el, text, tone = 'muted') {
    if (el) { el.textContent = text || ''; el.dataset.tone = normalizeTone(tone); }
}

export async function openViaHostBridge(url, filename = 'file') {
    const api = window.pywebview?.api;
    const openBridge = api?.open_file_with_default_app;
    if (openBridge) {
        const result = await openBridge(url, filename);
        if (!result?.ok) throw new Error(result?.error || 'open failed');
        return { ...result, native: true };
    }
    // Version-skew fallback: the served frontend auto-updates via the managed
    // repo, but the outer desktop launcher only changes on a full app reinstall,
    // so a packaged launcher can predate open_file_with_default_app (added
    // v6.58.3) while still shipping download_file_to_downloads with an
    // open_external flag (since v5.5.0). Reuse that long-shipped bridge — it
    // saves to ~/Downloads AND opens externally — instead of window.open, which
    // is a silent no-op in the desktop WKWebView.
    const downloadBridge = api?.download_file_to_downloads;
    if (downloadBridge) {
        const result = await downloadBridge(url, filename, true);
        if (!result?.ok) throw new Error(result?.error || 'open failed');
        return { ...result, native: true };
    }
    // True web / non-desktop: open in a new tab. This never navigates the app
    // itself; the browser previews (e.g. PDF) or downloads per its own handling.
    window.open(url, '_blank', 'noopener');
    return { ok: true, native: false };
}

export async function downloadViaHostBridge(url, filename = 'download', { openExternal = false, fetchOptions = {} } = {}) {
    const bridge = window.pywebview?.api?.download_file_to_downloads;
    if (bridge) {
        const result = await bridge(url, filename, Boolean(openExternal));
        if (!result?.ok) throw new Error(result?.error || 'desktop download failed');
        return { ...result, native: true };
    }
    const resp = await apiFetch(url, fetchOptions);
    if (!resp.ok) throw new Error(`download failed: HTTP ${resp.status}`);
    const blobUrl = URL.createObjectURL(await resp.blob());
    const link = document.createElement('a');
    Object.assign(link, { href: blobUrl, download: filename, rel: 'noopener' });
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    return { ok: true, native: false };
}
