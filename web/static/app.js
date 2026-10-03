// agency-os dashboard — interactivity
document.addEventListener('DOMContentLoaded', () => {
    // Auto-submit filter form on select change
    document.querySelectorAll('.filter-select').forEach(sel => {
        sel.addEventListener('change', () => {
            const form = sel.closest('form');
            if (form) form.submit();
        });
    });

    // Navigation: mobile hamburger panel plus grouped disclosures.
    // Native <details> groups work with keyboard and without JavaScript.
    const navToggle = document.getElementById('nav-toggle');
    const navLinks = document.getElementById('nav-links');
    const groups = Array.from(document.querySelectorAll('.nav-group'));
    const closeGroups = () => groups.forEach(group => { group.open = false; });
    const mobileNavOpen = () => !!navLinks && navLinks.classList.contains('nav-open');
    const setMobileNav = open => {
        if (!navToggle || !navLinks) return;
        navLinks.classList.toggle('nav-open', open);
        navToggle.classList.toggle('nav-toggle-active', open);
        navToggle.setAttribute('aria-expanded', String(open));
        // Show the current page's section instead of a fully collapsed menu.
        if (open) groups.forEach(group => { group.open = group.classList.contains('nav-group-active'); });
    };

    if (navToggle && navLinks) {
        navToggle.addEventListener('click', () => setMobileNav(!mobileNavOpen()));
        navLinks.querySelectorAll('a').forEach(link => {
            link.addEventListener('click', () => setMobileNav(false));
        });
    }

    groups.forEach(group => {
        group.querySelector('summary').addEventListener('click', () => {
            if (!group.open) groups.forEach(other => { if (other !== group) other.open = false; });
        });
        // Close a dropdown when keyboard focus moves out of it.
        group.addEventListener('focusout', event => {
            if (event.relatedTarget && !group.contains(event.relatedTarget)) group.open = false;
        });
    });

    document.addEventListener('click', event => {
        if (!event.target.closest('.nav-group, #nav-toggle')) closeGroups();
        if (mobileNavOpen() && !event.target.closest('#nav-links, #nav-toggle')) setMobileNav(false);
    });

    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        const openGroup = groups.find(group => group.open);
        if (openGroup && !mobileNavOpen()) {
            closeGroups();
            openGroup.querySelector('summary').focus();
        } else if (mobileNavOpen()) {
            setMobileNav(false);
            navToggle.focus();
        }
    });
});
