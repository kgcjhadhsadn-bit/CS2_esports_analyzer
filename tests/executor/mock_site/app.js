// Mock "Betclic-like" single page app for the executor's browser tests (local only).
// The server (tests/executor/mock_betclic.py) owns all state; this script renders it and forwards
// every user action to /api/*, so the tests can assert what was clicked (e.g. the final button).
"use strict";

const ui = { data: null, stakeText: "", receipt: null, oddsDialog: null, errorText: "" };

async function api(path, body) {
  const opts = body === undefined
    ? { cache: "no-store" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  return res.json();
}

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else if (key === "oninput") node.addEventListener("input", value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function parsePl(text) {
  const cleaned = String(text || "").replace(/\s/g, "").replace(",", ".");
  const value = parseFloat(cleaned);
  return Number.isFinite(value) ? value : null;
}

// ------------------------------------------------------------------------------------- header

function renderHeader() {
  const d = ui.data;
  const header = document.getElementById("header");
  header.innerHTML = "";
  header.appendChild(h("a", { href: "/", class: "header_logo", text: "Betclic" }));
  header.appendChild(h("a", { href: "/e-sport-s102", class: "header_link", text: "E-sport" }));
  if (d.logged_in) {
    header.appendChild(h("a", { href: "/moje-zaklady", class: "header_link", text: "Moje zakłady" }));
    header.appendChild(
      h("div", { class: "header_account" }, h("span", { class: "header_balance", text: d.balance_text }))
    );
  } else {
    header.appendChild(h("button", { class: "btn is-secondary header_loginButton", onclick: openLogin, text: "Zaloguj się" }));
    header.appendChild(h("button", { class: "btn is-primary", text: "Zarejestruj się" }));
  }
}

function openLogin() {
  if (document.getElementById("loginModal")) return;
  const sendForm = () => api("/api/login_form", {
    username: document.getElementById("loginUser").value,
    password: document.getElementById("loginPass").value,
  });
  const modal = h("div", { id: "loginModal", class: "modal", role: "dialog", "aria-label": "Logowanie" },
    h("h2", { text: "Logowanie" }),
    h("input", { id: "loginUser", name: "login", autocomplete: "username", placeholder: "E-mail lub login", oninput: sendForm }),
    h("input", { id: "loginPass", type: "password", autocomplete: "current-password", placeholder: "Hasło", oninput: sendForm }),
    h("button", { id: "loginSubmit", class: "btn is-primary", text: "Zaloguj się", onclick: () => api("/api/login", {}) }),
  );
  document.body.appendChild(modal);
}

function closeLogin() {
  const modal = document.getElementById("loginModal");
  if (modal) modal.remove();
}

// --------------------------------------------------------------------------------------- pages

function renderHome(main) {
  main.appendChild(h("h1", { text: "Zakłady bukmacherskie" }));
  main.appendChild(h("a", { href: "/e-sport-s102", text: "Zobacz e-sport" }));
}

function renderListing(main, events) {
  main.appendChild(h("h1", { text: "E-sport - Counter-Strike 2" }));
  for (const ev of events) {
    main.appendChild(
      h("sports-events-event", { class: "groupEvents_card" },
        h("a", { class: "cardEvent", href: ev.path },
          h("span", { class: "scoreboard_contestantLabel", text: ev.team1 }),
          " - ",
          h("span", { class: "scoreboard_contestantLabel", text: ev.team2 }),
        ),
      ),
    );
  }
}

function renderEvent(main, ev) {
  main.appendChild(h("h1", { class: "scoreboard", text: `${ev.team1} - ${ev.team2}` }));
  const showAll = ui.showAllMarkets === true;
  const tabs = h("div", { role: "tablist", class: "marketTabs" },
    h("button", { role: "tab", "aria-selected": String(!showAll), text: "Top", onclick: () => { ui.showAllMarkets = false; render(); } }),
    h("button", { role: "tab", "aria-selected": String(showAll), text: "Wszystkie", onclick: () => { ui.showAllMarkets = true; render(); } }),
  );
  main.appendChild(tabs);
  const container = h("sports-match-markets", { class: "matchMarkets" });
  for (const market of ev.markets) {
    if (market.only_in_all && !showAll) continue;
    const line = h("div", { class: "marketBox_lineSelection" });
    for (const sel of market.selections) {
      const cls = "btn is-odd" + (sel.suspended ? " is-locked" : "");
      const button = h("sports-selections-selection", {
        class: cls,
        role: "button",
        tabindex: "0",
        "data-selection-id": sel.id,
        "aria-disabled": sel.suspended ? "true" : null,
        onclick: () => { if (!sel.suspended) addToSlip(sel.id); },
      },
        h("span", { class: "btn_label is-top", text: sel.name }),
        h("span", { class: "btn_label" }, h("span", { class: "oddValue", text: sel.odds })),
        sel.suspended ? h("span", { class: "icon_lock", "aria-hidden": "true" }) : null,
      );
      line.appendChild(button);
    }
    container.appendChild(
      h("sports-markets-single-market", { class: "marketBox", "data-market-id": market.id },
        h("div", { class: "marketBox_head" }, h("h2", { class: "marketBox_headTitle", text: market.name })),
        h("div", { class: "marketBox_body" }, line),
      ),
    );
  }
  main.appendChild(container);
}

function renderHistory(main, bets) {
  main.appendChild(h("h1", { text: "Moje zakłady" }));
  main.appendChild(h("div", { role: "tablist" },
    h("button", { role: "tab", "aria-selected": "true", text: "Wszystkie" }),
    h("button", { role: "tab", "aria-selected": "false", text: "Rozliczone" }),
  ));
  if (!bets.length) {
    main.appendChild(h("p", { class: "myBets_empty", text: "Brak zakładów" }));
    return;
  }
  const pageSize = ui.data.history_page_size;
  const shown = Math.min(bets.length, ui.historyShown || pageSize);
  const list = h("div", { class: "myBets_list" });
  for (const bet of bets.slice(0, shown)) {
    list.appendChild(
      h("div", { class: "myBets_item", "data-bet-id": bet.bet_id },
        h("div", { class: "myBets_head" },
          h("span", { class: "myBets_date", text: bet.date_text }),
          h("span", { class: "myBets_betId", text: `Nr zakładu: ${bet.bet_id}` }),
          h("span", { class: "myBets_status", text: bet.status }),
        ),
        h("div", { class: "myBets_event", text: bet.event }),
        h("div", { class: "myBets_market", text: bet.market }),
        h("div", { class: "myBets_selection", text: bet.selection }),
        h("div", { class: "myBets_odds", text: `Kurs: ${bet.odds}` }),
        h("div", { class: "myBets_stake", text: `Stawka: ${bet.stake_text}` }),
        h("div", { class: "myBets_payout", text: bet.payout_text }),
      ),
    );
  }
  main.appendChild(list);
  if (shown < bets.length) {
    main.appendChild(h("button", { class: "btn", text: "Pokaż więcej", onclick: () => {
      ui.historyShown = shown + pageSize;
      render();
    } }));
  }
}

// ------------------------------------------------------------------------------------ bet slip

function renderSlip(items) {
  const slip = document.getElementById("slip");
  slip.innerHTML = "";
  const head = h("div", { class: "betslip_head" },
    h("span", { class: "betslip_title", text: "Kupon" }),
    h("span", { class: "betslip_count", text: String(items.length) }),
  );
  if (items.length) {
    head.appendChild(h("button", { class: "btn is-text betslip_clear", text: "Wyczyść", onclick: clearSlip }));
  }
  slip.appendChild(head);
  if (!items.length) {
    slip.appendChild(h("p", { class: "betslip_empty", text: "Twój kupon jest pusty" }));
  }
  for (const item of items) {
    slip.appendChild(
      h("div", { class: "betBox", "data-selection-id": item.selection_id },
        h("button", { class: "betBox_close", "aria-label": "Usuń zakład", text: "×", onclick: () => removeFromSlip(item.selection_id) }),
        h("div", { class: "betBox_contestantName", text: item.selection }),
        h("div", { class: "betBox_marketName", text: item.market }),
        h("div", { class: "betBox_matchName", text: item.event }),
        h("div", { class: "betBox_odds" }, h("span", { class: "oddValue", text: item.odds })),
      ),
    );
  }
  if (items.length) {
    const input = h("input", {
      id: "stake", class: "input_field", inputmode: "decimal", "aria-label": "Stawka", autocomplete: "off",
      oninput: (e) => { ui.stakeText = e.target.value; validateStake(); },
    });
    input.value = ui.stakeText;
    slip.appendChild(h("div", { class: "betslip_stake" }, h("label", { for: "stake", text: "Stawka" }), input, " zł"));
    const error = h("div", { id: "slipError", class: "betslip_error", role: "alert", text: ui.errorText });
    error.hidden = !ui.errorText;
    slip.appendChild(error);
    if (ui.oddsDialog) {
      slip.appendChild(
        h("div", { id: "oddsChange", class: "betslip_oddsChange" },
          "Kurs uległ zmianie: ",
          h("span", { class: "oddValue", text: ui.oddsDialog.new_odds }),
          h("button", { class: "btn", text: "Akceptuj zmiany", onclick: acceptOdds }),
          h("button", { class: "btn", text: "Anuluj", onclick: declineOdds }),
        ),
      );
    }
    const pending = h("div", { id: "slipPending", class: "betslip_pending", text: "Przetwarzanie zakładu…" });
    pending.hidden = !ui.pending;
    slip.appendChild(pending);
    const button = h("button", { id: "placeBet", class: "btn is-primary betslip_placeButton", text: "Postaw zakład", onclick: () => placeBet(false) });
    button.disabled = Boolean(ui.pending || ui.oddsDialog || ui.stakeBlocked);
    slip.appendChild(button);
  }
  if (ui.receipt) {
    slip.appendChild(
      h("div", { class: "betslip_receipt" },
        "Zakład przyjęty! Numer zakładu: ",
        h("span", { class: "betslip_receiptId", text: ui.receipt.bet_id }),
        h("button", { class: "btn betslip_receiptClose", text: "Zamknij", onclick: closeReceipt }),
      ),
    );
  }
}

async function closeReceipt() {
  await api("/api/receipt/close", {});
  ui.receipt = null;
  renderSlip(ui.data.slip);
}

function validateStake() {
  const value = parsePl(ui.stakeText);
  const max = parsePl(ui.data.max_stake_text);
  ui.stakeBlocked = value !== null && max !== null && value > max;
  ui.errorText = ui.stakeBlocked ? `Maksymalna stawka dla tego zakładu wynosi ${ui.data.max_stake_text} zł` : "";
  const error = document.getElementById("slipError");
  if (error) { error.textContent = ui.errorText; error.hidden = !ui.errorText; }
  const button = document.getElementById("placeBet");
  if (button) button.disabled = Boolean(ui.pending || ui.oddsDialog || ui.stakeBlocked);
}

async function addToSlip(selectionId) {
  const r = await api("/api/slip/add", { selection_id: selectionId });
  ui.data.slip = r.slip;
  ui.errorText = r.error || "";
  renderSlip(ui.data.slip);
}

async function removeFromSlip(selectionId) {
  const r = await api("/api/slip/remove", { selection_id: selectionId });
  ui.data.slip = r.slip;
  renderSlip(ui.data.slip);
}

async function clearSlip() {
  const r = await api("/api/slip/clear", {});
  ui.data.slip = r.slip;
  ui.errorText = "";
  ui.oddsDialog = null;
  ui.stakeText = "";
  renderSlip(ui.data.slip);
}

function applyPlaceResponse(r) {
  ui.pending = r.result === "hang";
  if (r.slip) ui.data.slip = r.slip;
  if (r.result === "ok") {
    ui.receipt = { bet_id: r.bet_id };
    ui.stakeText = "";
    ui.errorText = "";
    ui.oddsDialog = null;
    ui.data.balance_text = r.balance_text;
    renderHeader();
  } else if (r.result === "odds_changed") {
    ui.oddsDialog = { new_odds: r.new_odds };
  } else if (r.result === "error") {
    ui.errorText = r.message;
    ui.oddsDialog = null;
  }
  renderSlip(ui.data.slip);
}

async function placeBet(accept) {
  ui.pending = true;
  renderSlip(ui.data.slip);
  const r = await api("/api/place", { stake_text: ui.stakeText, accept: accept });
  applyPlaceResponse(r);
}

async function acceptOdds() {
  if (ui.data.accept_mode === "places") {
    ui.oddsDialog = null;
    await placeBet(true);
  } else {
    await api("/api/accept_odds", {});
    ui.oddsDialog = null;
    renderSlip(ui.data.slip);
  }
}

async function declineOdds() {
  await api("/api/decline", {});
  ui.oddsDialog = null;
  renderSlip(ui.data.slip);
}

// -------------------------------------------------------------------------------------- render

function render() {
  const main = document.getElementById("main");
  main.innerHTML = "";
  const page = ui.data.page;
  if (page.kind === "home") renderHome(main);
  else if (page.kind === "listing") renderListing(main, page.events);
  else if (page.kind === "event") renderEvent(main, page.event);
  else if (page.kind === "history") renderHistory(main, page.bets);
  else main.appendChild(h("h1", { text: "Nie znaleziono strony" }));
  renderHeader();
  renderSlip(ui.data.slip);
}

async function boot() {
  ui.data = await api("/api/state?path=" + encodeURIComponent(location.pathname));
  ui.receipt = ui.data.receipt ? { bet_id: ui.data.receipt } : null;
  render();
  // Poll the session like a real site (login finished in another flow, balance updates).
  setInterval(async () => {
    try {
      const s = await api("/api/session");
      if (s.logged_in !== ui.data.logged_in || s.balance_text !== ui.data.balance_text) {
        ui.data.logged_in = s.logged_in;
        ui.data.balance_text = s.balance_text;
        renderHeader();
        if (s.logged_in) closeLogin();
      }
      // live odds updates in the slip (like the real site's push updates), without re-rendering inputs
      for (const box of document.querySelectorAll(".betBox")) {
        const odds = (s.slip_odds || {})[box.getAttribute("data-selection-id")];
        const el = box.querySelector(".oddValue");
        if (odds && el && el.textContent !== odds) el.textContent = odds;
      }
    } catch (e) { /* server stopped */ }
  }, 300);
}

boot();
