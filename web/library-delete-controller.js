(function attachLibraryDeleteController(root, createModule) {
  const moduleApi = createModule();
  if (typeof module === "object" && module.exports) {
    module.exports = moduleApi;
  } else {
    root.MatrixMotionLibraryDelete = moduleApi;
  }
})(globalThis, function createLibraryDeleteControllerModule() {
  function createLibraryDeleteController({
    confirmDelete,
    deleteVideo,
    removeVideo,
    notify,
  }) {
    if (
      typeof confirmDelete !== "function"
      || typeof deleteVideo !== "function"
      || typeof removeVideo !== "function"
      || typeof notify !== "function"
    ) {
      throw new TypeError("Library delete controller requires complete callbacks.");
    }

    const activeStatuses = new Set(["queued", "processing"]);
    const pendingIds = new Set();

    return {
      async deleteSelected(item) {
        if (!item?.id) return false;
        const id = String(item.id);
        if (pendingIds.has(id)) return false;
        if (activeStatuses.has(String(item.status || "").toLowerCase())) {
          notify("No se puede eliminar un vídeo mientras está en procesamiento.", true);
          return false;
        }

        if (!confirmDelete(item)) return false;
        pendingIds.add(id);
        try {
          await deleteVideo(id);
          removeVideo(id);
          notify("Vídeo eliminado. Los resultados e historial existentes se conservan.", false);
          return true;
        } catch (error) {
          notify(`No se pudo eliminar el vídeo: ${error.message}`, true);
          return false;
        } finally {
          pendingIds.delete(id);
        }
      },
    };
  }

  return { createLibraryDeleteController };
});
