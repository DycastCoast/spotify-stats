/* Crate Dive: the page. The server checks every answer (dive.py); this file does the
   timer, the crate-dig animation, sound and the screens. Everything typed or sent back
   by the server is put on the page with textContent, never as HTML. */
(() => {
  const $ = id => document.getElementById(id);
  const root = $("cd");
  if (!root) return;
  const boot = JSON.parse($("cd-boot").textContent);
  const CSRF = root.dataset.csrf;
  const T = boot.tiers;
  const COL = { bin: "#B8B8B8", radio: "#6FB7FF", album: "#F3CD52", bside: "#FF9D4A", white: "#C6F432", boot: "#B593FF", claim: "#F2A7D8", miss: "#5A5A5A" };
  const DESC = { bin: "Everyone says this one", radio: "Well known, not the first thought", album: "You had to actually know it",
    bside: "Most people never get here", white: "The rarest in the crate", boot: "Technically true, bends the rules",
    claim: "Not in the crate, counted on your word", miss: "Time ran out or you passed" };
  const ORDER = ["bin", "radio", "album", "bside", "white", "boot", "claim", "miss"];
  const REAL = ["bin", "radio", "album", "bside", "white"];
  const TARGET = { bin: 1, boot: 1, radio: 4, claim: 4, album: 8, bside: 12, white: 16, miss: 0 };
  const DIV = { 3: "radio", 7: "album", 11: "bside", 15: "white" };
  const N = 20;
  const ZONES = [[0, "the Shop Floor"], [20, "the Back Racks"], [100, "the Basement"], [400, "the Storage Unit"], [600, "the Vault"]];
  const ZCOL = ["#B8B8B8", "#6FB7FF", "#F3CD52", "#FF9D4A", "#C6F432"];
  const COVERS = ["#2B2B2B", "#3B2F4F", "#1F3A4D", "#5A3A22", "#22402A", "#5A2330", "#3A3A3A", "#2A3550", "#56491F", "#41254A", "#1E4442", "#4F2E1C"];
  const PATS = ["radial-gradient(circle at 70% 32%, rgba(255,255,255,.14) 0 20%, transparent 21%)",
    "linear-gradient(135deg, rgba(255,255,255,.09) 0 50%, transparent 50%)",
    "radial-gradient(circle at 30% 62%, transparent 0 18%, rgba(255,255,255,.10) 18% 30%, transparent 31%)",
    "repeating-linear-gradient(90deg, rgba(255,255,255,.07) 0 6px, transparent 6px 18px)",
    "linear-gradient(0deg, rgba(255,255,255,.11) 0 32%, transparent 32%)",
    "repeating-radial-gradient(circle at 50% 120%, rgba(255,255,255,.07) 0 4px, transparent 4px 12px)",
    "linear-gradient(90deg, transparent 0 40%, rgba(255,255,255,.12) 40% 60%, transparent 60%)"];
  const reduce = () => document.documentElement.classList.contains("reduce-motion");
  const zoneIdx = p => p < 20 ? 0 : p < 100 ? 1 : p < 400 ? 2 : p < 600 ? 3 : 4;
  const zoneOf = p => ZONES[zoneIdx(p)][1];
  const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
  const prettyDay = d => new Date(d + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" });

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") el.style.cssText = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: fine */ } },
  };
  async function api(path, body) {
    const r = await fetch("/trivia/dive/api/" + path, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF": CSRF }, body: JSON.stringify(body || {}) });
    if (r.status === 429) return { ok: false, reason: "slow" };
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  let pending = [];
  const later = (fn, ms) => { const t = setTimeout(fn, ms); pending.push(t); return t; };
  const clearLater = () => { pending.forEach(clearTimeout); pending = []; };

  // ------------------------------------------------------------------ sound (made on the fly, no files)
  const Sound = {
    ctx: null, muted: store.get("cd-muted") === "1",
    init() { if (this.muted) return; try { this.ctx = this.ctx || new (window.AudioContext || window.webkitAudioContext)(); if (this.ctx.state === "suspended") this.ctx.resume(); } catch (e) { this.ctx = null; } },
    tone(f, dur, { type = "sine", vol = .07, at = 0, to = null } = {}) {
      if (this.muted || !this.ctx) return; const c = this.ctx, t = c.currentTime + at;
      const o = c.createOscillator(), g = c.createGain(); o.type = type; o.frequency.setValueAtTime(f, t);
      if (to) o.frequency.exponentialRampToValueAtTime(to, t + dur);
      g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(vol, t + .01); g.gain.exponentialRampToValueAtTime(.0001, t + dur);
      o.connect(g).connect(c.destination); o.start(t); o.stop(t + dur + .05);
    },
    noise(dur, { vol = .08, type = "lowpass", from = 3000, to = 300, at = 0, q = .8 } = {}) {
      if (this.muted || !this.ctx) return; const c = this.ctx, t = c.currentTime + at;
      const b = c.createBuffer(1, Math.max(1, Math.floor(c.sampleRate * dur)), c.sampleRate), d = b.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
      const s = c.createBufferSource(); s.buffer = b; const f = c.createBiquadFilter(); f.type = type; f.Q.value = q;
      f.frequency.setValueAtTime(from, t); f.frequency.exponentialRampToValueAtTime(to, t + dur);
      const g = c.createGain(); g.gain.setValueAtTime(vol, t); g.gain.exponentialRampToValueAtTime(.0001, t + dur);
      s.connect(f).connect(g).connect(c.destination); s.start(t);
    },
    crackle() { for (let i = 0; i < 7; i++) this.noise(.025, { vol: .05, type: "bandpass", from: 1800 + Math.random() * 3500, to: 1200, at: i * .06 + Math.random() * .05, q: 3 }); },
    flick(k) { this.noise(.05, { vol: .09, type: "bandpass", from: 700 + k * 55, to: 400, q: 2 }); },
    wrong() { this.tone(170, .18, { type: "triangle", vol: .06, to: 120 }); },
    tick(urgent) { this.tone(urgent ? 1100 : 760, .12, { vol: urgent ? .05 : .025 }); },
    scratch() { this.tone(700, .4, { type: "sawtooth", vol: .035, to: 80 }); this.noise(.4, { vol: .07, from: 5000, to: 200 }); },
    tier(t) {
      const seq = { bin: [440], radio: [440, 523], album: [440, 523, 659], bside: [392, 440, 523, 659], white: [440, 523, 659, 784, 880, 1047],
        boot: [523, 494, 523, 659], claim: [587, 659] }[t];
      if (!seq) return this.scratch();
      seq.forEach((f, i) => { this.tone(f, .8, { type: t === "white" ? "triangle" : "sine", vol: .06, at: i * .085 });
        if (t === "white") this.tone(f * 2, .5, { vol: .015, at: i * .085 + .04 }); });
    },
  };
  function paintMute() { $("cd-mute").textContent = Sound.muted ? "Sound off" : "Sound on"; $("cd-mute").setAttribute("aria-pressed", String(!Sound.muted)); }
  $("cd-mute").onclick = () => { Sound.muted = !Sound.muted; store.set("cd-muted", Sound.muted ? "1" : "0"); paintMute(); if (!Sound.muted) { Sound.init(); Sound.tier("bin"); } };
  paintMute();

  // ------------------------------------------------------------------ the depth rail
  let shown = 0, countTimer = 0;
  function paintRail(p) {
    const zi = zoneIdx(p);
    $("cd-rail-n").textContent = p;
    $("cd-rail-z").textContent = zoneOf(p);
    document.querySelectorAll(".cd-floor").forEach(g => { const n = +g.dataset.z; g.style.color = n < zi ? ZCOL[n] + "99" : n === zi ? ZCOL[n] : ""; });
    document.querySelectorAll(".cd-band").forEach(r => { r.style.fill = +r.dataset.z === zi ? ZCOL[zi] + "14" : "transparent"; });
    document.querySelector(".cd-shop").style.setProperty("--bo", zi >= 2 ? ".18" : "0");
    document.querySelector(".cd-vaultglow").style.fill = zi === 4 ? "rgba(198,244,50,.18)" : "transparent";
    const edges = [0, 20, 100, 400, 600, 700];
    const frac = Math.min(1, (p - edges[zi]) / (edges[zi + 1] - edges[zi]));
    $("cd-marker").style.top = Math.min(99, (zi + frac) / 5 * 100) + "%";
  }
  function countTo(to, ms = 900, els = []) {
    clearInterval(countTimer);
    const from = shown, t0 = performance.now();
    if (reduce() || from === to) { shown = to; paintRail(to); els.forEach(e => e.textContent = to); return; }
    countTimer = setInterval(() => {
      const k = Math.min(1, (performance.now() - t0) / ms), v = Math.round(from + (to - from) * (1 - Math.pow(1 - k, 3)));
      shown = v; $("cd-rail-n").textContent = v; els.forEach(e => e.textContent = v);
      if (k >= 1) { clearInterval(countTimer); paintRail(to); }
    }, 30);
    paintRail(to);
    $("cd-rail-n").textContent = from;
  }

  // ------------------------------------------------------------------ screens
  function show(which) {
    for (const s of ["start", "play", "end"]) $("cd-" + s).hidden = s !== which;
    scrollTo({ top: Math.max(0, root.getBoundingClientRect().top + scrollY - 90), behavior: reduce() ? "auto" : "smooth" });
  }
  let timerPref = [25, 15, 0].includes(+store.get("cd-timer")) && store.get("cd-timer") !== null ? +store.get("cd-timer") : boot.timeLimit;
  let pack = "all";

  function chips(box, items, current, pick) {
    box.replaceChildren(...items.map(([v, label]) => h("button", { type: "button", class: "cd-chip", "aria-pressed": String(v === current),
      onclick: () => { pick(v); [...box.children].forEach((b, i) => b.setAttribute("aria-pressed", String(items[i][0] === v))); } }, label)));
  }
  function squares(tiers) { return h("div", { class: "cd-squares", "aria-label": tiers.map(t => T[t].name).join(", ") }, tiers.map(t => h("i", { style: `background:${COL[t]}` }))); }

  function renderStart() {
    clearLater(); stopClock();
    shown = 0; paintRail(0);
    $("cd-today").textContent = "Daily dive · " + prettyDay(boot.today);
    const daily = $("cd-daily");
    if (boot.done) {
      daily.replaceChildren(h("div", { class: "cd-done" },
        h("b", {}, boot.done.points + " records deep"),
        h("span", { class: "cd-muted" }, "You reached " + zoneOf(boot.done.points) + (boot.done.timer ? "" : " · untimed")),
        squares(boot.done.tiers)),
        h("div", {}, h("button", { type: "button", class: "cd-btn cd-ghost", onclick: reviewDaily }, boot.friend ? "See today's dive vs " + boot.friend : "See today's dive")));
    } else {
      daily.replaceChildren(h("span", { class: "cd-muted" }, "Seven prompts, the same for everyone today. Your own-listening prompts have different answers for each of you."),
        h("div", {}, h("button", { type: "button", class: "cd-btn", onclick: () => startDive("daily") }, "Start today's dive")));
    }
    const note = () => $("cd-timer-note").textContent = timerPref ? timerPref + " seconds per prompt." : "No clock. Your daily gets an \"untimed\" tag next to your score.";
    chips($("cd-timer"), [[25, "25 seconds"], [15, "15 seconds (hard)"], [0, "Off"]], timerPref, v => { timerPref = v; store.set("cd-timer", String(v)); note(); });
    note();
    chips($("cd-packs"), boot.packs, pack, v => pack = v);
    if (boot.pastDays && boot.pastDays.length) {
      $("cd-archive").hidden = false;
      $("cd-arc-day").replaceChildren(...boot.pastDays.map(d => h("option", { value: d }, prettyDay(d))));
    }
    const st = boot.stats;
    $("cd-stats").replaceChildren(...[[st.dives, "Dives"], [st.best, "Deepest dive"], [st.avg, "Average depth"], [st.streak, "Daily streak"]]
      .map(([v, l]) => h("div", {}, h("b", {}, String(v)), h("span", {}, l))));
    $("cd-legend").replaceChildren(...ORDER.map(t => h("div", {}, h("span", { class: "cd-pill", style: `background:${COL[t]}` }, T[t].pts + " pts"),
      h("span", {}, h("b", {}, T[t].name), h("small", {}, DESC[t])))));
    if (boot.claims && boot.claims.length) {
      $("cd-claims").hidden = false;
      $("cd-claims-list").replaceChildren(...boot.claims.map(c => h("div", {}, h("b", {}, c.answer), h("span", { class: "cd-muted" }, " for “" + c.q + "” · " + c.who))));
    }
    show("start");
  }
  $("cd-copy-claims").onclick = async () => {
    const text = "Please check these answers for my Crate Dive game and tell me which are real, so they can be added:\n" +
      boot.claims.map(c => `- ${c.answer} (prompt: ${c.q})`).join("\n");
    try { await navigator.clipboard.writeText(text); $("cd-claims-copied").textContent = "Copied."; }
    catch (e) { $("cd-claims-copied").textContent = "Your browser blocked copying. Select the list instead."; }
  };

  // ------------------------------------------------------------------ a dive
  let game = null, busy = false, pendingClaim = null;
  async function startDive(mode, extra = {}) {
    Sound.init();
    let res;
    try { res = await api("start", { mode, ...extra, pack }); }
    catch (e) { alert("Couldn't start the dive. Try again in a moment."); return; }
    if (!res.prompts || !res.prompts.length) { alert("Not enough data for a dive yet. The artist lookups are still filling the crates."); return; }
    game = { mode, day: res.day, prompts: res.prompts, i: 0, results: [], pts: 0, timer: timerPref,
      label: mode === "daily" ? "Daily · " + prettyDay(res.day) : mode === "archive" ? "Archive · " + prettyDay(res.day) : "Unlimited · " + Object.fromEntries(boot.packs)[pack] };
    shown = 0; paintRail(0);
    $("cd-pips").replaceChildren(...game.prompts.map(() => h("i")));
    show("play");
    Sound.crackle();
    renderPrompt();
  }
  $("cd-start-unl").onclick = () => startDive("unlimited");
  $("cd-start-arc").onclick = () => startDive("archive", { day: $("cd-arc-day").value });

  function renderPrompt() {
    clearLater();
    const p = game.prompts[game.i];
    $("cd-mode").textContent = game.label + (game.timer ? "" : " · untimed");
    $("cd-count").textContent = `Prompt ${game.i + 1} of ${game.prompts.length}`;
    $("cd-pts").textContent = game.pts;
    $("cd-pack").textContent = p.pack;
    $("cd-q").textContent = p.q;
    $("cd-note").textContent = `${p.note} · ${p.count.toLocaleString("en-GB")} in the crate`;
    [...$("cd-pips").children].forEach((el, k) => { const r = game.results[k]; el.style.background = r ? COL[r.tier] : k === game.i ? "#F2F2F2" : ""; });
    buildCrate(game.i);
    $("cd-strip").replaceChildren(...REAL.map(t => h("div", { "data-t": t, style: `--tc:${COL[t]};--tg:${COL[t]}88` }, h("b", {}, T[t].name), h("span", {}, T[t].pts + " PTS"))));
    $("cd-special").replaceChildren();
    $("cd-answer").hidden = false;
    $("cd-reveal").hidden = true;
    $("cd-inputrow").classList.remove("busy");
    $("cd-guess").disabled = false; $("cd-dig").disabled = false; $("cd-pass").disabled = false;
    $("cd-guess").value = "";
    $("cd-fb").textContent = ""; $("cd-fb").className = "cd-fb";
    $("cd-claim").hidden = true; pendingClaim = null; busy = false;
    $("cd-guess").focus({ preventScroll: true });
    startClock();
  }

  // ---------- the clock
  let clockRaf = 0, t0 = 0, lastSec = 99;
  function stopClock() { cancelAnimationFrame(clockRaf); clockRaf = 0; }
  function startClock() {
    stopClock();
    const timed = game.timer > 0;
    $("cd-clock").classList.toggle("off", !timed); $("cd-clockbar").classList.toggle("off", !timed);
    if (!timed) return;
    t0 = performance.now(); lastSec = 99;
    const tick = () => {
      const left = Math.max(0, game.timer - (performance.now() - t0) / 1000), sec = Math.ceil(left), urgent = left <= 5;
      $("cd-clock").textContent = sec;
      $("cd-clock").classList.toggle("urgent", urgent); $("cd-clockbar").classList.toggle("urgent", urgent);
      $("cd-clockbar").firstElementChild.style.transform = `scaleX(${left / game.timer})`;
      if (sec !== lastSec) { if (lastSec !== 99 && urgent && sec > 0) Sound.tick(true); lastSec = sec; }
      if (left <= 0) { clockRaf = 0; return miss("time"); }
      clockRaf = requestAnimationFrame(tick);
    };
    tick();
  }

  // ---------- the crate
  let sleeves = [];
  function buildCrate(i) {
    const box = $("cd-sleeves"); box.replaceChildren();
    const crate = $("cd-crate");
    crate.classList.remove("shake");
    crate.querySelectorAll(".cd-pulled, .cd-dead").forEach(e => e.remove());
    $("cd-glow").classList.remove("on");
    $("cd-front").style.setProperty("--lip", "#2A2A2A");
    $("cd-crate-no").textContent = "No. " + (i + 1);
    $("cd-status").textContent = "Waiting for your answer";
    crate.style.setProperty("--sc", "");
    $("cd-flips").textContent = "";
    const seedBase = i * 3 + (game.day ? +game.day.slice(-2) : 7);
    sleeves = [];
    for (let j = 0; j < N; j++) {
      const isDiv = DIV[j] !== undefined, seed = j * 7 + seedBase;
      const el = h("div", { class: "cd-s" + (isDiv ? " cd-div" : ""),
        style: `top:${150 - j * 3 - (isDiv ? 26 : 0)}px;z-index:${N - j};scale:${(1 - j * .011).toFixed(3)};filter:brightness(${(1 - j * .035).toFixed(2)})` });
      if (isDiv) {
        const c = COL[DIV[j]];
        el.append(h("div", { class: "cd-tab", style: `background:${c}` }, T[DIV[j]].name), h("div", { class: "cd-divbody", style: `background-color:${c}` }));
      } else {
        el.append(h("div", { class: "cd-cover", style: `background-color:${COVERS[seed % COVERS.length]};background-image:${PATS[(j + i) % PATS.length]}, linear-gradient(rgba(255,255,255,.10), transparent 18%, transparent 80%, rgba(0,0,0,.35))` },
          h("span", { class: "cd-cat" }, "CD-" + (1000 + (seed * 37) % 9000)),
          seed % 3 === 0 ? h("span", { class: "cd-price" }, "£" + (2 + seed % 9) + ".99") : null));
      }
      box.append(el); sleeves.push(el);
    }
    const dust = $("cd-dust");
    if (!dust.childElementCount && !reduce()) {
      for (let n = 0; n < 14; n++) dust.append(h("i", { style: `left:${(n * 37) % 100}%;top:${30 + (n * 23) % 60}%;width:${1 + n % 3}px;height:${1 + n % 3}px;--o:${(.25 + (n % 4) * .1).toFixed(2)};--dx:${(n % 2 ? 1 : -1) * (10 + n * 2)}px;animation-duration:${7 + (n % 5) * 2}s;animation-delay:${-(n * 1.3).toFixed(1)}s` }));
    }
  }
  function catNo(name) { let x = 7; for (const ch of name) x = (x * 31 + ch.charCodeAt(0)) % 9973; return "CD-" + String(game.i + 1).padStart(2, "0") + String(x).padStart(4, "0"); }
  function recordArt(r) {
    const cat = catNo(r.name || "x");
    const bars = (cat.replace(/\D/g, "") + "371942").split("").map(d => h("i", { style: `width:${1 + (+d % 3)}px` }));
    return h("div", { class: "cd-record", style: `background-color:${COL[r.tier]}` },
      h("div", { class: "cd-rtop" }, h("span", {}, cat), h("span", {}, T[r.tier].pts + " PTS")),
      h("div", {}, h("div", { class: "cd-rtier" }, T[r.tier].name), h("div", { class: "cd-rname" }, r.name),
        r.detail ? h("div", { class: "cd-rdetail" }, r.detail) : null, h("div", { class: "cd-barcode" }, bars)));
  }

  // ---------- answering
  async function submit() {
    if (busy || !game) return;
    const text = $("cd-guess").value.trim();
    if (!text) return;
    busy = true;
    let r;
    try { r = await api("guess", { pid: game.prompts[game.i].id, text }); }
    catch (e) { busy = false; $("cd-fb").textContent = "Couldn't check that (connection?). Try again."; return; }
    busy = false;
    if (r.ok) return resolve({ tier: r.tier, name: r.name, detail: r.detail, typed: text, reveal: r.reveal });
    Sound.wrong();
    const row = $("cd-inputrow"); row.classList.remove("nope"); void row.offsetWidth; row.classList.add("nope");
    const fb = $("cd-fb"); fb.className = "cd-fb bad";
    $("cd-guess").select();
    if (r.reason === "known") { fb.textContent = `“${text}” is real, but it doesn't fit this prompt. Try another.`; $("cd-claim").hidden = true; pendingClaim = null; }
    else if (r.reason === "slow") { fb.textContent = "Easy! Give it a second."; }
    else { fb.textContent = `“${text}” isn't in the crate. Try another, or if you're sure:`; $("cd-claim").hidden = false; pendingClaim = text; }
  }
  $("cd-dig").onclick = submit;
  $("cd-guess").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
  $("cd-pass").onclick = () => miss("pass");
  $("cd-claim").onclick = async () => {
    if (!pendingClaim || busy) return;
    const text = pendingClaim; busy = true;
    let r = {};
    try { r = await api("claim", { pid: game.prompts[game.i].id, text }); } catch (e) { /* still count it */ }
    busy = false;
    resolve({ tier: "claim", name: text, typed: text, reveal: r.reveal || null });
  };
  async function miss(why) {
    if (!game || $("cd-guess").disabled) return;
    let rev = null;
    resolve({ tier: "miss", name: "", why, reveal: null });
    try { rev = await api("reveal", { pid: game.prompts[game.i].id }); } catch (e) { /* reveal lists are a nice-to-have */ }
    if (rev && game.results.length) { game.results[game.results.length - 1].reveal = rev; fillReveal(game.results[game.results.length - 1]); }
  }

  function resolve(r) {
    if ($("cd-guess").disabled) return;          // already answered (e.g. the clock ran out mid-check)
    stopClock();
    $("cd-guess").disabled = true; $("cd-dig").disabled = true; $("cd-pass").disabled = true; $("cd-claim").hidden = true;
    $("cd-inputrow").classList.add("busy");
    game.results.push(r);
    game.pts += T[r.tier].pts;
    const pip = $("cd-pips").children[game.i]; pip.style.background = COL[r.tier];
    $("cd-sr").textContent = r.tier === "miss" ? "Dead Air." : `${r.name}: ${T[r.tier].name}, ${T[r.tier].pts} points.`;
    if (r.tier === "miss") return deadAir(r);
    dig(r);
  }

  function deadAir(r) {
    Sound.scratch();
    $("cd-status").textContent = "Nothing pulled";
    $("cd-crate").append(h("div", { class: "cd-dead" }, h("b", {}, "Dead Air"), r.why === "time" ? "The needle ran out. Time's up." : "You lifted the needle. Passed."));
    showReveal(r, true);
  }

  function dig(r) {
    const target = TARGET[r.tier];
    let k = 0;
    $("cd-status").textContent = "Digging…";
    const strip = [...$("cd-strip").children];
    const step = () => {
      if (k === target - 1 && sleeves[target]) { sleeves[target].replaceChildren(recordArt(r)); }
      if (k >= target) return land(r, target);
      sleeves[k].classList.remove("lean");
      sleeves[k].classList.add("flipped");
      k++;
      if (sleeves[k] && k < target) sleeves[k].classList.add("lean"); else if (sleeves[k]) sleeves[k].classList.remove("lean");
      const passed = Object.keys(DIV).filter(j => +j < k).map(j => DIV[j]).pop();
      $("cd-status").textContent = passed ? `Digging past ${T[passed].name}…` : "Digging…";
      $("cd-flips").textContent = k + " sleeve" + (k === 1 ? "" : "s") + " flipped";
      if (REAL.includes(r.tier)) strip.forEach(el => { if (TARGET[el.dataset.t] <= k) el.classList.add("on"); });
      Sound.flick(k);
      later(step, 55 + 340 * Math.pow(k / target, 3.2));
    };
    if (reduce()) {
      sleeves[target].replaceChildren(recordArt(r));
      for (let j = 0; j < target; j++) sleeves[j].classList.add("flipped");
      k = target;
      if (REAL.includes(r.tier)) strip.forEach(el => { if (TARGET[el.dataset.t] <= k) el.classList.add("on"); });
      return land(r, target);
    }
    later(step, 240);
  }

  function land(r, target) {
    const c = COL[r.tier], s = sleeves[target];
    const crate = $("cd-crate");
    crate.style.setProperty("--sc", c);
    $("cd-status").textContent = "Pulled from " + T[r.tier].name;
    s.classList.add("gone");
    const scale = (1 - target * .011).toFixed(3);
    const sparks = r.tier === "white" ? Array.from({ length: 22 }, (_, n) => {
      const a = n / 22 * Math.PI * 2, rad = 150 + (n % 4) * 36, sz = 6 + (n % 3) * 3;
      return h("i", { class: "cd-spark", style: `width:${sz}px;height:${sz}px;rotate:${n * 33}deg;background:${["#F2F2F2", "#C6F432", "#F2E8D0"][n % 3]};--dx:${Math.round(Math.cos(a) * rad)}px;--dy:${Math.round(Math.sin(a) * rad)}px;animation-delay:${(.5 + (n % 5) * .04).toFixed(2)}s` });
    }) : [];
    const vinyl = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    vinyl.setAttribute("viewBox", "0 0 100 100"); vinyl.setAttribute("class", "cd-vinyl"); vinyl.setAttribute("aria-hidden", "true");
    vinyl.innerHTML = '<circle cx="50" cy="50" r="49.5" fill="#0E0E0E"/><circle cx="50" cy="50" r="46" fill="none" stroke="#1F1F1F" stroke-width=".6"/><circle cx="50" cy="50" r="42" fill="none" stroke="#262626" stroke-width=".5"/><circle cx="50" cy="50" r="38" fill="none" stroke="#1F1F1F" stroke-width=".6"/><circle cx="50" cy="50" r="34" fill="none" stroke="#2A2A2A" stroke-width=".5"/><circle cx="50" cy="50" r="30" fill="none" stroke="#1F1F1F" stroke-width=".6"/><circle cx="50" cy="50" r="26" fill="none" stroke="#262626" stroke-width=".5"/><path d="M50 4a46 46 0 0 1 32 13L50 50Z" fill="rgba(255,255,255,.06)"/><path d="M50 96a46 46 0 0 1-32-13L50 50Z" fill="rgba(255,255,255,.04)"/><circle class="lbl" cx="50" cy="50" r="17"/><circle cx="50" cy="50" r="13" fill="none" stroke="rgba(0,0,0,.25)" stroke-width=".6"/><text x="50" y="45" text-anchor="middle" font-size="3.6" font-weight="700" fill="#000">CRATE DIVE</text><text x="50" y="58" text-anchor="middle" font-size="3" fill="#000">33⅓ RPM</text><circle cx="50" cy="50" r="2" fill="#000"/>';
    vinyl.querySelector(".lbl").setAttribute("fill", c);     // fixed markup above; only the colour is ours
    const top = 150 - target * 3, lift = 46 - top;          // lift it until its top sits just under the status line
    const pulled = h("div", { class: "cd-pulled", style: `top:${top}px;--s0:${scale};--py:${lift}px;--py2:${lift - 18}px;--tc:${c};--tg:${c}55` },
      h("div", { class: "cd-halo" }),
      h("i", { class: "cd-ring", style: "animation-delay:.45s" }), h("i", { class: "cd-ring", style: "animation-delay:.62s" }), h("i", { class: "cd-ring", style: "animation-delay:.8s;border-width:2px" }),
      sparks, vinyl, recordArt(r), h("div", { class: "cd-stamp" }, "+" + T[r.tier].pts));
    crate.append(pulled);
    $("cd-glow").style.setProperty("--tc", c + "55");
    $("cd-glow").classList.add("on");
    $("cd-front").style.setProperty("--lip", c);
    if (r.tier === "white") crate.classList.add("shake");
    const hit = [...$("cd-strip").children].find(el => el.dataset.t === r.tier);
    if (hit) hit.classList.add("on", "hit");
    else $("cd-special").replaceChildren(h("div", { class: "cd-special", style: `background:${c}` },
      `${T[r.tier].name} · ${T[r.tier].pts} pts · ${r.tier === "boot" ? "technically true" : "counted on your word"}`));
    Sound.tier(r.tier);
    later(() => countTo(game.pts, 900, [$("cd-pts")]), reduce() ? 0 : 650);
    showReveal(r, false);
  }

  // ---------- the reveal panel
  function fillReveal(r) {
    const c = COL[r.tier];
    $("cd-r-tier").textContent = r.tier === "miss" ? (r.why === "time" ? "Dead Air · time's up" : "Dead Air · passed") : T[r.tier].name;
    $("cd-r-tier").style.color = c;
    $("cd-r-gain").textContent = T[r.tier].pts ? `+${T[r.tier].pts} pts · ${T[r.tier].pts} records deeper` : "No records flipped this time";
    const rv = r.reveal || {};
    $("cd-r-rare").textContent = rv.rarer_than != null ? `Rarer than ${rv.rarer_than}% of the ${rv.count.toLocaleString("en-GB")} answers in this crate.` : "";
    const kv = [];
    if (r.typed && r.name && r.tier !== "claim" && r.typed.trim().toLowerCase() !== r.name.toLowerCase()) kv.push(["You typed", `“${r.typed}”, matched to ${r.name}`]);
    if (r.detail) kv.push(["By", r.detail]);
    if (rv.bargain && rv.bargain.length) kv.push(["Bargain Bin", rv.bargain.join(", ")]);
    if (rv.deepest && rv.deepest.length) kv.push(["Deepest in the crate", rv.deepest.join(", ")]);
    $("cd-r-kv").replaceChildren(...kv.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  }
  function showReveal(r, quick) {
    fillReveal(r);
    $("cd-check-in").value = ""; $("cd-check-out").replaceChildren();
    $("cd-next").textContent = game.i === game.prompts.length - 1 ? "Count my records" : "Keep digging";
    $("cd-answer").hidden = true;
    const panel = $("cd-reveal");
    panel.classList.toggle("quick", quick || reduce());
    panel.hidden = false;
    later(() => $("cd-next").focus({ preventScroll: true }), quick || reduce() ? 100 : 1300);
  }
  $("cd-next").onclick = () => {
    if (game.i >= game.prompts.length - 1) return finish();
    game.i++;
    renderPrompt();
  };

  // ---------- search ("would it have counted?")
  async function runSearch(pid, input, out) {
    const text = input.value.trim();
    if (text.length < 2) { out.replaceChildren(); return; }
    let r;
    try { r = await api("search", { pid, text }); } catch (e) { out.replaceChildren(h("p", { class: "cd-muted" }, "Couldn't search just now.")); return; }
    if (!r.hits.length) {
      out.replaceChildren(h("p", { class: "cd-muted" }, r.known ? `“${text}” is real, but it doesn't fit this prompt.`
        : `“${text}” isn't in this crate. If you're sure it's right, it would have been a Trust Me Bro.`));
      return;
    }
    out.replaceChildren(...r.hits.map(a => h("div", { class: "cd-hit" }, h("span", {}, a.name, a.detail ? h("small", {}, " · " + a.detail) : null),
      h("span", { class: "cd-pill", style: `background:${COL[a.tier]}` }, `${T[a.tier].name} · ${a.pts}`))));
  }
  const checkNow = () => runSearch(game.prompts[game.i].id, $("cd-check-in"), $("cd-check-out"));
  $("cd-check-go").onclick = checkNow;
  $("cd-check-in").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); checkNow(); } });

  // ------------------------------------------------------------------ the end of a dive
  async function finish() {
    stopClock(); clearLater();
    let res = null;
    try {
      res = await api("finish", { mode: game.mode, day: game.day, ids: game.prompts.map(p => p.id),
        tiers: game.results.map(r => r.tier), answers: game.results.map(r => r.name), timer: game.timer });
    } catch (e) { /* still show the result; it just wasn't saved */ }
    if (res) { boot.stats = res.stats; if (game.mode === "daily") boot.done = res.result; }
    showEnd(game, res);
  }
  async function reviewDaily() {
    let res;
    try { res = await api("daily", {}); } catch (e) { return; }
    const r = res.result;
    game = { mode: "daily", day: r.day, prompts: res.prompts, i: 0, pts: r.points, timer: r.timer, review: true,
      label: "Daily · " + prettyDay(r.day), results: r.tiers.map((t, k) => ({ tier: t, name: r.answers[k] })) };
    showEnd(game, res);
  }

  function showEnd(g, res) {
    const p = g.pts, zi = zoneIdx(p);
    show("end");
    $("cd-endhero").style.setProperty("--zc", ZCOL[zi]);
    $("cd-e-mode").textContent = g.label + (g.timer ? "" : " · untimed");
    if (!g.review) shown = 0;
    countTo(p, 1500, [$("cd-e-depth")]);
    const best = res && res.first && res.stats && res.stats.best === p && res.stats.dives > 1 && p > 0 ? " New personal best." : "";
    $("cd-e-sub").textContent = `You reached ${zoneOf(p)}. ${p} of 700 points.${best}`;
    $("cd-wall").replaceChildren(...g.results.map((r, k) => h("div", { style: `background-color:${COL[r.tier]};animation-delay:${(.25 + k * .12).toFixed(2)}s` },
      h("span", { class: "cd-wq" }, g.prompts[k] ? g.prompts[k].q : ""),
      h("div", {}, h("div", { class: "cd-wt" }, `${T[r.tier].name} · ${T[r.tier].pts}`), h("div", { class: "cd-wn" }, r.name || "Dead Air")))));
    // you vs your friend
    const vs = res && res.versus, box = $("cd-vs");
    box.hidden = !vs;
    if (vs) {
      if (vs.played && vs.rows) {
        const diff = p - vs.points;
        const head = h("div", { class: "cd-split" }, h("span", { class: "cd-label", style: "font-size:20px" }, "You vs " + vs.friend),
          h("span", { class: "cd-muted" }, diff > 0 ? `You dug ${diff} deeper than ${vs.friend} (${vs.points}${vs.timer ? "" : ", untimed"})`
            : diff < 0 ? `${vs.friend} dug ${-diff} deeper (${vs.points}${vs.timer ? "" : ", untimed"})` : `Dead level at ${p}`));
        const cells = [h("span", { class: "h" }, "Prompt"), h("span", { class: "h" }, "You"), h("span", { class: "h" }, vs.friend)];
        g.results.forEach((r, k) => {
          const o = vs.rows[k] || { tier: "miss", answer: "" };
          cells.push(h("span", {}, g.prompts[k] ? g.prompts[k].q : ""),
            h("span", {}, h("b", {}, r.name || "No answer"), h("br"), h("span", { style: `color:${COL[r.tier]}` }, `${T[r.tier].name} · ${T[r.tier].pts}`)),
            h("span", {}, h("b", {}, o.answer || "No answer"), h("br"), h("span", { style: `color:${COL[o.tier]}` }, `${T[o.tier].name} · ${T[o.tier].pts}`)));
        });
        box.replaceChildren(head, h("div", { class: "cd-vsgrid" }, cells),
          h("span", { class: "cd-muted" }, "Your-listening prompts have different answers for each of you, so compare points there, not names."));
      } else {
        box.replaceChildren(h("span", { class: "cd-label", style: "font-size:20px" }, "You vs " + vs.friend),
          h("span", { class: "cd-muted" }, `${vs.friend} hasn't done today's dive yet. Their answers show up here once they have.`));
      }
    }
    // search the crates
    let pick = 0;
    chips($("cd-search-picks"), g.prompts.map((pr, k) => [k, (k + 1) + ". " + pr.q.replace(/^Name (a|an) /, "")]), 0, v => { pick = v; $("cd-search-out").replaceChildren(); });
    const go = () => runSearch(g.prompts[pick].id, $("cd-search-in"), $("cd-search-out"));
    $("cd-search-go").onclick = go;
    $("cd-search-in").onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); go(); } };
    $("cd-search-in").value = ""; $("cd-search-out").replaceChildren();
    // share
    $("cd-share").textContent = `Crate Dive · ${g.mode === "unlimited" ? g.label.replace("Unlimited · ", "Unlimited, ") : prettyDay(g.day)}${g.timer ? "" : " · untimed"}\n` +
      g.results.map(r => T[r.tier].name).join(" / ") + `\n${p} records deep · ${cap(zoneOf(p).replace("the ", "The "))}`;
    $("cd-copied").textContent = "";
    $("cd-again").textContent = g.mode === "unlimited" ? "Dig again" : "Play an unlimited dive";
    if (!g.review && p > 0) later(() => Sound.tier(p >= 350 ? "white" : "album"), 500);
  }
  $("cd-copy").onclick = async () => {
    try { await navigator.clipboard.writeText($("cd-share").textContent); $("cd-copied").textContent = "Copied."; }
    catch (e) { const r = document.createRange(); r.selectNodeContents($("cd-share")); getSelection().removeAllRanges(); getSelection().addRange(r); $("cd-copied").textContent = "Selected: press Ctrl+C to copy."; }
  };
  $("cd-again").onclick = () => startDive("unlimited");
  $("cd-home").onclick = renderStart;

  renderStart();
})();
