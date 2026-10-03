// agency-os dashboard — interactivity
document.addEventListener('DOMContentLoaded', () => {
    // Auto-submit filter form on select change
    document.querySelectorAll('.filter-select').forEach(sel => {
        sel.addEventListener('change', () => {
            const form = sel.closest('form');
            if (form) form.submit();
        });
    });

    // Mobile nav toggle
    const navToggle = document.getElementById('nav-toggle');
    const navLinks = document.getElementById('nav-links');
    if (navToggle && navLinks) {
        navToggle.addEventListener('click', () => {
            navLinks.classList.toggle('nav-open');
            navToggle.classList.toggle('nav-toggle-active');
            navToggle.setAttribute('aria-expanded', String(navLinks.classList.contains('nav-open')));
        });
        // Close menu when a link is clicked (mobile)
        navLinks.querySelectorAll('a').forEach(link => {
            link.addEventListener('click', () => {
                navLinks.classList.remove('nav-open');
                navToggle.classList.remove('nav-toggle-active');
                navToggle.setAttribute('aria-expanded', 'false');
            });
        });
    }
});
// Native disclosures work with keyboard and without JavaScript.
document.addEventListener('DOMContentLoaded', () => {
    const groups = Array.from(document.querySelectorAll('.nav-group'));
    const closeGroups = () => groups.forEach(group => { group.open = false; });
    groups.forEach(group => {
        group.querySelector('summary').addEventListener('click', () => {
            if (!group.open) groups.forEach(other => { if (other !== group) other.open = false; });
        });
    });
    document.addEventListener('click', event => {
        if (!event.target.closest('.nav-group')) closeGroups();
    });
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        const openGroup = groups.find(group => group.open);
        if (openGroup) {
            closeGroups();
            openGroup.querySelector('summary').focus();
        } else {
            const nav = document.getElementById('nav-links');
            const toggle = document.getElementById('nav-toggle');
            if (nav && nav.classList.contains('nav-open')) {
                nav.classList.remove('nav-open');
                toggle.classList.remove('nav-toggle-active');
                toggle.setAttribute('aria-expanded', 'false');
                toggle.focus();
            }
        }
    });
});
