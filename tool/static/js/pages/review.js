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
      '<header class="page-intro">' +
        '<div class="pi-text"><h1>Review</h1>' +
          "<p>The shots that need your eye: a check flagged them, or the render failed. Approve a shot to keep it, " +
          "or send it back to render again.</p></div>" +
        '<div class="pi-acts">' +
          (flagged.length
            ? '<span class="pi-stat"><b>' + reviewN + "</b> to review, <b>" + failedN + "</b> failed</span>" +
              '<button class="btn small" type="button" id="rv-requeue-all" ' +
                'data-tip="Send every flagged shot of this movie back to render, instead of one card at a time">' +
                Icon.get("refresh") + "Render all " + flagged.length + " again</button>"
            : "") +
          '<button class="btn small ghost icon" type="button" id="rv-refresh" data-tip="Refresh">' + Icon.get("refresh") + "</button>" +
        "</div>" +
      "</header>" +
      (flagged.length
        ? '<div class="shot-grid single">' + flagged.map(function () { return ""; }).join("") + "</div>"
        : '<div class="clear-card">' + Icon.get("shield-check") +
            "<div><b>Nothing to review.</b><p>Shots come here when their check flags them or a render fails. " +
            "Passed shots go straight to the Film.</p></div></div>");
    const wrap = el.querySelector(".shot-grid.single");
    if (wrap) flagged.forEach(function (r) { wrap.appendChild(UI.shotCard(r, { review: true })); });
    const rb = el.querySelector("#rv-refresh");
    if (rb) rb.onclick = function () { State.refreshShots(); };
    const ra = el.querySelector("#rv-requeue-all");
    if (ra) ra.onclick = function () {
      Dlg.confirm({
        title: "Render every flagged shot again?",
        message: "The " + flagged.length + " flagged shots of this movie go back in the queue, "
          + "and the renderer renders them again.",
        okLabel: "Render " + flagged.length + " again",
      }).then(async function (yes) {
        if (!yes) return;
        const r = await post("/api/review/requeue-all",
                             { project_id: State.pid }, 120000);
        if (r && r.ok) {
          toast(r.requeued + (r.requeued === 1 ? " shot is" : " shots are") + " back in the queue");
          await State.refreshShots();
        } else {
          toast((r && r.error) || "Could not send them back", true);
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
