(function attachLibrarySelectionController(root, createModule) {
  const moduleApi = createModule();
  if (typeof module === "object" && module.exports) {
    module.exports = moduleApi;
  } else {
    root.MatrixMotionLibrarySelection = moduleApi;
  }
})(globalThis, function createLibrarySelectionControllerModule() {
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

    return { selectExistingVideo };
  }

  return { createLibrarySelectionController };
});
