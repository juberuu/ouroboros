function setCatalogStatus(statusEl, text, tone = 'muted') {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.dataset.tone = tone;
}

function broadcastCatalog(items) {
    document.dispatchEvent(new CustomEvent('settings-model-catalog:updated', {
        detail: { items },
    }));
}

function fillCatalogDatalist(items) {
    const list = document.getElementById('settings-model-catalog');
    if (list) {
        list.innerHTML = '';
        for (const item of items) {
            const option = document.createElement('option');
            option.value = item.value || item.id || '';
            option.label = item.label || item.provider || '';
            list.appendChild(option);
        }
    }
    broadcastCatalog(items);
}

export async function refreshModelCatalog() {
    const statusEl = document.getElementById('settings-model-catalog-status');
    setCatalogStatus(statusEl, 'Refreshing model catalog...', 'muted');

    try {
        const resp = await fetch('/api/model-catalog', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        const items = Array.isArray(data.items) ? data.items : [];
        const errors = Array.isArray(data.errors) ? data.errors : [];
        fillCatalogDatalist(items);

        if (errors.length && items.length) {
            setCatalogStatus(
                statusEl,
                `Loaded ${items.length} models. Some providers failed: ${errors.map((e) => e.provider_id).join(', ')}`,
                'warn',
            );
        } else if (errors.length) {
            setCatalogStatus(
                statusEl,
                `Model catalog unavailable right now: ${errors.map((e) => e.provider_id).join(', ')}`,
                'warn',
            );
        } else if (items.length) {
            setCatalogStatus(statusEl, `Loaded ${items.length} models.`, 'ok');
        } else {
            setCatalogStatus(statusEl, 'No provider catalogs available yet. This is optional.', 'muted');
        }
        return { items, errors };
    } catch (err) {
        fillCatalogDatalist([]);
        setCatalogStatus(
            statusEl,
            `Model catalog failed: ${err.message || err}. This is optional.`,
            'warn',
        );
        return { items: [], errors: [{ provider_id: 'catalog', error: String(err) }] };
    }
}
