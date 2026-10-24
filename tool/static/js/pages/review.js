/* review.js — flagged shots (review / failed) with QA scores, attempts,
   error, side-by-side keyframe vs clip, Approve / Requeue. */
(function () {
  "use strict";

  function render(el) {
    if (!State.pid) { el.innerHTML = noProject(); return; }
    const shots = State.shots || [];
    const flagged = shots.filter(function (r) {
      return r.status === "review" || r.status === "failed";
    }).sort(function (a, b) {
      return (a.scene || "").localeCompare(b.scene || "") ||
        (a.shot_id || "").localeCompare(b.shot_id || "");
    });
    const reviewN = shots.filter(function (r) { return r.status === "review"; }).length;
    const failedN = shots.filter(function (r) { return r.status === "failed"; }).length;
    el.innerHTML =
      '<div class="page-slate">' +
        "<h1>Review</h1>" +
        '<span class="slate-meta">' + reviewN + " in review · " + failedN + " failed</span>" +
        '<span class="spacer"></span>' +
        '<div class="slate-acts">' +
          (flagged.length
            ? '<button class="btn small primary" type="button" id="rv-requeue-all" ' +
                'data-tip="Requeue EVERY flagged shot of this project (review + failed) — after a pipeline fix you should not have to click Continue on each card">' +
                Icon.get("refresh") + "Requeue all (" + flagged.length + ")</button>"
            : "") +
          '<button class="btn small" type="button" id="rv-refresh">' + Icon.get("refresh") + "Refresh</button>" +
        "</div>" +
      "</div>" +
      (flagged.length
        ? '<div class="shot-grid single">' + flagged.map(function () { return ""; }).join("") + "</div>"
        : empty("Nothing flagged — all clear", "Shots land here when QA fails or a render errors out."));
    const wrap = el.querySelector(".shot-grid.single");
    if (wrap) flagged.forEach(function (r) { wrap.appendChild(UI.shotCard(r, { review: true })); });
    const rb = el.querySelector("#rv-refresh");
    if (rb) rb.onclick = function () { State.refreshShots(); };
    const ra = el.querySelector("#rv-requeue-all");
    if (ra) ra.onclick = function () {
      Dlg.confirm({
        title: "Requeue all flagged shots",
        message: "Set " + flagged.length + " review/failed shot(s) of this project "
          + "back to queued? The orchestrator picks them up and re-renders them.",
        okLabel: "Requeue " + flagged.length,
      }).then(async function (yes) {
        if (!yes) return;
        const r = await post("/api/review/requeue-all",
                             { project_id: State.pid }, 120000);
        if (r && r.ok) {
          toast(r.requeued + " shot(s) requeued");
          await State.refreshShots();
        } else {
          toast((r && r.error) || "requeue failed", true);
        }
      });
    };
  }

  Router.register("review", {
    polls: ["status", "shots"],
    subs: [],
    mount: function (el) {
      const page = this;
      page.el = el;
      page.render = function () { render(el); };
      page.subs.push(Bus.on("shots", page.render));
      page.render();
    },
    unmount: function () {
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      if (this.el) this.el.innerHTML = "";
    },
  });
})();
