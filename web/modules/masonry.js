const bound = new WeakMap();
let nextId = 1;

function ensureStyleElement(id) {
    const styleId = `masonry-style-${id}`;
    let el = document.getElementById(styleId);
    if (!el) {
        el = document.createElement('style');
        el.id = styleId;
        document.head.appendChild(el);
    }
    return el;
}

function shortestColumn(columns) {
    let index = 0;
    for (let i = 1; i < columns.length; i += 1) {
        if (columns[i] < columns[index]) index = i;
    }
    return index;
}

function bestPair(columns) {
    if (columns.length < 2) return 0;
    let index = 0;
    let best = Math.max(columns[0], columns[1]);
    for (let i = 1; i < columns.length - 1; i += 1) {
        const candidate = Math.max(columns[i], columns[i + 1]);
        if (candidate < best) {
            best = candidate;
            index = i;
        }
    }
    return index;
}

function layout(container, config) {
    const items = Array.from(container.querySelectorAll(config.itemSelector));
    if (!items.length) {
        ensureStyleElement(container.dataset.masonryId).textContent = '';
        return;
    }
    const width = container.clientWidth;
    if (!width) return;
    const gap = Number(config.gap || 14);
    const minColumnWidth = Number(config.minColumnWidth || 280);
    const count = Math.max(1, Math.floor((width + gap) / (minColumnWidth + gap)));
    const columnWidth = Math.floor((width - gap * (count - 1)) / count);
    const heights = Array(count).fill(0);
    const rules = [
        `.widgets-list[data-masonry-id="${container.dataset.masonryId}"]{height:0;}`,
    ];
    items.forEach((item, idx) => {
        const wantsSpan = item.classList.contains(config.spanClass || 'widgets-card-span-2');
        const span = wantsSpan && count > 1 ? 2 : 1;
        const col = span === 2 ? bestPair(heights) : shortestColumn(heights);
        const top = span === 2 ? Math.max(heights[col], heights[col + 1]) : heights[col];
        const left = col * (columnWidth + gap);
        const itemWidth = span * columnWidth + (span - 1) * gap;
        const height = item.offsetHeight;
        const bottom = top + height + gap;
        for (let i = col; i < col + span; i += 1) heights[i] = bottom;
        rules.push(
            `.widgets-list[data-masonry-id="${container.dataset.masonryId}"]>${config.itemSelector}:nth-child(${idx + 1}){width:${itemWidth}px;transform:translate(${left}px,${top}px);}`
        );
    });
    const maxHeight = Math.max(...heights, 0);
    rules[0] = `.widgets-list[data-masonry-id="${container.dataset.masonryId}"]{height:${Math.max(0, maxHeight - gap)}px;}`;
    ensureStyleElement(container.dataset.masonryId).textContent = rules.join('\n');
}

export function applyMasonry(container, options = {}) {
    if (!container) return;
    const config = {
        itemSelector: options.itemSelector || '.widgets-card',
        gap: options.gap ?? 14,
        minColumnWidth: options.minColumnWidth ?? 280,
        spanClass: options.spanClass || 'widgets-card-span-2',
    };
    if (!container.dataset.masonryId) {
        container.dataset.masonryId = `m${nextId}`;
        nextId += 1;
    }
    const run = () => requestAnimationFrame(() => layout(container, config));
    run();
    if (bound.has(container)) return;
    const resizeObserver = new ResizeObserver(run);
    resizeObserver.observe(container);
    const mutationObserver = new MutationObserver(run);
    mutationObserver.observe(container, { childList: true, subtree: true });
    bound.set(container, { resizeObserver, mutationObserver });
}
