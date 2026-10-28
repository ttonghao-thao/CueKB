/* Same-tab review routes. Back restores the calling view/detail and filters. */
let reviewNavigating = false;
function enterReviewPage(view, documentId, versionId) {
  const previous = state.view;
  if (!reviewNavigating && (history.state?.reviewRoute?.view !== view || history.state?.reviewRoute?.versionId !== versionId)) {
    const origin = history.state?.reviewRoute ? history.state : {
      portalReturn: {view: previous, documentId: $("#document-dialog").open ? state.lastDocument : null, scroll: window.scrollY}
    };
    history.replaceState({...origin, scroll: window.scrollY}, "");
    history.pushState({canGoBackReview:true, reviewRoute: {view, documentId, versionId}}, "", `#${view}/${documentId}/${versionId}`);
  }
  if ($("#document-dialog").open) $("#document-dialog").close();
  showView(view);
  window.scrollTo(0, 0);
}
function returnFromReview() {
  if (history.state?.reviewRoute && history.state.canGoBackReview) history.back();
  else {history.replaceState({portalReturn:{view:"documents"}},"","#documents");showView("documents");}
}
function leaveReviewForUpload() {
  history.replaceState({portalReturn: {view: "upload"}}, "", "#upload");
  showView("upload");
}
window.addEventListener("popstate", async event => {
  reviewNavigating = true;
  try {
    if (event.state?.reviewRoute) {
      const r = event.state.reviewRoute;
      if (r.view === "review") await openProcessingReview(r.documentId, r.versionId, true);
      else await openLegacyProcessingReview(r.documentId, r.versionId);
    } else {
      const r = event.state?.portalReturn || {view: "documents"};
      showView(r.view);
      if (r.documentId && !state.deletedDocuments.has(r.documentId)) await openDocument(r.documentId);
    }
    window.scrollTo(0, event.state?.scroll || event.state?.portalReturn?.scroll || 0);
  } finally {reviewNavigating = false;}
});
$$("[data-review-back]").forEach(button => button.addEventListener("click", returnFromReview));

function resetReviewNavigation() {
  clearTimeout(choiceState.timer); choiceState.products.clear(); choiceState.selectedFacts.clear();
  choiceState.request++;choiceState.selected.clear();choiceState.plan=null;choiceState.page=null;choiceState.selectionRevision=null;
  state.reviewRequest++;state.review=null;
  $("#choice-items").innerHTML="";
  if (state.view.startsWith("review")) {
    history.replaceState({portalReturn:{view:"documents"}},"","#documents");showView("documents");
  }
}

function beforePortalViewChange(name) {
  if (!reviewNavigating && state.view.startsWith("review") && !name.startsWith("review") && history.state?.reviewRoute) {
    history.pushState({portalReturn:{view:name}},"",`#${name}`);
  }
}
async function resumeReviewRoute() {
  const match=location.hash.match(/^#(review|review-advanced)\/([a-f0-9-]{36})\/([a-f0-9-]{36})$/i);
  if (!match) return;
  if (!history.state?.reviewRoute) history.replaceState({canGoBackReview:false,reviewRoute:{view:match[1],documentId:match[2],versionId:match[3]}},"");
  reviewNavigating=true;
  try {
    if (match[1]==="review") await openProcessingReview(match[2],match[3],true);
    else await openLegacyProcessingReview(match[2],match[3]);
  } finally {reviewNavigating=false;}
}
