const API = "http://localhost:8000";

const $ = (id) => document.getElementById(id);
const deckSelect = $("deckSelect");

async function api(path, opts={}) {
  const res = await fetch(API + path, opts);
  if (!res.ok) throw new Error(await res.text());
  return res.headers.get("content-type")?.includes("application/json") ? res.json() : res.text();
}

async function refreshDecks() {
  const decks = await api("/decks");
  deckSelect.innerHTML = "";
  decks.forEach(d => {
    const opt = document.createElement("option");
    opt.value = d.id; opt.textContent = d.name;
    deckSelect.appendChild(opt);
  });
  if (!decks.length) return;
  refreshCards();
  refreshStats();
}

async function createDeck() {
  const name = $("deckName").value.trim();
  if (!name) return;
  await api("/decks", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({name})
  });
  $("deckName").value = "";
  refreshDecks();
}

async function addCard() {
  const deck_id = Number(deckSelect.value);
  const tag = $("tag").value.trim() || "general";
  const question = $("question").value.trim();
  const answer = $("answer").value.trim();
  if (!deck_id || !question || !answer) return;
  await api("/cards", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({deck_id, tag, question, answer})
  });
  $("question").value = ""; $("answer").value = "";
  refreshCards();
  refreshStats();
}

async function loadNext() {
  const deck_id = Number(deckSelect.value);
  const tag = $("tagFilter").value.trim();
  const q = new URLSearchParams({ deck_id, ...(tag && {tag}) }).toString();
  const card = await api(`/review/next?${q}`);
  const qBox = $("reviewQ"), aBox = $("reviewA");
  if (!card) {
    qBox.textContent = "Nothing due yet—nice! Add cards or wait until due.";
    aBox.classList.add("hidden");
    aBox.textContent = "";
    qBox.dataset.cardId = "";
    return;
  }
  qBox.textContent = card.question;
  aBox.textContent = card.answer;
  aBox.classList.add("hidden");
  qBox.dataset.cardId = card.id;
}

async function submitResult(result) {
  const cardId = Number($("reviewQ").dataset.cardId || 0);
  if (!cardId) return;
  await api("/review/submit", {
    method:"POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ card_id: cardId, result })
  });
  await loadNext();
  await refreshCards();
  await refreshStats();
}

function showAnswer(){ $("reviewA").classList.remove("hidden"); }

async function ingestPdf() {
  const f = $("pdfFile").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const res = await fetch(API + "/ingest/pdf", { method:"POST", body: fd });
  const data = await res.json();
  const list = $("qaList");
  list.innerHTML = "";
  data.qa.forEach(item => {
    const div = document.createElement("div");
    div.className = "box";
    div.innerHTML = `<b>Q:</b> ${item.q}<br/><b>A:</b> ${item.a}
      <div class="row" style="margin-top:6px">
        <button class="ghost" data-add="1">+ Add as Card</button>
      </div>`;
    div.querySelector("button").onclick = async () => {
      const deck_id = Number(deckSelect.value);
      await api("/cards", {
        method:"POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ deck_id, tag: $("tag").value.trim() || "general", question: item.q, answer: item.a })
      });
      refreshCards(); refreshStats();
    };
    list.appendChild(div);
  });
}

async function refreshCards() {
  const deck_id = Number(deckSelect.value);
  const tag = $("tagFilter").value.trim();
  const q = new URLSearchParams({ ...(deck_id && {deck_id}), ...(tag && {tag}) }).toString();
  const cards = await api(`/cards?${q}`);
  const tbody = $("cardsTbody"); tbody.innerHTML = "";
  cards.forEach(c => {
    const tr = document.createElement("tr");
    const hard = c.wrong_count > c.right_count;
    const medium = c.wrong_count === c.right_count && (c.right_count + c.wrong_count) > 0;
    const easy = c.right_count > c.wrong_count;
    tr.className = hard ? "hard" : medium ? "medium" : easy ? "easy" : "";
    const due = c.due_at ? new Date(c.due_at + "Z").toLocaleString() : "";
    tr.innerHTML = `
      <td>${c.tag}</td>
      <td>${c.question}</td>
      <td>${c.answer}</td>
      <td>${c.last_result ?? ""}</td>
      <td>${c.right_count}</td>
      <td>${c.wrong_count}</td>
      <td>${due}</td>`;
    tbody.appendChild(tr);
  });
}

async function refreshStats() {
  const deck_id = Number(deckSelect.value);
  const q = new URLSearchParams({ ...(deck_id && {deck_id}) }).toString();
  const s = await api(`/reflect/stats?${q}`);
  $("stats").innerHTML = `
    <div class="box">Total: ${s.total}</div>
    <div class="box" style="border-color:#ef4444">Red/Hard: ${s.buckets.red_hard}</div>
    <div class="box" style="border-color:#f59e0b">Orange/Medium: ${s.buckets.orange_medium}</div>
    <div class="box" style="border-color:#10b981">Green/Easy: ${s.buckets.green_easy}</div>
    <div class="box" style="border-color:#9ca3af">Never Reviewed: ${s.buckets.gray_never}</div>
  `;
}

$("createDeckBtn").onclick = createDeck;
$("addCardBtn").onclick = addCard;
$("loadNextBtn").onclick = loadNext;
$("showAnswerBtn").onclick = showAnswer;
document.querySelectorAll('button[data-res]').forEach(b => b.onclick = () => submitResult(b.dataset.res));
$("ingestBtn").onclick = ingestPdf;

deckSelect.onchange = () => { refreshCards(); refreshStats(); };

window.addEventListener("DOMContentLoaded", refreshDecks);
