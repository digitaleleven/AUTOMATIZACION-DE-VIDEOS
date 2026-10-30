(function attachLibrarySelectionController(root, createModule) {
  const moduleApi = createModule();
  if (typeof module === "object" && module.exports) {
    module.exports = moduleApi;
  } else {
    root.MatrixMotionLibrarySelection = moduleApi;
  }
})(globalThis, function createLibrarySelectionControllerModule() {
  const errorStatuses = new Set([
    "failed", "error", "rejected", "interrupted", "render_failed", "upload_failed",
  ]);

  function isErrorStatus(status) {
    return errorStatuses.has(String(status || "").toLowerCase());
  }

  function getLibraryAction(status) {
    const normalized = String(status || "").toLowerCase();
    if (normalized === "ready") return "create";
    if (isErrorStatus(normalized)) return "review_error";
    return null;
  }

  function createLibrarySelectionController(options) {
    function selectExistingVideo(videoId) {
      const id = String(videoId ?? "");
      if (!id || options.isAvailable?.(id) === false) return false;

      options.setSelectedId(id);
      options.clearAuthorizations?.();
      options.setActivePage("create");
      options.renderLibrary();
      options.renderSelectedItem();
      return true;
    }

    function createWithExistingVideo(videoId) {
      return selectExistingVideo(videoId);
    }

    function reviewVideoError(videoId) {
      const id = String(videoId ?? "");
      if (!id || options.isErrorVideo?.(id) === false) return false;
      if (!selectExistingVideo(id)) return false;
      options.focusError?.(id);
      return true;
    }

    return { selectExistingVideo, createWithExistingVideo, reviewVideoError };
  }

  return { createLibrarySelectionController, getLibraryAction, isErrorStatus };
});
