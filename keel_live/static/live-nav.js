"use strict";
// Added by the integrated server; original Workbench files remain unchanged.
const originalLiveApi = api;
api = async function (...args) {
  try {return await originalLiveApi(...args);}
  catch (error) {
    state.view = null; state.report = null; state.catalog = []; state.history = [];
    const detail = document.querySelector("#detail");
    if (detail.open) detail.close();
    document.querySelector("#detail-content").replaceChildren();
    document.querySelector("#mode-pill").textContent = "Unverified";
    const notice = document.querySelector("#notice");
    notice.hidden = false;
    notice.textContent = "The latest host check failed. Previous readiness and reports have been cleared. Refresh evidence to reconnect.";
    showLogin("The host view is unverified. " + error.message);
    throw error;
  }
};
const liveReviewButton = document.createElement("button");
liveReviewButton.type = "button";
liveReviewButton.textContent = "◇ Material review";
liveReviewButton.addEventListener("click", () => {
  location.assign("/review" + (state.token ? "#token=" + encodeURIComponent(state.token) : ""));
});
document.querySelector("#navigation").prepend(liveReviewButton);
function hostOwnedImportNotice() {
  const form = document.querySelector("#import-form");
  if (!form) return;
  const notice = document.createElement("p");
  notice.className = "callout";
  notice.textContent = "This workspace reads the trusted host export. Supply fresh captures through the configured host adapter; browser snapshot imports are disabled.";
  form.replaceWith(notice);
}
new MutationObserver(hostOwnedImportNotice).observe(document.querySelector("#main"), {childList: true, subtree: true});
hostOwnedImportNotice();
