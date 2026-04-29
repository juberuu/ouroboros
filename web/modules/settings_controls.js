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
