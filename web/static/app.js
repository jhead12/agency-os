// agency-os dashboard — minor interactivity
document.addEventListener('DOMContentLoaded', () => {
    // Auto-submit filter form on select change
    document.querySelectorAll('.filter-select').forEach(sel => {
        sel.addEventListener('change', () => {
            const form = sel.closest('form');
            if (form) form.submit();
        });
    });
});