import { escapeHtmlAttr as escapeHtml } from './utils.js';

function classAttr(parts) {
    return parts.filter(Boolean).join(' ');
}

export function renderMobileNavToggle() {
    return `
        <button class="mobile-nav-toggle" type="button" data-mobile-nav-toggle aria-label="Open navigation" aria-controls="primary-sidebar" aria-expanded="false">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/></svg>
        </button>
    `;
}

export function renderPageHeader({
    title,
    icon = '',
    description = '',
    leadingHtml,
    toolbarHtml = '',
    trailingHtml = '',
    actionsHtml = '',
    tabsHtml = '',
    variant = '',
    className = '',
    showMobileNav = true,
} = {}) {
    const variantClass = variant ? `app-page-header-${escapeHtml(variant)}` : '';
    const iconHtml = icon ? `<span class="app-page-icon" aria-hidden="true">${icon}</span>` : '';
    const leading = leadingHtml !== undefined
        ? leadingHtml
        : (showMobileNav ? renderMobileNavToggle() : '');
    const descriptionHtml = description
        ? `<p class="app-page-description">${escapeHtml(description)}</p>`
        : '';
    const toolbar = (toolbarHtml || actionsHtml)
        ? `<div class="app-page-toolbar app-page-actions">${toolbarHtml || actionsHtml}</div>`
        : '';
    const trailing = trailingHtml
        ? `<div class="app-page-trailing">${trailingHtml}</div>`
        : '';
    const tabs = tabsHtml
        ? `<div class="app-page-tabs">${tabsHtml}</div>`
        : '';
    return `
        <div class="${classAttr(['page-header', 'app-page-header', variantClass, className])}">
            <div class="app-page-leading">${leading}</div>
            <div class="app-page-title-block">
                <div class="app-page-title-row">
                    ${iconHtml}
                    <h2 class="app-page-title">${escapeHtml(title)}</h2>
                </div>
                ${descriptionHtml}
            </div>
            ${toolbar}
            ${trailing}
            ${tabs}
        </div>
    `;
}

// SSOT for a segmented single-select control (the settings effort/mode toggles):
// one generator owns the markup so every group renders identically — the sibling
// of renderTabStrip/.app-tab, but for in-card segmented choices (.ui-segment).
// Buttons keep the data-effort-* hooks the controls layer binds on, plus the
// legacy .settings-effort-* classes that carry settings-specific overrides
// (per-group column counts and accent colors). `modifier` is a bare boolean
// data-attribute (e.g. 'data-enforcement-group') that selects a column override.
export function renderSegmentedField({
    target,
    options = [],
    modifier = '',
    title = '',
} = {}) {
    const tgt = String(target || '').trim();
    if (!tgt) {
        throw new Error('renderSegmentedField requires target');
    }
    const buttons = options.map((opt) => {
        const value = String(opt.value ?? '');
        return `<button type="button" class="ui-segment settings-effort-btn" data-effort-value="${escapeHtml(value)}">${escapeHtml(opt.label ?? value)}</button>`;
    }).join('');
    const mod = String(modifier || '').trim();
    const modAttr = mod ? ` ${mod}` : '';
    const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
    return `
        <div class="ui-segment-group settings-effort-group" data-effort-group${modAttr} data-effort-target="${escapeHtml(tgt)}"${titleAttr}>
            ${buttons}
        </div>
    `;
}

export function renderTabStrip({
    items = [],
    active = '',
    dataAttr,
    activeClass = 'active',
    ariaLabel = 'Page views',
    stripClass = '',
    tabClass = '',
} = {}) {
    const attr = String(dataAttr || '').trim();
    if (!attr) {
        throw new Error('renderTabStrip requires dataAttr');
    }
    const buttons = items.map((item) => {
        const value = String(item.value ?? item.id ?? '');
        const isActive = value === active;
        const pill = item.pillId
            ? `<span class="${classAttr(['app-tab-pill', item.pillClass || ''])}" id="${escapeHtml(item.pillId)}" hidden></span>`
            : '';
        return `
            <button
                type="button"
                class="${classAttr(['app-tab', tabClass, item.className || '', isActive ? activeClass : ''])}"
                ${attr}="${escapeHtml(value)}"
                role="tab"
                aria-selected="${isActive ? 'true' : 'false'}"
            >
                ${escapeHtml(item.label ?? value)}
                ${pill}
            </button>
        `;
    }).join('');
    return `
        <div class="${classAttr(['app-tab-strip', stripClass])}" role="tablist" aria-label="${escapeHtml(ariaLabel)}">
            ${buttons}
        </div>
    `;
}
