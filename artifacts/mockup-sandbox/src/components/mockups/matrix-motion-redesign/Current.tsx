import './_group.css';

/** Extracted visual baseline from web/index.html and web/premium.css. */
export function Current() {
  return (
    <div className="app-shell current-extract">
      <aside className="sidebar" aria-label="Navegación principal">
        <a className="brand" href="#inicio">
          <span className="brand-mark" aria-hidden="true">M</span>
          <span className="brand-copy"><strong>MATRIX MOTION</strong><small>VIDEO AUTOMATION STUDIO</small></span>
        </a>
        <div className="sidebar-divider" />
        <p className="nav-caption">ESPACIO DE TRABAJO</p>
        <nav className="primary-nav">
          <button className="rail-link active" type="button"><span className="nav-symbol">⌂</span>Inicio</button>
          <button className="rail-link" type="button"><span className="nav-symbol">▤</span>Biblioteca</button>
          <button className="rail-link" type="button"><span className="nav-symbol">▣</span>Estudio de edición</button>
          <button className="rail-link" type="button"><span className="nav-symbol">⌁</span>Procesamiento</button>
          <button className="rail-link" type="button"><span className="nav-symbol">☷</span>Configuración</button>
        </nav>
        <div className="sidebar-safety">
          <div className="local-indicator"><i /> Entorno local</div>
          <p>Los originales permanecen intactos. No hay envíos sin autorización explícita.</p>
          <button className="text-button" type="button">Revisar conexión →</button>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div className="breadcrumb"><span>MATRIX MOTION</span><b>/</b><span>VIDEO AUTOMATION STUDIO</span></div>
          <div className="topbar-right"><span className="local-pill"><i /> Entorno local</span><button className="quiet-button" type="button">Actualizar datos ↻</button><button className="quiet-button" type="button">Cerrar sesión</button></div>
        </header>
        <div className="content">
          <section className="intro" id="inicio">
            <div><p className="eyebrow">VIDEO AUTOMATION STUDIO · VERTICAL 9:16</p><h1>Vídeos para <span>publicar.</span></h1><p className="intro-copy">De vídeo original a contenido listo para publicar. Prepara tus vídeos verticales, revisa cada resultado y autoriza el procesamiento cuando quieras.</p></div>
            <div className="hero-action"><button className="hero-upload" type="button"><span className="upload-mark">＋</span>Añadir vídeo<b>→</b></button><p>MP4 o MOV · H.264 o HEVC</p></div>
          </section>
          <section className="summary-strip">
            <article className="summary-item"><span className="summary-mark" /><div><p>PENDIENTES</p><strong>—</strong><small>Pendiente de actualizar</small></div></article>
            <article className="summary-item"><span className="summary-mark processed-mark" /><div><p>PROCESADOS</p><strong>—</strong><small>Archivos con resultado</small></div></article>
            <article className="summary-item"><span className="summary-mark error-mark" /><div><p>ERRORES</p><strong>—</strong><small>Requieren revisión</small></div></article>
          </section>
          <div className="notice-band"><span className="notice-icon">i</span><p><strong>El original no se modifica.</strong> OpenAI y el motor de render solo se usan al iniciar un procesamiento con tu autorización explícita.</p><span className="notice-local">SIN ENVÍOS AUTOMÁTICOS</span></div>
          <section className="section-block">
            <div className="section-head"><div><p className="eyebrow">01 / ARCHIVOS</p><h2>Tu biblioteca</h2></div><span className="count-tag">Pendiente de actualizar</span></div>
            <div className="library-panel"><div className="list-toolbar">VÍDEOS DISPONIBLES<button className="small-action" type="button">＋ Añadir vídeo</button></div><div className="empty-state"><div className="empty-glyph">▧</div><h3>Tu biblioteca está vacía</h3><p>Añade tu primer vídeo para empezar a crear contenido.</p><button className="outline-button" type="button">Añadir un vídeo</button></div></div>
          </section>
          <section className="section-block"><div className="section-head"><div><p className="eyebrow">02 / ESTUDIO</p><h2>Vídeo original y resultado</h2></div></div><div className="studio-grid"><article className="preview-card"><div className="preview-head">ORIGINAL <span>ORIGINAL PROTEGIDO</span></div><div className="video-stage"><div className="stage-placeholder"><div className="play-outline">▶</div><p>Selecciona un vídeo para ver el original</p></div></div></article><article className="preview-card output-card"><div className="preview-head">VÍDEO VERTICAL <span>1080 × 1920 · 9:16</span></div><div className="output-empty"><div className="output-symbol">▣</div><strong>Aún no hay resultado</strong><p>El vídeo editado aparecerá aquí cuando termine el procesamiento.</p></div></article></div></section>
          <section className="section-block"><div className="section-head"><div><p className="eyebrow">03 / PROCESO</p><h2>Estado y costes</h2></div></div><div className="process-layout"><div className="job-content"><div className="job-empty"><span className="job-empty-line" /><div><strong>Todavía no has iniciado ningún procesamiento</strong><p>El estado y los errores aparecerán aquí cuando haya información disponible.</p></div></div></div><aside className="process-card"><div className="card-kicker">ANTES DE CONTINUAR</div><div className="cost-box"><span className="cost-label">ESTIMACIÓN DE COSTE</span><strong>Desconocido</strong><p>Selecciona un vídeo para consultar la estimación disponible.</p></div><button className="outline-button" type="button">Preparar propuesta local</button></aside></div></section>
          <footer><span>MATRIX MOTION / ENTORNO LOCAL</span><span>Los originales permanecen intactos.</span></footer>
        </div>
      </main>
    </div>
  );
}
