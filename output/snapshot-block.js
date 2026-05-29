/* Ai4 attendee snapshot — loader script.
   Loaded by a <script src="..."> tag in the WordPress block.
   Hosted from the GitHub repo via jsDelivr so WordPress can't mangle it. */
(function () {
  const SNAPSHOT_URL = "https://cdn.jsdelivr.net/gh/ai4conferences/ai4-attendee-snapshot@main/output/snapshot.json";
  const SHOW_ALL_LABEL = "All Industries";

  const root = document.getElementById("ai4-snapshot");
  if (!root) return;
  const toolbar = root.querySelector(".ai4-toolbar");
  const listEl  = root.querySelector(".ai4-companies");

  let snapshot = null;
  let activeIndustry = SHOW_ALL_LABEL;

  function render() {
    if (!snapshot) return;
    const filtered = activeIndustry === SHOW_ALL_LABEL
      ? snapshot.companies
      : snapshot.companies.filter(function (c) {
          return (c.industries || []).indexOf(activeIndustry) !== -1;
        });

    const cmp = function (a, b) {
      return a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    };
    const ordered = filtered.filter(function (c) { return c.fortune500; }).sort(cmp)
      .concat(filtered.filter(function (c) { return !c.fortune500; }).sort(cmp));

    listEl.innerHTML = "";
    if (!ordered.length) {
      const li = document.createElement("li");
      li.className = "is-empty";
      li.textContent = "No companies in this category yet.";
      listEl.appendChild(li);
      return;
    }
    const frag = document.createDocumentFragment();
    for (let i = 0; i < ordered.length; i++) {
      const li = document.createElement("li");
      li.textContent = ordered[i].name;
      frag.appendChild(li);
    }
    listEl.appendChild(frag);
  }

  function buildIndustryButtons(industries) {
    const labels = [SHOW_ALL_LABEL].concat(industries.filter(function (i) {
      return i !== SHOW_ALL_LABEL;
    }));
    const frag = document.createDocumentFragment();
    labels.forEach(function (name) {
      const btn = document.createElement("button");
      btn.className = "ai4-filter" + (name === activeIndustry ? " is-active" : "");
      btn.dataset.industry = name;
      btn.textContent = name;
      frag.appendChild(btn);
    });
    toolbar.appendChild(frag);
  }

  toolbar.addEventListener("click", function (e) {
    const btn = e.target.closest(".ai4-filter");
    if (!btn) return;
    activeIndustry = btn.dataset.industry;
    const buttons = toolbar.querySelectorAll(".ai4-filter");
    for (let i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle("is-active", buttons[i] === btn);
    }
    render();
  });

  fetch(SNAPSHOT_URL, { cache: "no-cache" })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      snapshot = data;
      buildIndustryButtons(data.industries || []);
      root.dataset.state = "ready";
      render();
    })
    .catch(function (err) {
      console.error("ai4-snapshot:", err);
      root.dataset.state = "error";
      const errEl = root.querySelector(".ai4-error");
      if (errEl) errEl.textContent = "Couldn't load snapshot: " + err.message;
    });
})();
