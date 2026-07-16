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

export function planMasonryLayout(width, itemSpecs, options = {}) {
    const gap = Number(options.gap ?? 14);
    const minColumnWidth = Number(options.minColumnWidth ?? 280);
    const denseMinColumnWidth = Number(options.denseMinColumnWidth ?? 240);
    const spans = itemSpecs.map((item) => Number(item.span) >= 2 ? 2 : 1);
    const desiredColumns = spans.reduce((total, span) => total + span, 0);
    const availableColumns = Math.max(1, Math.floor((width + gap) / (minColumnWidth + gap)));
    let count = Math.min(desiredColumns, availableColumns);

    // Multiple wide cards cannot pack usefully into three tracks: every pair
    // overlaps an occupied track and leaves a tall visual void. When four
    // still-legible tracks fit, let wide cards sit side by side. One-wide and
    // narrow layouts retain the ordinary minimum width.
    const wideCount = spans.filter((span) => span === 2).length;
    const denseAvailableColumns = Math.max(
        1,
        Math.floor((width + gap) / (denseMinColumnWidth + gap)),
    );
    if (wideCount >= 2 && denseAvailableColumns >= 4) {
        count = Math.min(desiredColumns, denseAvailableColumns);
    }

    // On the common four-track desktop layout, one narrow card between
    // multiple wide cards otherwise occupies only half of the right lane.
    // That leaves a persistent visual hole and needlessly squeezes long
    // readouts. Treat the lone narrow span as a responsive width hint and
    // give it the same readable lane width as its wide neighbours.
    const effectiveSpans = [...spans];
    const narrowIndexes = spans
        .map((span, index) => span === 1 ? index : -1)
        .filter((index) => index >= 0);
    if (count === 4 && wideCount >= 2 && narrowIndexes.length === 1) {
        effectiveSpans[narrowIndexes[0]] = 2;
    }

    const columnWidth = Math.floor((width - gap * (count - 1)) / count);
    const heights = Array(count).fill(0);
    const placements = itemSpecs.map((item, index) => {
        const span = effectiveSpans[index] === 2 && count > 1 ? 2 : 1;
        const column = span === 2 ? bestPair(heights) : shortestColumn(heights);
        const top = span === 2
            ? Math.max(heights[column], heights[column + 1])
            : heights[column];
        const left = column * (columnWidth + gap);
        const itemWidth = span * columnWidth + (span - 1) * gap;
        const bottom = top + Math.max(0, Number(item.height) || 0) + gap;
        for (let i = column; i < column + span; i += 1) heights[i] = bottom;
        return { span, column, top, left, width: itemWidth };
    });
    return {
        columnCount: count,
        columnWidth,
        height: Math.max(0, Math.max(...heights, 0) - gap),
        placements,
    };
}

function layout(container, config) {
    const items = Array.from(container.querySelectorAll(config.itemSelector));
    if (!items.length) {
        ensureStyleElement(container.dataset.masonryId).textContent = '';
        return;
    }
    const width = container.clientWidth;
    if (!width) return;
    const spanClass = config.spanClass || 'widgets-card-span-2';
    const itemSpecs = items.map((item) => ({
        span: item.classList.contains(spanClass) ? 2 : 1,
        height: item.offsetHeight,
    }));
    const plan = planMasonryLayout(width, itemSpecs, config);
    const rules = [
        `.widgets-list[data-masonry-id="${container.dataset.masonryId}"]{height:0;}`,
    ];
    items.forEach((item, idx) => {
        const placement = plan.placements[idx];
        rules.push(
            `.widgets-list[data-masonry-id="${container.dataset.masonryId}"]>${config.itemSelector}:nth-child(${idx + 1}){width:${placement.width}px;transform:translate(${placement.left}px,${placement.top}px);}`
        );
    });
    rules[0] = `.widgets-list[data-masonry-id="${container.dataset.masonryId}"]{height:${plan.height}px;}`;
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
    const observedItems = new Set();
    const itemResizeObserver = new ResizeObserver(run);
    const observeItems = () => {
        Array.from(observedItems).forEach((item) => {
            if (container.contains(item)) return;
            itemResizeObserver.unobserve(item);
            observedItems.delete(item);
        });
        container.querySelectorAll(config.itemSelector).forEach((item) => {
            if (observedItems.has(item)) return;
            observedItems.add(item);
            itemResizeObserver.observe(item);
        });
    };
    observeItems();
    const resizeObserver = new ResizeObserver(run);
    resizeObserver.observe(container);
    const mutationObserver = new MutationObserver(() => {
        observeItems();
        run();
    });
    mutationObserver.observe(container, { childList: true, subtree: true });
    bound.set(container, { resizeObserver, itemResizeObserver, mutationObserver });
}
