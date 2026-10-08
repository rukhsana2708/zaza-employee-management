// ZaZa manager dashboard — small progressive enhancements only.
// All figures are calculated on the server. Untrusted text is only ever
// written with textContent (never innerHTML).
(function () {
  "use strict";

  // Show the From/To fields only for "Custom Date Range".
  function syncCustomDates(select) {
    var custom = select.value === "custom";
    var form = select.form;
    form.querySelectorAll("[data-custom-dates]").forEach(function (el) {
      el.hidden = !custom;
      el.querySelectorAll("input").forEach(function (input) { input.disabled = !custom; });
    });
  }

  document.querySelectorAll("[data-period-select]").forEach(function (select) {
    syncCustomDates(select);
    select.addEventListener("change", function () { syncCustomDates(select); });
  });

  // Overview: refresh the current status once a minute while the tab is visible.
  var table = document.querySelector("[data-status-table]");
  if (!table) { return; }
  var REFRESH_MS = 60000;
  var timer = null;

  function update(data) {
    Object.keys(data.counts || {}).forEach(function (key) {
      var el = document.querySelector('[data-count="' + key + '"]');
      if (el) { el.textContent = String(data.counts[key]); }
    });
    (data.employees || []).forEach(function (row) {
      var tr = null;
      table.querySelectorAll("tr[data-employee-id]").forEach(function (candidate) {
        if (candidate.getAttribute("data-employee-id") === row.employee_id) { tr = candidate; }
      });
      if (!tr) { return; }
      var badge = tr.querySelector('[data-field="status"]');
      if (badge) {
        badge.textContent = row.label;
        badge.className = "badge s-" + String(row.status).toLowerCase().replace(/[^a-z_]/g, "");
      }
      var seen = tr.querySelector('[data-field="last-seen"]');
      if (seen) { seen.textContent = row.last_seen_text; }
    });
    var stamp = document.querySelector("[data-refreshed]");
    if (stamp) { stamp.textContent = "(updated " + new Date().toLocaleTimeString() + ")"; }
  }

  function refresh() {
    fetch("/manager/api/current-status", { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (response.status === 401) { window.location.assign("/manager/login"); return null; }
        return response.ok ? response.json() : null;
      })
      .then(function (data) { if (data) { update(data); } })
      .catch(function () { /* offline: try again next minute */ });
  }

  function schedule() {
    if (timer) { window.clearInterval(timer); timer = null; }
    if (document.visibilityState === "visible") { timer = window.setInterval(refresh, REFRESH_MS); }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") { refresh(); }
    schedule();
  });
  schedule();
}());
