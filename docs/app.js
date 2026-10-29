// Renders docs/catalog.json (written by `vinyl publish` on chombo). No build step.
const $ = (s) => document.querySelector(s);
const norm = (s) => (s || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const state = { q: "", kind: "", sort: "artist", albums: [] };

function art(a, cls = "") {
  return a.cover ? `<img class="${cls}" src="${esc(a.cover)}" alt="" loading="lazy">` : `<span aria-hidden="true">${a.kind === '7"' ? "🎵" : "💿"}</span>`;
}

function render() {
  const q = norm(state.q.trim());
  let list = state.albums.filter((a) => !state.kind || a.kind === state.kind);
  const songs = [];
  if (q) {
    list = list.filter((a) => {
      const hit = a.tracks.filter((t) => norm(t.title).includes(q));
      hit.forEach((t) => songs.push({ t, a }));
      return hit.length || norm(a.artist).includes(q) || norm(a.title).includes(q);
    });
  }
  list.sort(state.sort === "recent"
    ? (x, y) => (y.last_played || "").localeCompare(x.last_played || "")
    : (x, y) => norm(x.artist).localeCompare(norm(y.artist)) || (x.year || "").localeCompare(y.year || ""));

  $("#count").textContent = q
    ? `${songs.length} song${songs.length === 1 ? "" : "s"} · ${list.length} record${list.length === 1 ? "" : "s"} match “${state.q.trim()}”`
    : `${list.length} record${list.length === 1 ? "" : "s"}`;
  $("#songs").hidden = !songs.length;
  $("#songs").innerHTML = songs.slice(0, 50).map(({ t, a }) =>
    `<li>♪ <b>${esc(t.title)}</b> <span>— ${esc(a.artist)}, <i>${esc(a.title)}</i>${a.kind === '7"' ? " (45)" : ""}</span></li>`).join("");
  $("#grid").innerHTML = list.map((a) => `
    <button class="rec" data-id="${esc(a.id)}">
      <div class="art">${art(a)}</div>
      <div class="meta"><div class="t">${esc(a.title)}${a.kind === '7"' ? '<span class="badge">45</span>' : ""}</div>
      <div class="a">${esc(a.artist)}</div><div class="y">${esc(a.year || "")}</div></div>
    </button>`).join("");
}

function open(id) {
  const a = state.albums.find((x) => x.id === id);
  if (!a) return;
  const tracks = a.tracks.map((t) => `<li class="${t.heard ? "heard" : ""}">${t.n ? `<span class="badge">${esc(t.n)}</span> ` : ""}${esc(t.title)}</li>`).join("");
  $("#dlg").innerHTML = `
    <button class="close" aria-label="Close">×</button>
    <div class="dhead">${a.cover ? art(a) : ""}<div><h2>${esc(a.title)}</h2>
      <div class="a">${esc(a.artist)}${a.year ? " · " + esc(a.year) : ""} · ${a.kind === '7"' ? "45" : "LP"}</div>
      ${a.last_played ? `<div class="y">Last played ${esc(a.last_played)}</div>` : ""}
      ${a.musicbrainz ? `<div class="y"><a href="${esc(a.musicbrainz)}" style="color:var(--accent)">MusicBrainz</a></div>` : ""}</div></div>
    <ol>${tracks}</ol>`;
  $("#dlg").showModal();
}

document.addEventListener("click", (e) => {
  const rec = e.target.closest(".rec");
  if (rec) return open(rec.dataset.id);
  if (e.target.closest(".close") || e.target === $("#dlg")) return $("#dlg").close();
  const chip = e.target.closest(".chips button");
  if (chip) {
    chip.parentElement.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b === chip));
    if ("kind" in chip.dataset) state.kind = chip.dataset.kind;
    if (chip.dataset.sort) state.sort = chip.dataset.sort;
    render();
  }
});
$("#q").addEventListener("input", (e) => { state.q = e.target.value; render(); });

fetch("catalog.json", { cache: "no-cache" })
  .then((r) => r.json())
  .then((cat) => {
    state.albums = cat.albums;
    $("#foot").innerHTML = `Updated ${esc(cat.generated)} · ♪ = heard on the turntable · <a href="catalog.json" style="color:inherit">catalog.json</a>`;
    render();
  })
  .catch(() => { $("#count").textContent = "Couldn't load the catalog."; });

// Now playing: the turntable's stream title, read from the public Livesets (Icecast) status. Hidden when not live.
const STATUS = "https://djuplink.com/status-json.xsl";
const MOUNT = "/7mg1.ogg";
async function nowPlaying() {
  const box = $("#now");
  try {
    const st = (await (await fetch(STATUS, { cache: "no-store" })).json()).icestats;
    const src = [].concat(st.source || []).find((x) => (x.listenurl || "").endsWith(MOUNT));
    if (!src) { box.hidden = true; return; }
    const title = (src.title || "").trim();
    let line = title ? "Between records" : "Live from the turntable", from = "", cover = "";
    if (title && !/^(between records|unknown track)$/i.test(title)) {
      line = title;
      const [artist, song] = title.split(" - ");
      const a = song && state.albums.find((x) => norm(x.artist).includes(norm(artist).split(" ")[0]) &&
        x.tracks.some((t) => norm(t.title) === norm(song)));
      if (a) { from = `from <i>${esc(a.title)}</i>`; cover = a.cover || ""; }
    }
    box.innerHTML = `${cover ? `<img src="${esc(cover)}" alt="">` : '<span style="font-size:2rem">📀</span>'}
      <div><div class="live">🔴 ON THE TURNTABLE NOW</div><div class="np">${esc(line)}</div>
      <div class="from">${from ? from + " · " : ""}listen live →</div></div>`;
    box.hidden = false;
  } catch { box.hidden = true; }
}
setTimeout(nowPlaying, 500);
setInterval(nowPlaying, 30000);
