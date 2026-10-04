// agency-os dashboard — interactivity
document.addEventListener('DOMContentLoaded', () => {
    // Auto-submit filter forms on select change. Only GET forms are filters:
    // a POST form (log a call, change a stage) waits for its own button, so
    // picking an outcome doesn't save the call before the notes are written.
    document.querySelectorAll('.filter-select').forEach(sel => {
        sel.addEventListener('change', () => {
            const form = sel.closest('form');
            if (form && (form.getAttribute('method') || 'get').toLowerCase() === 'get') form.submit();
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

// "Ask an agent" panel on the prospect page (only rendered when AI is on).
document.addEventListener('DOMContentLoaded', () => {
    const panel = document.getElementById('agent-panel');
    const form = document.getElementById('agent-form');
    if (!panel || !form) return;
    const prospect = panel.dataset.prospect;
    const agentSelect = document.getElementById('agent-select');
    const taskSelect = document.getElementById('agent-task');
    const result = document.getElementById('agent-result');
    const output = document.getElementById('agent-output');
    const button = form.querySelector('button[type=submit]');

    // Offer only the tasks the chosen agent does.
    const syncTasks = () => {
        const allowed = (agentSelect.selectedOptions[0].dataset.tasks || '').split(',');
        let first = null;
        for (const opt of taskSelect.options) {
            opt.hidden = !allowed.includes(opt.value);
            if (!opt.hidden && !first) first = opt;
        }
        if (taskSelect.selectedOptions[0].hidden && first) taskSelect.value = first.value;
    };
    agentSelect.addEventListener('change', syncTasks);
    syncTasks();

    const post = (path, data) => fetch(path, {
        method: 'POST', headers: { 'X-AOS-Tool': '1' }, body: new URLSearchParams(data),
    }).then(r => r.json()).catch(() => ({ ok: false, error: 'Network error' }));

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        button.disabled = true;
        button.textContent = 'Drafting…';
        const reply = await post(`/prospects/${prospect}/agent`, new FormData(form));
        button.disabled = false;
        button.textContent = 'Draft';
        result.hidden = false;
        output.textContent = reply.ok ? reply.text : `Couldn't draft: ${reply.error}`;
        output.dataset.ok = reply.ok ? '1' : '';
    });

    document.getElementById('agent-copy')?.addEventListener('click', () => {
        navigator.clipboard?.writeText(output.textContent);
    });
    document.getElementById('agent-save')?.addEventListener('click', async (event) => {
        if (!output.dataset.ok) return;
        const reply = await post(`/prospects/${prospect}/agent/note`, { note: output.textContent });
        event.target.textContent = reply.ok ? 'Saved' : 'Not saved';
        if (reply.ok) setTimeout(() => location.reload(), 600);
    });
});

// Copy buttons: <button data-copy="text">
document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-copy]');
    if (!button) return;
    const label = button.textContent;
    try {
        await navigator.clipboard.writeText(button.dataset.copy);
        button.textContent = 'Copied';
    } catch {
        button.textContent = 'Select and copy';
    }
    setTimeout(() => { button.textContent = label; }, 1500);
});

// Tap-to-call: the tel: link hands the call to the phone; then bring up the call log
// form so the outcome is quick to record when the call ends.
document.addEventListener('click', (event) => {
    const link = event.target.closest('a.call-link[data-call-log]');
    if (!link) return;
    const form = document.querySelector(link.dataset.callLog);
    if (!form) return;
    setTimeout(() => {
        form.scrollIntoView({ behavior: 'smooth', block: 'start' });
        form.classList.add('call-log-highlight');
        setTimeout(() => form.classList.remove('call-log-highlight'), 2500);
    }, 400);
});
