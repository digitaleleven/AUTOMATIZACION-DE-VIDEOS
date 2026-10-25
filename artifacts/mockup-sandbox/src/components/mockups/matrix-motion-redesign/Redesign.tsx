import { useRef, useState } from 'react';
import './_group.css';
import './Redesign.css';

type View = 'Inicio' | 'Crear vídeo' | 'Mis vídeos' | 'Ajustes';
type VideoItem = { name: string; size: string; duration: string; resolution: string; status: string; date: string; source: string; mock?: boolean };

const categories = ['Beauty & Skincare', 'Fashion & Style', 'Lifestyle & Travel', 'Fitness & Sport', 'UGC & Productos', 'Genérico/Viral'];
const mockItems: VideoItem[] = [
  { name: 'rutina-mañana.mov', size: '184,6 MB', duration: '00:42', resolution: '2160 × 3840', status: 'Listo para revisar', date: 'Hoy · 10:42', source: 'Equipo', mock: true },
  { name: 'lanzamiento-serum.mp4', size: '92,3 MB', duration: '01:18', resolution: '1080 × 1920', status: 'Procesando', date: 'Ayer · 16:08', source: 'Google Drive', mock: true },
  { name: 'lookbook-verano.mp4', size: '327,1 MB', duration: '00:56', resolution: '3840 × 2160', status: 'Completado', date: '12 jun · 09:31', source: 'Equipo', mock: true },
];

const initialSettings = [
  ['Tipografía', 'Montserrat'],
  ['Tamaño del texto', '54'],
  ['Grosor del contorno', '3'],
  ['Color del texto', '#FFFFFF'],
  ['Color del contorno', '#10272B'],
  ['Color de resaltado', '#12C8BE'],
];

function BrandMark() {
  return <span className="mm-mark" aria-hidden="true"><svg viewBox="0 0 40 40"><path d="M7 10v20M7 20h8l4-7 4 14 4-7h5M29 10h4v20h-4" /></svg></span>;
}

function Icon({ name }: { name: string }) {
  const paths: Record<string, string> = {
    home: 'M3 10.5 12 3l9 7.5M5.5 9v11h13V9M9 20v-6h6v6',
    create: 'M12 3v18M3 12h18',
    library: 'M5 4h14v16H5zM8 8h8M8 12h8M8 16h5',
    settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM4 12l-2-1 1-3 2 .1 1-1.7L5 4l3-1 1.2 1.6h2L13 3l3 1-.2 2.3 1.7 1.3 2.1-.4 1 3-1.7 1.2v2L21 15l-1 3-2.3-.2-1.3 1.7.4 2.1-3 1L12.5 21h-2L9 22.6l-3-1 .2-2.3-1.7-1.3-2.1.4-1-3 1.7-1.2v-2Z',
    arrow: 'M4 12h15M13 5l7 7-7 7',
    upload: 'M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4',
    check: 'm5 12 4 4L19 6',
    play: 'm9 6 10 6-10 6z',
    cloud: 'M7 18h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 9a4.5 4.5 0 0 0 1 9Z',
    refresh: 'M20 7v5h-5M4 17v-5h5m10-2a7 7 0 0 0-12-3L4 9m16 2-3 2a7 7 0 0 1-12 3',
  };
  return <svg className="mm-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.check} /></svg>;
}

export function Redesign() {
  const [view, setView] = useState<View>('Inicio');
  const [items, setItems] = useState<VideoItem[]>(mockItems);
  const [category, setCategory] = useState('Genérico/Viral');
  const [style, setStyle] = useState('');
  const [notes, setNotes] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState('');
  const [metadata, setMetadata] = useState<{ duration: string; resolution: string } | null>(null);
  const [dragging, setDragging] = useState(false);
  const [authorized, setAuthorized] = useState(false);
  const [unknownCost, setUnknownCost] = useState(false);
  const [driveConnected, setDriveConnected] = useState(false);
  const [toast, setToast] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const visibleItems = items.slice(0, 3);

  function notify(message: string) {
    setToast(message);
    window.setTimeout(() => setToast(''), 2800);
  }

  function receiveFile(next: File | undefined) {
    if (!next) return;
    if (!/\.(mp4|mov)$/i.test(next.name)) {
      notify('Selecciona un archivo MP4 o MOV.');
      return;
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    const url = URL.createObjectURL(next);
    setFile(next);
    setPreviewUrl(url);
    setMetadata(null);
    setView('Crear vídeo');
    const probe = document.createElement('video');
    probe.preload = 'metadata';
    probe.onloadedmetadata = () => {
      setMetadata({
        duration: Number.isFinite(probe.duration) ? `${Math.floor(probe.duration / 60)}:${String(Math.round(probe.duration % 60)).padStart(2, '0')}` : 'No disponible',
        resolution: probe.videoWidth && probe.videoHeight ? `${probe.videoWidth} × ${probe.videoHeight}` : 'No disponible',
      });
    };
    probe.onerror = () => setMetadata({ duration: 'No disponible', resolution: 'No disponible' });
    probe.src = url;
  }

  function saveToMockLibrary() {
    if (!file) {
      inputRef.current?.click();
      return;
    }
    setItems((current) => [{
      name: file.name,
      size: `${(file.size / 1024 / 1024).toFixed(1)} MB`,
      duration: metadata?.duration || 'Leyendo…',
      resolution: metadata?.resolution || 'Leyendo…',
      status: 'En biblioteca',
      date: 'Ahora · vista local',
      source: 'Equipo',
      mock: true,
    }, ...current]);
    notify('Añadido a la vista de biblioteca de este mockup.');
    setView('Mis vídeos');
  }

  function openCreator() {
    setView('Crear vídeo');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  const navItems: { title: View; icon: string }[] = [
    { title: 'Inicio', icon: 'home' },
    { title: 'Crear vídeo', icon: 'create' },
    { title: 'Mis vídeos', icon: 'library' },
    { title: 'Ajustes', icon: 'settings' },
  ];

  const topTitle = view === 'Inicio' ? 'Vista general' : view;
  return (
    <div className="mm-app">
      <aside className="mm-sidebar">
        <a className="mm-brand" href="#inicio" onClick={(event) => { event.preventDefault(); setView('Inicio'); }}>
          <BrandMark /><span><b>MATRIX MOTION</b><small>VIDEO STUDIO</small></span>
        </a>
        <div className="mm-workspace-label">ESPACIO DE TRABAJO</div>
        <nav className="mm-nav" aria-label="Navegación principal">
          {navItems.map((item) => <button key={item.title} className={`mm-nav-item ${view === item.title ? 'active' : ''}`} onClick={() => setView(item.title)} type="button"><Icon name={item.icon} /><span>{item.title}</span>{view === item.title && <i />}</button>)}
        </nav>
        <div className="mm-sidebar-spacer" />
        <div className="mm-safety-card">
          <span className="mm-safety-status"><i /> CONTROL DE PROPIETARIA</span>
          <p>Los originales permanecen intactos. Ninguna acción de pago ocurre sin tu autorización.</p>
          <button type="button" onClick={() => setView('Ajustes')}>Revisar conexiones <Icon name="arrow" /></button>
        </div>
        <div className="mm-owner"><span className="mm-avatar">MM</span><span><b>Propietaria</b><small>Cuenta privada</small></span><span className="mm-owner-dots">···</span></div>
      </aside>
      <main className="mm-main">
        <header className="mm-topbar">
          <div className="mm-mobile-brand"><BrandMark /><b>MATRIX MOTION</b></div>
          <div className="mm-crumb"><span>Workspace</span><b>/</b><strong>{topTitle}</strong></div>
          <div className="mm-top-actions"><span className="mm-mock-badge"><i /> MOCKUP · DATOS DE DEMOSTRACIÓN</span><button className="mm-icon-button" aria-label="Actualizar vista" onClick={() => notify('Vista de demostración actualizada.')} type="button"><Icon name="refresh" /></button><span className="mm-top-avatar">MM</span></div>
        </header>
        <div className="mm-content">
          {view === 'Inicio' && <section className="mm-page mm-enter" id="inicio">
            <div className="mm-hero">
              <div className="mm-hero-copy"><div className="mm-eyebrow"><span>ESTUDIO DE VÍDEO</span><i /> <span>WORKSPACE PERSONAL</span></div><h1>Tu vídeo.<br /><em>Otro nivel.</em></h1><p>Convierte tus originales en ediciones verticales listas para revisión. Tú mantienes el control, desde el primer archivo hasta cada acción de pago.</p><button className="mm-primary" onClick={openCreator} type="button"><Icon name="create" /> Nuevo vídeo <Icon name="arrow" /></button><div className="mm-hero-foot"><span className="mm-check-dot"><Icon name="check" /></span> Originales intactos <span className="mm-hero-sep" /> Autorización siempre explícita</div></div>
              <div className="mm-hero-visual" aria-label="Resumen de flujo de vídeo"><div className="mm-visual-grid" /><div className="mm-frame-outline"><span className="mm-frame-chip">9:16</span><div className="mm-frame-center"><span className="mm-play"><Icon name="play" /></span><span>PREVIEW DE EDICIÓN</span></div><span className="mm-frame-line" /></div><div className="mm-visual-caption"><span><i /> FLUJO SEGURO</span><b>01 — 03</b></div><div className="mm-floating-stat"><span>ORIGINAL</span><b>Siempre protegido</b><small>Sin sobrescritura</small></div></div>
            </div>
            <div className="mm-section-title mm-recent-title"><div><span className="mm-eyebrow">TU ESPACIO DE TRABAJO</span><h2>Mis últimos vídeos</h2></div><button className="mm-text-link" onClick={() => setView('Mis vídeos')} type="button">Ver biblioteca <Icon name="arrow" /></button></div>
            <div className="mm-mock-disclaimer"><span>i</span><p><b>Vista de prototipo.</b> Los elementos mostrados abajo son datos de mockup; no representan tu biblioteca real.</p><span className="mm-demo-tag">MOCK DATA</span></div>
            <div className="mm-video-list">
              {visibleItems.map((item, index) => <VideoRow key={`${item.name}-${index}`} item={item} onSelect={() => setView('Mis vídeos')} />)}
            </div>
            <div className="mm-bottom-note"><span className="mm-mini-shield"><Icon name="check" /></span><span><b>Tu espacio, bajo tu control.</b> Revisar es gratis. Procesar requiere autorización explícita.</span><button type="button" onClick={() => setView('Ajustes')}>Ver ajustes <Icon name="arrow" /></button></div>
          </section>}

          {view === 'Crear vídeo' && <section className="mm-page mm-enter">
            <div className="mm-page-heading"><div><span className="mm-eyebrow">NUEVO PROYECTO <i /> VERTICAL 9:16</span><h1>Crear vídeo</h1><p>Prepara tu edición en pasos claros. No se inicia ningún servicio de pago durante esta configuración.</p></div><span className="mm-mock-badge compact"><i /> MOCKUP</span></div>
            <div className="mm-stepper" aria-label="Progreso de 5 pasos"><div className="mm-step active"><b>01</b><span>Vídeo</span></div><i /><div className={`mm-step ${file ? 'active' : ''}`}><b>02</b><span>Categoría</span></div><i /><div className={`mm-step ${style ? 'active' : ''}`}><b>03</b><span>Estilo</span></div><i /><div className="mm-step muted"><b>04</b><span>Personal</span></div><i /><div className="mm-step muted"><b>05</b><span>No especificado</span></div></div>
            <div className="mm-step-warning"><span>i</span><p>El prototipo solo muestra los elementos confirmados. La referencia disponible termina en «Paso 4: Personal»; no se define aquí su comportamiento ni el paso siguiente.</p></div>
            <div className="mm-create-grid">
              <div className="mm-create-main">
                <section className="mm-form-section">
                  <div className="mm-form-title"><span className="mm-step-index">01</span><div><h2>Sube tu vídeo</h2><p>El original se conserva. Elige un archivo de tu equipo o selecciona uno de Drive.</p></div></div>
                  <div className={`mm-dropzone ${dragging ? 'dragging' : ''} ${file ? 'has-file' : ''}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); receiveFile(event.dataTransfer.files[0]); }}>
                    {file ? <div className="mm-selected-file"><div className="mm-file-symbol">MP4</div><div className="mm-file-copy"><b>{file.name}</b><span>{(file.size / 1024 / 1024).toFixed(1)} MB · {metadata?.duration || 'Leyendo metadatos…'} · {metadata?.resolution || 'resolución detectada localmente'}</span></div><button type="button" onClick={() => { URL.revokeObjectURL(previewUrl); setFile(null); setPreviewUrl(''); setMetadata(null); }} aria-label="Quitar archivo">×</button></div> : <><span className="mm-upload-icon"><Icon name="upload" /></span><b>Suelta el vídeo aquí</b><span>o <button onClick={() => inputRef.current?.click()} type="button">selecciona un archivo</button> desde tu equipo</span><small>MP4 o MOV · Vista previa y metadatos leídos en este navegador</small></>}
                  </div>
                  <input ref={inputRef} className="mm-hidden-input" type="file" accept=".mp4,.mov,video/mp4,video/quicktime" onChange={(event) => receiveFile(event.currentTarget.files?.[0])} />
                  {file && <div className="mm-preview-card"><div className="mm-preview-video">{previewUrl && <video src={previewUrl} controls playsInline preload="metadata" />}</div><div className="mm-metadata"><div><small>DURACIÓN</small><b>{metadata?.duration || 'Leyendo…'}</b></div><div><small>RESOLUCIÓN</small><b>{metadata?.resolution || 'Leyendo…'}</b></div><div><small>TAMAÑO</small><b>{(file.size / 1024 / 1024).toFixed(1)} MB</b></div><div><small>FORMATO</small><b>{file.name.split('.').pop()?.toUpperCase()}</b></div></div></div>}
                </section>
                <section className="mm-form-section">
                  <div className="mm-form-title"><span className="mm-step-index">02</span><div><h2>Elige una categoría</h2><p>Ayuda a orientar el tono de la edición.</p></div></div>
                  <div className="mm-category-grid">{categories.map((name, index) => <button key={name} type="button" className={`mm-category ${category === name ? 'selected' : ''}`} onClick={() => setCategory(name)}><span className="mm-category-no">0{index + 1}</span><b>{name}</b>{category === name && <span className="mm-checkmark"><Icon name="check" /></span>}</button>)}</div>
                </section>
                <section className="mm-form-section">
                  <div className="mm-form-title"><span className="mm-step-index">03</span><div><h2>Selecciona un estilo</h2><p>Elige el ritmo visual que mejor encaja con tu vídeo.</p></div></div>
                  <div className="mm-style-grid">{['Natural Premium', 'Dinámico'].map((name, index) => <button className={`mm-style-card ${style === name ? 'selected' : ''}`} key={name} onClick={() => setStyle(name)} type="button"><div className={`mm-style-art art-${index}`}><span className="mm-style-art-line" /><span className="mm-style-art-dot" /><span className="mm-style-art-text">{index === 0 ? 'NATURAL' : 'RITMO'}</span></div><span className="mm-style-radio">{style === name && <i />}</span><b>{name}</b><small>{index === 0 ? 'Cortes suaves y una presencia cuidada.' : 'Una cadencia ágil con energía visual.'}</small></button>)}</div>
                  <label className="mm-field-label" htmlFor="style-note">Notas de estilo <span>OPCIONAL</span></label><textarea id="style-note" className="mm-textarea" value={notes} onChange={(event) => setNotes(event.target.value)} maxLength={500} placeholder="Añade contexto o instrucciones para este vídeo…" /><div className="mm-note-foot"><span>Las indicaciones se conservan como briefing.</span><span>{notes.length}/500</span></div>
                </section>
              </div>
              <aside className="mm-control-panel">
                <div className="mm-control-header"><span className="mm-eyebrow">CONTROL DE PROCESAMIENTO</span><span className="mm-lock">PROPIETARIA</span></div>
                <div className="mm-engine-label">MOTOR DISPONIBLE</div><div className="mm-engine"><span className="mm-engine-dot" /><div><b>Remotion + FFmpeg</b><small>Procesamiento en Modal</small></div><span className="mm-engine-check"><Icon name="check" /></span></div>
                <div className="mm-cost-panel"><span>ESTIMACIÓN DE COSTE</span><b>Desconocido</b><p>No hay un coste confirmado para esta ejecución. Se mostrará antes de cualquier autorización.</p></div>
                <div className="mm-privacy-note"><span className="mm-mini-shield"><Icon name="check" /></span><p><b>Tu original permanece intacto.</b><br />No se sobrescribe ni se procesa automáticamente.</p></div>
                <label className="mm-consent"><input type="checkbox" checked={authorized} onChange={(event) => setAuthorized(event.target.checked)} /><span>Autorizo explícitamente el uso del vídeo para esta acción.</span></label>
                <label className="mm-consent"><input type="checkbox" checked={unknownCost} onChange={(event) => setUnknownCost(event.target.checked)} /><span>Entiendo que el coste es desconocido y podría haber cargos.</span></label>
                <button type="button" className="mm-primary mm-full" disabled={!file || !authorized || !unknownCost} onClick={() => notify('Prototipo únicamente: no se ha enviado ni procesado ningún archivo.')}>Autorizar procesamiento <Icon name="arrow" /></button>
                <small className="mm-button-caption">Prototipo visual: este control no inicia un render real.</small>
                <div className="mm-control-footer"><span>MODAL ONLY</span><span>NO HAY ENVÍOS AUTOMÁTICOS</span></div>
              </aside>
            </div>
          </section>}

          {view === 'Mis vídeos' && <section className="mm-page mm-enter">
            <div className="mm-page-heading"><div><span className="mm-eyebrow">ARCHIVOS Y ESTADOS</span><h1>Mis vídeos</h1><p>Biblioteca de origen y estado del trabajo, en un mismo lugar.</p></div><button className="mm-primary" type="button" onClick={openCreator}><Icon name="create" /> Nuevo vídeo <Icon name="arrow" /></button></div>
            <div className="mm-library-toolbar"><div className="mm-filter-active">Todos <span>{items.length}</span></div><span className="mm-mock-badge compact"><i /> MOCKUP · NO ES LA BIBLIOTECA REAL</span></div>
            <div className="mm-mock-disclaimer"><span>i</span><p><b>Datos de demostración.</b> Los nombres, metadatos y estados visibles no proceden del API de producción.</p><span className="mm-demo-tag">MOCK DATA</span></div>
            <div className="mm-video-list mm-library-full">{items.map((item, index) => <VideoRow key={`${item.name}-${index}`} item={item} onSelect={() => notify(`Vista de detalle de ${item.name} · datos de mockup.`)} />)}</div>
            {items.length === 0 && <div className="mm-empty"><span className="mm-empty-icon"><Icon name="library" /></span><b>Aún no hay vídeos en esta vista</b><p>Añade un vídeo para iniciar tu biblioteca.</p><button className="mm-primary" onClick={openCreator} type="button">Añadir vídeo <Icon name="arrow" /></button></div>}
          </section>}

          {view === 'Ajustes' && <section className="mm-page mm-enter">
            <div className="mm-page-heading"><div><span className="mm-eyebrow">PREFERENCIAS DEL WORKSPACE</span><h1>Ajustes</h1><p>Controles de estilo de subtítulos y conexiones para la cuenta propietaria.</p></div><span className="mm-mock-badge compact"><i /> MOCKUP</span></div>
            <div className="mm-settings-grid">
              <section className="mm-settings-card"><div className="mm-settings-card-head"><div><span className="mm-eyebrow">PRESENTACIÓN</span><h2>Subtítulos y estilo</h2></div><button className="mm-outline" type="button" onClick={() => notify('Preferencias guardadas en el estado local del mockup.')}>Guardar cambios</button></div><p className="mm-settings-intro">Se conservan las preferencias existentes de subtítulos y composición. Los cambios de esta maqueta no se guardan en el servicio.</p>
                <div className="mm-settings-form">{initialSettings.map(([label, value]) => <label key={label}>{label}<input type={label.toLowerCase().includes('color') ? 'color' : 'text'} defaultValue={value} /></label>)}
                  <label>Animación de subtítulos<select defaultValue="highlight"><option value="highlight">Resaltar palabra</option><option value="karaoke">Karaoke</option></select></label><label>Posición de subtítulos<select defaultValue="bottom"><option value="bottom">Inferior</option><option value="middle">Centro</option><option value="top">Superior</option></select></label>
                  <label className="mm-settings-toggle"><input type="checkbox" defaultChecked /><span>Mostrar subtítulos en nuevas salidas</span></label>
                  <label className="mm-settings-wide">Emojis asociados a subtítulos<textarea defaultValue={'{"palabra":"carácter Unicode"}'} /></label>
                </div>
                <div className="mm-setting-divider"><span className="mm-eyebrow">COMPOSICIÓN Y SALIDA</span></div>
                <div className="mm-settings-form">
                  <label>Ajuste vertical del subtítulo<input type="number" min="0" max="0.8" step="0.01" defaultValue="0.72" /></label>
                  <label>Ubicación del logo<input type="text" placeholder="Sin logo configurado" /></label>
                  <label>Posición del logo<select defaultValue="bottomRight"><option value="top">Arriba</option><option value="topRight">Arriba a la derecha</option><option value="right">Derecha</option><option value="bottomRight">Abajo a la derecha</option><option value="bottom">Abajo</option><option value="bottomLeft">Abajo a la izquierda</option><option value="left">Izquierda</option><option value="topLeft">Arriba a la izquierda</option><option value="center">Centro</option></select></label>
                  <label>Tamaño del logo<input type="number" min="0.02" max="0.4" step="0.01" defaultValue="0.12" /></label>
                  <label>Fotogramas por segundo<select defaultValue="30"><option>25</option><option>30</option><option>50</option><option>60</option></select></label>
                  <label>Ajuste horizontal X del logo<input type="number" min="-1" max="1" step="0.01" defaultValue="0" /></label>
                  <label>Ajuste vertical Y del logo<input type="number" min="-1" max="1" step="0.01" defaultValue="0" /></label>
                </div>
                <div className="mm-setting-divider"><span className="mm-eyebrow">LÍMITES Y COSTES</span></div>
                <div className="mm-settings-form">
                  <label>Duración máxima del vídeo (segundos)<input type="number" min="1" defaultValue="1800" /></label>
                  <label>Fragmentos máximos por transcripción<input type="number" min="1" defaultValue="8" /></label>
                  <label>OpenAI · coste por minuto de audio (USD)<input type="number" min="0" step="0.0001" placeholder="Desconocido" /></label>
                  <label>Límite estimado total (USD)<input type="number" min="0" step="0.01" placeholder="Sin límite configurado" /></label>
                </div>
              </section>
              <section className="mm-settings-card mm-integrations"><span className="mm-eyebrow">SERVICIOS CONECTADOS</span><h2>Integraciones</h2><p className="mm-settings-intro">Las conexiones se autorizan por separado. Ninguna conexión inicia un procesamiento.</p>
                <div className="mm-integration-row"><span className="mm-service-icon drive-icon"><Icon name="cloud" /></span><span className="mm-service-copy"><b>Google Drive</b><small>Acceso OAuth de propietaria</small></span><span className={`mm-connection ${driveConnected ? 'connected' : ''}`}><i />{driveConnected ? 'Conectado · mockup' : 'Sin conectar'}</span></div>
                <p className="mm-drive-copy">Autoriza acceso a archivos de Drive para seleccionar originales. El flujo OAuth real conserva sus controles de acceso y permisos.</p>
                <button className="mm-outline mm-drive-button" type="button" onClick={() => { setDriveConnected(!driveConnected); notify('Interacción simulada: el OAuth de Google Drive no se inicia desde este mockup.'); }}>{driveConnected ? 'Desconectar vista de mockup' : 'Conectar Google Drive (OAuth)'} <Icon name="arrow" /></button>
                <div className="mm-integration-row"><span className="mm-service-icon openai-icon">AI</span><span className="mm-service-copy"><b>OpenAI</b><small>Solo si la acción autorizada lo requiere</small></span><span className="mm-connection"><i />Configurado en servicio</span></div>
                <div className="mm-integration-foot"><span>Las claves y el estado real de conexión no se exponen en esta vista.</span></div>
              </section>
            </div>
            <div className="mm-settings-note"><span>i</span><p><b>Esta es una maqueta estática.</b> No modifica ajustes de producción ni inicia OAuth. La autenticación y las protecciones CSRF permanecen en la aplicación real.</p></div>
          </section>}
          <footer className="mm-footer"><span>MATRIX MOTION <i /> WORKSPACE PRIVADO</span><span>ORIGINALES INTACTOS · AUTORIZACIÓN EXPLÍCITA</span></footer>
        </div>
      </main>
      <nav className="mm-mobile-nav" aria-label="Navegación móvil">{navItems.map((item) => <button type="button" key={item.title} className={view === item.title ? 'active' : ''} onClick={() => setView(item.title)}><Icon name={item.icon} /><span>{item.title}</span></button>)}</nav>
      {toast && <div className="mm-toast" role="status">{toast}</div>}
    </div>
  );
}

function VideoRow({ item, onSelect }: { item: VideoItem; onSelect: () => void }) {
  const completed = item.status === 'Completado' || item.status === 'Listo para revisar';
  return <button className="mm-video-row" type="button" onClick={onSelect}>
    <span className={`mm-thumb mm-thumb-${item.name.length % 3}`}><span className="mm-thumb-lines" /><span className="mm-thumb-play"><Icon name="play" /></span><small>9:16</small></span>
    <span className="mm-row-content"><span className="mm-row-title"><b>{item.name}</b><span className="mm-mock-tag">MOCK DATA</span></span><span className="mm-row-meta"><span>{item.source}</span><i />{item.duration}<i />{item.size}<i />{item.date}</span></span>
    <span className={`mm-status ${completed ? 'done' : 'progress'}`}><i />{item.status}</span><span className="mm-row-arrow"><Icon name="arrow" /></span>
  </button>;
}
