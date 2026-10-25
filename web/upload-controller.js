(function attachUploadController(root, createModule) {
  const moduleApi = createModule();
  if (typeof module === "object" && module.exports) {
    module.exports = moduleApi;
  } else {
    root.MatrixMotionUploadController = moduleApi;
  }
})(globalThis, function createUploadControllerModule() {
  function createUploadController(options) {
    let uploading = false;

    async function handleSelection(file) {
      if (!file) return { status: "empty" };
      if (uploading) return { status: "busy" };

      const fileName = String(file.name || "");
      if (!/\.(mp4|mov)$/iu.test(fileName)) {
        const message = "Solo se admiten archivos .MP4 y .MOV.";
        options.setMessage(message, { error: true });
        options.notify(message, true);
        return { status: "invalid" };
      }

      uploading = true;
      const niche = options.getNiche();
      const styleNotes = options.getStyleNotes();
      const editingStyle = options.getEditingStyle?.() || "dynamic";
      options.setBusy(true);
      options.setMessage(`Archivo seleccionado: ${fileName}. Subiendo a la biblioteca…`, { busy: true });

      try {
        const response = await options.upload(file, niche, styleNotes, editingStyle);
        const uploadedItem = response?.item ?? response;
        if (!uploadedItem?.id) {
          throw new Error("El servidor no devolvió el identificador del vídeo guardado.");
        }

        const current = options.getLibrary();
        options.setLibrary([
          uploadedItem,
          ...current.filter((item) => item.id !== uploadedItem.id),
        ]);
        options.setSelectedId(uploadedItem.id);
        options.renderLibrary();
        options.renderSelectedItem();
        options.clearStyleNotes();
        options.setMessage(`Vídeo guardado: ${fileName}. Actualizando la biblioteca…`, { busy: true });

        try {
          const refreshed = await options.loadLibrary();
          options.setLibrary(refreshed);
          options.setSelectedId(uploadedItem.id);
          options.renderLibrary();
          options.renderSelectedItem();
          options.setMessage(`Vídeo guardado: ${fileName}. Revisa sus datos antes de iniciar el procesamiento.`);
          options.notify("Vídeo añadido a la biblioteca.");
          return { status: "uploaded", item: uploadedItem };
        } catch (refreshError) {
          options.setMessage(`Vídeo guardado: ${fileName}. La biblioteca no se pudo actualizar automáticamente; pulsa «Actualizar datos».`, { error: true });
          options.notify("El vídeo se guardó, pero no se pudo actualizar la biblioteca.", true);
          return { status: "uploaded_refresh_failed", item: uploadedItem, error: refreshError };
        }
      } catch (error) {
        options.setMessage(`No se pudo cargar ${fileName}: ${error.message}`, { error: true });
        options.notify(`Error de carga: ${error.message}`, true);
        return { status: "failed", error };
      } finally {
        uploading = false;
        options.setBusy(false);
      }
    }

    return {
      handleSelection,
      get isUploading() {
        return uploading;
      },
    };
  }

  function bindFileInput(input, controller) {
    if (!input || typeof input.addEventListener !== "function") {
      throw new TypeError("No se encontró el selector de archivos de vídeo.");
    }
    input.addEventListener("change", (event) => {
      const target = event.currentTarget || event.target || input;
      return controller.handleSelection(target.files?.[0]).finally(() => {
        target.value = "";
      });
    });
  }

  return { createUploadController, bindFileInput };
});
