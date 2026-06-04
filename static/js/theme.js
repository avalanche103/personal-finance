(function () {
    const storageKey = 'pf-theme';
    const root = document.documentElement;

    function preferredTheme() {
        const stored = localStorage.getItem(storageKey);
        if (stored === 'light' || stored === 'dark') {
            return stored;
        }
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    function applyTheme(theme) {
        root.setAttribute('data-theme', theme);
        document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
            const isDark = theme === 'dark';
            button.setAttribute('aria-pressed', String(isDark));
            button.setAttribute('aria-label', isDark ? 'Switch to light theme' : 'Switch to dark theme');
            const label = button.querySelector('[data-theme-label]');
            if (label) {
                label.textContent = isDark ? 'Light' : 'Dark';
            }
        });
    }

    applyTheme(preferredTheme());

    document.addEventListener('click', function (event) {
        const toggle = event.target.closest('[data-theme-toggle]');
        if (!toggle) {
            return;
        }
        const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        localStorage.setItem(storageKey, next);
        applyTheme(next);
    });
})();
