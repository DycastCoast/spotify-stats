// Only used in a snapshot: a saved copy of the site that opens straight from a folder,
// with no server behind it. make_snapshot.py puts this file and snapshot-data.js next to
// the pages. It does the jobs the server normally does:
//   - turns a site address like "/song/ID?who=X" into the saved file for it (or missing.html)
//   - makes the dropdown filters jump to the saved page for the chosen options
//   - answers the search box and serves pictures from built-in lists
(() => {
  const data = window.SNAPSHOT_DATA;

  // The same rules as key() in make_snapshot.py: both sides must turn an address into
  // exactly the same text, or the lookup misses.
  const key = url => {
    const u = new URL(url, "http://site.invalid");
    const params = [...u.searchParams.entries()]
      .filter(([k, v]) => v !== "" && !data.drop.includes(`${k}=${v}`) && !data.ignore.includes(k))
      .sort((a, b) => (a[0] + "=" + a[1]).localeCompare(b[0] + "=" + b[1]));
    return u.pathname + (params.length ? "?" + params.map(([k, v]) => `${k}=${v}`).join("&") : "");
  };
  const link = url => data.pages[key(url)] || "missing.html";

  window.SNAPSHOT = {
    link,
    art: (kind, track) => data.art[`${kind}:${track}`] || null,
    search(q, who) {
      q = q.toLowerCase();
      const person = who || data.viewer;
      const songs = data.songs.filter(s => s.who === person && s.name.toLowerCase().includes(q)).slice(0, 6);
      const artists = data.artists.filter(a => a.who === person && a.name.toLowerCase().includes(q)).slice(0, 4);
      return {songs, artists};
    },
  };

  // Filters: "this.form.submit()" and the Enter key would ask a server; go to the saved page instead.
  const go = form => {
    const action = form.getAttribute("action") || window.SNAPSHOT_PATH;   // no action = "this page"
    if (action === "/search") return;            // pressing Enter in search: the results list is already showing
    const params = new URLSearchParams(new FormData(form));
    location.href = link(action + "?" + params.toString());
  };
  HTMLFormElement.prototype.submit = function () { go(this); };
  document.addEventListener("submit", e => { e.preventDefault(); go(e.target); }, true);

  document.addEventListener("DOMContentLoaded", () => {
    // Links to pages that aren't in the snapshot: dim them and say why on hover.
    document.querySelectorAll('a[href="missing.html"]').forEach(a => {
      a.classList.add("snap-missing");
      a.dataset.tip = "Not in this snapshot: only on the live site";
    });
  });
})();
