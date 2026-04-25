const FALLBACK_MODEL_ITEMS = [
    { value: 'anthropic::claude-opus-4-7', label: 'Official Anthropic' },
    { value: 'anthropic::claude-opus-4-6', label: 'Official Anthropic' },
    { value: 'anthropic::claude-sonnet-4-6', label: 'Official Anthropic' },
    { value: 'anthropic/claude-opus-4.7', label: 'Anthropic via OpenRouter' },
    { value: 'anthropic/claude-opus-4.6', label: 'Anthropic via OpenRouter' },
    { value: 'anthropic/claude-sonnet-4.6', label: 'Anthropic via OpenRouter' },
    { value: 'google/gemini-3.1-pro-preview', label: 'Google via OpenRouter' },
    { value: 'google/gemini-3-flash-preview', label: 'Google via OpenRouter' },
    { value: 'openai/gpt-5.4', label: 'OpenAI via OpenRouter' },
    { value: 'openai::gpt-5.4', label: 'Official OpenAI' },
    { value: 'openai::gpt-5.4-mini', label: 'Official OpenAI' },
];

let modelCatalogItems = FALLBACK_MODEL_ITEMS.slice();

function normalizeCatalogItems(items) {
    if (!Array.isArray(items) || !items.length) return FALLBACK_MODEL_ITEMS.slice();
    return items.map((item) => ({
        value: item.value || item.id || '',
        label: item.label || item.provider || '',
        provider: item.provider || '',
    })).filter((item) => item.value);
}

function filterCatalogItems(query) {
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) return modelCatalogItems.slice(0, 8);
    return modelCatalogItems
        .filter((item) => {
            const haystack = `${item.value} ${item.label} ${item.provider || ''}`.toLowerCase();
            return haystack.includes(needle);
        })
        .slice(0, 8);
}

function closePicker(picker) {
    const panel = picker.querySelector('.model-picker-results');
    if (!panel) return;
    panel.hidden = true;
    panel.innerHTML = '';
}

function renderPickerResults(picker, query) {
    const panel = picker.querySelector('.model-picker-results');
    if (!panel) return;
    const items = filterCatalogItems(query);
    if (!items.length) {
        panel.hidden = true;
        panel.innerHTML = '';
        return;
    }
    panel.innerHTML = items.map((item) => `
        <button type="button" class="model-picker-item" data-value="${item.value}">
            <span class="model-picker-item-value">${item.value}</span>
            <span class="model-picker-item-label">${item.label || item.provider || 'Catalog model'}</span>
        </button>
    `).join('');
    panel.hidden = false;
}

export function bindEffortSegments(root) {
    root.querySelectorAll('[data-effort-group]').forEach((group) => {
        const targetId = group.dataset.effortTarget;
        const input = root.querySelector(`#${targetId}`);
        if (!input) return;
        const buttons = Array.from(group.querySelectorAll('[data-effort-value]'));

        function sync() {
            buttons.forEach((button) => {
                button.classList.toggle('active', button.dataset.effortValue === input.value);
            });
        }

        buttons.forEach((button) => {
            button.addEventListener('click', () => {
                input.value = button.dataset.effortValue || input.value;
                sync();
            });
        });

        sync();
    });
}

export function syncEffortSegments(root) {
    root.querySelectorAll('[data-effort-group]').forEach((group) => {
        const targetId = group.dataset.effortTarget;
        const input = root.querySelector(`#${targetId}`);
        if (!input) return;
        group.querySelectorAll('[data-effort-value]').forEach((button) => {
            button.classList.toggle('active', button.dataset.effortValue === input.value);
        });
    });
}

export function bindModelPickers(root) {
    const pickers = Array.from(root.querySelectorAll('[data-model-picker]'));
    if (!pickers.length) return;

    function closeAll(except = null) {
        pickers.forEach((picker) => {
            if (picker !== except) closePicker(picker);
        });
    }

    pickers.forEach((picker) => {
        const input = picker.querySelector('input');
        const panel = picker.querySelector('.model-picker-results');
        if (!input || !panel) return;

        input.addEventListener('focus', () => {
            closeAll(picker);
            renderPickerResults(picker, input.value);
        });

        input.addEventListener('input', () => {
            closeAll(picker);
            renderPickerResults(picker, input.value);
        });

        input.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') closePicker(picker);
        });

        panel.addEventListener('mousedown', (event) => {
            const item = event.target.closest('.model-picker-item');
            if (!item) return;
            event.preventDefault();
            input.value = item.dataset.value || '';
            closePicker(picker);
            input.dispatchEvent(new Event('change', { bubbles: true }));
        });
    });

    document.addEventListener('click', (event) => {
        const picker = event.target instanceof Element
            ? event.target.closest('[data-model-picker]')
            : null;
        if (!picker) {
            closeAll();
            return;
        }
        closeAll(picker);
    });

    document.addEventListener('settings-model-catalog:updated', (event) => {
        modelCatalogItems = normalizeCatalogItems(event.detail?.items || []);
        pickers.forEach((picker) => {
            const panel = picker.querySelector('.model-picker-results');
            if (panel && !panel.hidden) {
                const input = picker.querySelector('input');
                renderPickerResults(picker, input?.value || '');
            }
        });
    });
}
