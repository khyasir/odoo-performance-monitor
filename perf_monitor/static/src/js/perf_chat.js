/** @odoo-module **/
/**
 * AI Chat tab of perf.issue: small progressive enhancements, no widget. Without this file the tab still works
 * with its plain buttons (Send = the action_ai_chat_send object button, which saves the draft first).
 * - Enter in the composer clicks Send, Shift+Enter inserts a new line;
 * - the composer grows with its text (capped by max-height in perf_chat.scss, then it scrolls);
 * - the message area scrolls to the last message when it is shown or when messages change;
 * - the example questions of an empty chat fill the composer;
 * - while the AI works (the Send button runs one long server call): the question shows at once as a bubble, an
 *   animated "thinking" row with a seconds counter appears and the Send button turns into a spinner. All of it is
 *   removed when the call ends (new messages arrive, the button is enabled again, an error dialog opens, or after
 *   5 minutes). Texts are put in with textContent, never as HTML.
 */

const COMPOSER = ".o_perf_chat_composer textarea";
const MAX_COMPOSER_PX = 160;

function grow(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, MAX_COMPOSER_PX) + "px";
}

const BUSY_MAX_MS = 300000;
let busy = null; // {panel, button, started, sawDisabled, key, timer}

function threadKey(panel) {
    const thread = panel.querySelector(".o_perf_chat_thread");
    return thread ? String(thread.childElementCount) + ":" + thread.textContent.length : "";
}

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) {
        node.className = cls;
    }
    if (text) {
        node.textContent = text;
    }
    return node;
}

function startBusy(panel, button, question) {
    const pending = panel.querySelector(".o_perf_chat_pending");
    if (busy || !pending) {
        return;
    }
    pending.replaceChildren();
    pending.appendChild(el("div", "o_perf_chat_pending_user", question));
    const ai = el("div", "o_perf_chat_pending_ai");
    const avatar = el("span", "o_perf_chat_pending_avatar");
    avatar.appendChild(el("i", "fa fa-magic"));
    const dots = el("span", "o_perf_chat_dots");
    for (let i = 0; i < 3; i++) {
        dots.appendChild(document.createElement("span"));
    }
    const label = el("span", "o_perf_chat_pending_label", "AI is thinking... 0s");
    ai.append(avatar, dots, label);
    pending.appendChild(ai);
    pending.classList.remove("d-none");
    panel.classList.add("o_perf_chat_busy");
    const scroll = panel.querySelector(".o_perf_chat_scroll");
    if (scroll) {
        scroll.scrollTop = scroll.scrollHeight;
    }
    busy = { panel, button, started: Date.now(), sawDisabled: false, key: threadKey(panel) };
    busy.timer = window.setInterval(() => {
        if (!busy) {
            return;
        }
        const secs = Math.floor((Date.now() - busy.started) / 1000);
        label.textContent = "AI is thinking... " + secs + "s" + (secs > 20 ? " (long answers take up to a minute)" : "");
        checkBusy();
    }, 1000);
}

function stopBusy() {
    if (!busy) {
        return;
    }
    window.clearInterval(busy.timer);
    const { panel } = busy;
    busy = null;
    panel.classList.remove("o_perf_chat_busy");
    const pending = panel.querySelector(".o_perf_chat_pending");
    if (pending) {
        pending.replaceChildren();
        pending.classList.add("d-none");
    }
}

function checkBusy() {
    if (!busy) {
        return;
    }
    if (!busy.panel.isConnected || Date.now() - busy.started > BUSY_MAX_MS) {
        stopBusy();
        return;
    }
    const sinceStart = Date.now() - busy.started;
    if (busy.button.disabled) {
        busy.sawDisabled = true;
    }
    const finished =
        (busy.sawDisabled && !busy.button.disabled) ||
        threadKey(busy.panel) !== busy.key ||
        (sinceStart > 800 && document.querySelector(".modal.o_technical_modal, .o_dialog .modal"));
    if (finished) {
        stopBusy();
    }
}

function sendButton(el) {
    const panel = el.closest(".o_perf_chat_panel");
    return panel && panel.querySelector("button[name='action_ai_chat_send']");
}

document.addEventListener(
    "keydown",
    (ev) => {
        const textarea = ev.target;
        if (
            ev.key !== "Enter" || ev.shiftKey || ev.ctrlKey || ev.altKey || ev.metaKey || ev.isComposing ||
            !(textarea instanceof HTMLTextAreaElement) || !textarea.matches(COMPOSER)
        ) {
            return;
        }
        ev.preventDefault();
        ev.stopPropagation();
        const button = sendButton(textarea);
        if (!textarea.value.trim() || !button || button.disabled) {
            return;
        }
        if (busy) {
            return;
        }
        // commit the draft to the record (the button saves the record before calling the method)
        textarea.dispatchEvent(new Event("change", { bubbles: true }));
        button.click(); // the click handler below starts the pending state
    },
    true
);

// bubbling phase: runs after the field's own autoresize listener, so the cap applies
document.addEventListener("input", (ev) => {
    if (ev.target instanceof HTMLTextAreaElement && ev.target.matches(COMPOSER)) {
        grow(ev.target);
    }
});

// the round Send button (mouse click): show the pending state at once, then let Odoo run the action
document.addEventListener(
    "click",
    (ev) => {
        const button = ev.target instanceof Element && ev.target.closest("button.o_perf_chat_send");
        if (!button) {
            return;
        }
        const panel = button.closest(".o_perf_chat_panel");
        const textarea = panel && panel.querySelector(COMPOSER);
        if (busy) {
            ev.preventDefault();
            ev.stopPropagation();
            return;
        }
        if (textarea && textarea.value.trim()) {
            startBusy(panel, button, textarea.value.trim());
        }
    },
    true
);

document.addEventListener("click", (ev) => {
    const chip = ev.target instanceof Element && ev.target.closest(".o_perf_chat_chip");
    const panel = chip && chip.closest(".o_perf_chat_panel");
    const textarea = panel && panel.querySelector(COMPOSER);
    if (!textarea || textarea.readOnly || textarea.disabled) {
        return;
    }
    textarea.value = chip.dataset.question || chip.textContent;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.focus();
});

let scheduled = false;
function refresh() {
    scheduled = false;
    checkBusy();
    for (const scroll of document.querySelectorAll(".o_perf_chat_scroll")) {
        // scroll down only when the thread changed (a new message, another chat), not on every re-render
        const thread = scroll.querySelector(".o_perf_chat_thread");
        const key = thread ? String(thread.childElementCount) + ":" + thread.textContent.length : "";
        if (scroll.dataset.perfChatKey !== key) {
            scroll.dataset.perfChatKey = key;
            scroll.scrollTop = scroll.scrollHeight;
        }
    }
    for (const textarea of document.querySelectorAll(COMPOSER)) {
        if (!textarea.dataset.perfChatGrown) {
            textarea.dataset.perfChatGrown = "1";
            grow(textarea);
        }
    }
}

function observe() {
    new MutationObserver(() => {
        if (!scheduled) {
            scheduled = true;
            window.requestAnimationFrame(refresh);
        }
    }).observe(document.body, { childList: true, subtree: true });
}

if (document.body) {
    observe();
} else {
    document.addEventListener("DOMContentLoaded", observe);
}
