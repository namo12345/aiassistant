const STORAGE_KEY = "autopilot-chat-v2";
const THEME_KEY = "autopilot-theme";
const AUTOSPEAK_KEY = "autopilot-autospeak";

const META_LABELS = {
    count: "Emails",
    source_count: "Sources",
    filename: "File",
    recipient: "To",
    reminder_backend: "Saved in",
};

const BACKEND_LABELS = {
    google_calendar: "Google Calendar",
    gmail_fallback: "Gmail mailbox",
    local_fallback: "Local file",
};

const messagesEl = document.getElementById("messages");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const statusPill = document.getElementById("status-pill");
const themeToggle = document.getElementById("theme-toggle");
const autospeakToggle = document.getElementById("autospeak-toggle");
const newChatBtn = document.getElementById("new-chat");
const voiceButton = document.getElementById("voice-button");
const sendButton = chatForm.querySelector(".send-button");
const chips = document.querySelectorAll(".chip");
const modalBackdrop = document.getElementById("modal-backdrop");
const modalBody = document.getElementById("modal-body");
const modalTitle = document.getElementById("modal-title");
const modalClose = document.getElementById("modal-close");

// Auth + user menu elements
const loginScreen = document.getElementById("login-screen");
const appShell = document.getElementById("app-shell");
const userMenu = document.getElementById("user-menu");
const userAvatar = document.getElementById("user-avatar");
const userDropdown = document.getElementById("user-dropdown");
const userNameEl = document.getElementById("user-name");
const userEmailEl = document.getElementById("user-email");
const userAvatarLetter = document.getElementById("user-avatar-letter");
const logoutButton = document.getElementById("logout-button");

let messages = [];
let recognition = null;
let currentUtterance = null;
let autoSpeak = false;
let isWorking = false;
let currentUser = null;
let feedbackState = {}; // trace_id -> {reward, label}
let implicitRewardSent = {}; // trace_id -> true; track implicit rewards so we don't double-fire

// Implicit-feedback reward — a small positive signal sent when the user engages
// with a response (copy, speak, open a source/calendar link) but hasn't given
// an explicit thumbs-up/down yet. Keeps the bandit learning even when users
// don't bother to rate responses.
const IMPLICIT_REWARD = 0.3;

// -- Storage ---------------------------------------------------------------
function saveMessages() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(messages)); }
    catch { /* quota exceeded – ignore */ }
}
function loadMessages() {
    try {
        const raw = sessionStorage.getItem(STORAGE_KEY);
        return raw ? JSON.parse(raw) : [];
    } catch { return []; }
}

// -- Utils -----------------------------------------------------------------
function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
function linkify(escaped) {
    return escaped.replace(/(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
}
function renderRich(raw) {
    return linkify(escapeHtml(raw));
}
function setStatus(text) { statusPill.textContent = text; }
function setWorking(flag) {
    isWorking = flag;
    sendButton.disabled = flag;
    if (flag) setStatus("Thinking...");
}

function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
}
function applyAutoSpeak(enabled) {
    autoSpeak = enabled;
    autospeakToggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    localStorage.setItem(AUTOSPEAK_KEY, enabled ? "1" : "0");
}

function exportText(entry) {
    if (entry.role === "user") return entry.text;
    const r = entry.payload?.response;
    return r?.export_text || r?.text || "";
}

function downloadText(filename, text) {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
}

function buildErrorPayload(title, text, hint = "") {
    const full = hint ? `${text}\n\n${hint}` : text;
    return {
        ok: false,
        response: {
            title, text: full, items: [], meta: {}, sources: [],
            export_text: `${title}\n\n${full}`,
        },
    };
}

function friendlyErrorHint(text) {
    const lowered = (text || "").toLowerCase();
    if (lowered.includes("invalid_grant") || lowered.includes("missing google credentials") || lowered.includes("no refresh token")) {
        return "Tip: sign out and sign in again so Google can re-issue a fresh token with the required Gmail + Calendar scopes.";
    }
    if (lowered.includes("tavily_api_key")) {
        return "Tip: the research provider isn't configured on the server yet.";
    }
    if (lowered.includes("llm_api_key") || lowered.includes("llm is unavailable")) {
        return "Tip: the LLM provider is temporarily unavailable. Try again in a minute.";
    }
    if (lowered.includes("rate-limited")) {
        return "Tip: the free model is rate-limited. Wait about a minute and retry.";
    }
    return "";
}

// -- Rendering -------------------------------------------------------------
function metaChipsHtml(meta) {
    if (!meta) return "";
    const keys = Object.keys(meta).filter((k) => META_LABELS[k] && meta[k]);
    if (!keys.length) return "";
    return `<div class="meta-row">${keys.map((k) => {
        const raw = meta[k];
        const label = META_LABELS[k];
        const value = k === "reminder_backend" ? (BACKEND_LABELS[raw] || raw) : raw;
        return `<span class="meta-chip">${escapeHtml(label)}: ${escapeHtml(value)}</span>`;
    }).join("")}</div>`;
}

function itemsHtml(items) {
    if (!items || !items.length) return "";
    return `<div class="items">${items.map((it) => `
        <article class="item-card">
            ${it.title ? `<h4>${escapeHtml(it.title)}</h4>` : ""}
            ${it.subtitle ? `<p class="subtitle">${escapeHtml(it.subtitle)}</p>` : ""}
            ${it.body ? `<p class="body">${renderRich(it.body)}</p>` : ""}
        </article>`).join("")}</div>`;
}

function sourcesHtml(sources) {
    if (!sources || !sources.length) return "";
    return `<div class="sources">${sources.map((s) => `
        <div class="source">
            <span>${escapeHtml(s.title || "Source")}</span>
            ${s.url ? `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : ""}
        </div>`).join("")}</div>`;
}

function feedbackHtml(entry) {
    const traceId = entry.payload?.response?.meta?.trace_id;
    if (!traceId) return "";
    const state = feedbackState[traceId];
    const upActive = state?.label === "positive" ? " active" : "";
    const downActive = state?.label === "negative" ? " active" : "";
    const disabled = state ? " disabled" : "";
    return `<button type="button" data-act="feedback-up" class="feedback-btn${upActive}" title="Helpful"${disabled}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H7V10l4-9 1.5.5A2 2 0 0 1 14 4.5z"/></svg>
        </button>
        <button type="button" data-act="feedback-down" class="feedback-btn${downActive}" title="Not helpful"${disabled}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H17v12l-4 9-1.5-.5A2 2 0 0 1 10 19.5z"/></svg>
        </button>`;
}

function actionsHtml(entry) {
    const hasSpeech = "speechSynthesis" in window;
    const speakBtn = hasSpeech ? `<button type="button" data-act="speak">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>
        Speak</button>` : "";
    return `<div class="actions">
        ${speakBtn}
        <button type="button" data-act="copy">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            Copy</button>
        <button type="button" data-act="download">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            Download</button>
        ${feedbackHtml(entry)}
    </div>`;
}

function assistantRoleLabel(payload) {
    if (!payload?.ok) return "error";
    return "assistant";
}

function renderMessage(entry, idx) {
    if (entry.role === "user") {
        return `<article class="message user" data-idx="${idx}">
            <div class="avatar">You</div>
            <div class="bubble">
                <div class="role">You</div>
                <div class="text">${escapeHtml(entry.text)}</div>
            </div>
        </article>`;
    }
    if (entry.role === "typing") {
        return `<article class="message assistant" data-idx="${idx}">
            <div class="avatar">A</div>
            <div class="bubble">
                <div class="role">AutoPilot</div>
                <div class="typing"><span></span><span></span><span></span></div>
            </div>
        </article>`;
    }
    const r = entry.payload?.response || {};
    const roleClass = assistantRoleLabel(entry.payload);
    const roleLabel = entry.payload?.ok ? "AutoPilot" : "Issue";
    const isGenericGreeting = r.title === "AutoPilot AI";
    const title = (r.title && !isGenericGreeting) ? `<div class="title">${escapeHtml(r.title)}</div>` : "";
    const text = r.text ? `<div class="text">${renderRich(r.text)}</div>` : "";
    return `<article class="message ${roleClass}" data-idx="${idx}">
        <div class="avatar">${roleClass === "error" ? "!" : "A"}</div>
        <div class="bubble">
            <div class="role">${escapeHtml(roleLabel)}</div>
            ${title}${text}
            ${metaChipsHtml(r.meta)}
            ${itemsHtml(r.items)}
            ${sourcesHtml(r.sources)}
            ${actionsHtml(entry)}
        </div>
    </article>`;
}

function renderWelcome() {
    messagesEl.innerHTML = `<div class="welcome">
        <div class="welcome-mark">A</div>
        <h2>How can I help today?</h2>
        <p>Summarize your inbox, send an email, set a reminder, research any topic, or summarize a document. Tap a chip below, type a request, or hit the mic.</p>
    </div>`;
}

function render() {
    if (!messages.length) { renderWelcome(); return; }
    messagesEl.innerHTML = messages.map(renderMessage).join("");
    requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
}

function pushMessage(entry) {
    messages.push(entry);
    if (entry.role !== "typing") saveMessages();
    render();
    return messages.length - 1;
}

function replaceMessage(idx, entry) {
    messages[idx] = entry;
    saveMessages();
    render();
}

function removeMessage(idx) {
    messages.splice(idx, 1);
    saveMessages();
    render();
}

// -- Voice -----------------------------------------------------------------
function initVoice() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) { voiceButton.disabled = true; voiceButton.title = "Voice not supported in this browser"; return; }

    recognition = new Recognition();
    recognition.lang = "en-US";
    recognition.interimResults = true;
    recognition.continuous = true;
    recognition.maxAlternatives = 1;

    let finalText = "";
    let isListening = false;
    let userStopped = false;

    const startListening = () => {
        finalText = "";
        userStopped = false;
        try {
            recognition.start();
        } catch { /* already started */ }
    };

    const stopListening = () => {
        userStopped = true;
        try { recognition.stop(); } catch { /* already stopped */ }
    };

    recognition.addEventListener("start", () => {
        isListening = true;
        setStatus("Listening... click mic to stop");
        voiceButton.classList.add("recording");
        voiceButton.title = "Click to stop recording";
    });

    recognition.addEventListener("result", (event) => {
        let interim = "";
        for (let i = event.resultIndex; i < event.results.length; i++) {
            const res = event.results[i];
            if (res.isFinal) finalText += res[0].transcript + " ";
            else interim += res[0].transcript;
        }
        const combined = (finalText + interim).trim();
        if (combined) {
            chatInput.value = combined;
            autoResize();
        }
    });

    recognition.addEventListener("end", () => {
        isListening = false;
        voiceButton.classList.remove("recording");
        voiceButton.title = "Click to speak";
        const text = (finalText || chatInput.value).trim();
        if (userStopped && text) {
            chatInput.value = "";
            autoResize();
            runCommand(text);
        } else if (!userStopped && text) {
            setStatus("Paused — click mic again to keep recording, or press Enter to send");
        } else {
            setStatus("Ready");
        }
    });

    recognition.addEventListener("error", (event) => {
        isListening = false;
        voiceButton.classList.remove("recording");
        voiceButton.title = "Click to speak";
        if (event.error === "not-allowed" || event.error === "service-not-allowed") setStatus("Mic access denied");
        else if (event.error === "no-speech") setStatus("No speech detected — click mic and try again");
        else if (event.error === "aborted") setStatus("Ready");
        else if (event.error === "network") setStatus("Voice needs an internet connection");
        else setStatus("Voice error: " + event.error);
    });

    voiceButton.addEventListener("click", () => {
        if (isListening) stopListening();
        else startListening();
    });
}

function cancelSpeaking(button) {
    if (!window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    if (currentUtterance?.button) currentUtterance.button.classList.remove("speaking");
    currentUtterance = null;
    if (button) button.classList.remove("speaking");
    setStatus("Ready");
}

function speakText(text, button) {
    if (!window.speechSynthesis || !text) return;
    if (window.speechSynthesis.speaking) { cancelSpeaking(button); return; }
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.02;
    u.pitch = 1;
    u.onstart = () => { setStatus("Speaking..."); if (button) button.classList.add("speaking"); };
    u.onend = () => { setStatus("Ready"); if (button) button.classList.remove("speaking"); currentUtterance = null; };
    u.onerror = () => { setStatus("Ready"); if (button) button.classList.remove("speaking"); currentUtterance = null; };
    currentUtterance = { utterance: u, button };
    window.speechSynthesis.speak(u);
}

// -- API calls -------------------------------------------------------------
async function fetchJson(url, options = {}) {
    const merged = { credentials: "same-origin", ...options };
    const res = await fetch(url, merged);
    if (res.status === 401) {
        showLogin();
        return buildErrorPayload("Sign in required", "Please sign in with Google to continue.");
    }
    let payload;
    try { payload = await res.json(); }
    catch { payload = buildErrorPayload("Unexpected response", `Status ${res.status}`); }
    return payload;
}

function pushTyping() { return pushMessage({ role: "typing" }); }

function finalizeAssistant(typingIdx, payload, { userShownAt = null } = {}) {
    const entry = { role: "assistant", payload };
    replaceMessage(typingIdx, entry);
    setStatus(payload.ok ? "Ready" : "Needs attention");
    if (autoSpeak && payload.ok) {
        const text = payload.response?.text || "";
        if (text) speakText(text.slice(0, 800));
    }
    return entry;
}

function buildContextTurns() {
    const turns = [];
    for (const msg of messages) {
        if (msg.role === "user" && typeof msg.text === "string" && msg.text.trim()) {
            turns.push({ role: "user", text: msg.text.trim() });
        } else if (msg.role === "assistant" && msg.payload?.response) {
            const r = msg.payload.response;
            const snippet = (r.text || r.title || "").toString().trim();
            if (snippet) turns.push({ role: "assistant", text: snippet });
        }
    }
    return turns.slice(-4);
}

async function runCommand(text) {
    if (!text || isWorking) return;
    const contextTurns = buildContextTurns();
    pushMessage({ role: "user", text });
    const typingIdx = pushTyping();
    setWorking(true);
    try {
        const payload = await fetchJson("/api/command", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text, context: contextTurns }),
        });
        if (!payload.ok) {
            const hint = friendlyErrorHint(payload.response?.text);
            if (hint) payload.response.text = `${payload.response.text}\n\n${hint}`;
        }
        finalizeAssistant(typingIdx, payload);
    } catch (err) {
        replaceMessage(typingIdx, { role: "assistant", payload: buildErrorPayload("Request failed", err.message) });
        setStatus("Request failed");
    } finally {
        setWorking(false);
    }
}

async function runFeature(feature, body) {
    const userLabel = featureLabel(feature);
    pushMessage({ role: "user", text: userLabel });
    const typingIdx = pushTyping();
    setWorking(true);
    try {
        let payload;
        if (feature === "inbox") {
            payload = await fetchJson("/api/mail/summary");
        } else if (feature === "schedule") {
            payload = await fetchJson("/api/events/upcoming?days=7");
        } else if (feature === "briefing") {
            payload = await fetchJson("/api/briefing");
        } else if (feature === "email") {
            payload = await fetchJson("/api/email/send", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
        } else if (feature === "reminder") {
            payload = await fetchJson("/api/reminder/create", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
        } else if (feature === "research") {
            payload = await fetchJson("/api/research", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
        } else if (feature === "document") {
            payload = await fetchJson("/api/attachment/summarize", {
                method: "POST",
                body: body, // FormData
            });
        }
        if (!payload.ok) {
            const hint = friendlyErrorHint(payload.response?.text);
            if (hint) payload.response.text = `${payload.response.text}\n\n${hint}`;
        }
        finalizeAssistant(typingIdx, payload);
    } catch (err) {
        replaceMessage(typingIdx, { role: "assistant", payload: buildErrorPayload("Request failed", err.message) });
        setStatus("Request failed");
    } finally {
        setWorking(false);
    }
}

function featureLabel(feature) {
    switch (feature) {
        case "inbox": return "Summarize my inbox";
        case "schedule": return "Show my upcoming events";
        case "briefing": return "Give me today's briefing";
        case "email": return "Send an email";
        case "reminder": return "Set a reminder";
        case "research": return "Research a topic";
        case "document": return "Summarize a document";
        default: return "Run tool";
    }
}

// -- Modal / feature forms -------------------------------------------------
function openModal(title, innerHtml) {
    modalTitle.textContent = title;
    modalBody.innerHTML = innerHtml;
    modalBackdrop.hidden = false;
    const firstInput = modalBody.querySelector("input, textarea");
    if (firstInput) setTimeout(() => firstInput.focus(), 50);
}
function closeModal() {
    modalBackdrop.hidden = true;
    modalBody.innerHTML = "";
}

const FEATURE_FORMS = {
    email: () => `
        <label>Recipient <input type="email" name="recipient" placeholder="name@example.com" required></label>
        <label>Subject <input type="text" name="subject" placeholder="Project update"></label>
        <label>Message <textarea name="message" placeholder="Leave blank to auto-draft."></textarea></label>
        <label class="checkbox-row"><input type="checkbox" name="polish" checked> Polish with AI</label>
        <div class="modal-actions">
            <button type="button" class="btn-secondary" data-close>Cancel</button>
            <button type="submit" class="btn-primary">Send</button>
        </div>`,
    reminder: () => `
        <label>Title <input type="text" name="title" placeholder="Team sync" required></label>
        <label>When <input type="text" name="when" placeholder="tomorrow 6pm" required></label>
        <label>Description <textarea name="description" placeholder="Optional details"></textarea></label>
        <label>Duration (minutes) <input type="number" name="duration_minutes" min="15" max="720" value="60"></label>
        <div class="modal-actions">
            <button type="button" class="btn-secondary" data-close>Cancel</button>
            <button type="submit" class="btn-primary">Create</button>
        </div>`,
    research: () => `
        <label>Topic <input type="text" name="topic" placeholder="AI workflow automation" required></label>
        <div class="modal-actions">
            <button type="button" class="btn-secondary" data-close>Cancel</button>
            <button type="submit" class="btn-primary">Research</button>
        </div>`,
    document: () => `
        <label>File <input type="file" name="file" accept=".pdf,.docx,.csv,.txt,.md,.json,.html,.css,.js" required></label>
        <div class="modal-actions">
            <button type="button" class="btn-secondary" data-close>Cancel</button>
            <button type="submit" class="btn-primary">Summarize</button>
        </div>`,
};

const FEATURE_TITLES = {
    email: "Send email",
    reminder: "Set reminder",
    research: "Research topic",
    document: "Summarize document",
};

function handleFeatureClick(feature) {
    if (feature === "inbox" || feature === "schedule" || feature === "briefing") { runFeature(feature); return; }
    const formBuilder = FEATURE_FORMS[feature];
    if (!formBuilder) return;
    openModal(FEATURE_TITLES[feature], `<form id="feature-form" data-feature="${feature}">${formBuilder()}</form>`);
    const form = modalBody.querySelector("#feature-form");
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        let body;
        if (feature === "document") {
            body = new FormData(form);
        } else {
            const data = new FormData(form);
            body = {};
            data.forEach((v, k) => { body[k] = v; });
            if (feature === "email") body.polish = data.get("polish") === "on";
            if (feature === "reminder") body.duration_minutes = parseInt(body.duration_minutes || "60", 10);
        }
        closeModal();
        await runFeature(feature, body);
    });
}

// -- Composer resize -------------------------------------------------------
function autoResize() {
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 200) + "px";
}

// -- Event listeners -------------------------------------------------------
themeToggle.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});

autospeakToggle.addEventListener("click", () => applyAutoSpeak(!autoSpeak));

newChatBtn.addEventListener("click", () => {
    if (!messages.length) return;
    if (!confirm("Clear the current conversation?")) return;
    messages = [];
    saveMessages();
    cancelSpeaking();
    render();
    setStatus("Ready");
});

chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    autoResize();
    await runCommand(text);
});

chatInput.addEventListener("input", autoResize);
chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        chatForm.requestSubmit();
    }
});

chips.forEach((btn) => {
    btn.addEventListener("click", () => handleFeatureClick(btn.dataset.feature));
});

modalClose.addEventListener("click", closeModal);
modalBackdrop.addEventListener("click", (e) => {
    if (e.target === modalBackdrop || e.target.dataset.close !== undefined) closeModal();
});
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modalBackdrop.hidden) closeModal();
});

messagesEl.addEventListener("click", (e) => {
    // Implicit positive signal: the user clicked a source/citation/calendar link
    // inside an assistant response. Treat it as a small reward.
    const link = e.target.closest("a[href]");
    if (link && messagesEl.contains(link)) {
        const article = link.closest(".message.assistant");
        if (article) {
            const idx = Number(article.dataset.idx);
            const entry = messages[idx];
            if (entry) sendImplicitReward(entry, "link");
        }
    }

    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const article = btn.closest(".message");
    const idx = Number(article?.dataset.idx);
    const entry = messages[idx];
    if (!entry) return;
    const act = btn.dataset.act;
    if (act === "copy") {
        const text = exportText(entry);
        navigator.clipboard.writeText(text).then(() => setStatus("Copied"));
        sendImplicitReward(entry, "copy");
    } else if (act === "download") {
        const text = exportText(entry);
        downloadText("autopilot-response.txt", text);
        sendImplicitReward(entry, "download");
    } else if (act === "speak") {
        const text = exportText(entry);
        speakText(text, btn);
        sendImplicitReward(entry, "speak");
    } else if (act === "feedback-up" || act === "feedback-down") {
        submitFeedback(entry, act === "feedback-up" ? 1 : 0);
    }
});

async function sendImplicitReward(entry, source) {
    const traceId = entry?.payload?.response?.meta?.trace_id;
    if (!traceId) return;
    // Don't overwrite explicit thumbs-up/down, and only fire once per trace.
    if (feedbackState[traceId] || implicitRewardSent[traceId]) return;
    implicitRewardSent[traceId] = true;
    try {
        await fetch("/api/feedback", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                trace_id: traceId,
                reward: IMPLICIT_REWARD,
                label: "implicit_positive",
                comment: `implicit:${source}`,
            }),
        });
    } catch { /* fire-and-forget — never break the UX */ }
}

async function submitFeedback(entry, reward) {
    const traceId = entry.payload?.response?.meta?.trace_id;
    if (!traceId || feedbackState[traceId]) return;
    const label = reward >= 0.5 ? "positive" : "negative";
    feedbackState[traceId] = { reward, label };
    render();
    setStatus(reward >= 0.5 ? "Thanks — glad that helped!" : "Thanks — I'll try a different approach next time.");
    try {
        await fetchJson("/api/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ trace_id: traceId, reward, label }),
        });
    } catch {
        /* silently ignore — learning failures shouldn't bother the user */
    }
    setTimeout(() => { if (!isWorking) setStatus("Ready"); }, 2200);
}

// -- Auth & user menu ------------------------------------------------------
function showLogin() {
    if (loginScreen) loginScreen.hidden = false;
    if (appShell) appShell.hidden = true;
}

function showApp(user) {
    if (loginScreen) loginScreen.hidden = true;
    if (appShell) appShell.hidden = false;
    currentUser = user;
    if (userNameEl) userNameEl.textContent = user.name || user.email || "—";
    if (userEmailEl) userEmailEl.textContent = user.email || "—";
    if (userAvatarLetter) {
        const letterSource = (user.name || user.email || "?").trim();
        userAvatarLetter.textContent = (letterSource[0] || "?").toUpperCase();
    }
}

async function initAuth() {
    try {
        const res = await fetch("/api/me", { credentials: "same-origin" });
        if (res.ok) {
            const data = await res.json();
            if (data.ok && data.user) {
                showApp(data.user);
                return true;
            }
        }
    } catch {
        /* network error — fall through to login */
    }
    showLogin();
    return false;
}

if (userAvatar && userDropdown) {
    userAvatar.addEventListener("click", (e) => {
        e.stopPropagation();
        userDropdown.hidden = !userDropdown.hidden;
    });
    document.addEventListener("click", (e) => {
        if (!userDropdown.hidden && !userMenu.contains(e.target)) {
            userDropdown.hidden = true;
        }
    });
}

if (logoutButton) {
    logoutButton.addEventListener("click", async () => {
        try {
            await fetch("/logout", { method: "POST", credentials: "same-origin" });
        } catch { /* ignore */ }
        sessionStorage.removeItem(STORAGE_KEY);
        window.location.reload();
    });
}

// -- Bootstrap -------------------------------------------------------------
applyTheme(localStorage.getItem(THEME_KEY) || "light");
applyAutoSpeak(localStorage.getItem(AUTOSPEAK_KEY) === "1");
messages = loadMessages();
render();
initVoice();
autoResize();
initAuth();
